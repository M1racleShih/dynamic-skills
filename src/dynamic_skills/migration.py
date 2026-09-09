"""Conservative discovery and reversible deactivation of global user skills."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

from .adapters import ADAPTERS
from .errors import SkillsError
from .files import (
    atomic_write,
    digest_files,
    json_bytes,
    metadata,
    read_json,
    skill_files,
    valid_name,
)
from .pool import Pool, now
from .transaction import exists


def ownership(path: Path, pool: Pool) -> str:
    resolved = path.resolve()
    if resolved.is_relative_to(pool.root):
        return "managed"
    parts = set(path.parts) | set(resolved.parts)
    if ".system" in parts or str(resolved).startswith("/etc/"):
        return "system"
    if "plugins" in parts or ".claude-plugin" in parts or "synced" in parts:
        return "plugin"
    if path.is_dir() and ((path / ".claude-plugin").exists() or (path / "plugin.json").exists()):
        return "plugin"
    return "user"


def scan(pool: Pool, home: Path | None = None, roots: tuple[Path, ...] = ()) -> list[dict]:
    home = (home or Path.home()).expanduser().absolute()
    locations = {}
    for name, adapter in ADAPTERS.items():
        for relative in adapter.global_dirs:
            locations.setdefault(home / relative, set()).add(name)
    if roots:
        locations = {root.expanduser().absolute(): {"custom"} for root in roots}
    result = {}
    visited_count = 0

    def visit(path: Path, agents: set, depth=0):
        nonlocal visited_count
        visited_count += 1
        if depth > 24 or visited_count > 20000:
            raise SkillsError(
                "Scan exceeds 24 levels / 20,000 entries. Select a narrower --from path."
            )
        canonical = path.parent.resolve() / path.name
        if str(canonical) in result:
            result[str(canonical)]["agents"] = sorted(
                set(result[str(canonical)]["agents"]) | agents
            )
            return
        if not path.exists():
            return
        candidate = (path.is_dir() and (path / "SKILL.md").is_file()) or (
            path.is_file() and path.suffix.lower() == ".md"
        )
        if candidate:
            owner = ownership(path, pool)
            row = {
                "path": str(canonical),
                "agents": sorted(agents),
                "ownership": owner,
                "eligible": owner == "user",
                "symlink": path.is_symlink(),
            }
            try:
                files = skill_files(path)
                meta = metadata(files)
                row.update(
                    name=meta["name"], description=meta["description"], digest=digest_files(files)
                )
                name = re.sub(r"[^a-z0-9]+", "-", meta["name"].lower()).strip("-")[:48].rstrip("-")
                row["id"] = name or "skill"
            except (SkillsError, OSError) as exc:
                row.update(eligible=False, issue=str(exc))
            result[str(canonical)] = row
            return
        if path.is_dir():
            if path.is_symlink():
                result[str(canonical)] = {
                    "path": str(canonical),
                    "agents": sorted(agents),
                    "ownership": "linked-group",
                    "eligible": False,
                    "issue": "Pass the resolved group as --from to scan its children.",
                }
                return
            for child in sorted(path.iterdir()):
                if child.name.startswith(".") and child.name != ".system":
                    continue
                if child.name in {"node_modules", "__pycache__"}:
                    continue
                visit(child, agents, depth + 1)

    for root, agents in locations.items():
        if root.exists():
            visit(root, agents)
    return sorted(result.values(), key=lambda item: item["path"])


def migration_plan(pool: Pool, rows: list[dict], disable: bool) -> list[dict]:
    ids = {key: item["current"] for key, item in pool.index()["skills"].items()}
    plan = []
    for row in rows:
        if not row["eligible"]:
            plan.append({**row, "action": "skip"})
            continue
        skill_id = row["id"]
        if skill_id in ids and ids[skill_id] != row["digest"]:
            suffix = hashlib.sha256(row["path"].encode()).hexdigest()[:8]
            skill_id = f"{skill_id}-{suffix}"
            if skill_id in ids and ids[skill_id] != row["digest"]:
                raise SkillsError(
                    f"Migration ID conflict for {row['path']}; import with an explicit --id."
                )
        ids[skill_id] = row["digest"]
        plan.append(
            {**row, "id": skill_id, "action": "import-and-disable" if disable else "import"}
        )
    return plan


def migrate(pool: Pool, roots=(), *, home=None, disable=False, apply=False) -> dict:
    with pool.lock:
        plan = migration_plan(pool, scan(pool, home, roots), disable)
        eligible = [row for row in plan if row["action"] != "skip"]
        if not apply or not eligible:
            return {"applied": False, "plan": plan}
        # Preflight every source before imports and before moving any global entry.
        for row in eligible:
            path = Path(row["path"])
            files = skill_files(path)
            if digest_files(files) != row["digest"]:
                raise SkillsError(f"Source changed during scan: {path}", "conflict")
            if disable and path.lstat().st_dev != (pool.root / "backups").stat().st_dev:
                raise SkillsError(
                    "Deactivation requires source and pool on the same filesystem. "
                    "Use --home on that filesystem, or import without --disable."
                )
        migration_id = uuid.uuid4().hex
        folder = pool.root / "backups" / migration_id
        folder.mkdir()
        record = {
            "schema_version": 1,
            "id": migration_id,
            "at": now(),
            "state": "preparing",
            "disable": disable,
            "entries": [],
        }
        for i, row in enumerate(eligible):
            path = Path(row["path"])
            record["entries"].append(
                {
                    "path": str(path),
                    "id": row["id"],
                    "digest": row["digest"],
                    "backup": f"{i}.md" if path.is_file() else str(i),
                    "flat": path.is_file(),
                    "link": os.readlink(path) if path.is_symlink() else None,
                }
            )
            if row["id"] not in pool.index()["skills"]:
                pool.install({"kind": "local", "path": str(path.resolve())}, row["id"])
            pool.verify(row["digest"])
        atomic_write(folder / "record.json", json_bytes(record))
        try:
            if disable:
                for entry in record["entries"]:
                    path = Path(entry["path"])
                    if digest_files(skill_files(path)) != entry["digest"]:
                        raise SkillsError(f"Source changed before deactivation: {path}", "conflict")
                    if (os.readlink(path) if path.is_symlink() else None) != entry["link"]:
                        raise SkillsError(f"Source link changed: {path}", "conflict")
                    os.replace(path, folder / entry["backup"])
            record["state"] = "complete"
            atomic_write(folder / "record.json", json_bytes(record))
        except Exception:
            restore_migration(pool, migration_id)
            raise
        pool.event("migrate")
        return {
            "applied": True,
            "migration": migration_id,
            "plan": plan,
            "restore": f"dskills migrate-restore {migration_id}",
            "note": "Import-only leaves globals active."
            if not disable
            else "Selected globals moved to backups. Refresh affected agents.",
        }


def migrations(pool: Pool) -> list[dict]:
    result = []
    for path in sorted((pool.root / "backups").glob("*/record.json")):
        if path.parent.is_symlink():
            raise SkillsError("Symlinked migration backup.", "unsafe_path")
        record = read_json(path)
        result.append(
            {
                "id": record["id"],
                "at": record["at"],
                "state": record["state"],
                "disabled": record["disable"],
                "skills": len(record["entries"]),
            }
        )
    return result


def restore_migration(pool: Pool, migration_id: str) -> dict:
    valid_name(migration_id)
    with pool.lock:
        folder = pool.root / "backups" / migration_id
        if folder.is_symlink():
            raise SkillsError("Symlinked migration backup.", "unsafe_path")
        record = read_json(folder / "record.json")
        if not record:
            raise SkillsError(f"Unknown migration: {migration_id}", "not_found")
        if record["state"] == "restored" or not record["disable"]:
            return {"migration": migration_id, "restored": False}
        for i, entry in enumerate(record["entries"]):
            path = Path(entry["path"])
            expected = f"{i}.md" if entry.get("flat") else str(i)
            backup = folder / expected
            if expected != entry["backup"] or not path.is_absolute():
                raise SkillsError("Invalid migration record.")
            # A source parent replaced by a symlink must not redirect restoration.
            if path.parent.resolve() != path.parent:
                raise SkillsError(f"Source parent has moved: {path.parent}", "conflict")
            if exists(backup):
                if exists(path):
                    raise SkillsError(
                        f"Restore would overwrite {path}. Move it aside first.", "conflict"
                    )
                if entry["link"] is not None:
                    if not backup.is_symlink() or os.readlink(backup) != entry["link"]:
                        raise SkillsError("Migration link backup changed.", "integrity_error")
                elif digest_files(skill_files(backup)) != entry["digest"]:
                    raise SkillsError("Migration backup changed.", "integrity_error")
            elif not exists(path):
                raise SkillsError(f"Both source and backup are missing: {path}", "integrity_error")
        for i, entry in enumerate(record["entries"]):
            backup = folder / (f"{i}.md" if entry.get("flat") else str(i))
            if exists(backup):
                path = Path(entry["path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, path)
        record["state"] = "restored"
        atomic_write(folder / "record.json", json_bytes(record))
        pool.event("migrate-restore")
        return {
            "migration": migration_id,
            "restored": True,
            "note": "Original global entries restored; pool snapshots retained.",
        }
