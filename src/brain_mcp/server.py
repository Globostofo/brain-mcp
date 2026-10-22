"""Remote MCP server exposing a git-backed Markdown notes vault to AI agents."""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from anyio import to_thread
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from brain_mcp.vault import Vault, VaultError

INSTRUCTIONS = """\
This server gives access to a knowledge base: a folder of Markdown notes versioned with git,
linked with `[[wiki-links]]` (by note name, not path) and shared with other tools that edit
the same repository.

- `read_note` accepts a relative path or a bare note name, as used in `[[wiki-links]]`.
- Prefer `edit_note` for small changes; use `write_note` only for new notes or full
  rewrites. Every write, move or deletion is committed and pushed immediately, and can be
  reverted from the git history.
"""

UPSTREAM_SCOPES = "openid profile email offline_access"


def index_first(index: str) -> str:
    """Hint appended to every tool description: some MCP clients do not show the server
    instructions to the model, only tool descriptions."""
    name = index.removesuffix(".md")
    return (
        f"At the start of a task, read the note `{name}` first (`read_note(\"{name}\")`): it "
        "explains how the notes are organized and links to the rules to follow."
    )


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        sys.exit(f"Missing environment variable {name}")
    return value


def ensure_clone(root: Path) -> None:
    if (root / ".git").is_dir():
        return
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", env("BRAIN_REPO"), str(root)], check=True)


def env_list(name: str) -> list[str] | None:
    value = os.environ.get(name, "")
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


def build_auth(base_url: str):
    if os.environ.get("BRAIN_NO_AUTH") == "1":
        if urlparse(base_url).scheme == "https":
            sys.exit("BRAIN_NO_AUTH is for local development only")
        return None

    from fastmcp.server.auth.oidc_proxy import OIDCProxy

    return OIDCProxy(
        config_url=env("OIDC_CONFIG_URL"),
        client_id=env("OIDC_CLIENT_ID"),
        client_secret=env("OIDC_CLIENT_SECRET"),
        base_url=base_url,
        jwt_signing_key=env("JWT_SIGNING_KEY"),
        # Some MCP clients request no scope; without offline_access the identity provider
        # issues no refresh token and the client is logged out when the access token expires
        extra_authorize_params={"scope": UPSTREAM_SCOPES},
        # None: any MCP client may register (access is still limited to ALLOWED_USERNAME)
        allowed_client_redirect_uris=env_list("ALLOWED_REDIRECT_URIS"),
    )


def token_username() -> str | None:
    token = get_access_token()
    return (token.claims or {}).get("preferred_username") if token else None


def build_server(
    vault: Vault,
    auth=None,
    allowed_user: str | None = None,
    current_user=token_username,
    extra_instructions: str | None = None,
) -> FastMCP:
    """`allowed_user` is checked on every call, on top of the identity provider's own
    access rules, so that a misconfigured provider cannot open the notes to other accounts.
    `extra_instructions` (the deployment's own conventions) are appended to the server
    instructions; tool descriptions only point to the index note, when there is one."""
    extra = extra_instructions.strip() if extra_instructions else ""
    instructions = f"{INSTRUCTIONS}\n{extra}\n" if extra else INSTRUCTIONS
    mcp = FastMCP(name="brain", instructions=instructions, auth=auth)
    hint = index_first(vault.index_note) if vault.index_note else ""

    def tool(name: str | None = None, annotations: dict | None = None):
        """Register a tool whose description is its docstring followed by the index hint."""

        def register(fn):
            description = "\n\n".join(part for part in (inspect.getdoc(fn), hint) if part)
            return mcp.tool(name=name, annotations=annotations, description=description)(fn)

        return register

    async def call(fn, *args):
        if allowed_user is not None and current_user() != allowed_user:
            raise ToolError("Access denied: this account is not allowed to use these notes")
        try:
            return await to_thread.run_sync(fn, *args)
        except VaultError as e:
            raise ToolError(str(e)) from e

    @tool(name="list", annotations={"readOnlyHint": True})
    async def list_notes(dir: str = "") -> str:
        """List the Markdown notes, optionally under a directory (e.g. "projects")."""
        return "\n".join(await call(vault.list, dir))

    @tool(annotations={"readOnlyHint": True})
    async def search(query: str, dir: str = "") -> str:
        """Case-insensitive search in note contents and file names. Returns `path:line: excerpt`."""
        hits = await call(vault.search, query, dir)
        return "\n".join(hits) or "No match"

    @tool(annotations={"readOnlyHint": True})
    async def read_note(path: str) -> str:
        """Read a note by relative path ("setup/brain-sync.md") or by note name ("brain-sync")."""
        rel, content = await call(vault.read, path)
        return f"<!-- {rel} -->\n{content}"

    @tool(annotations={"destructiveHint": True})
    async def write_note(path: str, content: str) -> str:
        """Create a note or replace its whole content, then commit and push.
        Path is relative to the vault root and must end in .md."""
        rel, created = await call(vault.write, path, content)
        if not created:
            return f"Saved {rel}"
        link = "[[" + rel.rsplit("/", 1)[-1].removesuffix(".md") + "]]"
        if not vault.index_note:
            return f"Created {rel}. Reminder: link it from related notes (`{link}`)."
        return (
            f"Created {rel}. Reminder: this is a new note. If it matters, add it to the "
            f"matching section of `{vault.index_note}` (with `edit_note`), and link it from "
            f"related notes (`{link}`)."
        )

    @tool(annotations={"destructiveHint": False})
    async def edit_note(path: str, old: str, new: str) -> str:
        """Replace one exact passage of a note, then commit and push.
        `old` must appear exactly once in the note; include surrounding lines if needed."""
        return f"Edited {await call(vault.edit, path, old, new)}"

    @tool(annotations={"destructiveHint": True})
    async def delete_note(path: str) -> str:
        """Delete a note by relative path or by note name, then commit and push.
        It can be restored from the git history."""
        rel, backlinks = await call(vault.delete, path)
        if not backlinks:
            return f"Deleted {rel}"
        return (
            f"Deleted {rel}. These notes still link to it; update or remove the links "
            "(with `edit_note`): " + ", ".join(backlinks)
        )

    @tool(annotations={"destructiveHint": False})
    async def move_note(path: str, dest: str) -> str:
        """Move or rename a note, then commit and push. `dest` is a relative path ending
        in .md, or a directory. On a rename, [[links]] to the note are rewritten everywhere."""
        rel, rewritten = await call(vault.move, path, dest)
        if not rewritten:
            return f"Moved to {rel}"
        return f"Moved to {rel}. Links rewritten in: " + ", ".join(rewritten)

    return mcp


def main() -> None:
    base_url = env("BASE_URL")
    root = Path(env("BRAIN_DIR", "/data/brain"))
    ensure_clone(root)
    vault = Vault(
        root,
        host_label=env("SYNC_HOST_LABEL", "brain-mcp"),
        index_note=os.environ.get("INDEX_NOTE") or None,
    )
    auth = build_auth(base_url)
    allowed_user = env("ALLOWED_USERNAME") if auth else None
    mcp = build_server(vault, auth, allowed_user, extra_instructions=env("BRAIN_INSTRUCTIONS", ""))
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(env("PORT", "8000")),
        path="/mcp",
        allowed_hosts=[urlparse(base_url).netloc],
    )


if __name__ == "__main__":
    main()
