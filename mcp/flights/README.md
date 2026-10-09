# Flight MCP configs

Local source of truth for the flight MCP servers this project uses: **kiwi** and **skiplagged**. Both are public remote HTTP servers (no local process to spawn, no tokens required).

| Server | URL | Python client notes |
| --- | --- | --- |
| kiwi | `https://mcp.kiwi.com` | `list_mcp_tools`; expect `search-flight` |
| skiplagged | `https://mcp.skiplagged.com/mcp` | `legacy=True`; expect `sk_flights_search` |

Override URLs with `KIWI_MCP_URL` / `SKIPLAGGED_MCP_URL`. If a token is ever needed, put it in `.env` (gitignored) and reference `${env:KIWI_MCP_TOKEN}` from a gitignored `mcp.json.local` — do not write secrets into `mcp.json`.

`mcp.json` is the flight-only Cursor-shaped copy. The full machine set (plus github, google-calendar, x, vercel — no Origin) lives in [`../mcp.json`](../mcp.json). `.cursor/mcp.json` and `~/.cursor/mcp.json` match that parent file.

## Cloud Agents

Copying this folder (or writing `.cursor/mcp.json` / `~/.cursor/mcp.json`) does **not** attach those servers as Cursor MCP tools on a Cloud Agent. Cloud Agents load MCP from the Cloud Agents dashboard; HTTP MCP is proxied through Cursor's backend and the config never lands in the VM. The project's `flightsearch.mcp_client` still talks to the public URLs directly.
