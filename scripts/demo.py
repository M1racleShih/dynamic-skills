"""Run a small CLI lifecycle demo without touching the user's pool or project."""

import json
import os
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path


def main():
    source = Path(__file__).resolve().parents[1] / "examples/review-checklist"
    if not (source / "SKILL.md").is_file():
        raise RuntimeError("Clone the repository with its examples before running the demo.")
    print(f"dskills {version('dynamic-skills')} — disposable skill lifecycle demo", flush=True)
    print("Summaries below are checked against CLI JSON and actual files, not model execution.")
    with tempfile.TemporaryDirectory(prefix="dskills-demo-") as temporary:
        root = Path(temporary)
        project = root / "project"
        project.mkdir()
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["DYNAMIC_SKILLS_HOME"] = str(root / "pool")
        # Older releases inspect English Git errors for non-repository directories.
        env["LC_ALL"] = "C"

        def run(*args, display=True):
            if display:
                shown = [
                    "<repository>/examples/review-checklist" if a == str(source) else a
                    for a in args
                ]
                print("\n$ dskills " + " ".join(shown), flush=True)
            result = subprocess.run(
                [sys.executable, "-m", "dynamic_skills", "--json", *args],
                cwd=project,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            response = json.loads(result.stdout)
            assert response["ok"] and response["schema_version"] == 1, response
            return response["data"]

        run("install", str(source))
        print("CHECK: review-checklist is stored in the temporary pool.")
        initialized = run("init", "--agent", "codex")
        assert initialized["skills"] == []
        print("CHECK: the temporary project has no selected skills.")
        output = project / ".agents/skills/review-checklist/SKILL.md"
        preview = run("plug", "review-checklist", "--dry-run")
        assert preview["plan"] and not output.exists()
        print("CHECK: activation is previewed; no project copy exists.")
        run("plug", "review-checklist")
        content = output.read_bytes()
        assert content == (source / "SKILL.md").read_bytes()
        selected = run("status", display=False)
        assert selected["healthy"]
        pins = selected["pins"]
        print("CHECK: the project copy exists and matches the imported content.")
        run("unplug", "review-checklist")
        assert not output.exists()
        assert any(item["id"] == "review-checklist" for item in run("list", display=False))
        print("CHECK: the project copy is gone; the pool entry remains.")
        run("undo")
        restored = run("status", display=False)
        assert output.read_bytes() == content
        assert restored["healthy"] and restored["pins"] == pins
        print("CHECK: undo restored the same bytes and pinned version.")
        assert run("doctor")["healthy"]
        print("CHECK: the temporary pool and project are healthy.")
    assert not root.exists()
    print("\nDemo passed. Temporary files removed; your existing skills were not changed.")


if __name__ == "__main__":
    main()
