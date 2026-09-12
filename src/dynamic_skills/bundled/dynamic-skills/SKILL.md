---
name: dynamic-skills
description: Find and read specialist skills from the local dynamic-skills pool when the current task needs one, or manage this project's skill selection.
---

Use the `dskills` CLI to expose only relevant skills for the current project.

- Inspect selection: `dskills --json status`.
- Find candidates: `dskills --json search "task keywords" --limit 5`.
- Browse categories: `dskills --json category list`; filter with `list --tag <tag>`
  or find uncategorized skills with `list --untagged`.
- Inspect reusable combinations: `dskills --json package list` and
  `dskills --json package show <name>`.
- When the user wants a reusable combination, create one with
  `dskills package create <name> <id>...` or `--from-tag <tag>`.
  Tags are resolved only at creation; packages store skill IDs, not versions.
- Read selected instructions: `dskills --json read <id> --project .`.
- Read an unselected pool skill on demand: `dskills --json read <id>`.
  The response includes its resource directory; resolve relative references there.
- Activate a skill when native discovery is useful: `dskills plug <id>`.
- Apply a relevant, authorized combination to an initialized project with
  `dskills package apply <name>... --dry-run`, then without `--dry-run`.
  Existing pins and project settings stay unchanged; missing members use pool
  current versions. One `undo` reverses the whole application. Unlike packages,
  `preset apply` replaces the project configuration and pins.
- Remove obsolete project skills within the user's authorized scope:
  `dskills unplug <id>`. `dskills undo` restores the previous selection.

Use `--help` for other operations. JSON responses contain `ok`, `data` on success,
and `error.code` / `error.message` on failure. Honor nonzero exit codes.

Reading a skill does not execute its scripts or grant extra permissions. Do not
import the entire pool, migrate global installations, or update versions merely
to prepare for an unrelated task. Native refresh varies by agent; the CLI reports
refresh instructions. Removing a skill does not erase existing conversation context.
