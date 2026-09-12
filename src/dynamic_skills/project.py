"""Explicit project selection, reproducible pins and owned filesystem outputs."""

from __future__ import annotations

import copy
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock

from .adapters import ADAPTERS, detect_agents, select_agents
from .errors import SkillsError
from .files import (
    atomic_write,
    json_bytes,
    metadata,
    read_json,
    safe_child,
    skill_files,
    tree_digest,
    valid_digest,
    valid_name,
)
from .pool import Pool, now
from .transaction import Transaction, exists, output_path

LEGACY_MANIFEST = "dynamic-skills.json"
LEGACY_LOCKFILE = "dynamic-skills.lock.json"
MANIFEST = ".dynamic-skills/config.json"
LOCKFILE = ".dynamic-skills/lock.json"
IGNORE_FILE = ".dynamic-skills/.gitignore"
STATE = ".dynamic-skills/state.json"


def project_root(path: Path) -> Path:
    path = path.expanduser().resolve()
    for candidate in (path, *path.parents):
        if (candidate / MANIFEST).is_file() or (candidate / LEGACY_MANIFEST).is_file():
            return candidate
        if (candidate / ".git").exists():
            break
    return path


def validate_manifest(data):
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise SkillsError("Unsupported project manifest schema.")
    if not isinstance(data.get("track", False), bool):
        raise SkillsError("Manifest track must be a boolean.")
    if not isinstance(data.get("agents"), list):
        raise SkillsError("Manifest agents must be a list.")
    select_agents(data["agents"])
    if data.get("mode") not in {"copy", "symlink"}:
        raise SkillsError("Manifest mode must be copy or symlink.")
    if not isinstance(data.get("skills"), list) or not all(
        isinstance(s, str) for s in data["skills"]
    ):
        raise SkillsError("Manifest skills must be a list of skill IDs.")
    for skill in data["skills"]:
        valid_name(skill)
    if len(set(data["skills"])) != len(data["skills"]):
        raise SkillsError("Duplicate skill IDs in manifest.")


def validate_pins(pins: dict, ids: list[str]):
    if not isinstance(pins, dict) or set(pins) != set(ids):
        raise SkillsError("Manifest and lockfile differ. Use plug/unplug to change selection.")
    for skill_id, pin in pins.items():
        valid_name(skill_id)
        if not isinstance(pin, dict):
            raise SkillsError("Invalid lockfile pin.")
        valid_digest(pin.get("digest"))
        if not isinstance(pin.get("source"), dict):
            raise SkillsError("Lockfile pin is missing source provenance.")


