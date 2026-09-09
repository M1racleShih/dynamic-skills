from pathlib import Path

import pytest

from dynamic_skills.pool import Pool


@pytest.fixture
def pool(tmp_path):
    return Pool(tmp_path / "pool")


@pytest.fixture
def make_skill(tmp_path):
    def create(name="example", body="Follow this workflow.", parent=None):
        root = (parent or tmp_path / "sources") / name
        root.mkdir(parents=True, exist_ok=True)
        (root / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Help with {name} tasks.\n---\n\n{body}\n"
        )
        (root / "references").mkdir(exist_ok=True)
        (root / "references" / "guide.md").write_text("Read this reference.")
        return root

    return create


def local(path: Path):
    return {"kind": "local", "path": str(path)}
