"""Recoverable project writes. A journal remains after process interruption."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from .adapters import ADAPTERS
from .errors import SkillsError
from .files import atomic_write, json_bytes, read_json, safe_child, tree_digest, valid_name


def exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def fingerprint(path: Path) -> str | None:
    if path.is_symlink():
        return "link:" + os.readlink(path)
    if not path.exists():
        return None
    if path.is_file():
        return "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return "tree:" + tree_digest(path)


def remove(path: Path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def output_path(root: Path, relative: str) -> Path:
    allowed = {
        "dynamic-skills.json",
        "dynamic-skills.lock.json",
        ".gitignore",
        ".dynamic-skills/state.json",
    }
    path = Path(relative)
    if relative not in allowed:
        if path.parent.as_posix() in {a.project_dir for a in ADAPTERS.values()}:
            valid_name(path.name)
        elif path.parent.as_posix() == ".dynamic-skills/history" and path.suffix == ".json":
            valid_name(path.stem)
        else:
            raise SkillsError(f"Not a managed output path: {relative}", "unsafe_path")
    return safe_child(root, relative)


class Transaction:
    def __init__(self, root: Path):
        self.root = root
        self.folder = safe_child(root, ".dynamic-skills/transaction")
        if self.folder.is_symlink():
            raise SkillsError("Transaction directory must not be a symlink.", "unsafe_path")

    def apply(self, changes: dict[str, bytes | Path | tuple | None]):
        if self.folder.exists():
            raise SkillsError(
                "An interrupted transaction exists. Run dskills recover.", "recovery_needed"
            )
        # Stage every replacement before touching any existing output.
        self.folder.mkdir(parents=True)
        entries = []
        try:
            for i, (relative, value) in enumerate(changes.items()):
                target = output_path(self.root, relative)
                staged = self.folder / f"new-{i}"
                if isinstance(value, bytes):
                    staged.write_bytes(value)
                elif isinstance(value, Path):
                    shutil.copytree(value, staged)
                elif isinstance(value, tuple):
                    staged.symlink_to(value[1], target_is_directory=True)
                entries.append(
                    {"path": relative, "before": fingerprint(target), "after": fingerprint(staged)}
                )
            atomic_write(self.folder / "journal.json", json_bytes({"entries": entries}))
        except Exception:
            shutil.rmtree(self.folder)
            raise
        try:
            for i, entry in enumerate(entries):
                target = output_path(self.root, entry["path"])
                staged = self.folder / f"new-{i}"
                backup = self.folder / f"old-{i}"
                if fingerprint(target) != entry["before"]:
                    raise SkillsError(f"Output changed during transaction: {target}", "conflict")
                target.parent.mkdir(parents=True, exist_ok=True)
                if exists(target):
                    os.replace(target, backup)
                if exists(staged):
                    os.replace(staged, target)
            atomic_write(self.folder / "committed", b"committed\n")
        except Exception:
            self.recover()
            raise
        shutil.rmtree(self.folder)

    def recover(self) -> dict:
        if not self.folder.exists():
            return {"recovered": False}
        journal = read_json(self.folder / "journal.json")
        if (self.folder / "committed").exists():
            shutil.rmtree(self.folder)
            return {"recovered": True, "action": "cleaned committed transaction"}
        if journal is None:
            # Staging failed before publication of the journal; outputs are untouched.
            shutil.rmtree(self.folder)
            return {"recovered": True, "action": "discarded unpublished staging"}
        for i, entry in reversed(list(enumerate(journal["entries"]))):
            target = output_path(self.root, entry["path"])
            backup = self.folder / f"old-{i}"
            current = fingerprint(target)
            if current == entry["before"] and not exists(backup):
                continue
            if current not in (None, entry["after"]):
                raise SkillsError(
                    f"Recovery preserved a newly edited file: {target}. "
                    "Move it aside, then run recover again.",
                    "conflict",
                )
            if exists(backup):
                if fingerprint(backup) != entry["before"]:
                    raise SkillsError("Transaction backup was modified.", "integrity_error")
                remove(target)
                os.replace(backup, target)
            elif entry["before"] is None:
                remove(target)
            else:
                raise SkillsError(f"Missing recovery backup for {target}.", "integrity_error")
        shutil.rmtree(self.folder)
        return {"recovered": True, "action": "rolled back interrupted transaction"}
