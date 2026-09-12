import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from dynamic_skills.adapters import ADAPTERS
from dynamic_skills.cli import cli
from dynamic_skills.errors import SkillsError
from dynamic_skills.project import LOCKFILE, MANIFEST, STATE, Project
from dynamic_skills.transaction import Transaction

from .conftest import local
from .test_cli import run


@pytest.fixture
def catalog(pool, make_skill):
    pool.install(local(make_skill("alpha")), tags=("frontend", "testing"))
    pool.install(local(make_skill("beta")), tags=("backend", "testing"))
    pool.install(local(make_skill("gamma")))
    return pool


@pytest.fixture
def package_project(tmp_path, catalog):
    root = tmp_path / "project"
    root.mkdir()
    project = Project(root, catalog)
    project.initialize(["codex", "kimi"], "copy", track=True)
    return project


def advance(pool, skill_id):
    source = Path(pool.version(skill_id)["source"]["path"])
    skill_file = source / "SKILL.md"
    skill_file.write_text(skill_file.read_text() + "\nUpdated version.\n")
    return pool.install(local(source), skill_id, updating=True)


def project_metadata(project):
    return {path: (project.root / path).read_bytes() for path in (MANIFEST, LOCKFILE, STATE)}


def test_category_counts_filters_and_batch_edit(catalog):
    original_versions = {item["id"]: item["current"] for item in catalog.search()}
    assert catalog.categories() == {
        "categories": [
            {"tag": "backend", "count": 1},
            {"tag": "frontend", "count": 1},
            {"tag": "testing", "count": 2},
        ],
        "untagged": 1,
    }
    assert [item["id"] for item in catalog.search(untagged=True)] == ["gamma"]
    assert not catalog.search("alpha", untagged=True)
    assert catalog.search("GAMMA tasks", untagged=True)
    with pytest.raises(SkillsError, match="not both"):
        catalog.search(tag="testing", untagged=True)
    catalog.tag_many(["gamma", "alpha", "gamma"], ("frontend",))
    assert not catalog.search(untagged=True)
    catalog.tag_many(["alpha", "gamma"], ("frontend",), remove=True)
    assert catalog.get("alpha")["tags"] == ["testing"]
    assert [item["id"] for item in catalog.search(untagged=True)] == ["gamma"]
    assert {item["id"]: item["current"] for item in catalog.search()} == original_versions
    for digest in original_versions.values():
        catalog.verify(digest)


@pytest.mark.parametrize("remove", [False, True])
def test_category_batch_unknown_id_does_not_write(catalog, remove):
    before = (catalog.root / "index.json").read_bytes()
    with pytest.raises(SkillsError, match="Unknown pool skills"):
        catalog.tag_many(["alpha", "missing"], ("frontend",), remove=remove)
    assert (catalog.root / "index.json").read_bytes() == before


def test_packages_are_static_id_lists_and_survive_pool_changes(catalog, make_skill):
    package = catalog.create_package(
        "web", ["alpha", "alpha"], ("frontend", "testing"), "前端与测试"
    )
    assert package == {"name": "web", "description": "前端与测试", "skills": ["alpha", "beta"]}
    assert catalog.index()["packages"]["web"] == {
        "description": "前端与测试",
        "skills": ["alpha", "beta"],
    }
    catalog.tag("gamma", ("testing",))
    catalog.tag("alpha", ("frontend", "testing"), remove=True)
    advance(catalog, "alpha")
    catalog.rollback("alpha", None)
    catalog.install(local(make_skill("delta")))
    assert catalog.get_package("web") == package
    assert catalog.list_packages() == [package]


def test_package_member_lifecycle(catalog):
    catalog.create_package("web", ["alpha"])
    assert catalog.edit_package("web", ["beta", "alpha", "beta"])["skills"] == ["alpha", "beta"]
    assert catalog.edit_package("web", ["beta"], remove=True)["skills"] == ["alpha"]
    catalog.create_package("api", ["beta"])
    assert [item["name"] for item in catalog.list_packages()] == ["api", "web"]
    assert catalog.resolve_packages(["web", "api", "web"]) == ["alpha", "beta"]
    assert catalog.delete_package("web") == {"deleted": "web"}
    assert catalog.get("alpha")
    assert [item["name"] for item in catalog.list_packages()] == ["api"]


