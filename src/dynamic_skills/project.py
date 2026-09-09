"""Explicit project selection, reproducible pins and owned filesystem outputs."""

from __future__ import annotations

import copy
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock

from .adapters import ADAPTERS, detect_agents, select_agents
from .errors import SkillsError
from .files import (
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

MANIFEST = "dynamic-skills.json"
LOCKFILE = "dynamic-skills.lock.json"
STATE = ".dynamic-skills/state.json"


def project_root(path: Path) -> Path:
    path = path.expanduser().resolve()
    for candidate in (path, *path.parents):
        if (candidate / MANIFEST).is_file():
            return candidate
        if (candidate / ".git").exists():
            break
    return path


def validate_manifest(data):
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise SkillsError("Unsupported project manifest schema.")
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

    def load(self) -> tuple[dict, dict]:
        manifest = read_json(self.root / MANIFEST)
        if manifest is None:
            raise SkillsError("Project is not initialized. Run dskills init --agent <name>.")
        validate_manifest(manifest)
        lock = read_json(self.root / LOCKFILE)
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

    def initialize(self, agents: list[str], mode: str, home: Path | None = None) -> dict:
        with self.locked():
            if exists(self.root / MANIFEST) or exists(self.root / LOCKFILE):
                raise SkillsError("Project already has a manifest or lockfile; use sync or plug.")
            agents = select_agents(agents or detect_agents(self.root, home or Path.home()))
            manifest = {"schema_version": 1, "agents": agents, "mode": mode, "skills": []}
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

    def sync(self, dry_run=False) -> dict:
        with self.locked():
            manifest, pins = self.load()
            return self.apply(manifest, pins, "sync", dry_run=dry_run)

    def check_owned(self, target: Path, old: dict):
        if not exists(target):
            return
        if old["mode"] == "symlink":
            if not target.is_symlink() or os.readlink(target) != old["link"]:
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
        previous = (
            None
            if initial
            else {
                "manifest": read_json(self.root / MANIFEST),
                "lock": read_json(self.root / LOCKFILE),
            }
        )
        new_lock = {"schema_version": 1, "skills": pins}
        if (
            not plan
            and previous
            and previous["manifest"] == manifest
            and previous["lock"] == new_lock
            and old_state["outputs"] == outputs
        ):
            return {**result, "changed": False}
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
        changes[MANIFEST] = json_bytes(manifest)
        changes[LOCKFILE] = json_bytes(new_lock)
        changes[STATE] = json_bytes(new_state)
        ignore = self.root / ".gitignore"
        if ignore.is_symlink():
            raise SkillsError("Refusing symlinked .gitignore.", "unsafe_path")
        text = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        entries = ["/.dynamic-skills/", *(f"/{relative}/" for relative in sorted(outputs))]
        missing = [entry for entry in entries if entry not in text.splitlines()]
        if missing:
            changes[".gitignore"] = (
                text
                + ("\n" if text and not text.endswith("\n") else "")
                + "\n".join(missing)
                + "\n"
            ).encode()
        Transaction(self.root).apply(changes)
        for skill_id in sorted(set(pins) | set((previous or {}).get("lock", {}).get("skills", {}))):
            old_pin = (previous or {}).get("lock", {}).get("skills", {}).get(skill_id)
            if old_pin != pins.get(skill_id):
                self.pool.event(action, skill_id, str(self.root))
        return {**result, "changed": True}

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
