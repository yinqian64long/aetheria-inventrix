#!/usr/bin/env python3
"""Minimal MCP smoke test: list_tools only. No fare search."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from flightsearch.mcp_client import list_mcp_tools

HERE = Path(__file__).resolve().parent
SERVERS = json.loads((HERE / "servers.json").read_text())["servers"]


async def check_one(name: str, spec: dict) -> dict:
    url = os.environ.get(spec["url_env"], spec["url"])
    legacy = bool(spec.get("legacy"))
    tools = await list_mcp_tools(url, legacy=legacy)
    names = [t["name"] for t in tools]
    missing = [tool for tool in spec.get("expect_tools", []) if tool not in names]
    return {
        "name": name,
        "url": url,
        "ok": not missing,
        "tool_count": len(names),
        "tools": names,
        "missing": missing,
    }


async def main() -> int:
    results = []
    failed = False
    for name, spec in SERVERS.items():
        try:
            row = await check_one(name, spec)
        except Exception as exc:  # noqa: BLE001 — smoke report
            row = {"name": name, "ok": False, "error": str(exc)}
        results.append(row)
        if not row.get("ok"):
            failed = True
        print(json.dumps(row, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
