"""Human-readable terminal output and a stable JSON envelope for agents."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from filelock import Timeout
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import __version__
from .adapters import adapter_info
from .errors import SkillsError
from .migration import migrate as perform_migration
from .migration import migrations, restore_migration, scan
from .pool import Pool
from .project import Project
from .sources import parse_source


class App(click.Group):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except (SkillsError, OSError, Timeout) as exc:
            code = exc.code if isinstance(exc, SkillsError) else "filesystem_error"
            if isinstance(exc, Timeout):
                code = "busy"
            if ctx.params.get("json_output"):
                click.echo(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "ok": False,
                            "error": {"code": code, "message": str(exc)},
                        }
                    )
                )
            else:
                Console(stderr=True).print(Text(f"Error · {exc}", style="red"))
            ctx.exit(1)


def emit(data):
    ctx = click.get_current_context().find_root()
    if ctx.params["json_output"]:
        # ASCII JSON escapes round-trip Unicode even through legacy Windows pipes.
        click.echo(json.dumps({"schema_version": 1, "ok": True, "data": data}))
        return
    console = Console()
    if isinstance(data, dict) and "plan" in data:
        preview = data.get("dry_run", False) or data.get("applied") is False
        label = "Preview" if preview else data.get("action", "Migration").capitalize()
        console.print(Text(f"dynamic-skills · {label}", style="bold cyan"))
        if data.get("project"):
            console.print(Text(data["project"], style="dim"))
        table = Table(box=None, header_style="bold", padding=(0, 2))
        for title in ("Action", "Skill / path", "Version / reason"):
            table.add_column(title, overflow="fold")
        for step in data["plan"]:
            table.add_row(
                Text(step["action"], style="yellow" if preview else "green"),
                Text(step.get("path", step.get("id", ""))),
                Text(
                    step.get("digest", "")[:12]
                    or step.get("issue", "")
                    or step.get("ownership", "")
                ),
            )
        if data["plan"]:
            console.print(table)
        else:
            console.print("No filesystem changes.", style="dim")
        if "skills" in data:
            console.print(Text(f"Selected: {', '.join(data['skills']) or 'none'}"))
        if data.get("restore"):
            console.print(Text(data["restore"], style="cyan"))
        for agent, hint in data.get("refresh", {}).items():
            console.print(Text(f"{agent}: {hint}", style="dim"))
        for key in ("note", "discovery_note"):
            if data.get(key):
                console.print(Text(data[key], style="dim"))
        return
    if isinstance(data, list):
        if not data:
            console.print("No matching items.", style="dim")
            return
        keys = [k for k in data[0] if k not in {"source", "versions", "documentation"}]
        if "current" in data[0]:
            keys = ["id", "description", "tags", "current"]
        table = Table(
            title="dynamic-skills",
            title_style="bold cyan",
            border_style="dim",
            header_style="bold",
            box=None,
            padding=(0, 2),
        )
        for key in keys:
            table.add_column(key.replace("_", " ").title(), overflow="fold")
        for row in data:
            table.add_row(*(Text(display(row.get(k), k)) for k in keys))
        console.print(table)
    elif isinstance(data, dict):
        for key, value in data.items():
            console.print(
                Text(key.replace("_", " ").capitalize() + "  ", style="bold cyan"),
                Text(display(value, key)),
            )
    else:
        console.print(Text(str(data)))


def display(value, key="") -> str:
    if key in {"digest", "current"} and isinstance(value, str):
        return value[:12]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return ", ".join(value) or "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value) if value is not None else "—"


def pool() -> Pool:
    ctx = click.get_current_context().find_root()
    if ctx.obj is None:
        ctx.obj = Pool(ctx.params["home"])
    return ctx.obj


@click.group(cls=App, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--home", type=click.Path(path_type=Path), help="Override the local skill pool.")
@click.option("--json", "json_output", is_flag=True, help="Emit a versioned JSON envelope.")
@click.version_option(__version__)
def cli(home, json_output):
    """A versioned skill pool. Just the skills your project needs.

    Start with install, init, and plug. Use --json before a command for agent automation.
    """


@cli.command()
def agents():
    """Show supported agents, native paths and refresh instructions."""
    emit(adapter_info())


@cli.command()
@click.argument("source")
@click.option("--skill", "subdir", help="Skill subdirectory inside a repository.")
@click.option("--ref", help="Git branch, tag or commit.")
@click.option("--id", "skill_id", help="Local ID; resolves duplicate skill names.")
@click.option("--tag", "tags", multiple=True, help="Assign a category; repeatable.")
@click.option("--dry-run", is_flag=True)
def install(source, subdir, ref, skill_id, tags, dry_run):
    """Import a local skill or Git URL into the pool; execute no skill code."""
    emit(pool().install(parse_source(source, subdir, ref), skill_id, tags, dry_run=dry_run))


@cli.command("list")
@click.option("--tag")
def list_skills(tag):
    """List pool skills, versions and categories."""
    emit(pool().search(tag=tag))


@cli.command()
@click.argument("query", default="")
@click.option("--tag")
@click.option("--limit", type=click.IntRange(1, 1000), default=20, show_default=True)
def search(query, tag, limit):
    """Search IDs, descriptions and tags locally (all words must match)."""
    emit(pool().search(query, tag)[:limit])


@cli.command()
@click.argument("skill_id")
def info(skill_id):
    """Inspect a skill's provenance and retained versions."""
    emit(pool().get(skill_id))


