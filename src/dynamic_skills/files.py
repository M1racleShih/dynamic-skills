"""Bounded skill parsing and filesystem primitives shared by pool and projects."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

import yaml

from .errors import SkillsError

MAX_BYTES = 64 * 1024 * 1024
MAX_FILES = 4096
IGNORE = {".git", ".venv", "node_modules", "__pycache__", ".DS_Store"}
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def valid_name(value: str) -> str:
    if not isinstance(value, str) or len(value) > 64 or not NAME.fullmatch(value):
        raise SkillsError(
            f"Invalid skill ID: {value!r}. Use lowercase letters, digits and hyphens."
        )
    return value


def valid_digest(value: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise SkillsError("Invalid content digest; expected a full SHA-256 hash.")
    return value


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    if path.is_symlink():
        raise SkillsError(f"Refusing symlinked metadata: {path}", "unsafe_path")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise SkillsError(f"Invalid JSON in {path}: {exc}") from exc


def json_bytes(data) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode()


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise SkillsError(f"Refusing symlinked metadata: {path}", "unsafe_path")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def safe_child(root: Path, relative: str) -> Path:
    """Reject traversal and symlink parents, including paths supplied by lockfiles."""
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or any(p in {"..", "."} for p in rel.parts):
        raise SkillsError(f"Unsafe relative path: {relative}", "unsafe_path")
    target = root / rel
    for parent in [target.parent, *target.parent.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise SkillsError(f"Symlinked parent is not managed: {parent}", "unsafe_path")
    return target


def skill_files(root: Path, *, ignore_generated: bool = True) -> list[tuple[str, bytes, bool]]:
    """Snapshot regular files; never follow links embedded in third-party skills."""
    root = root.resolve(strict=True)
    if root.is_file():
        if root.suffix.lower() != ".md":
            raise SkillsError(f"Expected a skill directory or Markdown file: {root}")
        if root.stat().st_size > MAX_BYTES:
            raise SkillsError("Skill exceeds the 64 MiB limit.")
        return [("SKILL.md", root.read_bytes(), False)]
    result = []
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not ignore_generated or d not in IGNORE)
        for name in dirs + sorted(files):
            path = Path(directory) / name
            if ignore_generated and name in IGNORE:
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise SkillsError(f"Embedded symlinks are not imported: {path}", "unsafe_path")
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise SkillsError(f"Not a regular file: {path}", "unsafe_path")
            total += info.st_size
            if total > MAX_BYTES or len(result) >= MAX_FILES:
                raise SkillsError("Skill exceeds the 64 MiB / 4096 file limit.")
            result.append(
                (
                    path.relative_to(root).as_posix(),
                    path.read_bytes(),
                    bool(info.st_mode & stat.S_IXUSR),
                )
            )
    if not any(name == "SKILL.md" for name, _, _ in result):
        raise SkillsError(f"No SKILL.md found in {root}.")
    return sorted(result)


def digest_files(files: list[tuple[str, bytes, bool]]) -> str:
    digest = hashlib.sha256()
    for name, content, executable in sorted(files):
        # Length framing prevents ambiguous path/content boundaries.
        for value in (name.encode(), b"x" if executable else b"-", content):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()


def tree_digest(path: Path) -> str:
    return digest_files(skill_files(path, ignore_generated=False))


def metadata(files: list[tuple[str, bytes, bool]]) -> dict:
    raw = next(content for name, content, _ in files if name == "SKILL.md")
    if len(raw) > 1024 * 1024:
        raise SkillsError("SKILL.md exceeds the 1 MiB limit.")
    try:
        text = raw.decode("utf-8-sig")
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ValueError("missing YAML frontmatter")
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
        header = "\n".join(lines[1:end])
        # YAML aliases are unnecessary in skill metadata and allow cyclic structures.
        if any(
            isinstance(t, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
            for t in yaml.scan(header)
        ):
            raise ValueError("YAML anchors and aliases are unsupported")
        data = yaml.safe_load(header)
        if not isinstance(data, dict):
            raise ValueError("frontmatter must be a mapping")
        name = data.get("name")
        description = data.get("description")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a non-empty string")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("description must be a non-empty string")
        extra = data.get("metadata") or {}
        tags = extra.get("tags", []) if isinstance(extra, dict) else []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            tags = []
        return {"name": name, "description": description.strip(), "tags": sorted(set(tags))}
    except (ValueError, StopIteration, UnicodeError, yaml.YAMLError) as exc:
        raise SkillsError(f"Invalid SKILL.md: {exc}") from exc


def write_skill(path: Path, files: list[tuple[str, bytes, bool]]):
    path.mkdir(parents=True, exist_ok=True)
    for name, content, executable in files:
        target = safe_child(path, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o755 if executable else 0o644)
