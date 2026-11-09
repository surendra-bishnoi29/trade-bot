"""The funnel engine: a day's shape from bars, the score and its evidence, the book routing, and
one full build on a faked journal, as known at a moment."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from desk.core.events import Event, Topics
from desk.core.payloads import ThesisPayload
from desk.funnel.build import (
    breadth,
    build_funnel,
    day_shape,
    describe,
    digest,
    levels,
    route_book,
    score,
    setup_tag,
    symbol_detail,
)
from desk.funnel.markets import Market, markets, session_window

DAY = datetime(2026, 11, 5, tzinfo=UTC)
OPEN = datetime(2026, 11, 5, 14, 30, tzinfo=UTC)
CLOSE = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)
MARKET = Market("stocks", "US stocks", ("UP", "DOWN", "HELD"), "SPY", "nyse", True)


def minute(symbol: str, start: float, end: float, n: int = 120) -> list[dict]:
    rows = []
    for i in range(n):
        c = start + (end - start) * (i + 1) / n
        o = start + (end - start) * i / n
        rows.append(
            {
                "symbol": symbol,
                "ts": OPEN + timedelta(minutes=i),
                "open": Decimal(str(o)),
                "high": Decimal(str(max(o, c) + 0.05)),
                "low": Decimal(str(min(o, c) - 0.05)),
                "close": Decimal(str(c)),
                "volume": 1000 + i,
            }
        )
    return rows


def daily(symbol: str, close: float, drift: float = 0.0, n: int = 25) -> list[dict]:
    rows = []
    for i in range(n):
        c = close - drift * (n - 1 - i)
        rows.append(
            {
                "symbol": symbol,
                "ts": DAY - timedelta(days=n - i) + timedelta(hours=5),
                "open": Decimal(str(c)),
                "high": Decimal(str(c + 1)),
                "low": Decimal(str(c - 1)),
                "close": Decimal(str(c)),
                "volume": 1_000_000,
            }
        )
    return rows


def ev(
    type: str, when: datetime, payload: dict, symbol: str | None = None, source: str = "test@0.1"
) -> Event:
    return Event(
        type=type, occurred_at=when, ingested_at=when, source=source, symbol=symbol, payload=payload
    )


class FakeDB:
    def __init__(self) -> None:
        self.minute = (
            minute("UP", 101, 104)
            + minute("DOWN", 99, 95)
            + minute("HELD", 50, 49)
            + minute("SPY", 700, 693)
        )
        self.daily = (
            daily("UP", 100, drift=0.4)
            + daily("DOWN", 100, drift=-0.3)
            + daily("HELD", 50)
            + daily("SPY", 700)
        )
        t = OPEN + timedelta(minutes=30)
        self.events = [
            ev(
                Topics.WATCHER_ALERT,
                t,
                {
                    "symbol": "UP",
                    "kind": "volume_spike",
                    "severity": "warning",
                    "details": {"volume": 9000, "multiple": "7.5", "median": "1200"},
                },
                "UP",
                "watcher@0.1",
            ),
            ev(
                Topics.WATCHER_ALERT,
                t,
                {
                    "symbol": "DOWN",
                    "kind": "abnormal_move",
                    "severity": "warning",
                    "details": {"bars": 5, "close": "97", "pct_change": "-1.5"},
                },
                "DOWN",
                "watcher@0.1",
            ),
            ev(
                Topics.NEWS_STATE,
                t,
                {"symbol": "UP", "tone_48h": 0.4, "count_24h": 3},
                "UP",
                "news_analyst@0.1",
            ),
            ev(
                Topics.NEWS_STATE,
                t,
                {"symbol": "DOWN", "tone_48h": -0.3, "count_24h": 2},
                "DOWN",
                "news_analyst@0.1",
            ),
            ev(
                Topics.CYCLE_PLAN,
                t + timedelta(minutes=5),
                {
                    "trigger": "mid_session",
                    "dry_run": False,
                    "plan": {
                        "decisions": [
                            {
                                "symbol": "UP",
                                "action": "buy",
                                "conviction": 0.6,
                                "thesis": "Breakout on news. More to come.",
                            },
                            {
                                "symbol": "DOWN",
                                "action": "buy",
                                "conviction": 0.5,
                                "thesis": "Dip buy.",
                            },
                        ]
                    },
                    "challenge": {
                        "objections": [
                            {
                                "symbol": "DOWN",
                                "severity": "blocking",
                                "argument": "Falling with the market.",
                            }
                        ]
                    },
                },
                None,
                "cycle@0.1",
            ),
            ev(
                Topics.RISK_APPROVED,
                t + timedelta(minutes=5),
                {
                    "intent_id": "i1",
                    "reason": "all rules passed",
                    "rules": [{"rule": "a", "passed": True}, {"rule": "b", "passed": True}],
                },
                "UP",
                "risk@0.2",
            ),
            ev(
                Topics.ORDER_INTENT,
                t + timedelta(minutes=5),
                {"intent_id": "i1", "side": "buy"},
                "UP",
                "cycle@0.1",
            ),
            ev(
                Topics.ORDER_FILLED,
                t + timedelta(minutes=6),
                {
                    "intent_id": "i1",
                    "order_id": "i1",
                    "status": "filled",
                    "filled_quantity": "5",
                    "avg_price": "102.2",
                },
                "UP",
                "execution@0.1",
            ),
            ev(
                Topics.PORTFOLIO_RECONCILED,
                t + timedelta(minutes=10),
                {
                    "equity": "100000",
                    "positions": [{"symbol": "HELD", "qty": "10", "avg_entry": "51"}],
                },
                None,
                "back_office@0.1",
            ),
        ]
        self.theses = [
            ThesisPayload(
                thesis_id="t1",
                symbol="HELD",
                book="swing",
                direction="long",
                status="active",
                cycle_id="c",
                narrative="Guidance raised. Plays out over weeks.",
                evidence_ids=[],
                expected_return_pct=3.0,
                horizon_days=10,
                conviction=0.55,
                quantity=Decimal(10),
                entry_price=Decimal(51),
                stop_price=Decimal("48.5"),
                target_price=Decimal(55),
                stop_pct=5.0,
                target_pct=8.0,
                created_at=OPEN - timedelta(days=1),
                updated_at=OPEN - timedelta(days=1),
            )
        ]

    async def bars_between(self, symbols, interval, start, end):
        rows = self.minute if interval == "1m" else self.daily
        return sorted(
            (r for r in rows if r["symbol"] in symbols and start <= r["ts"] < end),
            key=lambda r: r["ts"],
        )

    async def recent_events(self, types, since, *, symbol=None, limit=50, until=None):
        rows = [
            e
            for e in self.events
            if e.type in types
            and e.occurred_at >= since
            and (until is None or e.occurred_at <= until)
            and (symbol is None or e.symbol == symbol)
        ]
        return sorted(rows, key=lambda e: e.occurred_at, reverse=True)[:limit]

    async def recent_theses(self, limit=50):
        return self.theses


def test_day_shape_score_and_routing():
    grid = [OPEN - timedelta(minutes=5)] + [OPEN + timedelta(minutes=5 * i) for i in range(25)]
    sh = day_shape(minute("UP", 101, 104), daily("UP", 100, drift=0.4), grid)
    assert sh and sh["prior_close"] == 100 and sh["last"] == pytest.approx(104)
    assert sh["day_pct"] == pytest.approx(4.0) and sh["path"][-1] == pytest.approx(4.0, abs=0.01)
    assert sh["path"][0] is None and len(sh["profile"]) == 12 and max(sh["profile"]) == 1.0
    assert sh["vs_avg20_pct"] > 0 and sh["session_high"] > sh["session_low"]
    assert day_shape([], [], grid) is None
    sc, evidence = score(
        sh, {"volume_spike": 4}, {"tone_48h": 0.4, "count_24h": 3}, 1.0, "S&P 500 ETF"
    )
    assert sc > 60 and [e["source"] for e in evidence][:2] == ["Price feed", "Quant analyst"]
    assert all(e["verdict"] == "bullish" for e in evidence)
    weak, _ = score(sh, {}, None, -3.0, "S&P 500 ETF")
    assert weak < sc
    assert setup_tag(sh, "bull", {}) == "NEW 20-DAY HIGH"
    assert (
        setup_tag(sh, "bull", {"held": True}) == "HELD"
        and setup_tag(sh, "bull", {"blocked": True}) == "BLOCKED BY SKEPTIC"
    )
    assert setup_tag(sh, "bull", {"filled_today": "buy"}) == "BOUGHT TODAY"
    book = route_book(sh, {"volume_spike": 1}, {"tone_48h": 0.4, "count_24h": 3}, {}, MARKET)
    assert book["tag"] == "SWING CANDIDATE" and "multi-day trend" in book["reasons"][0]
    flat = day_shape(minute("X", 100, 102.5), daily("X", 100), grid)
    burst = route_book(flat, {"volume_spike": 5}, None, {}, MARKET)
    assert burst["tag"] == "INTRADAY CANDIDATE" and "intraday book is off" in burst["reasons"][1]
    quiet = day_shape(minute("X", 100, 100.1), daily("X", 100), grid)
    assert route_book(quiet, {}, None, {}, MARKET)["tag"] == "WATCH"
    held = route_book(
        sh,
        {},
        None,
        {
            "thesis": {
                "narrative": "Raised guidance. More.",
                "horizon_days": 10,
                "stop": 95,
                "target": 110,
            }
        },
        MARKET,
    )
    assert held["tag"] == "SWING" and "Raised guidance." in held["reasons"][0]
    watch_only = Market(
        "crypto", "Crypto", ("BTC/USD",), "BTC/USD", "utc_day", False, "Watch only."
    )
    assert route_book(sh, {}, None, {}, watch_only) == {
        "tag": "WATCH ONLY",
        "reasons": ["Watch only."],
    }
    lv = levels(sh, "bull", {"thesis": {"entry": 101, "stop": 98, "target": 110}})
    assert lv["stop"] == 98 and lv["rr"] == pytest.approx(1.0) and lv["zone"][0] <= lv["zone"][1]
    assert (
        breadth([2.0, -1.0, 0.0, -5.0])["bear"] == 2
        and sum(b["n"] for b in breadth([2.0, -1.0, 0.0, -5.0])["bins"]) == 4
    )


async def test_build_funnel_reads_the_journal_as_of_a_moment():
    db = FakeDB()
    at = OPEN + timedelta(minutes=119)
    f = await build_funnel(db, MARKET, CLOSE + timedelta(hours=3), at=at)  # type: ignore[arg-type]
    assert (
        f["session"]["day"] == "2026-11-05"
        and not f["session"]["is_live"]
        and f["market"] == "stocks"
    )
    assert f["times"][0] == (OPEN - timedelta(minutes=5)).isoformat()
    by = {s["symbol"]: s for s in f["symbols"]}
    assert set(by) == {"UP", "DOWN", "HELD"} and "SPY" not in by
    up, down, held = by["UP"], by["DOWN"], by["HELD"]
    assert up["side"] == "bull" and up["rank"] == 1 and up["path"][0] == 0.0
    assert up["checks"] == {
        "watcher": True,
        "quant": True,
        "news": True,
        "head": True,
        "risk": True,
    }
    assert (
        up["setup"] == "BOUGHT TODAY"
        and up["desk"]["fill"]["side"] == "buy"
        and up["desk"]["risk"]["passed"] == 2
    )
    assert (
        down["side"] == "bear"
        and down["setup"] == "BLOCKED BY SKEPTIC"
        and not down["checks"]["risk"]
    )
    assert down["desk"]["decision"]["objection"]["severity"] == "blocking" and down["strength"] > 0
    assert (
        held["held"]
        and held["book"]["tag"] == "SWING"
        and held["levels"]["stop"] == 48.5
        and held["side"] == "bear"
    )
    assert held["checks"]["head"] and held["checks"]["risk"]
    assert f["breadth"]["bull"] == 1 and f["breadth"]["bear"] == 2
    assert f["sentiment"]["verdict"] in ("BEARISH", "MIXED") and f["benchmark"][
        "day_pct"
    ] == pytest.approx(-1.0, abs=0.05)
    assert f["wire"] and f["wire"][0]["agent"] in (
        "Execution",
        "Risk officer",
        "Head trader",
        "Watcher",
    )
    text = digest(f)
    assert "Market sentiment" in text and "Strongest 1: UP" in text and "Weakest" in text
    # earlier in the session nothing had been decided yet: the replay never sees the future
    early = await build_funnel(db, MARKET, CLOSE, at=OPEN + timedelta(minutes=20))  # type: ignore[arg-type]
    e_up = next(s for s in early["symbols"] if s["symbol"] == "UP")
    assert not e_up["checks"]["head"] and e_up["setup"] != "BOUGHT TODAY" and early["wire"] == []
    detail = await symbol_detail(db, MARKET, "UP", CLOSE, at=at)  # type: ignore[arg-type]
    assert len(detail["candles"]) == 24 and detail["candles"][0]["o"] == pytest.approx(101)
    assert any("volume spike" in t["text"] for t in detail["trail"]) and detail["trail"][0]["agent"]


def test_markets_and_describe():
    ms = markets(["AAPL", "SPY", "QQQ", "V"])
    assert ms["stocks"].symbols == ("AAPL", "V") and ms["stocks"].all_symbols == (
        "AAPL",
        "V",
        "SPY",
    )
    assert (
        not ms["crypto"].traded
        and "BTC/USD" in ms["crypto"].all_symbols
        and "GLD" in ms["commodities"].symbols
    )
    day, start, end = session_window(ms["crypto"], datetime(2026, 11, 6, 7, 0, tzinfo=UTC))
    assert start == datetime(2026, 11, 6, tzinfo=UTC) and end - start == timedelta(days=1)
    day, start, end = session_window(
        ms["stocks"], datetime(2026, 11, 6, 7, 0, tzinfo=UTC)
    )  # before the open
    assert day.isoformat() == "2026-11-05" and start == OPEN
    e = ev(
        Topics.ORDER_FILLED,
        OPEN,
        {"side": "sell", "filled_quantity": "3", "avg_price": "10.5"},
        "KO",
    )
    assert describe(e) == "Sold 3 KO at 10.5"
    plan = ev(
        Topics.CYCLE_PLAN,
        OPEN,
        {"trigger": "pre_market", "plan": {"decisions": [{"symbol": "V", "action": "hold"}]}},
    )
    assert describe(plan) == "Review (pre_market): no action"


async def test_on_the_lab_desk_the_funnel_speaks_of_the_predictor_and_the_lines():
    lab = Market("stocks", "US stocks", ("UP", "DOWN", "HELD"), "SPY", "nyse", True, lab=True)
    db = FakeDB()
    db.events = [e for e in db.events if e.type == Topics.WATCHER_ALERT]  # no cycles on the lab
    db.open = []  # no theses either: the lab holds positions on lines
    at = OPEN + timedelta(minutes=119)

    def call(symbol: str, minutes: int, acted: bool, why: str = "") -> Event:
        return ev(
            Topics.PREDICTION_CALL,
            OPEN + timedelta(minutes=minutes),
            {
                "call_id": f"c-{symbol}",
                "symbol": symbol,
                "market": "stocks",
                "trigger": "surge: 40 to 60",
                "call": {
                    "direction": "long" if acted else "none",
                    "confidence": 0.71 if acted else 0.2,
                    "horizon_minutes": 120,
                    "max_loss_pct": 0.6,
                    "first_profit_pct": 0.9,
                    "step_pct": 0.4,
                    "reason": "Volume confirmed the break. More detail follows.",
                },
                "price": 102.0,
                "acted": acted,
                "skipped_because": why,
            },
            symbol,
            "predictor@0.1",
        )

    db.events += [
        call("UP", 40, True),
        call("DOWN", 50, False, "the predictor sees no edge"),
        ev(
            Topics.LINES_UPDATED,
            OPEN + timedelta(minutes=60),
            {
                "symbol": "HELD",
                "market": "stocks",
                "call_id": "c-HELD",
                "direction": "short",
                "base": 49.8,
                "stop": 50.1,
                "profit_line": 49.3,
                "step_pct": 0.4,
                "armed": False,
                "steps": 0,
                "event": "opened",
                "last": 49.8,
                "quantity": 20.0,
            },
            "HELD",
            "lines@0.1",
        ),
    ]
    f = await build_funnel(db, lab, CLOSE + timedelta(hours=3), at=at)  # type: ignore[arg-type]
    rows = {r["symbol"]: r for r in f["symbols"]}
    assert f["lab"] is True
    up, down, held = rows["UP"], rows["DOWN"], rows["HELD"]
    assert (
        up["book"]["tag"] == "CALLED"
        and "predictor called it long at 15:10" in up["book"]["reasons"][0]
    )
    assert "Volume confirmed the break." in up["book"]["reasons"][0]
    assert up["checks"]["head"] is True  # the predictor stands in the head trader's place
    assert down["book"]["tag"] == "PASSED" and "no edge" in down["book"]["reasons"][0]
    assert down["checks"]["head"] is False
    assert held["book"]["tag"] == "LINES" and held["held"] and held["checks"]["head"]
    assert "waiting for the first profit line" in held["book"]["reasons"][0]
    words = " ".join(" ".join(r["book"]["reasons"]) + r["book"]["tag"] for r in f["symbols"])
    assert "swing" not in words.lower() and "intraday" not in words.lower()
    text = digest(f)
    assert "watcher/quant/news/predictor/risk" in text and "state CALLED" in text
    # the main desk's funnel on the same journal still speaks of books
    main = await build_funnel(db, MARKET, CLOSE + timedelta(hours=3), at=at)  # type: ignore[arg-type]
    assert main["lab"] is False and "watcher/quant/news/head/risk" in digest(main)
