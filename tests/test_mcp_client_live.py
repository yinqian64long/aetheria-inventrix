from __future__ import annotations

import pytest

from flightsearch.mcp_client import McpSession, list_mcp_tools


@pytest.mark.live
async def test_kiwi_list_tools_includes_search_flight() -> None:
    tools = await list_mcp_tools("https://mcp.kiwi.com")
    names = {t["name"] for t in tools}
    assert "search-flight" in names
    for tool in tools:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool


@pytest.mark.live
async def test_kiwi_session_reuses_connection_for_list_and_call() -> None:
    async with McpSession("https://mcp.kiwi.com", timeout=60) as session:
        tools = await session.list_tools()
        assert "search-flight" in {t["name"] for t in tools}
        data = await session.call_tool(
            "search-flight",
            {
                "flyFrom": "CNX",
                "flyTo": "BKK",
                "departureDate": "25/10/2026",
                "adults": 1,
                "currency": "USD",
                "sort": "price",
                "locale": "en",
                "max_sector_stopovers": 0,
            },
            retries=2,
        )
    assert isinstance(data, dict)
