"""Exercise an installed distribution using only disposable project/pool paths."""

import importlib.metadata
import importlib.resources
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    version = importlib.metadata.version("dynamic-skills")
    if len(sys.argv) > 1:
        assert version == sys.argv[1], (version, sys.argv[1])
    resource = importlib.resources.files("dynamic_skills").joinpath(
        "bundled/dynamic-skills/SKILL.md"
    )
    assert "description:" in resource.read_text(encoding="utf-8")
    bindir = Path(sys.executable).parent
    suffix = ".exe" if os.name == "nt" else ""
    with tempfile.TemporaryDirectory(prefix="dskills-smoke-") as temp:
        root = Path(temp)
        project = root / "project"
        project.mkdir()
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["DYNAMIC_SKILLS_HOME"] = str(root / "pool")
        for name in ("dskills", "dynamic-skills"):
            result = subprocess.check_output(
                [str(bindir / (name + suffix)), "--version"], cwd=project, env=env, text=True
            )
            assert result.strip().endswith(version), result

        def run(*args):
            result = subprocess.check_output(
                [str(bindir / ("dskills" + suffix)), "--json", *args],
                cwd=project,
                env=env,
                text=True,
            )
            data = json.loads(result)
            assert data["ok"] is True and data["schema_version"] == 1, data
            return data["data"]

        source = root / "sample"
        source.mkdir()
        (source / "SKILL.md").write_text(
            "---\nname: sample\ndescription: Release smoke fixture.\n---\nFollow this guide.\n",
            encoding="utf-8",
        )
        run("install", str(source))
        run("init", "--agent", "codex")
        run("plug", "sample", "--dry-run")
        output = project / ".agents/skills/sample/SKILL.md"
        assert not output.exists()
        run("plug", "sample")
        assert output.read_bytes() == (source / "SKILL.md").read_bytes()
        run("read", "sample", "--project", str(project))
        run("unplug", "sample")
        assert not output.exists()
        run("undo")
        assert output.exists()
        run("bridge")
        assert (project / ".agents/skills/dynamic-skills/SKILL.md").is_file()
        run("sync")
        run("doctor")
    print(f"Installed distribution {version}: both CLI entry points and lifecycle smoke passed")


if __name__ == "__main__":
    main()