class Project:
    def __init__(self, path: Path, pool: Pool):
        self.root = project_root(path)
        self.pool = pool

    @contextmanager
    def locked(self):
        if not self.root.is_dir():
            raise SkillsError(f"Project directory does not exist: {self.root}")
        folder = safe_child(self.root, ".dynamic-skills")
        if folder.is_symlink():
            raise SkillsError("Project state directory must not be a symlink.", "unsafe_path")
        folder.mkdir(exist_ok=True)
        path = folder / "project.lock"
        if path.is_symlink():
            raise SkillsError("Project lock must not be a symlink.", "unsafe_path")
        with FileLock(path, timeout=10):
            yield

    def metadata_paths(self) -> tuple[str, str]:
        current = any(exists(self.root / p) for p in (MANIFEST, LOCKFILE))
        legacy = any(exists(self.root / p) for p in (LEGACY_MANIFEST, LEGACY_LOCKFILE))
        if current and legacy:
            raise SkillsError("Both legacy and current metadata exist; resolve the conflict first.")
        return (LEGACY_MANIFEST, LEGACY_LOCKFILE) if legacy else (MANIFEST, LOCKFILE)

    def configure(self, track: bool) -> dict:
        with self.locked():
            manifest, pins = self.load()
            manifest["track"] = track
            return self.apply(manifest, pins, "config")

    def load(self) -> tuple[dict, dict]:
        manifest_path, lock_path = self.metadata_paths()
        manifest = read_json(safe_child(self.root, manifest_path))
        if manifest is None:
            raise SkillsError("Project is not initialized. Run dskills init --agent <name>.")
        validate_manifest(manifest)
        lock = read_json(safe_child(self.root, lock_path))
        if not isinstance(lock, dict) or lock.get("schema_version") != 1:
            raise SkillsError("Missing or unsupported lockfile; restore it from version control.")
        pins = lock.get("skills")
        validate_pins(pins, manifest["skills"])
        return manifest, pins

    def state(self):
        state = read_json(self.root / STATE, {"schema_version": 1, "outputs": {}, "history": []})
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            raise SkillsError("Unsupported local project state.")
        for relative, output in state["outputs"].items():
            target = output_path(self.root, relative)
            if target.parent.relative_to(self.root).as_posix() not in {
                a.project_dir for a in ADAPTERS.values()
            }:
                raise SkillsError("Invalid owned skill path.", "unsafe_path")
            valid_digest(output["digest"])
            if output.get("mode") not in {"copy", "symlink"}:
                raise SkillsError("Invalid owned skill mode.")
        return state

    def initialize(
        self, agents: list[str], mode: str, home: Path | None = None, *, track: bool = False
    ) -> dict:
        with self.locked():
            if any(
                exists(self.root / p)
                for p in (MANIFEST, LOCKFILE, LEGACY_MANIFEST, LEGACY_LOCKFILE)
            ):
                raise SkillsError("Project already has a manifest or lockfile; use sync or plug.")
            agents = select_agents(agents or detect_agents(self.root, home or Path.home()))
            manifest = {
                "schema_version": 1,
                "agents": agents,
                "mode": mode,
                "skills": [],
                "track": track,
            }
            return self.apply(manifest, {}, "init", initial=True)

    def pin(self, skill_id: str, revision: str | None = None) -> dict:
        version = self.pool.version(skill_id, revision)
        return {"digest": version["digest"], "source": version["source"]}

    def plug(self, ids: list[str], revision: str | None = None, dry_run=False) -> dict:
        if revision and len(ids) != 1:
            raise SkillsError("--revision requires exactly one skill.")
        with self.locked():
            manifest, pins = self.load()
            for skill_id in ids:
                pins[valid_name(skill_id)] = self.pin(skill_id, revision)
            manifest["skills"] = sorted(pins)
            return self.apply(manifest, pins, "plug", dry_run=dry_run)

    def unplug(self, ids: list[str], dry_run=False) -> dict:
        with self.locked():
            manifest, pins = self.load()
            for skill_id in ids:
                if skill_id not in pins:
                    raise SkillsError(f"Skill is not selected in this project: {skill_id}")
                del pins[skill_id]
            manifest["skills"] = sorted(pins)
            return self.apply(manifest, pins, "unplug", dry_run=dry_run)

    def apply_packages(self, names: list[str], dry_run=False) -> dict:
        with self.locked():
            manifest, pins = self.load()
            with self.pool.lock:
                members = self.pool.resolve_packages(names)
                added = sorted(set(members) - set(pins))
                kept = sorted(set(members) & set(pins))
                for skill_id in added:
                    pins[skill_id] = self.pin(skill_id)
            manifest["skills"] = sorted(pins)
            result = self.apply(manifest, pins, "package", dry_run=dry_run)
            return {**result, "packages": sorted(set(names)), "added": added, "kept": kept}

    def sync(self, dry_run=False) -> dict:
        with self.locked():
            manifest, pins = self.load()
            return self.apply(manifest, pins, "sync", dry_run=dry_run)

    def check_owned(self, target: Path, old: dict):
        if not exists(target):
            return
        if old["mode"] == "symlink":
            # Windows readlink may include the extended-length \\?\ prefix.
            if not target.is_symlink() or target.resolve() != Path(old["link"]).resolve():
                raise SkillsError(f"Managed link was replaced: {target}", "conflict")
            # Verify the content too; linking never grants silent write-through permission.
            if target.exists() and tree_digest(target) != old["digest"]:
                raise SkillsError(f"Linked skill was modified: {target}", "conflict")
        elif target.is_symlink() or not target.is_dir() or tree_digest(target) != old["digest"]:
            raise SkillsError(
                f"Managed skill has local edits: {target}. Save them before retrying.", "conflict"
            )

    def apply(
        self,
        manifest: dict,
        pins: dict,
        action: str,
        *,
        dry_run=False,
        initial=False,
        history: list | None = None,
    ) -> dict:
        validate_manifest(manifest)
        validate_pins(pins, manifest["skills"])
        if Transaction(self.root).folder.exists():
            raise SkillsError(
                "An interrupted transaction exists. Run dskills recover.", "recovery_needed"
            )
        old_state = self.state()
        outputs = {}
        changes = {}
        plan = []
        paths = {}
        for skill_id, pin in sorted(pins.items()):
            if skill_id == "synced" and "claude" in manifest["agents"]:
                raise SkillsError(
                    "Claude Code reserves 'synced'. Import this skill with another --id."
                )
            for agent in manifest["agents"]:
                relative = f"{ADAPTERS[agent].project_dir}/{skill_id}"
                output = {"digest": pin["digest"], "mode": manifest["mode"]}
                if manifest["mode"] == "symlink":
                    output["link"] = str(self.pool.object_path(pin["digest"]))
                outputs[relative] = output
                paths[relative] = pin
        for relative in sorted(set(outputs) | set(old_state["outputs"])):
            target = output_path(self.root, relative)
            old = old_state["outputs"].get(relative)
            desired = outputs.get(relative)
            if old:
                self.check_owned(target, old)
            elif exists(target):
                raise SkillsError(
                    f"Unmanaged skill already exists: {target}. Move it aside first.", "conflict"
                )
            if desired == old and exists(target) and target.exists():
                continue
            plan.append(
                {
                    "path": relative,
                    "action": "write" if desired else "remove",
                    "digest": desired["digest"] if desired else old["digest"],
                }
            )
            if not desired:
                changes[relative] = None
        result = {
            "project": str(self.root),
            "action": action,
            "skills": manifest["skills"],
            "plan": plan,
            "refresh": {a: ADAPTERS[a].refresh for a in manifest["agents"]},
            "discovery_note": "Kimi/Pi may also discover shared or other agents' directories.",
        }
        if dry_run:
            return {**result, "dry_run": True}
        # Validate all objects, including unchanged outputs, before committing metadata.
        native_names = set()
        for skill_id, pin in pins.items():
            obj = self.pool.restore_object(pin["digest"], pin["source"])
            name = metadata(skill_files(obj))["name"].casefold()
            if name in native_names:
                raise SkillsError(
                    f"Multiple skills declare the native name {name!r}. "
                    "Pool aliases do not rename SKILL.md; select one per project.",
                    "name_conflict",
                )
            native_names.add(name)
            self.pool.remember_pin(skill_id, pin)
        for step in plan:
            if step["action"] == "write":
                output = outputs[step["path"]]
                source = self.pool.verify(output["digest"])
                changes[step["path"]] = (
                    ("symlink", str(source)) if output["mode"] == "symlink" else source
                )
        manifest_path, lock_path = self.metadata_paths()
        legacy = manifest_path == LEGACY_MANIFEST
        previous = (
            None
            if initial
            else {
                "manifest": read_json(self.root / manifest_path),
                "lock": read_json(self.root / lock_path),
            }
        )
        new_lock = {"schema_version": 1, "skills": pins}
        ignore_content = b"*\n"
        if manifest.get("track", False):
            ignore_content += b"!.gitignore\n!config.json\n!lock.json\n"
        ignore_path = safe_child(self.root, IGNORE_FILE)
        if ignore_path.is_symlink():
            raise SkillsError("Refusing symlinked metadata ignore file.", "unsafe_path")
        if not ignore_path.exists() or ignore_path.read_bytes() != ignore_content:
            changes[IGNORE_FILE] = ignore_content
        exclusions_changed = self.exclude_outputs(outputs)
        if (
            not plan
            and not legacy
            and previous
            and previous["manifest"] == manifest
            and previous["lock"] == new_lock
            and old_state["outputs"] == outputs
        ):
            if changes:
                Transaction(self.root).apply(changes)
            return {**result, "changed": exclusions_changed or bool(changes)}
        new_state = {
            "schema_version": 1,
            "outputs": outputs,
            "history": list(old_state["history"] if history is None else history),
        }
        if previous and history is None:
            snapshot = uuid.uuid4().hex
            changes[f".dynamic-skills/history/{snapshot}.json"] = json_bytes(
                {**previous, "action": action, "at": now()}
            )
            new_state["history"].append(snapshot)
        if legacy:
            changes[LEGACY_MANIFEST] = None
            changes[LEGACY_LOCKFILE] = None
        changes[MANIFEST] = json_bytes(manifest)
        changes[LOCKFILE] = json_bytes(new_lock)
        changes[STATE] = json_bytes(new_state)
        Transaction(self.root).apply(changes)
        for skill_id in sorted(set(pins) | set((previous or {}).get("lock", {}).get("skills", {}))):
            old_pin = (previous or {}).get("lock", {}).get("skills", {}).get(skill_id)
            if old_pin != pins.get(skill_id):
                self.pool.event(action, skill_id, str(self.root))
        return {**result, "changed": True}

    def exclude_outputs(self, outputs: dict) -> bool:
        """Keep generated files local without editing the project's shared ignore rules."""
        if not outputs:
            return False
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return False
        if result.returncode:
            if "not a git repository" in result.stderr.lower():
                return False
            raise SkillsError(f"Cannot locate Git repository: {result.stderr.strip()}")
        repository = Path(result.stdout.strip()).resolve()
        result = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "--git-path", "info/exclude"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise SkillsError(f"Cannot locate Git excludes: {result.stderr.strip()}")
        exclude = Path(result.stdout.strip())
        if not exclude.is_absolute():
            exclude = self.root / exclude
        if exclude.is_symlink():
            raise SkillsError("Refusing symlinked Git exclude file.", "unsafe_path")
        text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        prefix = self.root.relative_to(repository).as_posix()
        prefix = "" if prefix == "." else prefix + "/"
        paths = [relative + "/" for relative in sorted(outputs)]
        # Quote Git wildmatch metacharacters, including spaces in project directory names.
        entries = [
            "/" + "".join("\\" + char if char in "\\*?[] !#" else char for char in prefix + path)
            for path in paths
        ]
        missing = [entry for entry in entries if entry not in text.splitlines()]
        if not missing:
            return False
        atomic_write(
            exclude,
            (
                text
                + ("\n" if text and not text.endswith("\n") else "")
                + "\n".join(missing)
                + "\n"
            ).encode(),
        )
        return True

    def undo(self, dry_run=False) -> dict:
        with self.locked():
            self.load()
            state = self.state()
            if not state["history"]:
                raise SkillsError("No project change to undo.")
            snapshot = valid_name(state["history"][-1])
            previous = read_json(safe_child(self.root, f".dynamic-skills/history/{snapshot}.json"))
            if not previous:
                raise SkillsError("Project history snapshot is missing.")
            return self.apply(
                previous["manifest"],
                previous["lock"]["skills"],
                "undo",
                dry_run=dry_run,
                history=state["history"][:-1],
            )

    def recover(self) -> dict:
        with self.locked():
            return Transaction(self.root).recover()

    def status(self) -> dict:
        manifest, pins = self.load()
        state = self.state()
        issues = []
        expected = {f"{ADAPTERS[a].project_dir}/{s}" for a in manifest["agents"] for s in pins}
        for relative in sorted(expected | set(state["outputs"])):
            path = output_path(self.root, relative)
            old = state["outputs"].get(relative)
            if relative not in expected:
                issues.append({"path": relative, "issue": "stale output; run sync"})
            elif not old:
                issues.append(
                    {"path": relative, "issue": "unmanaged" if exists(path) else "missing"}
                )
            elif not path.exists():
                issues.append({"path": relative, "issue": "missing or broken link"})
            else:
                try:
                    self.check_owned(path, old)
                    pin = pins[path.name]
                    if old["digest"] != pin["digest"] or old["mode"] != manifest["mode"]:
                        issues.append({"path": relative, "issue": "out of sync"})
                except SkillsError as exc:
                    issues.append({"path": relative, "issue": str(exc)})
        if Transaction(self.root).folder.exists():
            issues.append({"path": ".dynamic-skills/transaction", "issue": "run recover"})
        return {
            "project": str(self.root),
            **manifest,
            "pins": pins,
            "issues": issues,
            "healthy": not issues,
        }

    def save_preset(self, name: str) -> dict:
        valid_name(name)
        with self.locked(), self.pool.lock:
            manifest, pins = self.load()
            data = self.pool.index()
            data["presets"][name] = {"manifest": manifest, "skills": pins}
            self.pool.save(data)
            return {"name": name, "skills": manifest["skills"]}

    def apply_preset(self, name: str, dry_run=False) -> dict:
        with self.locked():
            self.load()
            preset = self.pool.index()["presets"].get(valid_name(name))
            if preset is None:
                raise SkillsError(f"Unknown preset: {name}", "not_found")
            return self.apply(
                copy.deepcopy(preset["manifest"]),
                copy.deepcopy(preset["skills"]),
                "preset",
                dry_run=dry_run,
            )
