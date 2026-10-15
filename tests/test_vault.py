from pathlib import Path

import pytest

from conftest import git, origin_file
from heracles_brain.vault import Vault, VaultError


def test_list_skips_hidden_and_non_markdown(vault: Vault) -> None:
    assert vault.list() == ["_index.md", "projects/demo/demo.md"]
    assert vault.list("projects") == ["projects/demo/demo.md"]


def test_search_matches_content_and_names(vault: Vault) -> None:
    assert vault.search("traefik") == ["projects/demo/demo.md:2: Uses Traefik"]
    assert "projects/demo/demo.md: (file name)" in vault.search("demo")
    assert vault.search("secret config") == []


def test_read_by_path_and_wiki_name(vault: Vault) -> None:
    assert vault.read("projects/demo/demo.md")[1].startswith("Demo project")
    assert vault.read("[[demo]]")[0] == "projects/demo/demo.md"
    assert vault.read("_index")[0] == "_index.md"


@pytest.mark.parametrize("path", ["../outside.md", ".sync/CLAUDE.md", ".git/config", "/etc/passwd"])
def test_paths_are_confined(vault: Vault, path: str) -> None:
    with pytest.raises(VaultError):
        vault.read(path)
    with pytest.raises(VaultError):
        vault.write(path, "x")


def test_write_rejects_non_markdown(vault: Vault) -> None:
    with pytest.raises(VaultError, match="Only .md"):
        vault.write("notes.txt", "x")


def test_write_commits_and_pushes(vault: Vault, origin: Path) -> None:
    assert vault.write("setup/new.md", "# New\n") == ("setup/new.md", True)
    assert vault.write("setup/new.md", "# New v2\n") == ("setup/new.md", False)
    assert origin_file(origin, "setup/new.md") == "# New v2\n"
    assert git(origin, "log", "-1", "--format=%s", "main").startswith("sync: heracles ")


def test_write_unchanged_content_makes_no_commit(vault: Vault, origin: Path) -> None:
    before = git(origin, "rev-parse", "main")
    vault.write("_index.md", "# Brain\n\n- [[demo]]\n")
    assert git(origin, "rev-parse", "main") == before


def test_edit_requires_single_occurrence(vault: Vault, origin: Path) -> None:
    vault.write("dup.md", "a\na\n")
    with pytest.raises(VaultError, match="2 times"):
        vault.edit("dup.md", "a", "b")
    with pytest.raises(VaultError, match="0 times"):
        vault.edit("dup.md", "zzz", "b")
    vault.edit("[[demo]]", "Uses Traefik", "Uses Traefik v3")
    assert origin_file(origin, "projects/demo/demo.md") == "Demo project\nUses Traefik v3\n"


def test_push_rejected_is_rebased_and_retried(vault: Vault, other: Path, origin: Path) -> None:
    vault.read("_index.md")  # clone is now "fresh"
    vault.pull_interval = 3600
    (other / "todo.md").write_text("todo\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "pc")
    git(other, "push", "-q", "origin", "main")
    # Simulate the race: skip the pre-write pull so the first push is rejected
    vault._pull = lambda: None  # type: ignore[method-assign]
    vault.write("setup/new.md", "# New\n")
    assert origin_file(origin, "todo.md") == "todo\n"
    assert origin_file(origin, "setup/new.md") == "# New\n"


def test_conflict_leaves_clone_clean(vault: Vault, other: Path, origin: Path) -> None:
    (other / "_index.md").write_text("changed on the PC\n")
    git(other, "commit", "-q", "-am", "pc")
    git(other, "push", "-q", "origin", "main")
    vault._pull = lambda: None  # type: ignore[method-assign]
    with pytest.raises(VaultError, match="Conflict"):
        vault.write("_index.md", "changed from claude.ai\n")
    assert git(vault.root, "status", "--porcelain") == ""
    assert (vault.root / "_index.md").read_text() == "changed on the PC\n"
    assert origin_file(origin, "_index.md") == "changed on the PC\n"


def test_delete_commits_and_reports_backlinks(vault: Vault, origin: Path) -> None:
    vault.write("setup/old.md", "# Old\n")
    vault.write("setup/linker.md", "See [[old|the old note]]\n")
    assert vault.delete("[[old]]") == ("setup/old.md", ["setup/linker.md"])
    assert "setup/old.md" not in git(origin, "ls-tree", "-r", "--name-only", "main")
    assert git(origin, "log", "-1", "--format=%s", "main").startswith("sync: heracles ")
    with pytest.raises(VaultError, match="No such note"):
        vault.delete("old")


def test_delete_refuses_index_and_protected_paths(vault: Vault) -> None:
    with pytest.raises(VaultError, match="index"):
        vault.delete("_index")
    for path in ("notes.txt", ".sync/CLAUDE.md", "../outside.md"):
        with pytest.raises(VaultError):
            vault.delete(path)
    assert (vault.root / "notes.txt").exists()
    assert (vault.root / ".sync" / "CLAUDE.md").exists()
