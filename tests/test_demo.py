import os
import subprocess
import sys
from pathlib import Path


def test_demo_preserves_existing_resources_and_cleans_up(tmp_path):
    pool = tmp_path / "existing-pool"
    pool.mkdir()
    marker = pool / "keep.txt"
    marker.write_bytes(b"existing pool contents")
    skill = tmp_path / ".agents/skills/existing/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"existing project skill")
    temporary = tmp_path / "demo-temporary"
    temporary.mkdir()
    script = Path(__file__).resolve().parents[1] / "scripts/demo.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env={
            **os.environ,
            "DYNAMIC_SKILLS_HOME": str(pool),
            "TMPDIR": str(temporary),
            "TEMP": str(temporary),
            "TMP": str(temporary),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Demo passed." in result.stdout
    assert "undo restored the same bytes and pinned version" in result.stdout
    assert "the pool entry remains" in result.stdout
    assert marker.read_bytes() == b"existing pool contents"
    assert list(pool.iterdir()) == [marker]
    assert skill.read_bytes() == b"existing project skill"
    assert list(temporary.iterdir()) == []
