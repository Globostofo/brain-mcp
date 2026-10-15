"""Remote MCP server exposing the brain vault to claude.ai."""

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
  rewrites. Every write or deletion is committed and pushed immediately.
"""


UPSTREAM_SCOPES = "openid profile email offline_access"
# claude.ai does not show the server instructions to the model, only tool descriptions
INDEX_FIRST = (
    "At the start of a task, read the note `_index` first (`read_note(\"_index\")`): it "
    "explains how the brain is organized and links to the todo and to the rules to follow."
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
        # claude.ai requests no scope; without offline_access the SSO issues no refresh
        # token and the connector is logged out when the access token expires
        extra_authorize_params={"scope": UPSTREAM_SCOPES},
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

    def tool(name: str | None = None, annotations: dict | None = None):
        """Register a tool whose description is its docstring followed by INDEX_FIRST."""

        def register(fn):
            description = f"{inspect.getdoc(fn)}\n\n{INDEX_FIRST}"
            return mcp.tool(name=name, annotations=annotations, description=description)(fn)

        return register

    async def call(fn, *args):
        if allowed_user is not None and current_user() != allowed_user:
            raise ToolError("Access denied: this account is not allowed to use the brain")
        try:
            return await to_thread.run_sync(fn, *args)
        except VaultError as e:
            raise ToolError(str(e)) from e

    @tool(name="list", annotations={"readOnlyHint": True})
    async def list_notes(dir: str = "") -> str:
        """List the Markdown notes of the brain, optionally under a directory (e.g. "projects")."""
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
        Path is relative to the brain root and must end in .md."""
        rel, created = await call(vault.write, path, content)
        if not created:
            return f"Saved {rel}"
        return (
            f"Created {rel}. Reminder: this is a new note. If it matters, add it to the "
            "matching section of `_index.md` (with `edit_note`), and link it from related "
            "notes (`[[" + rel.rsplit("/", 1)[-1].removesuffix(".md") + "]]`)."
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
