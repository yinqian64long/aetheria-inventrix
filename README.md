# flightsearch

Scheduled one-way flight search from Bangkok or Chiang Mai to Da Nang. The app polls multiple sources and sends Telegram alerts when the total is at or below the configured USD cap.

## Search specification

Defaults live in [`config.yaml`](config.yaml):

| Parameter | Default |
|-----------|---------|
| Origins | BKK, DMK, CNX |
| Destinations | DAD |
| Priority routes | BKK–DAD, DMK–DAD, CNX–DAD. Paid Apify sources rotate through this list. Kiwi, Skiplagged and Google search every airport pair. |
| Travel dates | 2026-10-28 through 2026-10-29, one way |
| Passengers | 1 adult, 1 cabin bag, 1 checked bag |
| Alert caps | CNX: 120 USD with a suitcase, 90 USD without. Bangkok (BKK, DMK): 90 USD with a suitcase, 70 USD without. A fare with unknown bag status uses the no-suitcase cap. |
| Near-miss reporting | Up to 20 USD above that offer's own cap (no alert) |

A confirmed checked bag uses the higher cap for its origin. Unknown bag status counts as no suitcase. Kiwi is searched twice, once with a hold bag and once without, so those fares are marked accordingly. Agoda, AirAsia and VietJet count a checked bag only when the allowance is 15 kg or more. Other fares stay in the results with the note `checked bag not verified` and are judged against the no-suitcase cap.

After the last day of the window passes, `results/latest.json` sets `"window_over": true` and the GitHub Actions workflow disables itself.

## Sources

| Source | Auth | Cost / limits | Notes |
|--------|------|---------------|-------|
| **kiwi** | None (MCP) | Free | Kiwi.com MCP search |
| **skiplagged** | None (MCP) | Free | Hidden-city itineraries; airline terms may prohibit intentional skip — use at your own risk |
| **google** | None (`flights` / Google Flights) | Free | May be blocked or rate-limited from datacenter IPs |
| **letsfg** | OAuth (`LETSFG_CLIENT_ID`, `LETSFG_REFRESH_TOKEN`) + **`GH_SECRETS_PAT`** | ~100 searches/day | Refresh token rotates on every use; CI must update `LETSFG_REFRESH_TOKEN` via `GH_SECRETS_PAT` or the next run breaks |
| **skyscanner** | `APIFY_TOKEN` (optional), then `APIFY_TOKEN_BACKUP` | Apify usage; budgeted in config (`max_runs_per_day`) | Disabled without a token. Backup is used after the primary account runs out of credits |
| **rpl** | None | Free | Disabled. Polish charter inventory, not Thailand–Vietnam |
| **itaka** | None | Free | Disabled. Polish charter inventory, not Thailand–Vietnam |
| **chartershop** | None | Free | Disabled. Polish charter inventory, not Thailand–Vietnam |
| **trip** | `APIFY_TOKEN` (same secret as Skyscanner) | Apify `lentic_clockss/trip-com-scraper`, pay per result; `max_routes_per_day` priority routes, each covering the date window | Trip.com one-way fares. A cabin bag is unverified unless the actor says it is included. Disabled without the token |
| **agoda** | `APIFY_TOKEN` | Apify `romy/agoda-flight-scraper` (override with `actor: blaspat/agoda-flights-scraper`); `max_routes_per_day` priority routes | [Agoda Flights](https://www.agoda.com/flights). Checked bag is counted when the allowance is 15 kg or more |
| **airasia** | `APIFY_TOKEN` | Apify `mscraper/airasia-flights-scraper`, about $0.30 per 30 saved offers; `max_results` defaults to 10 | [AirAsia](https://www.airasia.com/). One-way economy, including partner airlines |
| **vietjet** | `APIFY_TOKEN` | Apify `maximedupre/flight-scraper` with preferred airline VJ; non-VietJet rows are dropped | [VietJet](https://www.vietjetair.com/). There is no VietJet-only Apify actor, so this uses a multi-source scraper filtered to VJ |

Enable or tune sources under `sources:` in `config.yaml`. Override for a single run with `--sources kiwi,skiplagged`.

Apify sources share one account. When `APIFY_TOKEN` is out of credits, the same actor run is retried with `APIFY_TOKEN_BACKUP`, and that backup is used for the rest of the process.

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
   gh secret set APIFY_TOKEN         # primary Apify account
   gh secret set APIFY_TOKEN_BACKUP  # used after the primary account runs out of credits
   gh secret set GROK_BOT_WEBHOOK_URL
   gh secret set GROK_BOT_WEBHOOK_KEY
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

4. **Grok Bot message**: in the Bot, add a routine whose only trigger is a webhook, and paste this instruction:

   ```text
   A flight search webhook arrived. The JSON body has a "message" field.
   Post that message in this chat. Do not send email, post publicly, or change any account.
   ```

   Copy the routine's POST URL and key into `GROK_BOT_WEBHOOK_URL` and `GROK_BOT_WEBHOOK_KEY`. The search posts deal alerts, health alerts, and the daily heartbeat there. A 200 means the Bot started a run.

   ```bash
   export GROK_BOT_WEBHOOK_URL=...
   export GROK_BOT_WEBHOOK_KEY=...
   uv run python -m flightsearch test-grok
   ```

5. **LetsFG OAuth** (one time):

   ```bash
   uv run python scripts/letsfg_auth.py --set-gh-secrets --repo OWNER/REPO
   ```

   Requires `gh` authenticated and, for `--set-gh-secrets`, permission to write secrets.

6. **Enable Actions** — the [`search`](.github/workflows/search.yml) workflow runs on a cron (every 3 hours at :17 UTC) and via manual dispatch.

See [docs/RUNNERS.md](docs/RUNNERS.md) for runner choice, backup triggers, and health monitoring.

## Local runs

```bash
uv sync
uv run pytest -q
uv run python -m flightsearch run --config config.yaml --state state/state.json --out results
uv run python -m flightsearch run --dry-run --no-notify   # no state write, no Telegram or Grok Bot
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
