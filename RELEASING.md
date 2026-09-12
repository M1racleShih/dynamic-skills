# Releasing Dynamic Skills

Releases use a preparation PR, required CI, an immutable `vX.Y.Z` tag on `main`,
PyPI Trusted Publishing and a GitHub Release. Version 0.1.0 is a regular numbered
release with **Alpha** maturity; it is not a Python `a`/`b`/`rc` prerelease.

## One-time configuration

- Protect `main`: require a pull request and the six OS/Python CI checks. For a
  single-maintainer repository, another person's approval is not required.
- Create the GitHub environment `pypi`, permitting only tags matching `v*`.
- On PyPI, configure a pending publisher for the first upload (an existing
  project's publisher for later uploads):

  | Field | Value |
  | --- | --- |
  | PyPI project | `dynamic-skills` |
  | GitHub owner | `M1racleShih` |
  | Repository | `dynamic-skills` |
  | Workflow filename | `release.yml` |
  | Environment | `pypi` |

No long-lived PyPI API token is needed. PyPI account verification and 2FA must be
completed by the account owner. A TestPyPI rehearsal is optional and requires its
own account and publisher; never point the production workflow at an unverified
publisher configuration.

## Preparation PR

1. Update `pyproject.toml`, `src/dynamic_skills/__init__.py`, and `uv.lock` together.
2. Add a version section to `CHANGELOG.md`, including compatibility and migration
   notes. Update the README's pinned install commands and versioned asset links.
3. Run `uv sync --locked --group dev`, `uv run pytest`,
   `uv run ruff check src tests scripts`, and
   `uv run ruff format --check src tests scripts`.
4. Build with `uv build`, check with `uv run python scripts/check_dist.py vX.Y.Z`
   and `uvx --from twine twine check --strict dist/*`.
5. Test both archives in isolated installations, outside the checkout's import
   environment:

   ```sh
   for artifact in dist/*.whl dist/*.tar.gz; do
     uv run --isolated --no-project --with "$artifact" python scripts/smoke_install.py X.Y.Z
   done
   ```

6. Review the PR diff and require all six CI jobs to pass before merging.

CI checks the packaged bridge and license, validates metadata, and exercises both
CLI entry points plus install/plug/dry-run/read/unplug/undo/bridge/sync/doctor using
disposable directories. It uploads the verified Linux/Python 3.11 distributions.

## Publish

1. Fetch `main`, verify the intended merged commit and its CI, then create and push
   an annotated tag `vX.Y.Z` at that commit. Never move a published tag.
2. The Release workflow rejects non-release refs and commits outside `main`, reruns
   the CI matrix, and uploads the preserved distributions using PyPI OIDC.
3. After PyPI succeeds, the same archives and `SHA256SUMS` are attached to a GitHub
   Release whose notes come from `CHANGELOG.md`.
4. Inspect the Actions run and both public release pages. Install the exact PyPI
   version in a clean environment and run `scripts/smoke_install.py X.Y.Z`; compare
   PyPI's SHA-256 hashes to the GitHub archives/checksums. Check rendered README
   images, download links, version and Alpha classifier.

## Recovery

For a transient failure, rerun failed jobs on the original tag/run. `skip-existing`
permits an already-uploaded PyPI file to be skipped; `scripts/check_pypi.py`
rejects existing file hashes that differ from the workflow artifact before upload. Do not
rebuild and substitute different files for an existing release. GitHub attachment
retries use the same workflow artifact.

If only GitHub publication failed, rerun that job. If a shipped package is broken,
yank it on PyPI with a clear reason and publish a corrected patch version, normally
`0.1.1`. Do not delete/reuse the version or move its tag. Record the issue and
upgrade guidance in release notes. Users can reinstall a known-good exact version;
restoring incompatible on-disk metadata may also require their pre-upgrade backup.
