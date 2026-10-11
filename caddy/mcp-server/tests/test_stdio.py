import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_stdio_lists_tools(tmp_path):
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        pytest.skip("mcp client 不可用")
    env = dict(os.environ, IMAGE_MCP_CONFIG=str(tmp_path / "config.json"), IMAGE_MCP_DATA_DIR=str(tmp_path / "data"))
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py"), "--stdio"], env=env, cwd=str(ROOT))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
    assert {"generate_image", "edit_image", "describe_image", "search_images", "transform_image"} <= names