@pytest.mark.parametrize(
    ("method", "args", "kwargs"),
    [
        ("create_package", ("web", ["beta"]), {}),
        ("create_package", ("empty", []), {}),
        ("create_package", ("../escape", ["alpha"]), {}),
        ("create_package", ("other", ["alpha", "missing"]), {}),
        ("create_package", ("other", ["alpha"]), {"from_tags": ("unknown",)}),
        ("edit_package", ("web", ["beta", "missing"]), {}),
        ("edit_package", ("web", ["alpha"]), {"remove": True}),
        ("edit_package", ("web", ["beta"]), {"remove": True}),
        ("edit_package", ("unknown", ["beta"]), {}),
        ("delete_package", ("unknown",), {}),
    ],
)
def test_invalid_package_operations_are_atomic(catalog, method, args, kwargs):
    catalog.create_package("web", ["alpha"])
    before = (catalog.root / "index.json").read_bytes()
    with pytest.raises(SkillsError):
        getattr(catalog, method)(*args, **kwargs)
    assert (catalog.root / "index.json").read_bytes() == before


def test_old_catalog_reads_without_migration(catalog):
    data = catalog.index()
    del data["packages"]
    catalog.save(data)
    before = (catalog.root / "index.json").read_bytes()
    assert catalog.list_packages() == []
    assert catalog.categories()["untagged"] == 1
    assert len(catalog.search()) == 3
    assert (catalog.root / "index.json").read_bytes() == before
    catalog.create_package("web", ["alpha"])
    assert catalog.index()["schema_version"] == 1
    assert len(catalog.search()) == 3


@pytest.mark.parametrize(
    "packages",
    [
        [],
        {"web": None},
        {"web": {"description": "", "skills": []}},
        {"web": {"description": [], "skills": ["alpha"]}},
        {"web": {"description": "", "skills": "alpha"}},
        {"web": {"description": "", "skills": ["alpha", "alpha"]}},
        {"web": {"description": "", "skills": [None]}},
        {"web": {"description": "", "skills": ["../escape"]}},
    ],
)
def test_malformed_package_catalog_is_rejected(catalog, packages):
    data = catalog.index()
    data["packages"] = packages
    catalog.save(data)
    before = (catalog.root / "index.json").read_bytes()
    with pytest.raises(SkillsError):
        catalog.index()
    assert (catalog.root / "index.json").read_bytes() == before


def test_apply_preserves_pins_and_uses_current_for_missing_members(package_project):
    project = package_project
    pool = project.pool
    project.plug(["alpha", "gamma"])
    previous_manifest, previous_pins = project.load()
    before = project_metadata(project)
    history = project.state()["history"]
    pool.create_package("web", ["alpha", "beta"])
    pool.create_package("tests", ["beta"])
    advance(pool, "alpha")
    current_beta = advance(pool, "beta")
    before_pool = (pool.root / "index.json").read_bytes()
    preview = project.apply_packages(["web", "tests"], dry_run=True)
    assert preview["packages"] == ["tests", "web"]
    assert preview["added"] == ["beta"]
    assert preview["kept"] == ["alpha"]
    assert len(preview["plan"]) == 2
    assert project_metadata(project) == before
    assert (pool.root / "index.json").read_bytes() == before_pool
    result = project.apply_packages(["web", "tests", "web"])
    assert result["changed"]
    manifest, pins = project.load()
    assert manifest == {**previous_manifest, "skills": ["alpha", "beta", "gamma"]}
    assert pins["alpha"] == previous_pins["alpha"]
    assert pins["gamma"] == previous_pins["gamma"]
    assert pins["beta"]["digest"] == current_beta["digest"]
    assert len(project.state()["history"]) == len(history) + 1
    after = project_metadata(project)
    counts = pool.index()["counts"]
    assert not project.apply_packages(["tests", "web"])["changed"]
    assert project_metadata(project) == after
    assert pool.index()["counts"] == counts
    project.undo()
    assert project.load() == (previous_manifest, previous_pins)
    assert not (project.root / ".agents/skills/beta").exists()


