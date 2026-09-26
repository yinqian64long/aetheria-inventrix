# Runners and scheduling

Where and how to run the flight search on a schedule, plus backup triggers when GitHub’s cron is delayed or dropped.

## Comparison

| Option | Schedule | Cost | Pros | Cons |
|--------|----------|------|------|------|
| **GitHub Actions** (recommended primary) | `cron: "17 */6 * * *"` (~4×/day, odd minute) | Free for public repos (standard minutes) | Native secrets, commits `state/` + `results/`, Step Summary, 60 min job budget | Datacenter IPs may be blocked; cron can slip under load; schedule auto-disables after 60 days repo inactivity |
| **cron-job.org** (backup trigger) | External HTTP cron → `workflow_dispatch` | Free tier | Independent of GitHub’s scheduler | Needs repo dispatch token/PAT; does not run the search itself |
| **Grok Bot routine** (backup + monitor) | Routines ≥ 5 min or webhook | Cursor Pro+/SuperGrok (metered weekly cap) | Different IP, real browser for hard sites; can health-check Actions | Nondeterministic, metered; secondary only |
| **Cursor Automations** | Cloud cron | Metered cloud agent | Can call MCPs directly | Cost; overlap with Actions |
| **Vercel Cron** | Platform cron | Vercel plan | — | Function timeout 300–800 s — too tight for full multi-source run |
| **Local machine** | Manual | Your hardware | Full control | Not reliable for 24/7 schedule |

### GitHub Actions details

- Job limit **6 hours**; this workflow uses **60 minutes**.
- Cron is **UTC**; minimum interval **5 minutes**.
- Prefer **odd minutes** (e.g. `:17`) to reduce collisions with top-of-hour load.
- **Do not rely on `actions/cache` alone** for dedup — this project commits `state/state.json` so alerts survive cache eviction.
- Secrets are masked in logs and are **not** exposed to workflows from fork PRs.
- With **LetsFG** enabled, set **`GH_SECRETS_PAT`** (fine-grained PAT, this repo only, **Secrets: Read and write**, expiry ≥ 45 days) so each run can persist LetsFG’s rotating refresh token; without it, the token from the previous run is invalid after one LetsFG search.
- The workflow disables itself when `results/latest.json` has `window_over: true`, even if that run’s search step failed, as long as the results file was written.

References: [Workflow syntax — schedule](https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions#onschedule), [Billing for Actions](https://docs.github.com/en/billing/managing-billing-for-github-actions/about-billing-for-github-actions).

## Recommendation

1. **Primary:** `.github/workflows/search.yml` on GitHub Actions (every 6 hours).
2. **Backup trigger:** [cron-job.org](https://cron-job.org) POST to GitHub `workflow_dispatch` (e.g. every 6 hours offset by 3 h) if you see missed runs.
3. **Optional monitor:** Grok Bot routine on a Cursor cloud computer (see below) to verify the last successful `search.yml` run and re-dispatch if stale.
4. **Local:** Manual `uv run python -m flightsearch run` for debugging only.

## cron-job.org backup (workflow_dispatch)

Configure an HTTP job that calls the GitHub API:

```http
POST /repos/OWNER/REPO/actions/workflows/search.yml/dispatches
Authorization: Bearer <PAT with actions:write>
{"ref":"main"}
```

Use a PAT stored only in cron-job.org, not in the repo. Offset the schedule from `:17` UTC to avoid duplicating the primary cron every time unless you want redundancy.

## Grok Bot monitor routine (paste into Grok Bot)

Grok Bot: [x.ai/bot](https://x.ai/bot) — [Cursor docs](https://cursor.com/docs/grok-bot), [Routines](https://cursor.com/help/grok-bot/routines). Routines can run on a schedule (minimum 5 minutes) or via webhook (`POST` + Bearer key; HTTP 200 means started).

**Routine instruction (ready to paste):**

```text
You are a health monitor for the GitHub repo OWNER/REPO flight search workflow.

Environment (set in the Grok Bot cloud environment, never log values):
- GH_TOKEN: GitHub PAT with repo + actions:read and actions:write
- TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID: for alerts

Every run:
1. cd to a clone of OWNER/REPO (pull latest main).
2. Run: gh run list --workflow search.yml --limit 5 --json databaseId,status,conclusion,createdAt,updatedAt
3. Find the latest run with conclusion "success". If none exists in the last 7 hours, or the newest success is older than 7 hours:
   - Run: gh workflow run search.yml --ref main
   - Send Telegram: "Flight search: re-dispatched search.yml (last success stale or missing)."
4. Else if the latest run (any conclusion) is "failure" and younger than 2 hours, send Telegram once with run URL and short log hint: gh run view <id> --log-failed | tail -40
5. Else send nothing (silent OK).

Do not echo secrets. Use UTC timestamps in messages.
```

Adjust `OWNER/REPO`, the 7 h threshold (workflow runs every 6 h), and Telegram wording as needed.

**Webhook alternative:** trigger the same routine from an external cron when you want a push-based check without waiting for Grok’s schedule.

## Cursor Automations (optional)

Cursor Automations can run on a cron and invoke MCP tools (Kiwi, Skiplagged, etc.) directly. Useful for experiments or sources that fail from GitHub IPs. Treat as **optional** and **metered** — keep GitHub Actions as the source of truth for `state/` and `results/` commits unless you deliberately duplicate logic.

Docs: [Cursor Automations](https://cursor.com/docs) (product documentation).

## Local manual runs

No scheduler — use for development and secret setup:

```bash
uv sync
uv run python -m flightsearch run --config config.yaml --state state/state.json --out results
```

Use `--dry-run` and `--no-notify` to avoid side effects.

## Related links

- [GitHub Actions: Encrypted secrets](https://docs.github.com/en/actions/security-guides/using-secrets-in-github-actions)
- [Disabling and enabling workflows](https://docs.github.com/en/actions/using-workflows/disabling-and-enabling-a-workflow)
- [Grok Bot Routines help](https://cursor.com/help/grok-bot/routines)
- [cron-job.org](https://cron-job.org)
