"""Refuse conflicting existing PyPI files; optionally require the whole release."""

import hashlib
import json
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
require_published = "--require-published" in sys.argv[1:]
try:
    with urllib.request.urlopen(
        f"https://pypi.org/pypi/dynamic-skills/{version}/json", timeout=30
    ) as response:
        published = json.load(response)
except urllib.error.HTTPError as error:
    if error.code != 404 or require_published:
        raise
    print(f"PyPI {version}: no existing release")
else:
    archives = [*(root / "dist").glob("*.whl"), *(root / "dist").glob("*.tar.gz")]
    local = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in archives}
    remote = {p["filename"]: p["digests"]["sha256"] for p in published["urls"]}
    assert local, "No local distributions"
    assert remote.keys() <= local.keys(), "Unexpected files already published"
    for name, digest in remote.items():
        assert local[name] == digest, f"PyPI file conflicts with workflow artifact: {name}"
    if require_published:
        assert local.keys() == remote.keys(), "Missing PyPI distributions"
    print(f"PyPI {version}: {len(remote)} existing distribution hashes match")