def test_package_changes_do_not_change_projects_or_presets(package_project):
    project = package_project
    pool = project.pool
    pool.create_package("web", ["alpha"])
    project.apply_packages(["web"])
    project.save_preset("web")
    before = project_metadata(project)
    pool.tag_many(["alpha"], ("frontend",), remove=True)
    pool.edit_package("web", ["beta"])
    pool.edit_package("web", ["alpha"], remove=True)
    pool.delete_package("web")
    assert project_metadata(project) == before
    project.unplug(["alpha"])
    project.apply_preset("web")
    assert list(project.load()[1]) == ["alpha"]
    old_pin = project.load()[1]["alpha"]
    advance(pool, "alpha")
    project.plug(["alpha"])
    assert project.load()[1]["alpha"] != old_pin


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("missing", ["package", "member"])
def test_missing_package_references_block_entire_apply(package_project, missing, dry_run):
    project = package_project
    pool = project.pool
    pool.create_package("web", ["alpha"])
    pool.create_package("api", ["beta"])
    if missing == "package":
        pool.delete_package("api")
    else:
        data = pool.index()
        del data["skills"]["beta"]
        pool.save(data)
    before = project_metadata(project)
    with pytest.raises(SkillsError) as error:
        project.apply_packages(["web", "api"], dry_run=dry_run)
    assert error.value.code == "not_found"
    assert project_metadata(project) == before
    assert not (project.root / ".agents/skills/alpha").exists()


def test_uninitialized_project_requires_explicit_init(tmp_path, catalog):
    root = tmp_path / "uninitialized"
    root.mkdir()
    catalog.create_package("web", ["alpha"])
    with pytest.raises(SkillsError, match="not initialized"):
        Project(root, catalog).apply_packages(["web"])
    assert not (root / MANIFEST).exists()


@pytest.mark.parametrize("mode", ["copy", "symlink"])
@pytest.mark.parametrize("agent", list(ADAPTERS))
def test_package_distribution_across_agents(tmp_path, catalog, mode, agent):
    root = tmp_path / "native-project"
    root.mkdir()
    if mode == "symlink":
        probe = tmp_path / "symlink-probe"
        try:
            probe.symlink_to(root, target_is_directory=True)
        except OSError:
            pytest.skip("Directory symlinks are unavailable.")
        probe.unlink()
    project = Project(root, catalog)
    project.initialize([agent], mode)
    catalog.create_package("web", ["alpha", "beta"])
    project.apply_packages(["web"])
    for skill_id in ("alpha", "beta"):
        output = root / ADAPTERS[agent].project_dir / skill_id
        assert output.is_symlink() == (mode == "symlink")
        assert (output / "SKILL.md").is_file()
        assert (output / "references/guide.md").read_text() == "Read this reference."
    assert project.status()["healthy"]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("conflict", ["unmanaged", "local-edit"])
def test_package_conflicts_preserve_existing_files(package_project, conflict, dry_run):
    project = package_project
    project.pool.create_package("web", ["alpha", "beta"])
    if conflict == "local-edit":
        project.plug(["alpha"])
    output = project.root / ".agents/skills/alpha"
    output.mkdir(parents=True, exist_ok=True)
    (output / "SKILL.md").write_text("User work")
    before = project_metadata(project)
    with pytest.raises(SkillsError):
        project.apply_packages(["web"], dry_run=dry_run)
    assert project_metadata(project) == before
    assert (output / "SKILL.md").read_text() == "User work"
    assert not (project.root / ".agents/skills/beta").exists()


def test_native_name_conflicts_across_packages_are_atomic(package_project):
    project = package_project
    pool = project.pool
    pool.install(pool.version("alpha")["source"], "alias")
    pool.create_package("web", ["alpha"])
    pool.create_package("other", ["alias", "beta"])
    before = project_metadata(project)
    with pytest.raises(SkillsError, match="native name"):
        project.apply_packages(["web", "other"])
    assert project_metadata(project) == before
    assert not (project.root / ".agents/skills/alpha").exists()


