from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from heracles_brain.server import build_server
from heracles_brain.vault import Vault

from conftest import clone, origin_file


@pytest.fixture
def server(origin: Path, tmp_path: Path):
    vault = Vault(clone(origin, tmp_path / "vault"), host_label="heracles", pull_interval=0)
    return build_server(vault)


@pytest.mark.anyio
async def test_tools_end_to_end(server, origin: Path) -> None:
    async with Client(server) as client:
        names = {t.name for t in await client.list_tools()}
        assert names == {"list", "search", "read_note", "write_note", "edit_note"}

        listing = await client.call_tool("list", {})
        assert listing.data == "_index.md\nprojects/demo/demo.md"

        note = await client.call_tool("read_note", {"path": "demo"})
        assert note.data.startswith("<!-- projects/demo/demo.md -->\nDemo project")

        await client.call_tool("edit_note", {"path": "demo", "old": "Traefik", "new": "Caddy"})
        assert "Uses Caddy" in origin_file(origin, "projects/demo/demo.md")

        with pytest.raises(ToolError, match="Hidden paths"):
            await client.call_tool("read_note", {"path": ".sync/CLAUDE.md"})


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
