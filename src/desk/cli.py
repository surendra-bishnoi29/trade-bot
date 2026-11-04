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


async def _backfill() -> None:
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
    sub.add_parser("backfill", help="load price history for the universe")
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
        asyncio.run(_backfill())
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
