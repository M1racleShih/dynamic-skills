import subprocess

import pytest

from dynamic_skills import sources
from dynamic_skills.errors import SkillsError
from dynamic_skills.files import skill_files
from dynamic_skills.project import Project


@pytest.mark.parametrize(
    "url",
    [
        "ext::sh -c evil",
        "file:///etc",
        "http://host/repo",
        "https://user:secret@host/repo",
        "https://host/repo?token=x",
    ],
)
def test_unsafe_git_urls_rejected(url):
    with pytest.raises(SkillsError):
        sources.git_url(url)


@pytest.mark.parametrize(
    "source",
    [{"kind": "local"}, {"kind": "git"}, {"kind": "git", "url": "https://host/repo", "ref": []}],
)
def test_malformed_source_reports_user_error(source):
    with pytest.raises(SkillsError), sources.source_files(source):
        pass


def test_git_update_and_exact_commit_restore_offline(pool, make_skill, tmp_path, monkeypatch):
    # A real disposable Git repository; only the transport is replaced to avoid network access.
    repository = tmp_path / "upstream"
    repository.mkdir()
    make_skill(parent=repository / "skills")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args], text=True).strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Fixture")
    git("add", "skills")
    git("-c", "core.hooksPath=/dev/null", "commit", "-m", "first")
    first_commit = git("rev-parse", "HEAD")
    real_run = sources.run_git

    def offline_run(args, cwd=None):
        args = list(args)
        if args[0] == "clone":
            args[-2] = repository.as_uri()
        return real_run(["-c", "protocol.file.allow=always", *args], cwd)

    monkeypatch.setattr(sources, "run_git", offline_run)
    source = sources.parse_source("https://example.invalid/skills.git", "skills/example", "main")
    first = pool.install(source)
    assert first["source"]["commit"] == first_commit
    doc = repository / "skills/example/SKILL.md"
    doc.write_text(doc.read_text() + "Second version.\n")
    git("add", "skills")
    git("-c", "core.hooksPath=/dev/null", "commit", "-m", "second")
    second = pool.install(source, "example", updating=True)
    assert second["digest"] != first["digest"]
    with sources.source_files(first["source"], pinned=True) as (path, resolved):
        assert resolved["commit"] == first_commit
        assert b"Second version" not in next(b for n, b, _ in skill_files(path) if n == "SKILL.md")


def test_bundled_bridge_is_importable(pool, tmp_path):
    result = pool.install({"kind": "builtin", "name": "dynamic-skills"})
    assert result["id"] == "dynamic-skills"
    root = tmp_path / "project"
    root.mkdir()
    project = Project(root, pool)
    project.initialize(["pi"], "copy")
    project.plug(["dynamic-skills"])
    assert project.status()["healthy"]
