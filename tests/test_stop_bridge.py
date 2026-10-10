import subprocess

import anyio
import pytest

from support import SCRIPTS_DIR, mcp_session, payload


@pytest.mark.stops_bridge
@pytest.mark.anyio
async def test_stop_command_stops_the_bridge():
    stopped = subprocess.run([str(SCRIPTS_DIR / "abaqus-mcp-stop.exe")], capture_output=True, text=True,
                             encoding="utf-8", timeout=60)
    assert stopped.returncode == 0, stopped.stderr
    assert '"stopping": true' in stopped.stdout
    await anyio.sleep(2)
    async with mcp_session() as session:
        result = await session.call_tool("ping", {})
    data = payload(result)
    assert result.is_error and data["state"] == "NOT_STARTED", data
    assert "Start MCP Bridge" in data["error"]["hint"]
