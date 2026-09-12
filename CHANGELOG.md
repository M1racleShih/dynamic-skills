# Changelog

## [0.1.0]

First public release of Dynamic Skills (`dskills`), an Alpha CLI for a versioned
local skill pool and explicit project skill selection.

### Included

- Import local and Git skills with complete resources, content hashes and provenance.
- Activate pinned skills for Codex, Claude Code, Kimi Code and Pi; preview changes,
  synchronize project outputs, unplug skills and undo project changes.
- Preserve unmanaged files and local edits, with process locks, transactional
  writes and explicit interrupted-operation recovery.
- Inventory and migrate global skills with reversible backups and restore support.
- Organize skills with tags/categories, additive reusable packages and pinned presets.
- Search and read skills through a versioned JSON CLI interface and an optional
  bundled bridge skill; inspect local operation statistics and health diagnostics.
- Keep project metadata local by default, with opt-in Git tracking of configuration
  and exact lockfile pins.

### Compatibility and limitations

- Python 3.11+; automated CI covers Linux, macOS and Windows on Python 3.11 and 3.13.
- Both `dskills` and `dynamic-skills` are supported CLI entry points.
- JSON responses use `schema_version: 1`. Alpha releases may change interfaces or
  metadata formats; consult release notes before upgrading and retain backups.
- Native agent discovery depends on agent versions and configuration. Removing a
  skill does not erase instructions already loaded into a conversation.
- Local source paths are not inherently portable. Cross-filesystem migration with
  deactivation is unsupported; symlink mode depends on filesystem permissions.
- No skill scripts run on import, activation or reading. Imported skills retain
  their own licenses; execution and trust remain the user's responsibility.

[0.1.0]: https://github.com/M1racleShih/dynamic-skills/releases/tag/v0.1.0
