import json
import os
import subprocess
import sys


def run(tmp_path, *args):
    env = {**os.environ, "DYNAMIC_SKILLS_HOME": str(tmp_path / "pool")}
    return subprocess.run(
        [sys.executable, "-m", "dynamic_skills", "--json", *args],
        env=env,
        text=True,
        capture_output=True,
    )


def test_json_success_and_error_exit_codes(tmp_path):
    success = run(tmp_path, "list")
    assert success.returncode == 0
    assert json.loads(success.stdout)["data"] == []
    assert "\x1b" not in success.stdout
    failure = run(tmp_path, "read", "missing")
    assert failure.returncode == 1
    assert json.loads(failure.stdout)["error"]["code"] == "not_found"
    assert not failure.stderr
    usage = run(tmp_path, "nonexistent")
    assert usage.returncode == 2
    assert json.loads(usage.stdout)["error"]["code"] == "usage_error"


def test_cli_end_to_end(tmp_path, make_skill):
    source = make_skill()
    assert run(tmp_path, "install", str(source)).returncode == 0
    project = tmp_path / "project"
    project.mkdir()
    assert run(tmp_path, "init", "--project", str(project), "--agent", "kimi").returncode == 0
    plug = run(tmp_path, "plug", "example", "--project", str(project))
    assert plug.returncode == 0, plug.stdout
    assert (project / ".kimi/skills/example/SKILL.md").exists()
    assert json.loads(run(tmp_path, "status", "--project", str(project)).stdout)["data"]["healthy"]
    assert run(tmp_path, "unplug", "example", "--project", str(project)).returncode == 0
    assert not (project / ".kimi/skills/example").exists()


def test_json_preserves_unicode_through_legacy_terminal_encoding(tmp_path):
    source = tmp_path / "unicode-skill"
    source.mkdir()
    description = "整理技能与文档。"
    (source / "SKILL.md").write_text(
        f"---\nname: unicode-skill\ndescription: {description}\n---\n说明正文。\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "dynamic_skills",
            "--home",
            str(tmp_path / "pool"),
            "--json",
            "install",
            str(source),
        ],
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["data"]["description"] == description
