import pytest

from dynamic_skills import files
from dynamic_skills.errors import SkillsError
from dynamic_skills.files import digest_files, metadata, skill_files

from .conftest import local


def test_skill_file_count_boundary(make_skill, monkeypatch):
    source = make_skill()
    monkeypatch.setattr(files, "MAX_FILES", 2)
    assert len(skill_files(source)) == 2
    (source / "extra.txt").write_text("extra")
    with pytest.raises(SkillsError, match="file limit"):
        skill_files(source)


def test_skill_total_bytes_boundary(make_skill, monkeypatch):
    source = make_skill()
    size = sum(len(content) for _, content, _ in skill_files(source))
    monkeypatch.setattr(files, "MAX_BYTES", size)
    assert sum(len(content) for _, content, _ in skill_files(source)) == size
    with (source / "references/guide.md").open("a") as stream:
        stream.write("x")
    with pytest.raises(SkillsError, match="limit"):
        skill_files(source)


def test_standalone_skill_bytes_boundary(tmp_path, monkeypatch):
    source = tmp_path / "skill.md"
    source.write_bytes(b"example")
    monkeypatch.setattr(files, "MAX_BYTES", 7)
    assert skill_files(source) == [("SKILL.md", b"example", False)]
    source.write_bytes(b"example!")
    with pytest.raises(SkillsError, match="limit"):
        skill_files(source)


def test_install_update_rollback_preserves_snapshots(pool, make_skill):
    source = make_skill()
    first = pool.install(local(source))
    (source / "SKILL.md").write_text((source / "SKILL.md").read_text() + "Changed.\n")
    preview = pool.install(local(source), "example", updating=True, dry_run=True)
    assert preview["changed"]
    assert pool.get("example")["current"] == first["digest"]
    second = pool.install(local(source), "example", updating=True)
    assert first["digest"] != second["digest"]
    assert len(pool.get("example")["versions"]) == 2
    pool.rollback("example", None)
    assert pool.get("example")["current"] == first["digest"]
    assert "Changed." not in pool.read("example")["content"]
    assert pool.verify(second["digest"]).is_dir()


def test_name_collision_needs_explicit_alias(pool, make_skill, tmp_path):
    pool.install(local(make_skill()))
    another = make_skill(parent=tmp_path / "elsewhere")
    with pytest.raises(SkillsError, match="different source"):
        pool.install(local(another))
    assert pool.install(local(another), "other-example")["id"] == "other-example"


def test_idempotent_import_and_search_statistics(pool, make_skill):
    source = make_skill()
    pool.install(local(source), tags=("testing",))
    pool.install(local(source))
    assert pool.index()["counts"]["example"]["install"] == 1
    assert len(pool.search("EXAMPLE tasks", "testing")) == 1
    pool.read("example")
    assert pool.index()["counts"]["example"]["read"] == 1
    pool.tag("example", ("testing",), remove=True)
    assert not pool.search(tag="testing")


def test_pool_corruption_detected(pool, make_skill):
    version = pool.install(local(make_skill()))
    (pool.object_path(version["digest"]) / "SKILL.md").write_text("tampered")
    with pytest.raises(SkillsError, match="modified"):
        pool.read("example")


def test_internal_symlinks_are_rejected(make_skill, tmp_path):
    path = make_skill()
    (path / "secret").symlink_to(tmp_path / "outside")
    with pytest.raises(SkillsError, match="symlinks"):
        skill_files(path)


@pytest.mark.parametrize(
    "frontmatter",
    [
        "[]",
        "name: example",
        "name: x\ndescription: 12",
        "name: !!python/object:evil {}",
        "name: &x [*x]",
    ],
)
def test_malformed_yaml_is_rejected(frontmatter):
    with pytest.raises(SkillsError):
        metadata([("SKILL.md", f"---\n{frontmatter}\n---\n".encode(), False)])


def test_hash_preserves_file_boundaries_and_executable_bits():
    assert digest_files([("a", b"bc", False)]) != digest_files([("ab", b"c", False)])
    assert digest_files([("a", b"bc", False)]) != digest_files([("a", b"bc", True)])


def test_missing_local_source_reports_clear_error(pool, tmp_path):
    with pytest.raises(SkillsError, match="unavailable"):
        pool.install(local(tmp_path / "gone"))