@cli.command()
@click.argument("skill_id")
@click.option("--revision", help="Full hash or unambiguous prefix.")
@click.option(
    "--project",
    "project_path",
    type=click.Path(path_type=Path),
    help="Read this project's pinned version instead of the pool default.",
)
def read(skill_id, revision, project_path):
    """Read instructions on demand and count this CLI read, without activating."""
    p = pool()
    if project_path is not None:
        if revision:
            raise SkillsError("Choose --project or --revision, not both.")
        project = Project(project_path, p)
        pin = project.load()[1].get(skill_id)
        if not pin:
            raise SkillsError(f"{skill_id} is not selected in this project.", "not_found")
        path = p.restore_object(pin["digest"], pin["source"])
        p.event("read", skill_id, str(project.root))
        result = {
            "id": skill_id,
            "digest": pin["digest"],
            "path": str(path),
            "content": (path / "SKILL.md").read_text(encoding="utf-8"),
        }
    else:
        result = p.read(skill_id, revision)
    if click.get_current_context().find_root().params["json_output"]:
        emit(result)
    else:
        click.echo(result["content"])
        click.echo(f"Resources: {result['path']}", err=True)


@cli.command()
@click.argument("skill_id")
@click.option("--dry-run", is_flag=True, help="Check upstream without changing the pool.")
def update(skill_id, dry_run):
    """Update a pool skill from its source; projects keep their pinned versions."""
    p = pool()
    emit(p.install(p.version(skill_id)["source"], skill_id, updating=True, dry_run=dry_run))


@cli.command()
@click.argument("skill_id")
@click.option("--revision", help="Default: the preceding imported version.")
def rollback(skill_id, revision):
    """Move a pool skill's current version back; projects are unaffected."""
    emit(pool().rollback(skill_id, revision))


@cli.command()
@click.argument("skill_id")
@click.argument("tags", nargs=-1, required=True)
@click.option("--remove", is_flag=True)
def tag(skill_id, tags, remove):
    """Add or remove explicit skill categories."""
    emit(pool().tag(skill_id, tags, remove))


@cli.command()
@click.option("--events", is_flag=True, help="Show the latest 500 operations instead of totals.")
def stats(events):
    """Local operation counts; native reads and actual execution are not observed."""
    index = pool().index()
    if events:
        emit(index["events"])
    else:
        emit(
            {
                "scope": "Local CLI operations, not native agent execution or token savings.",
                "counts": index["counts"],
            }
        )


def project_option(function):
    return click.option(
        "--project",
        "project_path",
        type=click.Path(path_type=Path),
        default=".",
        show_default=True,
        help="Project path; finds parent manifest.",
    )(function)


@cli.command()
@project_option
@click.option("--agent", "agent_names", multiple=True, help="codex, claude, kimi, pi; repeatable.")
@click.option("--mode", type=click.Choice(["copy", "symlink"]), default="copy", show_default=True)
def init(project_path, agent_names, mode):
    """Initialize project selection; detect installed agents if none are specified."""
    emit(Project(project_path, pool()).initialize(list(agent_names), mode))


@cli.command()
@click.argument("skills", nargs=-1, required=True)
@click.option("--revision", help="Pin one skill to a retained version instead of current.")
@click.option("--dry-run", is_flag=True)
@project_option
def plug(skills, revision, dry_run, project_path):
    """Activate pool skills at exact versions, or explicitly advance existing pins."""
    emit(Project(project_path, pool()).plug(list(skills), revision, dry_run))


@cli.command()
@click.argument("skills", nargs=-1, required=True)
@click.option("--dry-run", is_flag=True)
@project_option
def unplug(skills, dry_run, project_path):
    """Remove selected skills from this project's native directories."""
    emit(Project(project_path, pool()).unplug(list(skills), dry_run))


@cli.command()
@click.option("--dry-run", is_flag=True)
@project_option
def sync(dry_run, project_path):
    """Restore exact lockfile versions and rebuild missing owned outputs."""
    emit(Project(project_path, pool()).sync(dry_run))


@cli.command()
@project_option
def status(project_path):
    """Inspect project selection, pins and locally modified outputs."""
    emit(Project(project_path, pool()).status())


