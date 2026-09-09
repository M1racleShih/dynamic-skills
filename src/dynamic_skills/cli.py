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
from .pool import Pool
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
        click.echo(json.dumps({"schema_version": 1, "ok": True, "data": data}, ensure_ascii=False))
        return
    console = Console()
    if isinstance(data, list):
        if not data:
            console.print("No matching items.", style="dim")
            return
        keys = [k for k in data[0] if k not in {"source", "versions", "documentation"}]
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
def search(query, tag):
    """Search IDs, descriptions and tags locally (all words must match)."""
    emit(pool().search(query, tag))


@cli.command()
@click.argument("skill_id")
def info(skill_id):
    """Inspect a skill's provenance and retained versions."""
    emit(pool().get(skill_id))


@cli.command()
@click.argument("skill_id")
@click.option("--revision", help="Full hash or unambiguous prefix.")
def read(skill_id, revision):
    """Read instructions on demand and count this CLI read, without activating."""
    result = pool().read(skill_id, revision)
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


def main():
    # Parsing failures also honor the JSON contract. --help and --version remain text.
    try:
        cli(standalone_mode=False)
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
