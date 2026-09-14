# Quickstart demo

The GIF is a 40-second replay of real output from the PyPI `dynamic-skills==0.1.0`
package on Linux / Python 3.13.9, recorded on 2026-09-14. Pauses are edited for
readability; the filesystem panel comes from assertions against the generated files.
It is a CLI demonstration, not a recording of a model selecting or executing skills.

The public input is `openai/skills`, subdirectory `skills/.curated/gh-fix-ci`, pinned
to commit `49f948faa9258a0c61caceaf225e179651397431`.
The import completes before the shown activation sequence. The project and pool
are temporary, and no user's global skill directories are migrated or changed.

Reproduce with [the README quickstart](../README.md#try-it-in-a-fresh-directory).
The sequence checks that activation creates `SKILL.md`, removal deletes the project
copy while preserving the pool entry, and undo restores the exact bytes and lockfile.
`dskills doctor` must complete successfully afterwards.

The built-in manager skill is optional: run `dskills bridge` after initialization
to activate it. The video panel focuses on `gh-fix-ci`; it is not a count of all
skills discoverable by a running agent.
Native loading and refresh depend on your agent version and configuration.
