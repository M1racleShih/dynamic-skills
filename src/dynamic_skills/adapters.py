"""Native discovery paths, not an assertion of cross-agent isolation."""

from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import SkillsError


@dataclass(frozen=True)
class Adapter:
    name: str
    project_dir: str
    global_dirs: tuple[str, ...]
    refresh: str
    documentation: str
    shared_project_dirs: tuple[str, ...] = ()


ADAPTERS = {
    "codex": Adapter(
        "codex",
        ".agents/skills",
        (".agents/skills", ".codex/skills"),
        "Automatic discovery; restart if missing. Existing context remains.",
        "https://developers.openai.com/codex/skills",
    ),
    "claude": Adapter(
        "claude",
        ".claude/skills",
        (".claude/skills",),
        "Live change detection; restart if missing. Existing context remains.",
        "https://code.claude.com/docs/en/skills",
    ),
    "kimi": Adapter(
        "kimi",
        ".kimi/skills",
        (
            ".kimi/skills",
            ".claude/skills",
            ".codex/skills",
            ".config/agents/skills",
            ".agents/skills",
        ),
        "Restart Kimi Code to refresh discovered skills.",
        "https://github.com/MoonshotAI/kimi-cli/blob/main/docs/en/customization/skills.md",
        (".agents/skills",),
    ),
    "pi": Adapter(
        "pi",
        ".pi/skills",
        (".pi/agent/skills", ".agents/skills"),
        "Run /reload in a trusted project. Existing context remains.",
        "https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/skills.md",
        (".agents/skills",),
    ),
}


def select_agents(names: list[str]) -> list[str]:
    unknown = set(names) - ADAPTERS.keys()
    if unknown:
        raise SkillsError(
            f"Unknown agents: {', '.join(sorted(unknown))}. Choose {', '.join(ADAPTERS)}."
        )
    if not names:
        raise SkillsError("Select at least one agent with --agent.")
    return sorted(set(names))


def project_skill_dirs(names: list[str]) -> dict[str, str]:
    """Reuse a generated shared root instead of emitting duplicate native skills.

    Only roots required by selected agents count, not arbitrary existing directories.
    Keep branded outputs when no selected agent requires a shared root. In particular,
    do not depend on Kimi's configurable branded-directory merging/fallback behavior.
    """
    agents = select_agents(names)
    required = {ADAPTERS[name].project_dir for name in agents}
    return {
        name: next(
            (root for root in ADAPTERS[name].shared_project_dirs if root in required),
            ADAPTERS[name].project_dir,
        )
        for name in agents
    }


def detect_agents(project: Path, home: Path) -> list[str]:
    return [
        name
        for name, marker in (
            ("codex", ".codex"),
            ("claude", ".claude"),
            ("kimi", ".kimi"),
            ("pi", ".pi"),
        )
        if (project / marker).is_dir() or (home / marker).is_dir()
    ]


def adapter_info() -> list[dict]:
    return [asdict(adapter) for adapter in ADAPTERS.values()]
