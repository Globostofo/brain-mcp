"""Remote MCP server exposing the brain vault to claude.ai."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from anyio import to_thread
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from heracles_brain.vault import Vault, VaultError

INSTRUCTIONS = """\
This server gives access to Romain's brain: his central knowledge base, a folder of
Markdown notes versioned with git and shared with Claude Code on his PCs.

- At the start of any substantive task, read `_index.md`, then `todo.md`, and follow the
  links relevant to the task (profile, environment, `reference/` rules, the matching
  `projects/` note).
- Rules in `reference/` apply to every project.
- Notes are written in French and linked with `[[wiki-links]]` (by note name, not path).
  `read_note` accepts a bare note name.
- When you learn something durable, update or create the relevant note and keep
  `_index.md` in sync. Where each kind of note goes is defined in
  `reference/propose-notes.md`; new notes start from `_templates/`.
- Prefer `edit_note` for small changes; use `write_note` only for new notes or full
  rewrites. Every write is committed and pushed immediately.
"""


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
        allowed_client_redirect_uris=[
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.com/api/mcp/auth_callback",
        ],
    )


def token_username() -> str | None:
    token = get_access_token()
    return (token.claims or {}).get("preferred_username") if token else None


def build_server(
    vault: Vault, auth=None, allowed_user: str | None = None, current_user=token_username
) -> FastMCP:
    """`allowed_user` is checked on every call, on top of the SSO policy binding,
    so that a missing binding cannot open the brain to other SSO accounts."""
    mcp = FastMCP(name="brain", instructions=INSTRUCTIONS, auth=auth)

    async def call(fn, *args):
        if allowed_user is not None and current_user() != allowed_user:
            raise ToolError("Access denied: this account is not allowed to use the brain")
        try:
            return await to_thread.run_sync(fn, *args)
        except VaultError as e:
            raise ToolError(str(e)) from e

    @mcp.tool(name="list", annotations={"readOnlyHint": True})
    async def list_notes(dir: str = "") -> str:
        """List the Markdown notes of the brain, optionally under a directory (e.g. "projects")."""
        return "\n".join(await call(vault.list, dir))

    @mcp.tool(annotations={"readOnlyHint": True})
    async def search(query: str, dir: str = "") -> str:
        """Case-insensitive search in note contents and file names. Returns `path:line: excerpt`."""
        hits = await call(vault.search, query, dir)
        return "\n".join(hits) or "No match"

    @mcp.tool(annotations={"readOnlyHint": True})
    async def read_note(path: str) -> str:
        """Read a note by relative path ("setup/brain-sync.md") or by note name ("brain-sync")."""
        rel, content = await call(vault.read, path)
        return f"<!-- {rel} -->\n{content}"

    @mcp.tool(annotations={"destructiveHint": True})
    async def write_note(path: str, content: str) -> str:
        """Create a note or replace its whole content, then commit and push.
        Path is relative to the brain root and must end in .md."""
        return f"Saved {await call(vault.write, path, content)}"

    @mcp.tool(annotations={"destructiveHint": False})
    async def edit_note(path: str, old: str, new: str) -> str:
        """Replace one exact passage of a note, then commit and push.
        `old` must appear exactly once in the note; include surrounding lines if needed."""
        return f"Edited {await call(vault.edit, path, old, new)}"

    return mcp


def main() -> None:
    base_url = env("BASE_URL")
    root = Path(env("BRAIN_DIR", "/data/brain"))
    ensure_clone(root)
    vault = Vault(root, host_label=env("SYNC_HOST_LABEL", "heracles"))
    auth = build_auth(base_url)
    allowed_user = env("ALLOWED_USERNAME") if auth else None
    mcp = build_server(vault, auth, allowed_user)
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(env("PORT", "8000")),
        path="/mcp",
        allowed_hosts=[urlparse(base_url).netloc],
    )


if __name__ == "__main__":
    main()
