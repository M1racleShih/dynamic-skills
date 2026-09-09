"""Content-addressed snapshots with atomic metadata and local operation counts."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock
from platformdirs import user_data_path

from .errors import SkillsError
from .files import (
    atomic_write,
    digest_files,
    json_bytes,
    metadata,
    read_json,
    skill_files,
    tree_digest,
    valid_digest,
    valid_name,
    write_skill,
)
from .sources import source_files


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Pool:
    def __init__(self, root: Path | None = None):
        self.root = (
            (
                root
                or Path(
                    os.environ.get("DYNAMIC_SKILLS_HOME")
                    or user_data_path("dynamic-skills", appauthor=False)
                )
            )
            .expanduser()
            .resolve()
        )
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("objects", "backups"):
            if (self.root / name).is_symlink():
                raise SkillsError(f"Pool directory must not be a symlink: {name}", "unsafe_path")
            (self.root / name).mkdir(exist_ok=True)
        if (self.root / "pool.lock").is_symlink():
            raise SkillsError("Pool lock must not be a symlink.", "unsafe_path")
        self.lock = FileLock(self.root / "pool.lock", timeout=10)

    def index(self) -> dict:
        value = read_json(
            self.root / "index.json",
            {"schema_version": 1, "skills": {}, "presets": {}, "counts": {}, "events": []},
        )
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise SkillsError("Unsupported pool schema. Upgrade dynamic-skills.")
        return value

    def save(self, index: dict):
        atomic_write(self.root / "index.json", json_bytes(index))

    def event(self, action: str, skill_id: str = "", project: str = ""):
        with self.lock:
            data = self.index()
            counts = data["counts"].setdefault(skill_id or "_pool", {})
            counts[action] = counts.get(action, 0) + 1
            data["events"].append(
                {"at": now(), "action": action, "skill": skill_id, "project": project}
            )
            data["events"] = data["events"][-500:]
            self.save(data)

    def object_path(self, digest: str) -> Path:
        path = self.root / "objects" / valid_digest(digest)
        if path.is_symlink():
            raise SkillsError("Pool object must not be a symlink.", "unsafe_path")
        return path

    def verify(self, digest: str) -> Path:
        path = self.object_path(digest)
        if not path.is_dir():
            raise SkillsError(
                f"Snapshot {digest[:12]} is unavailable; run sync to restore it.", "object_missing"
            )
        if tree_digest(path) != digest:
            raise SkillsError(f"Pool snapshot {digest[:12]} has been modified.", "integrity_error")
        return path

    def put(self, files) -> str:
        digest = digest_files(files)
        target = self.object_path(digest)
        if target.exists():
            self.verify(digest)
        else:
            with tempfile.TemporaryDirectory(dir=self.root, prefix=".import-") as temporary:
                staged = Path(temporary) / "skill"
                write_skill(staged, files)
                os.replace(staged, target)
        return digest

    def get(self, skill_id: str) -> dict:
        valid_name(skill_id)
        item = self.index()["skills"].get(skill_id)
        if item is None:
            raise SkillsError(
                f"Unknown skill: {skill_id}. Use dskills list or install.", "not_found"
            )
        return item

    def version(self, skill_id: str, revision: str | None = None) -> dict:
        item = self.get(skill_id)
        revision = revision or item["current"]
        matches = [v for v in item["versions"] if v["digest"].startswith(revision)]
        if len(matches) != 1:
            raise SkillsError(f"Revision {revision!r} is unknown or ambiguous for {skill_id}.")
        return matches[0]

    def install(
        self,
        source: dict,
        skill_id: str | None = None,
        tags: tuple = (),
        updating: bool = False,
        dry_run: bool = False,
    ) -> dict:
        with source_files(source) as (path, resolved):
            files = skill_files(path)
        meta = metadata(files)
        skill_id = valid_name(skill_id or meta["name"])
        digest = digest_files(files)
        with self.lock:
            data = self.index()
            existing = data["skills"].get(skill_id)
            if existing and not updating:
                old_source = self.version(skill_id)["source"]

                def identity(s):
                    return {k: v for k, v in s.items() if k != "commit"}

                if identity(old_source) != identity(resolved):
                    raise SkillsError(
                        f"{skill_id} already has a different source. Choose --id.", "name_conflict"
                    )
            result = {
                "id": skill_id,
                **meta,
                "digest": digest,
                "source": resolved,
                "changed": not existing or existing["current"] != digest,
            }
            if dry_run:
                return result
            self.put(files)
            item = existing or {"id": skill_id, "versions": [], "tags": []}
            if not any(v["digest"] == digest for v in item["versions"]):
                item["versions"].append(
                    {
                        "digest": digest,
                        "source": resolved,
                        "created": now(),
                        "name": meta["name"],
                        "description": meta["description"],
                    }
                )
            item.update(name=meta["name"], description=meta["description"], current=digest)
            item["tags"] = sorted(set(item["tags"]) | set(meta["tags"]) | set(tags))
            data["skills"][skill_id] = item
            self.save(data)
            if result["changed"]:
                self.event("update" if updating else "install", skill_id)
            return result

    def restore_object(self, digest: str, source: dict) -> Path:
        with self.lock:
            if self.object_path(digest).exists():
                return self.verify(digest)
            with source_files(source, pinned=True) as (path, _):
                files = skill_files(path)
            if digest_files(files) != digest:
                raise SkillsError(
                    "Source content differs from the lockfile; restore the pinned "
                    "source or explicitly plug a new version.",
                    "integrity_error",
                )
            self.put(files)
            return self.verify(digest)

    def rollback(self, skill_id: str, revision: str | None) -> dict:
        with self.lock:
            data = self.index()
            item = self.get(skill_id)
            if not revision:
                position = next(
                    i for i, v in enumerate(item["versions"]) if v["digest"] == item["current"]
                )
                if position == 0:
                    raise SkillsError("No earlier version is available.")
                revision = item["versions"][position - 1]["digest"]
            version = self.version(skill_id, revision)
            self.verify(version["digest"])
            item.update(
                current=version["digest"], name=version["name"], description=version["description"]
            )
            data["skills"][skill_id] = item
            self.save(data)
            self.event("rollback", skill_id)
            return {"id": skill_id, **version}

    def tag(self, skill_id: str, tags: tuple, remove: bool = False) -> dict:
        with self.lock:
            data = self.index()
            item = self.get(skill_id)
            item["tags"] = sorted(
                set(item["tags"]) - set(tags) if remove else set(item["tags"]) | set(tags)
            )
            data["skills"][skill_id] = item
            self.save(data)
            return item

    def search(self, query: str = "", tag: str | None = None) -> list[dict]:
        terms = query.casefold().split()
        results = []
        for item in self.index()["skills"].values():
            haystack = " ".join(
                [item["id"], item["name"], item["description"], *item["tags"]]
            ).casefold()
            if all(term in haystack for term in terms) and (not tag or tag in item["tags"]):
                results.append({k: v for k, v in item.items() if k != "versions"})
        return sorted(results, key=lambda item: item["id"])

    def read(self, skill_id: str, revision: str | None = None) -> dict:
        version = self.version(skill_id, revision)
        path = self.verify(version["digest"])
        self.event("read", skill_id)
        return {
            "id": skill_id,
            "digest": version["digest"],
            "path": str(path),
            "content": (path / "SKILL.md").read_text(encoding="utf-8"),
        }
