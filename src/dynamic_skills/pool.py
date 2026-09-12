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


def _checked_ids(ids: list[str], catalog: dict) -> list[str]:
    for skill_id in ids:
        valid_name(skill_id)
    members = sorted(set(ids))
    if not members:
        raise SkillsError("At least one skill ID is required; packages cannot be empty.")
    missing = sorted(set(members) - set(catalog["skills"]))
    if missing:
        raise SkillsError(f"Unknown pool skills: {', '.join(missing)}.", "not_found")
    return members


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
        if any(
            not isinstance(value.get(k), dict) for k in ("skills", "presets", "counts")
        ) or not isinstance(value.get("events"), list):
            raise SkillsError("Invalid pool catalog structure; restore index.json from a backup.")
        for key, item in value["skills"].items():
            valid_name(key)
            if not isinstance(item, dict) or not isinstance(item.get("versions"), list):
                raise SkillsError("Invalid skill version history in pool catalog.")
            valid_digest(item.get("current"))
        packages = value.setdefault("packages", {})
        if not isinstance(packages, dict):
            raise SkillsError("Invalid packages in pool catalog.")
        for name, package in packages.items():
            valid_name(name)
            if (
                not isinstance(package, dict)
                or not isinstance(package.get("description"), str)
                or not isinstance(package.get("skills"), list)
                or not package["skills"]
            ):
                raise SkillsError(f"Invalid package definition: {name}.")
            for skill_id in package["skills"]:
                valid_name(skill_id)
            if len(set(package["skills"])) != len(package["skills"]):
                raise SkillsError(f"Duplicate package members: {name}.")
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

    def remember_pin(self, skill_id: str, pin: dict):
        """Make a restored skill searchable without changing existing catalog defaults."""
        with self.lock:
            data = self.index()
            if skill_id in data["skills"]:
                return
            meta = metadata(skill_files(self.verify(pin["digest"])))
            data["skills"][skill_id] = {
                "id": skill_id,
                **meta,
                "current": pin["digest"],
                "versions": [
                    {
                        **pin,
                        "created": now(),
                        "name": meta["name"],
                        "description": meta["description"],
                    }
                ],
            }
            self.save(data)

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
        return self.tag_many([skill_id], tags, remove)[0]

    def tag_many(self, ids: list[str], tags: tuple, remove: bool = False) -> list[dict]:
        with self.lock:
            data = self.index()
            members = _checked_ids(ids, data)
            for skill_id in members:
                item = data["skills"][skill_id]
                item["tags"] = sorted(
                    set(item["tags"]) - set(tags) if remove else set(item["tags"]) | set(tags)
                )
            self.save(data)
            return [data["skills"][skill_id] for skill_id in members]

    def categories(self) -> dict:
        counts = {}
        untagged = 0
        for item in self.index()["skills"].values():
            if not item["tags"]:
                untagged += 1
            for tag in set(item["tags"]):
                counts[tag] = counts.get(tag, 0) + 1
        return {
            "categories": [{"tag": tag, "count": count} for tag, count in sorted(counts.items())],
            "untagged": untagged,
        }

    def search(
        self, query: str = "", tag: str | None = None, *, untagged: bool = False
    ) -> list[dict]:
        if tag is not None and untagged:
            raise SkillsError("Choose --tag or --untagged, not both.")
        terms = query.casefold().split()
        results = []
        for item in self.index()["skills"].values():
            if untagged and item["tags"]:
                continue
            haystack = " ".join(
                [item["id"], item["name"], item["description"], *item["tags"]]
            ).casefold()
            if all(term in haystack for term in terms) and (not tag or tag in item["tags"]):
                results.append({k: v for k, v in item.items() if k != "versions"})
        return sorted(results, key=lambda item: item["id"])

    def create_package(
        self, name: str, ids: list[str], from_tags: tuple = (), description: str = ""
    ) -> dict:
        valid_name(name)
        if not isinstance(description, str):
            raise SkillsError("Package description must be a string.")
        with self.lock:
            data = self.index()
            if name in data["packages"]:
                raise SkillsError(
                    f"Package already exists: {name}. Use add/remove.", "name_conflict"
                )
            members = list(ids)
            for tag in from_tags:
                matches = [key for key, item in data["skills"].items() if tag in item["tags"]]
                if not matches:
                    raise SkillsError(f"No pool skills have tag: {tag}.", "not_found")
                members.extend(matches)
            package = {"description": description, "skills": _checked_ids(members, data)}
            data["packages"][name] = package
            self.save(data)
            return {"name": name, **package}

    def get_package(self, name: str) -> dict:
        package = self.index()["packages"].get(valid_name(name))
        if package is None:
            raise SkillsError(f"Unknown package: {name}. Use dskills package list.", "not_found")
        return {"name": name, **package}

    def list_packages(self) -> list[dict]:
        return [
            {"name": name, **package} for name, package in sorted(self.index()["packages"].items())
        ]

    def edit_package(self, name: str, ids: list[str], remove: bool = False) -> dict:
        with self.lock:
            data = self.index()
            package = self.get_package(name)
            members = set(_checked_ids(ids, data))
            existing = set(package["skills"])
            if remove and members - existing:
                raise SkillsError(
                    f"Skills are not in package {name}: {', '.join(sorted(members - existing))}.",
                    "not_found",
                )
            package["skills"] = _checked_ids(
                list(existing - members if remove else existing | members), data
            )
            data["packages"][name] = {
                "description": package["description"],
                "skills": package["skills"],
            }
            self.save(data)
            return package

    def delete_package(self, name: str) -> dict:
        with self.lock:
            data = self.index()
            self.get_package(name)
            del data["packages"][name]
            self.save(data)
            return {"deleted": name}

    def resolve_packages(self, names: list[str]) -> list[str]:
        with self.lock:
            members = []
            for name in names:
                members.extend(self.get_package(name)["skills"])
            return _checked_ids(members, self.index())

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
