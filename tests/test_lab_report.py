"""The lab's report: each call's story rebuilt from the journal, graded for real or on paper, and
totalled. No model, no database: a faked journal and hand-made minute bars."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from desk.core.events import Event, Topics
from desk.funnel.markets import markets
from desk.lab.report import findings, moved_after, report, simulate, stories, tally, tile_of

DAY = date(2026, 11, 6)  # a Friday: New York trades 14:30 to 21:00 UTC
T0 = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)
MARKETS = markets(["AAPL", "KO", "SPY"], {"stocks", "commodities", "crypto"})


def ev(type: str, when: datetime, payload: dict, symbol: str | None = None) -> Event:
    return Event(
        type=type,
        occurred_at=when,
        ingested_at=when,
        source="test@0",
        symbol=symbol,
        payload=payload,
    )


def bars(symbol: str, start: datetime, closes: list[float], spread: float = 0.05) -> list[dict]:
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        out.append(
            {
                "symbol": symbol,
                "ts": start + timedelta(minutes=i),
                "open": prev,
                "high": max(prev, c) + spread,
                "low": min(prev, c) - spread,
                "close": c,
                "volume": 100,
            }
        )
        prev = c
    return out


def call(cid, symbol, market, when, direction, confidence, acted, why="", price=100.0, **lines):
    c = {
        "direction": direction,
        "confidence": confidence,
        "horizon_minutes": 60,
        "max_loss_pct": 1.0,
        "first_profit_pct": 1.0,
        "step_pct": 0.5,
        "reason": "test reason",
        "supports": ["volume 3x its median"],
        "against": ["market mixed"],
        "decisive": "the break held",
        "invalidation": "back under 99.5",
        **lines,
    }
    return ev(
        Topics.PREDICTION_CALL,
        when,
        {
            "call_id": cid,
            "symbol": symbol,
            "market": market,
            "trigger": "surge: 40 to 60",
            "call": c,
            "price": price,
            "acted": acted,
            "skipped_because": why,
            "objection": "minor: one headline" if acted else "",
            "inputs": {"market": {"label": "x", "verdict": "MIXED", "score": 3}},
            "seconds": 4.2,
        },
        symbol,
    )


class FakeDB:
    def __init__(self, events: list[Event], bars_: dict[str, list[dict]]) -> None:
        self.events = events
        self.bars = bars_

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

    async def bars_between(self, symbols, interval, start, end):
        return [b for s in symbols for b in self.bars.get(s, []) if start <= b["ts"] < end]


def test_tiles_split_the_funds_into_oil_and_gold():
    assert tile_of("AAPL") == "equities" and tile_of("XOM") == "equities"
    assert tile_of("USO") == "oil" and tile_of("XLE") == "oil" and tile_of("UNG") == "oil"
    assert tile_of("GLD") == "gold" and tile_of("SLV") == "gold" and tile_of("CPER") == "gold"
    assert tile_of("BTC/USD") == "crypto"


def test_the_lines_run_on_paper_give_the_result_the_engine_would():
    c = {
        "direction": "long",
        "max_loss_pct": 1.0,
        "first_profit_pct": 1.0,
        "step_pct": 0.5,
        "horizon_minutes": 60,
    }
    # up through the first line (101) and one step (101.5), then back to the line: sold at 101.5
    up = bars("X", T0, [100.2, 100.6, 101.1, 101.6, 101.8, 101.4])
    r = simulate(c, "stocks", 100.0, T0, up)
    assert r["result"] == "right" and r["reason"] == "line" and r["exit"] == 101.5
    assert r["realized_pct"] == 1.4 and r["steps"] == 1  # 1.5% less 0.1% costs
    # straight down through the maximum loss line
    down = bars("X", T0, [99.8, 99.4, 98.9])
    r = simulate(c, "stocks", 100.0, T0, down)
    assert r["result"] == "wrong" and r["reason"] == "stop" and r["realized_pct"] == -1.1
    # a short is the mirror
    # a short is the mirror: the same fall reaches its first profit line (99) and waits there
    r = simulate({**c, "direction": "short"}, "stocks", 100.0, T0, down)
    assert r["result"] == "pending" and r["armed"] and r["mark_pct"] == 1.1
    # and is bought back when the price comes back up to the line
    r = simulate(
        {**c, "direction": "short"}, "stocks", 100.0, T0, bars("X", T0, [99.8, 99.4, 98.9, 99.2])
    )
    assert r["result"] == "right" and r["reason"] == "line" and r["realized_pct"] == 0.9
    # nothing decided before the bars end
    r = simulate(c, "stocks", 100.0, T0, bars("X", T0, [100.1, 100.2]))
    assert r["result"] == "pending" and r["mark_pct"] == 0.2
    # the horizon closes it at the market
    flat = bars("X", T0, [100.3] * 70)
    r = simulate(c, "stocks", 100.0, T0, flat)
    assert r["reason"] == "horizon" and r["result"] == "right" and r["realized_pct"] == 0.2
    # stocks are flat before the bell
    r = simulate(c, "stocks", 100.0, T0, flat, flat_at=T0 + timedelta(minutes=10))
    assert r["reason"] == "close"
    assert moved_after(100.0, T0, up)["up_pct"] == 1.85


async def test_stories_grades_every_call_and_the_report_counts_right_and_wrong():
    m = timedelta(minutes=1)
    events = [
        # 1: traded and won
        call("c1", "AAPL", "stocks", T0, "long", 0.72, True),
        ev(
            Topics.ORDER_INTENT,
            T0,
            {
                "intent_id": "i1",
                "call_id": "c1",
                "side": "buy",
                "quantity": "10",
                "stop_price": "99",
            },
            "AAPL",
        ),
        ev(
            Topics.RISK_APPROVED,
            T0,
            {
                "intent_id": "i1",
                "reason": "all rules passed",
                "rules": [{"rule": "x", "passed": True}],
            },
            "AAPL",
        ),
        ev(
            Topics.ORDER_FILLED,
            T0 + m,
            {"intent_id": "i1", "avg_price": "100.0", "filled_quantity": "10"},
            "AAPL",
        ),
        ev(
            Topics.LINES_UPDATED,
            T0 + m,
            {"call_id": "c1", "event": "opened", "base": 100, "stop": 99, "profit_line": 101},
            "AAPL",
        ),
        ev(
            Topics.LINES_UPDATED,
            T0 + 9 * m,
            {"call_id": "c1", "event": "exit", "base": 100, "stop": 100, "profit_line": 101},
            "AAPL",
        ),
        ev(
            Topics.PREDICTION_SCORE,
            T0 + 9 * m,
            {
                "call_id": "c1",
                "hit": True,
                "realized_pct": 0.9,
                "reason": "line",
                "entry": 100,
                "exit": 101,
            },
            "AAPL",
        ),
        # 2: traded and lost, a short on a fund
        call("c2", "GLD", "commodities", T0 + 2 * m, "short", 0.66, True),
        ev(
            Topics.ORDER_INTENT,
            T0 + 2 * m,
            {
                "intent_id": "i2",
                "call_id": "c2",
                "side": "sell",
                "quantity": "5",
                "stop_price": "101",
            },
            "GLD",
        ),
        ev(
            Topics.PREDICTION_SCORE,
            T0 + 20 * m,
            {"call_id": "c2", "hit": False, "realized_pct": -1.1, "reason": "stop"},
            "GLD",
        ),
        # 3: named a direction, held back by the bar; the price then ran: right on paper
        call(
            "c3",
            "KO",
            "stocks",
            T0 + 3 * m,
            "long",
            0.5,
            False,
            "confidence 0.50 below the bar 0.60",
        ),
        # 4: named a direction, held back; the price fell: wrong on paper
        call(
            "c4",
            "BTC/USD",
            "crypto",
            T0 + 4 * m,
            "long",
            0.45,
            False,
            "confidence 0.45 below the bar 0.60",
            first_profit_pct=1.6,
        ),
        # 5 and 6: passes, one of them followed by a move worth trading
        call("c5", "AAPL", "stocks", T0 + 30 * m, "none", 0.2, False, "the predictor sees no edge"),
        call(
            "c6", "ETH/USD", "crypto", T0 + 5 * m, "none", 0.15, False, "the predictor sees no edge"
        ),
        # 7: still open on its lines
        call("c7", "USO", "commodities", T0 + 6 * m, "long", 0.7, True),
        ev(
            Topics.ORDER_INTENT,
            T0 + 6 * m,
            {
                "intent_id": "i7",
                "call_id": "c7",
                "side": "buy",
                "quantity": "8",
                "stop_price": "99",
            },
            "USO",
        ),
        ev(
            Topics.ORDER_FILLED,
            T0 + 7 * m,
            {"intent_id": "i7", "avg_price": "100.0", "filled_quantity": "8"},
            "USO",
        ),
        ev(
            Topics.LINES_UPDATED,
            T0 + 7 * m,
            {
                "call_id": "c7",
                "event": "opened",
                "base": 100,
                "stop": 99,
                "profit_line": 101,
                "symbol": "USO",
            },
            "USO",
        ),
    ]
    db = FakeDB(
        events,
        {
            "KO": bars("KO", T0, [100.0] * 4 + [100.4, 101.2, 101.7, 101.2, 100.9]),
            "BTC/USD": bars("BTC/USD", T0, [100.0] * 5 + [99.6, 99.1, 98.7], spread=0.02),
            "AAPL": bars("AAPL", T0 + 30 * m, [100.1, 100.0, 100.1, 100.05] * 5),
            "ETH/USD": bars("ETH/USD", T0, [100.0] * 6 + [101.0, 102.1, 101.9], spread=0.02),
        },
    )
    now = T0 + timedelta(hours=2)
    rows = await stories(db, MARKETS, T0 - timedelta(hours=1), now, now=now)  # type: ignore[arg-type]
    by = {r["call_id"]: r for r in rows}
    assert rows[0]["call_id"] == "c5"  # newest first
    assert by["c1"]["outcome"] == "right" and by["c1"]["risk"]["decision"] == "approved"
    assert by["c1"]["fill"]["price"] == 100.0 and len(by["c1"]["lines"]) == 2
    assert by["c1"]["score"]["how"] == "came back to the profit line"
    assert by["c2"]["outcome"] == "wrong" and by["c2"]["tile"] == "gold"
    assert by["c3"]["outcome"] == "held_back" and by["c3"]["paper"]["result"] == "right"
    assert by["c4"]["paper"]["result"] == "wrong" and by["c4"]["paper"]["reason"] == "stop"
    assert by["c5"]["outcome"] == "passed" and by["c5"]["after"]["up_pct"] < 0.5
    assert by["c6"]["after"]["up_pct"] > 1.5  # a move the desk passed on
    assert by["c7"]["outcome"] == "open" and by["c7"]["tile"] == "oil"
    assert by["c1"]["call"]["supports"] == ["volume 3x its median"]

    t = tally(rows)
    x = t["total"]
    assert (x["calls"], x["passed"], x["directional"], x["traded"]) == (7, 2, 5, 3)
    assert (x["right"], x["wrong"], x["open"]) == (1, 1, 1)
    assert x["hit_rate"] == 0.5 and x["avg_pct"] == -0.1 and x["sum_pct"] == -0.2
    assert (x["paper_right"], x["paper_wrong"]) == (1, 1) and x["missed_moves"] == 1
    assert x["all_right"] == 2 and x["all_wrong"] == 2 and x["all_hit_rate"] == 0.5
    tiles = {k["key"]: k for k in t["tiles"]}
    assert tiles["equities"]["right"] == 1 and tiles["gold"]["wrong"] == 1
    assert tiles["oil"]["open"] == 1 and tiles["crypto"]["paper_wrong"] == 1
    assert t["directions"]["short"]["wrong"] == 1 and t["directions"]["long"]["right"] == 1
    assert {e["reason"]: e["n"] for e in t["exits"]} == {"line": 1, "stop": 1}
    low = next(b for b in t["confidence"] if b["lo"] == 0.4)
    assert (low["right"], low["wrong"], low["traded"]) == (1, 1, 0)

    words = " ".join(findings(t, final=True))
    assert "Right 1, wrong 1 (50% right)" in words and "graded on paper" in words
    assert "4 graded calls so far" in words and "The bar for judging the predictor is 60" in words

    r = await report(db, MARKETS, DAY, datetime(2026, 11, 7, 1, 0, tzinfo=UTC))  # type: ignore[arg-type]
    assert r["final"] and r["nyse_closed"] and r["tally"]["total"]["calls"] == 7
    early = await report(db, MARKETS, DAY, T0 + timedelta(minutes=12))  # type: ignore[arg-type]
    assert not early["final"] and not early["nyse_closed"]
    assert early["tally"]["total"]["calls"] == 6  # the 15:30 pass has not happened yet
    assert "still running" in early["findings"][-1]
    empty = await report(db, MARKETS, date(2026, 11, 3), now)  # type: ignore[arg-type]
    assert empty["findings"] == ["The predictor made no calls in this period."]


async def test_live_keeps_the_counts_and_the_flags_apart():
    from desk.lab.report import live

    events = [
        call("c7", "USO", "commodities", T0, "long", 0.7, True),
        ev(
            Topics.ORDER_INTENT,
            T0,
            {
                "intent_id": "i7",
                "call_id": "c7",
                "side": "buy",
                "quantity": "8",
                "stop_price": "99",
            },
            "USO",
        ),
        ev(
            Topics.ORDER_FILLED,
            T0,
            {"intent_id": "i7", "avg_price": "100.0", "filled_quantity": "8"},
            "USO",
        ),
        ev(
            Topics.LINES_UPDATED,
            T0,
            {
                "call_id": "c7",
                "event": "opened",
                "base": 100,
                "stop": 99,
                "profit_line": 101,
                "symbol": "USO",
                "direction": "long",
            },
            "USO",
        ),
        ev(
            Topics.SENTINEL_TRIGGER,
            T0,
            {"symbol": "USO", "reason": "surge", "detail": "40 to 60"},
            "USO",
        ),
    ]
    out = await live(FakeDB(events, {}), MARKETS, T0 + timedelta(minutes=5))  # type: ignore[arg-type]
    tiles = {t["key"]: t for t in out["tiles"]}
    oil = tiles["oil"]
    assert oil["traded"] == 1 and oil["open"] == 1  # counts: one call traded, one position open
    assert oil["is_traded"] is True and oil["is_open"] is True  # flags: the market, in session
    assert oil["on_lines"] == ["USO"] and oil["mood"]["verdict"] == "MIXED"
    assert tiles["crypto"]["is_open"] is True and tiles["crypto"]["shorts"] is False
    assert tiles["equities"]["instruments"] == 2  # AAPL and KO; SPY is the benchmark
    agents = {a["key"]: a for a in out["agents"]}
    assert agents["sentinel"]["count"] == 1 and "USO, surge" in agents["sentinel"]["line"]
    assert agents["predictor"]["count"] == 1 and "1 became orders" in agents["predictor"]["line"]
    assert agents["lines"]["state"] == "quiet"  # five minutes since its last move
    assert out["lines"][0]["symbol"] == "USO" and out["stories"][0]["outcome"] == "open"
    assert out["nyse_open"] is True and out["next_open"] is None
