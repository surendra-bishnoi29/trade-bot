"""desk: the command line. `desk run` starts the agents and the console; `desk ask` is one question."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import UTC
from logging.handlers import RotatingFileHandler
from pathlib import Path

import truststore
from dotenv import load_dotenv

from desk.core.config import load_settings


class _QuietStreamErrors(logging.Filter):
    """One line per websocket error, no traceback, at most once a minute per message."""

    def __init__(self) -> None:
        super().__init__()
        self._last: dict[str, float] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "connection limit exceeded" in msg:
            record.msg = (
                "Alpaca data stream refused: connection limit exceeded. The free tier allows one live connection per "
                "account; another desk process or script is probably still connected. Retrying."
            )
            record.args = ()
        record.exc_info = None
        now = time.monotonic()
        last = self._last.get(record.msg)
        self._last[record.msg] = now
        return last is None or now - last > 60


def _logging(verbose: bool, *, console: bool = False) -> None:
    """With the console, full logs go to logs/desk.log and only warnings reach the terminal."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(fmt)
    stderr.setLevel(logging.WARNING if console else (logging.DEBUG if verbose else logging.INFO))
    root.addHandler(stderr)
    if console:
        Path("logs").mkdir(exist_ok=True)
        file_handler = RotatingFileHandler("logs/desk.log", maxBytes=20_000_000, backupCount=5)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    for noisy in ("httpx", "httpx2", "httpcore", "websockets", "alpaca", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("alpaca.data.live.websocket").addFilter(_QuietStreamErrors())


async def _run(no_console: bool) -> None:
    from desk.app import Desk

    desk = await Desk.build(load_settings(), live=True)
    await desk.start()
    try:
        if no_console:
            await asyncio.Event().wait()
        else:
            await desk.console.repl()
    finally:
        await desk.stop()


async def _ask(question: str) -> None:
    from desk.app import Desk

    desk = await Desk.build(load_settings(), live=False)
    await desk.start()
    try:
        print(await desk.console.ask(question))
    finally:
        await desk.stop()


async def _cycle(dry_run: bool) -> None:
    from desk.app import Desk

    desk = await Desk.build(load_settings(), live=False)
    await desk.start()
    try:
        result = await desk.cycle.run("cli", dry_run=dry_run)
        print(result.render())
        print(f"\ntokens: {result.usage}")
        if not dry_run:
            await asyncio.sleep(5)  # let the risk officer and the execution trader report
            print(await desk.console.commands["theses"](""))
    finally:
        await desk.stop()


async def _theses() -> None:
    from desk.app import Desk

    desk = await Desk.build(load_settings(), live=False)
    await desk.start()
    try:
        print(await desk.console.commands["theses"](""))
    finally:
        await desk.stop()


async def _backfill(daily_sessions: int | None = None) -> None:
    from desk.agents.data_agent import DataAgent
    from desk.app import Desk

    settings = load_settings()
    desk = await Desk.build(settings, live=False)
    await desk.start()
    try:
        key = settings.alpaca_api_key.get_secret_value()  # type: ignore[union-attr]
        secret = settings.alpaca_secret_key.get_secret_value()  # type: ignore[union-attr]
        from desk.data.alpaca import AlpacaMarketData

        agent = DataAgent(
            desk.bus,
            desk.clock,
            desk.db,
            AlpacaMarketData(key, secret),
            list(settings.core_universe),
        )
        if daily_sessions:
            agent.daily_sessions = daily_sessions
        print(await agent.backfill())
        print(await desk.db.bar_counts())
    finally:
        await desk.stop()


async def _replay(args: argparse.Namespace) -> None:
    import json
    from datetime import date, datetime, timedelta
    from decimal import Decimal

    from desk.replay.assemble import run_replay

    day = date.fromisoformat(args.day)
    gap = None
    if args.feed_gap:
        a, b = args.feed_gap.split("-")
        start = datetime.combine(day, datetime.strptime(a, "%H:%M").time(), tzinfo=UTC)
        end = datetime.combine(day, datetime.strptime(b, "%H:%M").time(), tzinfo=UTC)
        gap = (start, end if end > start else end + timedelta(days=1))
    report = await run_replay(
        load_settings(),
        day,
        llm=args.llm,
        capital=Decimal(args.capital),
        slippage_bps=Decimal(args.slippage_bps),
        feed_gap=gap,
    )
    print(report.render())
    if args.json:
        print(json.dumps(report.as_dict(), default=str))


async def _evaluate(days: int) -> None:
    from datetime import UTC, datetime

    from desk.research.evaluate import evaluate, render
    from desk.storage.db import Database

    settings = load_settings()
    db = Database(settings.database_url)
    await db.open()
    try:
        print(render(await evaluate(db, datetime.now(UTC), days=days)))
    finally:
        await db.close()


async def _train(horizon: int | None, days: int, *, store: bool = True) -> None:
    from datetime import UTC, datetime

    from desk.forecast.train import train_pipeline
    from desk.storage.db import Database

    settings = load_settings()
    db = Database(settings.database_url)
    await db.open()
    try:
        result = await train_pipeline(
            db,
            datetime.now(UTC),
            list(settings.core_universe),
            horizon=horizon or settings.forecast_horizon,
            days=days,
            models_dir=settings.models_dir,
            store_features=store,
        )
        print(result["report"])
    finally:
        await db.close()


async def _features(action: str, days: int) -> None:
    from datetime import UTC, datetime

    from desk.features.audit import audit, render
    from desk.features.daily import build_frame
    from desk.forecast.train import frame_rows, load_bars
    from desk.storage.db import Database

    settings = load_settings()
    db = Database(settings.database_url)
    await db.open()
    try:
        now = datetime.now(UTC)
        bars = await load_bars(db, list(settings.core_universe), days, as_of=now)
        frame = build_frame(bars)
        if action == "audit":
            print(render(audit(bars, frame)))
        else:
            n = await db.upsert_features(frame_rows(frame, now))
            print(f"feature store: {n} rows over {frame['session'].nunique()} sessions")
    finally:
        await db.close()


async def _models() -> None:
    from desk.storage.db import Database

    settings = load_settings()
    db = Database(settings.database_url)
    await db.open()
    try:
        rows = await db.models("swing")
        if not rows:
            print("no models yet: run `desk train`")
        for r in rows:
            p = (r["metrics"] or {}).get("pooled") or {}
            ic = p.get("ic")
            print(
                f"{r['model_id']:<32} {r['status']:<9} horizon {r['horizon']}d through {r['trained_through']} "
                f"ic {ic:+.3f} hit {p.get('hit', 0):.0%} n {p.get('n', 0)}  {r['note']}"
                if ic is not None
                else f"{r['model_id']:<32} {r['status']:<9} horizon {r['horizon']}d through {r['trained_through']}  {r['note']}"
            )
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> int:
    truststore.inject_into_ssl()  # websockets and urllib use the system trust store, like the browser does
    load_dotenv()
    parser = argparse.ArgumentParser(prog="desk", description="the autonomous trading desk")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="start every agent and the console")
    run.add_argument("--no-console", action="store_true", help="run headless")
    ask = sub.add_parser("ask", help="one question to the head trader, then exit")
    ask.add_argument("question")
    backfill = sub.add_parser("backfill", help="load price history for the universe")
    backfill.add_argument(
        "--daily-sessions", type=int, help="how many sessions of daily bars to hold (default 250)"
    )
    cycle = sub.add_parser("cycle", help="run one swing cycle now")
    cycle.add_argument(
        "--dry-run", action="store_true", help="plan and challenge, but place no orders"
    )
    sub.add_parser("theses", help="list recent theses")
    evaluate = sub.add_parser(
        "evaluate", help="score the analysts and the head trader against what prices did"
    )
    evaluate.add_argument("--days", type=int, default=30)
    sub.add_parser("prompts", help="list the prompt files and their versions")
    train = sub.add_parser(
        "train", help="build features, audit for lookahead, walk-forward a forecaster, register it"
    )
    train.add_argument(
        "--horizon", type=int, help="sessions ahead (1, 5 or 20; default DESK_FORECAST_HORIZON)"
    )
    train.add_argument("--days", type=int, default=400, help="daily bars of history to use")
    train.add_argument("--no-store", action="store_true", help="do not write the feature store")
    features = sub.add_parser("features", help="the point-in-time feature store")
    features.add_argument("action", choices=["build", "audit"])
    features.add_argument("--days", type=int, default=400)
    sub.add_parser("models", help="list trained forecasters and the champion")
    replay = sub.add_parser(
        "replay", help="replay one past session through the agents with a simulated broker"
    )
    replay.add_argument("--day", required=True, help="session date, YYYY-MM-DD")
    replay.add_argument("--llm", action="store_true", help="use the real head trader (cached)")
    replay.add_argument("--capital", default="100000")
    replay.add_argument("--slippage-bps", default="5")
    replay.add_argument("--feed-gap", help="withhold bars between HH:MM-HH:MM UTC")
    replay.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    _logging(args.verbose, console=(args.command == "run" and not args.no_console))
    if args.command == "run":
        asyncio.run(_run(args.no_console))
    elif args.command == "ask":
        asyncio.run(_ask(args.question))
    elif args.command == "backfill":
        asyncio.run(_backfill(args.daily_sessions))
    elif args.command == "cycle":
        asyncio.run(_cycle(args.dry_run))
    elif args.command == "theses":
        asyncio.run(_theses())
    elif args.command == "replay":
        asyncio.run(_replay(args))
    elif args.command == "evaluate":
        asyncio.run(_evaluate(args.days))
    elif args.command == "prompts":
        from desk.core.prompts import render_registry

        print(render_registry())
    elif args.command == "train":
        asyncio.run(_train(args.horizon, args.days, store=not args.no_store))
    elif args.command == "features":
        asyncio.run(_features(args.action, args.days))
    elif args.command == "models":
        asyncio.run(_models())
    return 0


if __name__ == "__main__":
    sys.exit(main())
