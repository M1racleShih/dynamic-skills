import os

import pytest

from dynamic_skills.errors import SkillsError
from dynamic_skills.migration import migrate, migrations, restore_migration, scan


def test_global_scan_covers_four_agents_and_deduplicates_shared_paths(pool, make_skill, tmp_path):
    home = tmp_path / "home"
    for relative in (".agents/skills", ".claude/skills", ".kimi/skills", ".pi/agent/skills"):
        make_skill(parent=home / relative)
    rows = scan(pool, home)
    assert len(rows) == 4
    shared = next(row for row in rows if ".agents/skills" in row["path"])
    assert set(shared["agents"]) == {"codex", "kimi", "pi"}


def test_preview_does_not_import_or_move(pool, make_skill):
    source = make_skill()
    result = migrate(pool, (source.parent,), disable=True)
    assert not result["applied"]
    assert not pool.search()
    assert source.exists()
    assert not migrations(pool)


def test_import_only_keeps_globals_and_duplicate_names(pool, make_skill, tmp_path):
    a = make_skill()
    b = make_skill(body="Another version", parent=tmp_path / "other")
    result = migrate(pool, (a.parent, b.parent), apply=True)
    assert result["applied"]
    assert a.exists() and b.exists()
    assert len(pool.search()) == 2
    assert len({row["id"] for row in result["plan"]}) == 2


def test_directory_deactivation_and_restore(pool, make_skill):
    path = make_skill()
    original = (path / "SKILL.md").read_bytes()
    result = migrate(pool, (path.parent,), apply=True, disable=True)
    assert not path.exists()
    assert pool.read("example")["content"]
    assert restore_migration(pool, result["migration"])["restored"]
    assert (path / "SKILL.md").read_bytes() == original
    assert not restore_migration(pool, result["migration"])["restored"]


def test_relative_symlink_restored_without_moving_target(pool, make_skill, tmp_path):
    target = make_skill()
    root = tmp_path / "global"
    root.mkdir()
    link = root / "example"
    link.symlink_to("../sources/example", target_is_directory=True)
    result = migrate(pool, (root,), apply=True, disable=True)
    assert not link.is_symlink()
    assert target.exists()
    restore_migration(pool, result["migration"])
    assert link.is_symlink() and link.exists()
    assert os.readlink(link) == "../sources/example"


def test_flat_markdown_migration(pool, tmp_path):
    root = tmp_path / "global"
    root.mkdir()
    path = root / "flat.md"
    path.write_text("---\nname: flat\ndescription: A flat skill.\n---\nInstructions\n")
    result = migrate(pool, (root,), apply=True, disable=True)
    assert not path.exists()
    assert pool.read("flat")["content"]
    restore_migration(pool, result["migration"])
    assert path.is_file()


def test_plugin_system_and_pool_links_are_not_migrated(pool, make_skill, tmp_path):
    root = tmp_path / "global"
    make_skill(parent=root / ".system")
    plugin = make_skill(parent=tmp_path / "plugins/cache")
    root.mkdir(exist_ok=True)
    (root / "plugin").symlink_to(plugin, target_is_directory=True)
    result = migrate(pool, (root,), apply=True, disable=True)
    assert not result["applied"]
    assert {row["ownership"] for row in result["plan"]} == {"system", "plugin"}
    assert (root / "plugin").is_symlink()


def test_restore_preflights_all_collisions(pool, make_skill):
    a = make_skill("alpha")
    b = make_skill("beta")
    result = migrate(pool, (a.parent,), apply=True, disable=True)
    b.mkdir()
    (b / "SKILL.md").write_text("new content")
    with pytest.raises(SkillsError, match="overwrite"):
        restore_migration(pool, result["migration"])
    assert not a.exists()
    assert (b / "SKILL.md").read_text() == "new content"


def test_interrupted_migration_can_be_restored(pool, make_skill, monkeypatch):
    a = make_skill("alpha")
    b = make_skill("beta")
    original = os.replace

    def interrupt(source, destination):
        if source == b:
            raise KeyboardInterrupt()
        return original(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            migrate(pool, (a.parent,), apply=True, disable=True)
    record = migrations(pool)[0]
    assert record["state"] == "preparing"
    assert not a.exists() and b.exists()
    restore_migration(pool, record["id"])
    assert a.exists() and b.exists()
