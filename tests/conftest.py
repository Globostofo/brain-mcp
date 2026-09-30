import subprocess
from pathlib import Path

import pytest

from heracles_brain.vault import Vault


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def clone(origin: Path, dest: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(origin), str(dest)], check=True)
    git(dest, "config", "user.name", "Test")
    git(dest, "config", "user.email", "test@example.com")
    return dest


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = clone(origin, tmp_path / "seed")
    (seed / "projects" / "demo").mkdir(parents=True)
    (seed / "_index.md").write_text("# Brain\n\n- [[demo]]\n")
    (seed / "projects" / "demo" / "demo.md").write_text("Demo project\nUses Traefik\n")
    (seed / ".sync").mkdir()
    (seed / ".sync" / "CLAUDE.md").write_text("secret config\n")
    (seed / "notes.txt").write_text("not markdown\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "push", "-q", "origin", "main")
    return origin


@pytest.fixture
def vault(origin: Path, tmp_path: Path) -> Vault:
    return Vault(clone(origin, tmp_path / "vault"), host_label="heracles", pull_interval=0)


@pytest.fixture
def other(origin: Path, tmp_path: Path) -> Path:
    """A second clone standing in for a PC pushing concurrently."""
    return clone(origin, tmp_path / "other")


def origin_file(origin: Path, rel: str) -> str:
    return git(origin, "show", f"main:{rel}")
