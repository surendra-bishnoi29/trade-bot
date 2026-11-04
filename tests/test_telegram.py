import asyncio
from datetime import UTC, datetime

from desk.agents.telegram import TelegramNotifier, render
from desk.bus.memory import InMemoryBus
from desk.core.clock import SimClock
from desk.core.events import Topics
from tests.conftest import make_event

NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)


def test_render_picks_the_events_worth_a_message():
    clock = SimClock(NOW)
    fill = make_event(
        clock,
        type=Topics.ORDER_FILLED,
        symbol="NEE",
        payload={
            "order_id": "o1",
            "intent_id": "i1",
            "side": "buy",
            "status": "filled",
            "filled_quantity": "23",
            "avg_price": "84.31",
        },
    )
    assert render(fill) == "✅ BUY 23 NEE filled at 84.31"
    quiet = make_event(
        clock,
        type=Topics.WATCHER_ALERT,
        symbol="PLD",
        payload={"symbol": "PLD", "kind": "stale_data", "severity": "info", "details": {}},
    )
    assert render(quiet) is None
    kill = make_event(
        clock,
        type=Topics.RISK_KILL_SWITCH,
        payload={"engaged": True, "reason": "feed stale", "source": "monitor"},
    )
    assert render(kill).startswith("🛑 Kill switch ENGAGED: feed stale")
    ok = make_event(
        clock, type=Topics.SYSTEM_HEALTH, payload={"component": "broker", "status": "ok"}
    )
    assert render(ok) is None
    plan = make_event(
        clock,
        type=Topics.CYCLE_PLAN,
        payload={
            "cycle_id": "c1",
            "trigger": "mid_session",
            "dry_run": False,
            "plan": {
                "market_view": "",
                "risk_note": "",
                "decisions": [
                    {
                        "symbol": "CVX",
                        "action": "buy",
                        "quantity": 8,
                        "thesis": "Pullback in an uptrend. More words here.",
                        "evidence_ids": [],
                        "expected_return_pct": 3,
                        "horizon_days": 10,
                        "stop_pct": 4,
                        "target_pct": 7,
                        "invalidation": [],
                        "conviction": 0.5,
                    }
                ],
            },
            "challenge": {
                "overall": "",
                "objections": [
                    {"symbol": "CVX", "severity": "concern", "argument": "Thin edge. Really."}
                ],
            },
        },
    )
    text = render(plan)
    assert "1 trades" in text and "BUY 8 CVX" in text and "Pullback in an uptrend." in text
    assert "Skeptic (concern on CVX): Thin edge." in text


async def test_notifier_sends_through_the_injected_sender_and_survives_failures():
    clock = SimClock(NOW)
    bus = InMemoryBus(clock)
    sent: list[str] = []
    calls = 0

    async def send(text: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("telegram down")
        sent.append(text)

    TelegramNotifier(bus, clock, "token", "chat", send=send).start()
    task = asyncio.create_task(bus.run())
    for reason in ("first", "second"):
        await bus.publish(
            make_event(
                clock,
                type=Topics.RISK_KILL_SWITCH,
                payload={"engaged": False, "reason": reason, "source": "console"},
            )
        )
    await bus.wait_idle()
    assert sent == ["🟢 Kill switch released: second"]
    await bus.stop()
    task.cancel()
