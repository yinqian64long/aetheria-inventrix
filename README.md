# flightsearch

Scheduled one-way flight search from Thailand to Poland. The app polls multiple sources, ranks offers (including BKK/DMK/HKT departures with a CNX positioning hop in the total price), and sends Telegram alerts when the all-in total is at or below the configured USD cap.

## Search specification

Defaults live in [`config.yaml`](config.yaml):

| Parameter | Default |
|-----------|---------|
| Home origin | CNX (Chiang Mai) |
| Positioning origins | BKK, DMK, HKT (hop included in total) |
| Destinations | WAW, KRK, GDN, KTW, WRO, POZ, PRG (Václav Havel), IST (Istanbul) |
| Extra destinations | WMI (Warsaw Modlin) |
| Travel dates | 2026-10-20 through 2026-11-01 |
| Passengers | 1 adult, 1 cabin bag, no checked bag |
| Alert threshold | Total ≤ 250 USD |
| Near-miss reporting | Up to 300 USD (no alert) |

After the last day of the window passes, `results/latest.json` sets `"window_over": true` and the GitHub Actions workflow disables itself.

## Sources

| Source | Auth | Cost / limits | Notes |
|--------|------|---------------|-------|
| **kiwi** | None (MCP) | Free | Kiwi.com MCP search |
| **skiplagged** | None (MCP) | Free | Hidden-city itineraries; does not search WMI (use kiwi, google, letsfg, etc. for Modlin); airline terms may prohibit intentional skip — use at your own risk |
| **google** | None (`flights` / Google Flights) | Free | May be blocked or rate-limited from datacenter IPs |
| **letsfg** | OAuth (`LETSFG_CLIENT_ID`, `LETSFG_REFRESH_TOKEN`) + **`GH_SECRETS_PAT`** | ~100 searches/day | Refresh token rotates on every use; CI must update `LETSFG_REFRESH_TOKEN` via `GH_SECRETS_PAT` or the next run breaks |
| **skyscanner** | `APIFY_TOKEN` (optional) | Apify usage; budgeted in config (`max_runs_per_day`) | Disabled without token |
| **rpl** | None | Free | r.pl offers |
| **itaka** | None | Free | itaka.pl |
| **chartershop** | None | Free | Charter listings; many Thailand charters are restricted for sale outside Thailand (Thai CAA) |
| **trip** | `APIFY_TOKEN` (same secret as Skyscanner) | Apify `lentic_clockss/trip-com-scraper`, pay per result; `max_routes_per_day` routes, each covering the date window | Trip.com one-way fares. A cabin bag is unverified unless the actor says it is included. Disabled without the token |

Enable or tune sources under `sources:` in `config.yaml`. Override for a single run with `--sources kiwi,skiplagged`.

## Setup

1. **Create a GitHub repository** and push this project.
2. **Repository secrets** (Settings → Secrets and variables → Actions, or CLI):

   ```bash
   gh secret set TELEGRAM_BOT_TOKEN
   gh secret set TELEGRAM_CHAT_ID
   gh secret set LETSFG_CLIENT_ID
   gh secret set LETSFG_REFRESH_TOKEN
   gh secret set GH_SECRETS_PAT   # required if letsfg is enabled (see below)
   # Optional:
   gh secret set APIFY_TOKEN      # Skyscanner and Trip.com
   ```

   **`GH_SECRETS_PAT` (required when LetsFG is enabled):** LetsFG refresh tokens rotate on every API use. The workflow writes the new token back with `gh secret set LETSFG_REFRESH_TOKEN`, which needs a **fine-grained PAT** stored as `GH_SECRETS_PAT`: access limited to **this repository only**, permission **Secrets: Read and write**, expiry **at least 45 days** (rotate the PAT before it expires).

   If LetsFG is disabled in `config.yaml`, you can omit `GH_SECRETS_PAT`, `LETSFG_*`, and the LetsFG auth step.

3. **Telegram chat ID**: message your bot `/start`, then locally:

   ```bash
   export TELEGRAM_BOT_TOKEN=...
   uv sync
   uv run python -m flightsearch telegram-chat-id
   ```

   Test delivery:

   ```bash
   uv run python -m flightsearch test-telegram
   ```

4. **LetsFG OAuth** (one time):

   ```bash
   uv run python scripts/letsfg_auth.py --set-gh-secrets --repo OWNER/REPO
   ```

   Requires `gh` authenticated and, for `--set-gh-secrets`, permission to write secrets.

5. **Enable Actions** — the [`search`](.github/workflows/search.yml) workflow runs on a cron (every 6 hours at :17 UTC) and via manual dispatch.

See [docs/RUNNERS.md](docs/RUNNERS.md) for runner choice, backup triggers, and health monitoring.

## Local runs

```bash
uv sync
uv run pytest -q
uv run python -m flightsearch run --config config.yaml --state state/state.json --out results
uv run python -m flightsearch run --dry-run --no-notify   # no state write, no Telegram
uv run python -m flightsearch run --sources kiwi,google -v
```

Outputs:

- `results/latest.json` — structured run summary (includes `window_over`)
- `results/latest.md` — human-readable summary
- `state/state.json` — deduplication and cursor state (skipped with `--dry-run`)

The CLI exits with code **1** only if **every** configured source failed.

## Trigger a run on GitHub

```bash
gh workflow run search.yml
gh workflow run search.yml -f dry_run=true
gh workflow run search.yml -f sources=kiwi,skiplagged
gh run list --workflow search.yml
```

## How the schedule stops

When the travel window ends, the app sets `window_over` in `results/latest.json`. The workflow commits results, then runs `gh workflow disable search.yml` so cron stops. Re-enable manually if you extend dates in config:

```bash
gh workflow enable search.yml
```

GitHub also **auto-disables** scheduled workflows after 60 days of repository inactivity; push a commit or run the workflow manually to re-enable.

## Changing configuration

Edit [`config.yaml`](config.yaml) (dates, airports, price cap, per-source limits, Telegram heartbeat). Commit and push; the next scheduled or manual run picks up the new file.

For runner topology and backup cron, see [docs/RUNNERS.md](docs/RUNNERS.md).
