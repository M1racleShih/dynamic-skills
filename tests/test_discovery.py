"""Shared discovery roots must not get duplicate managed skill copies."""

import itertools
import json
import os
from pathlib import Path

import pytest

from dynamic_skills.adapters import ADAPTERS, project_skill_dirs
from dynamic_skills.errors import SkillsError
from dynamic_skills.project import MANIFEST, STATE, Project
from dynamic_skills.transaction import Transaction, exists

from .conftest import local

AGENT_COMBINATIONS = [
    list(names)
    for size in range(1, len(ADAPTERS) + 1)
    for names in itertools.combinations(ADAPTERS, size)
]


@pytest.fixture
def project(tmp_path, pool, make_skill, request):
    root = tmp_path / "project"
    root.mkdir()
    if (
        getattr(request.node, "callspec", None)
        and request.node.callspec.params.get("mode") == "symlink"
    ):
        probe = tmp_path / "symlink-probe"
        try:
            probe.symlink_to(root, target_is_directory=True)
        except OSError:
            pytest.skip("Directory symlinks are unavailable.")
        probe.unlink()
    pool.install(local(make_skill()))
    return Project(root, pool)


@pytest.mark.parametrize("agents", AGENT_COMBINATIONS)
@pytest.mark.parametrize("mode", ["copy", "symlink"])
def test_shared_discovery_lifecycle(project, agents, mode):
    expected_dirs = {
        agent: (
            ".agents/skills"
            if "codex" in agents and agent in {"kimi", "pi"}
            else ADAPTERS[agent].project_dir
        )
        for agent in agents
    }
    assert project_skill_dirs(list(reversed(agents)) + agents) == expected_dirs
    assert project.initialize(agents, mode)["project_dirs"] == expected_dirs
    preview = project.plug(["example"], dry_run=True)
    expected_paths = {f"{directory}/example" for directory in expected_dirs.values()}
    assert {step["path"] for step in preview["plan"]} == expected_paths
    assert all(step["action"] == "write" for step in preview["plan"])
    assert not project.state()["outputs"]

    project.plug(["example"])
    assert set(project.state()["outputs"]) == expected_paths
    for adapter in ADAPTERS.values():
        target = project.root / adapter.project_dir / "example"
        assert exists(target) == (f"{adapter.project_dir}/example" in expected_paths)
        if exists(target):
            assert (target / "SKILL.md").is_file()
            assert (target / "references/guide.md").is_file()
            assert target.is_symlink() == (mode == "symlink")
    status = project.status()
    assert status["healthy"]
    assert status["project_dirs"] == expected_dirs
    before = project.state()
    assert not project.sync()["changed"]
    assert project.state() == before
    project.unplug(["example"])
    assert all(not exists(project.root / path) for path in expected_paths)
    project.undo()
    assert project.status()["healthy"]
    assert set(project.state()["outputs"]) == expected_paths


def install_legacy_outputs(project, monkeypatch, mode):
    # Simulate the old release emitting one output for each agent.
    with monkeypatch.context() as patch:
        patch.setattr(
            "dynamic_skills.project.project_skill_dirs",
            lambda names: {name: ADAPTERS[name].project_dir for name in names},
        )
        project.initialize(list(ADAPTERS), mode)
        project.plug(["example"])
    assert len(project.state()["outputs"]) == 4


@pytest.mark.parametrize("mode", ["copy", "symlink"])
def test_sync_migrates_owned_duplicates_without_changing_pins(project, monkeypatch, mode):
    install_legacy_outputs(project, monkeypatch, mode)
    before = project.state()
    manifest, pins = project.load()
    assert not project.status()["healthy"]
    assert {issue["path"] for issue in project.status()["issues"]} == {
        ".kimi/skills/example",
        ".pi/skills/example",
    }
    preview = project.sync(dry_run=True)
    assert {(step["path"], step["action"]) for step in preview["plan"]} == {
        (".kimi/skills/example", "remove"),
        (".pi/skills/example", "remove"),
    }
    assert project.state() == before
    assert project.sync()["changed"]
    assert project.load() == (manifest, pins)
    assert project.status()["healthy"]
    assert not exists(project.root / ".pi/skills/example")
    assert not exists(project.root / ".kimi/skills/example")
    assert not project.sync()["changed"]
    # Undo restores selection, not the superseded duplicate output layout.
    project.undo()
    assert project.load() == (manifest, pins)
    assert project.status()["healthy"]
    assert len(project.state()["outputs"]) == 2


@pytest.mark.parametrize("mode", ["copy", "symlink"])
def test_migration_preserves_edited_duplicates(project, monkeypatch, mode):
    install_legacy_outputs(project, monkeypatch, mode)
    target = project.root / ".pi/skills/example"
    if mode == "symlink":
        target.unlink()
        target.mkdir()
    (target / "SKILL.md").write_text("local work")
    before = project.state()
    for dry_run in (True, False):
        with pytest.raises(SkillsError) as error:
            project.sync(dry_run=dry_run)
        assert error.value.code == "conflict"
    assert project.state() == before
    assert (target / "SKILL.md").read_text() == "local work"
    assert exists(project.root / ".kimi/skills/example")
    assert not Transaction(project.root).folder.exists()


def test_unused_unmanaged_native_directory_is_not_removed(project):
    target = project.root / ".pi/skills/example"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("unmanaged work")
    project.initialize(["codex", "pi"], "copy")
    project.plug(["example"])
    project.sync()
    project.unplug(["example"])
    assert (target / "SKILL.md").read_text() == "unmanaged work"


def test_existing_shared_root_does_not_imply_managed_output(project):
    target = project.root / ".agents/skills/example"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("unmanaged work")
    project.initialize(["pi"], "copy")
    project.plug(["example"])
    assert (project.root / ".pi/skills/example/SKILL.md").is_file()
    assert (target / "SKILL.md").read_text() == "unmanaged work"


@pytest.mark.parametrize("mode", ["copy", "symlink"])
def test_changing_agents_reconciles_shared_and_native_outputs(project, mode):
    project.initialize(["kimi", "pi"], mode)
    project.plug(["example"])
    config = project.root / MANIFEST
    manifest, pins = project.load()
    manifest["agents"].append("codex")
    config.write_text(json.dumps(manifest))
    assert project.sync()["changed"]
    assert set(project.state()["outputs"]) == {".agents/skills/example"}
    manifest["agents"].remove("codex")
    config.write_text(json.dumps(manifest))
    assert project.sync()["changed"]
    assert set(project.state()["outputs"]) == {".kimi/skills/example", ".pi/skills/example"}
    assert project.load()[1] == pins
    assert project.status()["healthy"]


@pytest.mark.parametrize("interrupted", [False, True])
def test_duplicate_cleanup_is_recoverable(project, monkeypatch, interrupted):
    install_legacy_outputs(project, monkeypatch, "copy")
    before = project.state()
    original = os.replace

    def fail_after_cleanup(source, destination):
        if Path(destination) == project.root / STATE and Path(source).name.startswith("new-"):
            if interrupted:
                raise KeyboardInterrupt()
            raise OSError("disk full")
        return original(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_after_cleanup)
        with pytest.raises(KeyboardInterrupt if interrupted else OSError):
            project.sync()
    if interrupted:
        assert project.recover()["recovered"]
    assert project.state() == before
    assert all((project.root / path / "SKILL.md").is_file() for path in before["outputs"])
    assert not Transaction(project.root).folder.exists()
    assert project.sync()["changed"]
    assert project.status()["healthy"]