@pytest.mark.parametrize("interrupted", [False, True])
def test_package_transaction_rollback_and_recovery(package_project, monkeypatch, interrupted):
    project = package_project
    project.plug(["gamma"])
    project.pool.create_package("web", ["alpha", "beta"])
    before = project_metadata(project)
    original = os.replace
    failure = KeyboardInterrupt if interrupted else OSError

    def fail_on_second_skill(source, destination):
        if Path(destination).as_posix().endswith(".agents/skills/beta"):
            raise failure("injected write failure")
        return original(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_on_second_skill)
        with pytest.raises(failure):
            project.apply_packages(["web"])
    if interrupted:
        assert Transaction(project.root).folder.exists()
        assert project.recover()["recovered"]
    assert not Transaction(project.root).folder.exists()
    assert project_metadata(project) == before
    assert not (project.root / ".agents/skills/alpha").exists()
    assert (project.root / ".agents/skills/gamma/SKILL.md").exists()


def test_category_and_package_cli_workflow(tmp_path, catalog):
    runner = CliRunner()
    prefix = ["--home", str(catalog.root), "--json"]

    def invoke(*args):
        result = runner.invoke(cli, [*prefix, *args])
        assert result.exit_code == 0, result.output
        assert "\x1b" not in result.output
        response = json.loads(result.output)
        assert response["schema_version"] == 1 and response["ok"]
        return response["data"]

    assert invoke("category", "list")["untagged"] == 1
    assert [item["id"] for item in invoke("list", "--untagged")] == ["gamma"]
    invoke("category", "add", "frontend", "alpha", "gamma")
    invoke("category", "remove", "frontend", "gamma")
    assert invoke("search", "gamma", "--untagged")
    invoke("package", "create", "web", "--from-tag", "frontend", "--description", "Web tools")
    invoke("package", "add", "web", "gamma")
    invoke("package", "remove", "web", "gamma")
    invoke("package", "create", "api", "beta")
    assert len(invoke("package", "list")) == 2
    assert invoke("package", "show", "web")["skills"] == ["alpha"]
    root = tmp_path / "cli-project"
    root.mkdir()
    invoke("init", "--project", str(root), "--agent", "pi")
    preview = invoke("package", "apply", "web", "api", "--project", str(root), "--dry-run")
    assert preview["dry_run"] and preview["added"] == ["alpha", "beta"]
    invoke("package", "apply", "web", "api", "--project", str(root))
    assert invoke("status", "--project", str(root))["healthy"]
    invoke("package", "delete", "web")
    invoke("undo", "--project", str(root))
    assert invoke("status", "--project", str(root))["skills"] == []
    for args in (("category", "list"), ("package", "list"), ("package", "show", "api")):
        result = runner.invoke(cli, ["--home", str(catalog.root), *args])
        assert result.exit_code == 0, result.output
        assert "beta" in result.output or "backend" in result.output
    result = runner.invoke(
        cli, ["--home", str(catalog.root), "package", "apply", "api", "--project", str(root)]
    )
    assert result.exit_code == 0, result.output
    assert "Packages: api" in result.output


@pytest.mark.parametrize(
    ("args", "exit_code", "error_code"),
    [
        (("list", "--tag", "frontend", "--untagged"), 2, "usage_error"),
        (("search", "--tag", "frontend", "--untagged"), 2, "usage_error"),
        (("package", "show", "missing"), 1, "not_found"),
        (("package", "create", "empty"), 1, "invalid_input"),
        (("package", "apply"), 2, "usage_error"),
        (("category", "add", "frontend", "missing"), 1, "not_found"),
    ],
)
def test_new_cli_error_envelopes(tmp_path, args, exit_code, error_code):
    result = run(tmp_path, *args)
    assert result.returncode == exit_code
    assert json.loads(result.stdout)["error"]["code"] == error_code
    assert not result.stderr
