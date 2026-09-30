"""File and git operations on the brain vault clone."""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SEARCH_LIMIT = 50
PULL_INTERVAL_SECONDS = 30


class VaultError(Exception):
    """Error reported back to the MCP client as a readable message."""


@dataclass
class Vault:
    root: Path
    branch: str = "main"
    host_label: str = field(default_factory=socket.gethostname)
    pull_interval: float = PULL_INTERVAL_SECONDS
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_pull: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()

    # ---------- git ----------

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check and result.returncode != 0:
            raise VaultError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result

    def _reset_to_remote(self) -> None:
        self._git("rebase", "--abort", check=False)
        self._git("reset", "--hard", f"origin/{self.branch}", check=False)

    def _pull(self) -> None:
        result = self._git("pull", "--rebase", "-q", "origin", self.branch, check=False)
        if result.returncode != 0:
            self._reset_to_remote()
            raise VaultError(f"git pull failed: {result.stderr.strip()}")
        self._last_pull = time.monotonic()

    def _maybe_pull(self) -> None:
        if time.monotonic() - self._last_pull >= self.pull_interval:
            self._pull()

    def _commit_and_push(self, rel: str) -> None:
        self._git("add", "--", rel)
        if not self._git("diff", "--cached", "--quiet", check=False).returncode:
            return  # content unchanged, nothing to commit
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._git("commit", "-q", "-m", f"sync: {self.host_label} {stamp}")
        for attempt in range(2):
            push = self._git("push", "-q", "origin", self.branch, check=False)
            if push.returncode == 0:
                return
            # Rejected: someone pushed in between; rebase our commit on top and retry
            rebase = self._git("pull", "--rebase", "-q", "origin", self.branch, check=False)
            if rebase.returncode != 0:
                self._reset_to_remote()
                raise VaultError(
                    f"Conflict with a concurrent change on {rel}; the edit was dropped. "
                    "Read the note again and retry."
                )
        self._reset_to_remote()
        raise VaultError(f"git push failed: {push.stderr.strip()}")

    # ---------- paths ----------

    def _resolve(self, rel: str) -> Path:
        rel = rel.strip().lstrip("/")
        if not rel:
            raise VaultError("Empty path")
        path = (self.root / rel).resolve()
        if path != self.root and self.root not in path.parents:
            raise VaultError(f"Path outside the vault: {rel}")
        if any(part.startswith(".") for part in path.relative_to(self.root).parts):
            raise VaultError(f"Hidden paths are not accessible: {rel}")
        return path

    def _note_path(self, name: str) -> Path:
        """Resolve a relative path, or a bare note name as used in [[wiki-links]]."""
        name = name.strip().removeprefix("[[").removesuffix("]]").split("|")[0].split("#")[0]
        if not name.endswith(".md"):
            name += ".md"
        path = self._resolve(name)
        if path.is_file() or "/" in name:
            return path
        matches = [p for p in self._markdown_files(self.root) if p.name == path.name]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            listed = ", ".join(self._rel(p) for p in matches)
            raise VaultError(f"Ambiguous note name {name}: {listed}")
        return path

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _markdown_files(self, base: Path) -> list[Path]:
        files = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            files += [Path(dirpath) / f for f in sorted(filenames) if f.endswith(".md")]
        return files

    def _base_dir(self, dir: str) -> Path:
        base = self._resolve(dir) if dir.strip().strip("/") else self.root
        if not base.is_dir():
            raise VaultError(f"No such directory: {dir}")
        return base

    # ---------- public operations ----------

    def list(self, dir: str = "") -> list[str]:
        with self._lock:
            self._maybe_pull()
            return [self._rel(p) for p in self._markdown_files(self._base_dir(dir))]

    def search(self, query: str, dir: str = "") -> list[str]:
        needle = query.strip().lower()
        if not needle:
            raise VaultError("Empty query")
        with self._lock:
            self._maybe_pull()
            hits = []
            for path in self._markdown_files(self._base_dir(dir)):
                rel = self._rel(path)
                if needle in rel.lower():
                    hits.append(f"{rel}: (file name)")
                for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if needle in line.lower():
                        hits.append(f"{rel}:{n}: {line.strip()[:200]}")
                if len(hits) >= SEARCH_LIMIT:
                    hits = hits[:SEARCH_LIMIT]
                    hits.append(f"... truncated at {SEARCH_LIMIT} results, refine the query")
                    break
            return hits

    def read(self, name: str) -> tuple[str, str]:
        with self._lock:
            self._maybe_pull()
            path = self._note_path(name)
            if not path.is_file():
                raise VaultError(f"No such note: {name}")
            return self._rel(path), path.read_text(encoding="utf-8")

    def write(self, rel: str, content: str) -> str:
        with self._lock:
            self._pull()
            path = self._resolve(rel)
            if path.suffix != ".md":
                raise VaultError("Only .md notes can be written")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            rel = self._rel(path)
            self._commit_and_push(rel)
            return rel

    def edit(self, name: str, old: str, new: str) -> str:
        if not old:
            raise VaultError("The text to replace is empty")
        with self._lock:
            self._pull()
            path = self._note_path(name)
            if not path.is_file():
                raise VaultError(f"No such note: {name}")
            content = path.read_text(encoding="utf-8")
            count = content.count(old)
            if count != 1:
                raise VaultError(
                    f"The text to replace appears {count} times in {self._rel(path)}; "
                    "it must appear exactly once (include more surrounding context)."
                )
            path.write_text(content.replace(old, new), encoding="utf-8")
            rel = self._rel(path)
            self._commit_and_push(rel)
            return rel
