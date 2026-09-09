import json
import os
import shutil

import pytest

from dynamic_skills.adapters import ADAPTERS
from dynamic_skills.errors import SkillsError
from dynamic_skills.pool import Pool
from dynamic_skills.project import LOCKFILE, MANIFEST, Project
from dynamic_skills.transaction import Transaction

from .conftest import local


@pytest.fixture
def project(tmp_path, pool):
    root = tmp_path / "project"
    root.mkdir()
    return Project(root, pool)


@pytest.mark.parametrize("agent", list(ADAPTERS))
def test_native_adapter_lifecycle(project, make_skill, agent):
    version = project.pool.install(local(make_skill()))
    project.initialize([agent], "copy")
    assert project.plug(["example"], dry_run=True)["plan"]
    target = project.root / ADAPTERS[agent].project_dir / "example"
    assert not target.exists()
    project.plug(["example"])
    assert (target / "SKILL.md").is_file()
    assert (target / "references" / "guide.md").is_file()
    assert project.status()["healthy"]
    assert project.load()[1]["example"]["digest"] == version["digest"]
    assert not project.sync()["changed"]
    project.unplug(["example"])
    assert not target.exists()
    project.undo()
    assert target.exists()


def test_pool_update_does_not_change_project_pin(project, make_skill):
    source = make_skill()
    v1 = project.pool.install(local(source))
    project.initialize(list(ADAPTERS), "copy")
    project.plug(["example"])
    (source / "SKILL.md").write_text((source / "SKILL.md").read_text() + "v2\n")
    v2 = project.pool.install(local(source), "example", updating=True)
    project.sync()
    assert project.load()[1]["example"]["digest"] == v1["digest"]
    project.plug(["example"])
    assert project.load()[1]["example"]["digest"] == v2["digest"]
    project.undo()
    assert project.load()[1]["example"]["digest"] == v1["digest"]


def test_unmanaged_collision_is_all_or_nothing(project, make_skill):
    project.pool.install(local(make_skill()))
    project.initialize(["codex", "kimi"], "copy")
    existing = project.root / ".kimi/skills/example"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("user content")
    with pytest.raises(SkillsError, match="Unmanaged"):
        project.plug(["example"])
    assert not (project.root / ".agents/skills/example").exists()
    assert not project.load()[1]
    assert (existing / "SKILL.md").read_text() == "user content"


def test_local_edits_block_unplug_and_preserve_state(project, make_skill):
    project.pool.install(local(make_skill()))
    project.initialize(["pi"], "copy")
    project.plug(["example"])
    target = project.root / ".pi/skills/example"
    (target / "SKILL.md").write_text("local changes")
    with pytest.raises(SkillsError, match="local edits"):
        project.unplug(["example"])
    assert "example" in project.load()[1]
    assert not project.status()["healthy"]
    assert (target / "SKILL.md").read_text() == "local changes"


def test_ignored_generated_files_are_still_local_edits(project, make_skill):
    project.pool.install(local(make_skill()))
    project.initialize(["pi"], "copy")
    project.plug(["example"])
    (project.root / ".pi/skills/example/node_modules").mkdir()
    (project.root / ".pi/skills/example/node_modules/valuable.txt").write_text("keep")
    with pytest.raises(SkillsError, match="local edits"):
        project.unplug(["example"])


def test_cold_pool_restore_and_subdirectory_discovery(project, make_skill, tmp_path):
    project.pool.install(local(make_skill()))
    project.initialize(["claude"], "copy")
    project.plug(["example"])
    clone = tmp_path / "clone"
    clone.mkdir()
    for filename in (MANIFEST, LOCKFILE):
        shutil.copy(project.root / filename, clone / filename)
    (clone / "src").mkdir()
    restored = Project(clone / "src", Pool(tmp_path / "empty-pool"))
    assert restored.root == clone
    restored.sync()
    assert restored.status()["healthy"]


def test_changed_source_does_not_satisfy_lock(project, make_skill, tmp_path):
    source = make_skill()
    project.pool.install(local(source))
    project.initialize(["pi"], "copy")
    project.plug(["example"])
    (source / "SKILL.md").write_text("different")
    clone = tmp_path / "clone"
    clone.mkdir()
    for filename in (MANIFEST, LOCKFILE):
        shutil.copy(project.root / filename, clone / filename)
    restored = Project(clone, Pool(tmp_path / "empty-pool"))
    with pytest.raises(SkillsError, match="differs"):
        restored.sync()
    assert not (clone / ".pi/skills/example").exists()


def test_symlink_output_and_repair_missing_output(project, make_skill):
    project.pool.install(local(make_skill()))
    project.initialize(["codex"], "symlink")
    project.plug(["example"])
    link = project.root / ".agents/skills/example"
    assert link.is_symlink()
    link.unlink()
    project.sync()
    assert link.is_symlink()
    project.unplug(["example"])
    assert not link.is_symlink()
    assert project.pool.read("example")["content"]


def test_symlink_parent_escape_blocked(project, make_skill, tmp_path):
    project.pool.install(local(make_skill()))
    project.initialize(["kimi"], "copy")
    outside = tmp_path / "outside"
    outside.mkdir()
    (project.root / ".kimi").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SkillsError, match="Symlinked parent"):
        project.plug(["example"])
    assert not list(outside.iterdir())


def test_preset_pins_versions_and_is_undoable(project, make_skill):
    project.pool.install(local(make_skill()))
    project.initialize(["kimi", "pi"], "copy")
    project.plug(["example"])
    project.save_preset("backend")
    project.unplug(["example"])
    project.apply_preset("backend")
    assert project.load()[0]["skills"] == ["example"]
    project.undo()
    assert not project.load()[1]


def test_failure_mid_transaction_restores_previous_outputs(project, make_skill, monkeypatch):
    project.pool.install(local(make_skill()))
    project.initialize(["codex", "kimi"], "copy")
    original = os.replace

    def fail_on_second_output(source, destination):
        if str(destination).endswith(".kimi/skills/example"):
            raise OSError("disk full")
        return original(source, destination)

    monkeypatch.setattr(os, "replace", fail_on_second_output)
    with pytest.raises(OSError, match="disk full"):
        project.plug(["example"])
    assert not project.load()[1]
    assert not (project.root / ".agents/skills/example").exists()
    assert not Transaction(project.root).folder.exists()


def test_process_interruption_leaves_recoverable_journal(project, make_skill, monkeypatch):
    project.pool.install(local(make_skill()))
    project.initialize(["codex", "kimi"], "copy")
    original = os.replace

    def interrupt(source, destination):
        if str(destination).endswith(".kimi/skills/example"):
            raise KeyboardInterrupt()
        return original(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            project.plug(["example"])
    assert Transaction(project.root).folder.exists()
    with pytest.raises(SkillsError, match="interrupted"):
        project.sync()
    assert project.recover()["recovered"]
    assert not project.load()[1]
    assert not (project.root / ".agents/skills/example").exists()


def test_manifest_traversal_rejected(project):
    project.initialize(["codex"], "copy")
    manifest = json.loads((project.root / MANIFEST).read_text())
    manifest["skills"] = ["../../../escape"]
    (project.root / MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(SkillsError, match="Invalid skill"):
        project.sync()
