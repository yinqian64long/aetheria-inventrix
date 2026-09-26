from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from flightsearch.config import load_config
from flightsearch.notify import TelegramNotifier, get_chat_ids
from flightsearch.pipeline import RunResult, run_search
from flightsearch.report import format_markdown
from flightsearch.state import State

log = logging.getLogger("flightsearch")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "verbose", False))
    try:
        return asyncio.run(_dispatch(args))
    except KeyboardInterrupt:
        return 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flightsearch")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Search flights and write results")
    run.add_argument("--config", default="config.yaml")
    run.add_argument("--sources", default=None, help="Comma-separated source names")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--no-notify", action="store_true")
    run.add_argument("--state", default="state/state.json")
    run.add_argument("--out", default="results")
    run.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("test-telegram", help="Send a test Telegram message")
    sub.add_parser("telegram-chat-id", help="Print chat ids from getUpdates")
    return parser


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "run":
        return await _cmd_run(args)
    if args.command == "test-telegram":
        return await _cmd_test_telegram()
    if args.command == "telegram-chat-id":
        return await _cmd_telegram_chat_id()
    return 2


async def _cmd_run(args: argparse.Namespace) -> int:
    env = os.environ
    config = load_config(args.config)
    state = State.load(args.state)
    only_sources = _parse_sources(args.sources)
    now = datetime.now(timezone.utc)

    async with httpx.AsyncClient(timeout=60.0) as http:
        notifier = _maybe_notifier(args, config, env, http)
        result = await run_search(
            config,
            env=env,
            state=state,
            now=now,
            only_sources=only_sources,
            notifier=notifier,
            dry_run=args.dry_run,
            http=http,
        )

    _write_outputs(Path(args.out), result, config.query.near_miss_usd)
    if not args.dry_run:
        state.save(args.state)
    return _exit_code(result)


def _maybe_notifier(
    args: argparse.Namespace,
    config,
    env,
    http: httpx.AsyncClient,
) -> TelegramNotifier | None:
    telegram = (config.notify or {}).get("telegram") or {}
    enabled = bool(telegram.get("enabled", False))
    if args.no_notify or not enabled:
        return None
    token = env.get("TELEGRAM_BOT_TOKEN") or ""
    chat_id = env.get("TELEGRAM_CHAT_ID") or ""
    if token and chat_id:
        return TelegramNotifier(token, chat_id, http)
    log.warning("Telegram notify enabled but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")
    return None


def _parse_sources(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _write_outputs(out_dir: Path, result: RunResult, near_miss_usd: float) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    capped = [o for o in result.offers if o.total_usd <= near_miss_usd]
    keys = State()
    qualifying = []
    for offer in result.qualifying:
        row = offer.to_dict()
        row["also_seen"] = list(result.also_seen.get(keys.alert_key(offer), []))
        qualifying.append(row)
    payload = {
        "window_over": result.window_over,
        "offers": [o.to_dict() for o in capped],
        "qualifying": qualifying,
        "near_misses": [o.to_dict() for o in result.near_misses],
        "best_per_source": {k: v.to_dict() for k, v in result.best_per_source.items()},
        "source_status": result.source_status,
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat(),
        "dropped_count": result.dropped_count,
        "messages_sent": result.messages_sent,
        "new_alerts": [o.to_dict() for o in result.new_alerts],
    }
    (out_dir / "latest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    markdown = format_markdown(result)
    (out_dir / "latest.md").write_text(markdown, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(markdown)
            if not markdown.endswith("\n"):
                fh.write("\n")


def _exit_code(result: RunResult) -> int:
    if result.window_over:
        return 0
    attempted = [
        st
        for name, st in result.source_status.items()
        if name != "hops" and st.get("status") in ("ok", "error")
    ]
    if not attempted:
        return 0
    if any(st.get("status") == "ok" for st in attempted):
        return 0
    return 1


async def _cmd_test_telegram() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or ""
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required", file=sys.stderr)
        return 1
    async with httpx.AsyncClient(timeout=30.0) as http:
        await TelegramNotifier(token, chat_id, http).send("flightsearch test ✅")
    return 0


async def _cmd_telegram_chat_id() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    if not token:
        print("TELEGRAM_BOT_TOKEN is required", file=sys.stderr)
        return 1
    async with httpx.AsyncClient(timeout=30.0) as http:
        chats = await get_chat_ids(token, http)
    for chat in chats:
        name = chat.get("username") or chat.get("title") or ""
        print(f"{chat['id']}  {chat.get('type') or ''}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
