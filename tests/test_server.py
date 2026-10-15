from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from brain_mcp.server import build_server
from brain_mcp.vault import Vault

from conftest import clone, origin_file


@pytest.fixture
def server(origin: Path, tmp_path: Path):
    vault = Vault(clone(origin, tmp_path / "vault"), host_label="heracles", pull_interval=0)
    return build_server(vault)


@pytest.mark.anyio
async def test_tools_end_to_end(server, origin: Path) -> None:
    async with Client(server) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools} == {
            "list", "search", "read_note", "write_note", "edit_note", "delete_note", "move_note"
        }
        # claude.ai ignores the server instructions: every tool points to the index
        assert all("read the note `_index` first" in t.description for t in tools)

        listing = await client.call_tool("list", {})
        assert listing.data == "_index.md\nprojects/demo/demo.md"

        note = await client.call_tool("read_note", {"path": "demo"})
        assert note.data.startswith("<!-- projects/demo/demo.md -->\nDemo project")

        await client.call_tool("edit_note", {"path": "demo", "old": "Traefik", "new": "Caddy"})
        assert "Uses Caddy" in origin_file(origin, "projects/demo/demo.md")

        created = await client.call_tool("write_note", {"path": "setup/new.md", "content": "x\n"})
        assert created.data.startswith("Created setup/new.md. Reminder")
        assert "`_index.md`" in created.data and "[[new]]" in created.data
        replaced = await client.call_tool("write_note", {"path": "setup/new.md", "content": "y\n"})
        assert replaced.data == "Saved setup/new.md"

        moved = await client.call_tool("move_note", {"path": "new", "dest": "setup/newer.md"})
        assert moved.data == "Moved to setup/newer.md"

        deleted = await client.call_tool("delete_note", {"path": "newer"})
        assert deleted.data == "Deleted setup/newer.md"
        deleted = await client.call_tool("delete_note", {"path": "demo"})
        assert deleted.data.startswith("Deleted projects/demo/demo.md. These notes")
        assert deleted.data.endswith(": _index.md")

        with pytest.raises(ToolError, match="Hidden paths"):
            await client.call_tool("read_note", {"path": ".sync/CLAUDE.md"})


@pytest.mark.anyio
async def test_other_account_is_denied(origin: Path, tmp_path: Path) -> None:
    vault = Vault(clone(origin, tmp_path / "vault"), pull_interval=0)
    for user in ("player42", None):
        server = build_server(vault, allowed_user="brain", current_user=lambda: user)
        async with Client(server) as client:
            with pytest.raises(ToolError, match="Access denied"):
                await client.call_tool("read_note", {"path": "_index"})
    server = build_server(vault, allowed_user="brain", current_user=lambda: "brain")
    async with Client(server) as client:
        assert (await client.call_tool("read_note", {"path": "_index"})).data


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