@cli.command()
@click.option("--dry-run", is_flag=True)
@project_option
def undo(dry_run, project_path):
    """Undo the last project change, preserving pool versions and local edits."""
    emit(Project(project_path, pool()).undo(dry_run))


@cli.command()
@project_option
def recover(project_path):
    """Roll back an interrupted project transaction without overwriting new edits."""
    emit(Project(project_path, pool()).recover())


@cli.group()
def preset():
    """Save and apply named, version-pinned project configurations."""


@preset.command("save")
@click.argument("name")
@project_option
def preset_save(name, project_path):
    """Save this project's selection, agent targets, mode and pins."""
    emit(Project(project_path, pool()).save_preset(name))


@preset.command("apply")
@click.argument("name")
@click.option("--dry-run", is_flag=True)
@project_option
def preset_apply(name, dry_run, project_path):
    """Replace project selection with a preset; use undo to reverse."""
    emit(Project(project_path, pool()).apply_preset(name, dry_run))


@preset.command("list")
def preset_list():
    """List locally saved presets."""
    emit(
        [
            {"name": name, "skills": p["manifest"]["skills"], "agents": p["manifest"]["agents"]}
            for name, p in sorted(pool().index()["presets"].items())
        ]
    )


@preset.command("remove")
@click.argument("name")
def preset_remove(name):
    """Remove a preset; existing projects are unaffected."""
    p = pool()
    with p.lock:
        data = p.index()
        if name not in data["presets"]:
            raise SkillsError(f"Unknown preset: {name}", "not_found")
        del data["presets"][name]
        p.save(data)
    emit({"removed": name})


def scan_options(function):
    function = click.option(
        "--from",
        "roots",
        multiple=True,
        type=click.Path(path_type=Path),
        help="Scan only this skills root; repeatable.",
    )(function)
    return click.option(
        "--user-home",
        type=click.Path(path_type=Path),
        help="Override the user home used for discovery.",
    )(function)


@cli.command("scan")
@scan_options
def scan_globals(roots, user_home):
    """Inventory global candidates, duplicate names and protected origins."""
    emit(scan(pool(), user_home, roots))


@cli.command()
@scan_options
@click.option(
    "--apply", "apply_changes", is_flag=True, help="Execute the displayed migration plan."
)
@click.option("--disable", is_flag=True, help="Also move original entries into reversible backups.")
def migrate(roots, user_home, apply_changes, disable):
    """Preview global import; --apply executes, --disable also stops global discovery."""
    emit(perform_migration(pool(), roots, home=user_home, disable=disable, apply=apply_changes))


@cli.command("migrations")
def list_migrations():
    """List completed and interrupted migrations and their restore IDs."""
    emit(migrations(pool()))


@cli.command("migrate-restore")
@click.argument("migration_id")
def migrate_restore(migration_id):
    """Restore originals after migration; refuse to overwrite newly created files."""
    emit(restore_migration(pool(), migration_id))


@cli.command()
@project_option
def doctor(project_path):
    """Check pool integrity, migration recovery and project output health."""
    p = pool()
    issues = []
    checked = set()
    for item in p.index()["skills"].values():
        for version in item["versions"]:
            digest = version["digest"]
            if digest in checked:
                continue
            checked.add(digest)
            try:
                p.verify(digest)
            except SkillsError as exc:
                issues.append({"skill": item["id"], "issue": str(exc)})
    for record in migrations(p):
        if record["state"] == "preparing":
            issues.append({"migration": record["id"], "issue": "Run migrate-restore to recover."})
    project = Project(project_path, p)
    if (project.root / "dynamic-skills.json").exists():
        issues.extend(project.status()["issues"])
    emit(
        {
            "healthy": not issues,
            "pool": str(p.root),
            "objects_checked": len(checked),
            "issues": issues,
        }
    )
    if issues:
        click.get_current_context().exit(1)


@cli.command()
@project_option
def bridge(project_path):
    """Activate one small manager skill for on-demand pool discovery and CLI reads."""
    p = pool()
    project = Project(project_path, p)
    project.load()
    p.install({"kind": "builtin", "name": "dynamic-skills"})
    emit(project.plug(["dynamic-skills"]))


def main():
    # Parsing failures also honor the JSON contract. --help and --version remain text.
    try:
        result = cli(standalone_mode=False)
        if isinstance(result, int):
            raise SystemExit(result)
    except click.ClickException as exc:
        if "--json" in sys.argv[1:]:
            click.echo(
                json.dumps(
                    {
                        "schema_version": 1,
                        "ok": False,
                        "error": {"code": "usage_error", "message": exc.format_message()},
                    }
                )
            )
        else:
            exc.show()
        raise SystemExit(exc.exit_code) from exc
