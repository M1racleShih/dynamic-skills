---
name: dynamic-skills
description: Find and read specialist skills from the local dynamic-skills pool when the current task needs one, or manage this project's skill selection.
---

Use the `dskills` CLI to expose only relevant skills for the current project.

- Inspect selection: `dskills --json status`.
- Find candidates: `dskills --json search "task keywords" --limit 5`.
- Read selected instructions: `dskills --json read <id> --project .`.
- Read an unselected pool skill on demand: `dskills --json read <id>`.
  The response includes its resource directory; resolve relative references there.
- Activate a skill when native discovery is useful: `dskills plug <id>`.
- Remove obsolete project skills within the user's authorized scope:
  `dskills unplug <id>`. `dskills undo` restores the previous selection.

Use `--help` for other operations. JSON responses contain `ok`, `data` on success,
and `error.code` / `error.message` on failure. Honor nonzero exit codes.

Reading a skill does not execute its scripts or grant extra permissions. Do not
import the entire pool, migrate global installations, or update versions merely
to prepare for an unrelated task. Native refresh varies by agent; the CLI reports
refresh instructions. Removing a skill does not erase existing conversation context.
