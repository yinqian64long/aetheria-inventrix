# MCP configs (no Origin)

Local source of truth for every MCP this VM/project had, except Cursor Origin (deleted / never present — do not add it back).

| Server | URL | Secret |
| --- | --- | --- |
| kiwi | `https://mcp.kiwi.com` | none |
| skiplagged | `https://mcp.skiplagged.com/mcp` | none |
| github | `https://api.githubcopilot.com/mcp/` | `GITHUB_PERSONAL_ACCESS_TOKEN` |
| google-calendar | `https://calendarmcp.googleapis.com/mcp/v1` | OAuth |
| x | `https://api.x.com/mcp` | OAuth |
| vercel | `https://mcp.vercel.com` | OAuth |

Flight subset: [`flights/`](flights/). Cursor copies: `.cursor/mcp.json` and `~/.cursor/mcp.json` (same contents as `mcp.json` here). Tokens stay in env / gitignored `.env`, never in these JSON files.
