"""Fetch source data without running skill code, Git hooks or submodules."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .errors import SkillsError
from .files import safe_child


def git_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme in {"https", "ssh"} and parsed.hostname:
        if parsed.password or parsed.query or parsed.fragment:
            raise SkillsError("Git URLs must not contain passwords, query strings or fragments.")
        if parsed.scheme == "https" and parsed.username:
            raise SkillsError("Use a Git credential helper instead of embedding credentials.")
        return value
    if re.fullmatch(r"git@[a-zA-Z0-9.-]+:[\w./-]+", value):
        return value
    raise SkillsError("Use a local skill path or an HTTPS / SSH Git repository URL.")


def run_git(args: list[str], cwd: Path | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_LFS_SKIP_SMUDGE="1",
    )
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "protocol.ext.allow=never",
                "-c",
                "protocol.file.allow=never",
                *args,
            ],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SkillsError(
            "Git is required to install or update Git sources.", "git_missing"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SkillsError("Git operation timed out after 90 seconds.", "git_timeout") from exc
    if result.returncode:
        raise SkillsError(f"Git failed: {result.stderr.strip()[-1200:]}", "git_failed")
    return result.stdout.strip()


@contextmanager
def checkout(url: str, ref: str | None = None, commit: str | None = None):
    git_url(url)
    for value in (ref, commit):
        if value and (value.startswith("-") or any(c.isspace() for c in value)):
            raise SkillsError("Invalid Git ref.")
    if commit and not re.fullmatch(r"[a-f0-9]{40,64}", commit):
        raise SkillsError("Invalid pinned Git commit.")
    with tempfile.TemporaryDirectory(prefix="dynamic-skills-git-") as temporary:
        repo = Path(temporary) / "source"
        run_git(["clone", "--no-checkout", "--depth", "1", "--", url, str(repo)])
        target = commit or ref
        if target:
            run_git(["fetch", "--depth", "1", "origin", target], repo)
            run_git(["checkout", "--detach", "FETCH_HEAD"], repo)
        else:
            run_git(["checkout", "--detach", "HEAD"], repo)
        resolved = run_git(["rev-parse", "HEAD"], repo)
        yield repo, resolved


@contextmanager
def source_files(source: dict, pinned: bool = False):
    if not isinstance(source, dict):
        raise SkillsError("Source must be an object.")
    if source.get("kind") == "local":
        if not isinstance(source.get("path"), str) or not source["path"]:
            raise SkillsError("Local source requires a path.")
        path = Path(source["path"]).expanduser().resolve()
        if not path.exists():
            raise SkillsError(f"Local source is unavailable: {path}", "source_missing")
        yield path, source
    elif source.get("kind") == "git":
        if not isinstance(source.get("url"), str):
            raise SkillsError("Git source requires a repository URL.")
        if not isinstance(source.get("subdir", "."), str):
            raise SkillsError("Git skill subdir must be a string.")
        if any(
            source.get(k) is not None and not isinstance(source[k], str) for k in ("ref", "commit")
        ):
            raise SkillsError("Git refs must be strings.")
        with checkout(
            source["url"], source.get("ref"), source.get("commit") if pinned else None
        ) as (repo, commit):
            subdir = source.get("subdir", ".")
            path = repo if subdir == "." else safe_child(repo, subdir)
            if path.is_symlink() or not path.exists():
                raise SkillsError(f"Skill path is missing or symlinked: {subdir}", "source_missing")
            yield path, {**source, "commit": commit}
    elif source.get("kind") == "builtin" and source.get("name") == "dynamic-skills":
        yield Path(__file__).parent / "bundled/dynamic-skills", source
    else:
        raise SkillsError("Unsupported source kind.")


def parse_source(value: str, subdir: str | None, ref: str | None) -> dict:
    path = Path(value).expanduser()
    if path.exists():
        if ref:
            raise SkillsError("--ref applies to Git URLs; local paths are imported as snapshots.")
        root = path.resolve()
        if subdir:
            root = safe_child(root, subdir)
        return {"kind": "local", "path": str(root)}
    return {"kind": "git", "url": git_url(value), "subdir": subdir or ".", "ref": ref}
