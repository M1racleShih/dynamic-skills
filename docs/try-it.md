# Try dskills without changing your skill collection

This is a new, local-file demonstration. It does not depend on another skill
repository, an agent account, a model, or a recording. The input is the small
[review checklist](../examples/review-checklist/SKILL.md) shipped in this repository.

## Automated demonstration

From a clone of this repository:

```sh
uv run --isolated --no-project --with dynamic-skills==0.1.0 python scripts/demo.py
```

The public package used here is **0.1.0**. Source on `main` is 0.1.1; do not confuse
a source version with a published package. For a source-checkout test instead, run
`uv run python scripts/demo.py` after setting up the project's development environment.

The runner creates a temporary pool and project, invokes the CLI, checks the JSON
responses and generated files, and removes the temporary directory on completion.
It checks that:

- Dry-run previews activation without writing the project copy.
- Activation creates content identical to the input skill.
- Unplug removes the project copy but leaves the pool entry.
- Undo restores identical content and the original project pin.
- Doctor reports the temporary setup as healthy.

The printed `CHECK` lines are summaries produced by assertions, **not verbatim CLI
terminal output**. The demo exits with an error if a command or check fails. It
never imports from or migrates the user's global skills, runs skill scripts,
loads agent extensions, or makes model calls. No skill execution or token savings
are being demonstrated.

The initial clone and dependency installation require network access. Once those
are available, the demonstration uses only local files.

## Manual commands

Install the CLI first using the [README instructions](../README.md#install).
The following is for a macOS/Linux shell, starting in the repository root. The
subshell keeps the temporary pool setting out of your parent shell; it leaves the
trial directory available for inspection.

```sh
(
  repo="$PWD"
  sandbox=$(mktemp -d)
  export DYNAMIC_SKILLS_HOME="$sandbox/pool"
  mkdir "$sandbox/project"
  cd "$sandbox/project"

  dskills install "$repo/examples/review-checklist"
  dskills init --agent codex
  dskills plug review-checklist --dry-run
  dskills plug review-checklist
  dskills status
  dskills unplug review-checklist
  dskills list
  dskills undo
  dskills doctor

  printf 'Trial directory: %s\n' "$sandbox"
)
```

Inspect `.agents/skills/review-checklist/SKILL.md` inside the trial project after
plug, unplug, and undo. Delete the printed trial directory when finished, after
checking that it is the generated temporary directory rather than your own project.
For a portable, automatically cleaned trial on Windows, use the Python runner above.
