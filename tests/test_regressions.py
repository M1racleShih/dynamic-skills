import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from dynamic_skills.cli import cli
from dynamic_skills.errors import SkillsError
from dynamic_skills.project import MANIFEST, Project, project_root
from dynamic_skills.transaction import Transaction

from .conftest import local


def test_uninitialized_child_does_not_target_parent_git_repository(tmp_path):
    (tmp_path / ".git").mkdir()
    child = tmp_path / "a/b"
    child.mkdir(parents=True)
    assert project_root(child) == child


def test_aliases_do_not_silently_shadow_same_native_skill_name(pool, make_skill, tmp_path):
    source = make_skill()
    pool.install(local(source))
    pool.install(local(source), "alias")
    root = tmp_path / "project"
    root.mkdir()
    project = Project(root, pool)
    project.initialize(["kimi", "pi"], "copy")
    with pytest.raises(SkillsError, match="native name"):
        project.plug(["example", "alias"])
    assert not project.load()[1]


def test_symlinked_metadata_is_never_overwritten(pool, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "valuable.json"
    outside.write_text("keep")
    (root / MANIFEST).symlink_to(outside)
    with pytest.raises(SkillsError):
        Project(root, pool).initialize(["pi"], "copy")
    assert outside.read_text() == "keep"


def test_recovery_refuses_edits_after_interruption(pool, make_skill, tmp_path, monkeypatch):
    pool.install(local(make_skill()))
    root = tmp_path / "project"
    root.mkdir()
    project = Project(root, pool)
    project.initialize(["codex", "kimi"], "copy")
    real = os.replace

    def interrupt(source, destination):
        if Path(destination).as_posix().endswith(".kimi/skills/example"):
            raise KeyboardInterrupt()
        return real(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            project.plug(["example"])
    doc = root / ".agents/skills/example/SKILL.md"
    doc.write_text("new edit after crash")
    with pytest.raises(SkillsError, match="newly edited"):
        project.recover()
    assert doc.read_text() == "new edit after crash"
    assert Transaction(root).folder.exists()


def test_agent_cli_workflow_and_pinned_read(pool, make_skill, tmp_path):
    runner = CliRunner()
    root = tmp_path / "project"
    root.mkdir()
    prefix = ["--home", str(pool.root), "--json"]

    def invoke(*args):
        result = runner.invoke(cli, [*prefix, *args])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)["data"]

    source = make_skill()
    invoke("install", str(source), "--tag", "testing")
    invoke("init", "--agent", "kimi", "--project", str(root))
    invoke("plug", "example", "--project", str(root))
    (source / "SKILL.md").write_text((source / "SKILL.md").read_text() + "v2")
    invoke("update", "example")
    assert "v2" not in invoke("read", "example", "--project", str(root))["content"]
    assert "v2" in invoke("read", "example")["content"]
    invoke("rollback", "example")
    invoke("tag", "example", "backend")
    assert invoke("search", "example", "--tag", "backend")
    invoke("preset", "save", "backend", "--project", str(root))
    assert invoke("preset", "list")
    invoke("unplug", "example", "--project", str(root))
    invoke("preset", "apply", "backend", "--project", str(root))
    invoke("undo", "--project", str(root))
    invoke("preset", "remove", "backend")
    invoke("bridge", "--project", str(root))
    assert invoke("doctor", "--project", str(root))["healthy"]
    assert invoke("stats")["counts"]
    assert invoke("stats", "--events")
    invoke("sync", "--project", str(root))
    invoke("recover", "--project", str(root))
    assert len(invoke("agents")) == 4


def test_migration_cli_preview_apply_and_restore(pool, make_skill):
    runner = CliRunner()
    path = make_skill()
    prefix = ["--home", str(pool.root), "--json"]
    for command in ("scan", "migrate"):
        result = runner.invoke(cli, [*prefix, command, "--from", str(path.parent)])
        assert result.exit_code == 0
        assert json.loads(result.output)["ok"]
    result = runner.invoke(
        cli, [*prefix, "migrate", "--from", str(path.parent), "--apply", "--disable"]
    )
    assert result.exit_code == 0, result.output
    migration = json.loads(result.output)["data"]["migration"]
    assert runner.invoke(cli, [*prefix, "migrations"]).exit_code == 0
    assert runner.invoke(cli, [*prefix, "migrate-restore", migration]).exit_code == 0
    assert path.exists()
