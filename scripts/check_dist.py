"""Check release versions and archive contents before uploading distributions."""

import email
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
namespace = {}
exec((root / "src/dynamic_skills/__init__.py").read_text(), namespace)
assert namespace["__version__"] == version
lock = tomllib.loads((root / "uv.lock").read_text())
assert next(p["version"] for p in lock["package"] if p["name"] == "dynamic-skills") == version
if len(sys.argv) > 1:
    assert sys.argv[1] == f"v{version}", "Tag must match package version"
assert f"## [{version}]" in (root / "CHANGELOG.md").read_text()
(wheel,) = (root / "dist").glob("*.whl")
(sdist,) = (root / "dist").glob("*.tar.gz")
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    assert "dynamic_skills/bundled/dynamic-skills/SKILL.md" in names
    assert any(n.endswith("/licenses/LICENSE") for n in names)
    assert all(
        n.startswith(("dynamic_skills/", f"dynamic_skills-{version}.dist-info/")) for n in names
    )
    metadata = email.message_from_bytes(
        archive.read(f"dynamic_skills-{version}.dist-info/METADATA")
    )
    assert metadata["Version"] == version
    assert metadata["Name"] == "dynamic-skills"
    assert metadata["License-Expression"] == "MIT"
    assert metadata["Requires-Python"] == ">=3.11"
with tarfile.open(sdist) as archive:
    names = archive.getnames()
    prefix = f"dynamic_skills-{version}/"
    for path in (
        "LICENSE",
        "README.md",
        "pyproject.toml",
        "src/dynamic_skills/bundled/dynamic-skills/SKILL.md",
    ):
        assert prefix + path in names, path
    assert not any(
        set(Path(n).parts) & {".git", ".venv", ".agents", ".dynamic-skills", "__pycache__"}
        for n in names
    )
print(f"Release {version}: versions, metadata, license and bundled skill verified")
