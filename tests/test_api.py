"""The api service against the test database: sessions, read models, command relay, live socket."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from desk_api.commands import Commands
from desk_api.live import LiveFeed, message_for
from desk_api.main import create_app
from desk_api.settings import ApiSettings
from desk_api.users import Users
from fastapi.testclient import TestClient

from desk.bus.memory import InMemoryBus
from desk.core.clock import SimClock
from desk.core.events import Topics
from desk.core.payloads import ThesisPayload
from desk.storage.db import Database
from tests.conftest import make_event

pytestmark = pytest.mark.usefixtures("database_url")

T0 = datetime(2026, 11, 3, 15, 13, tzinfo=UTC)


async def seed(db: Database, clock: SimClock) -> None:
    """One CVX trade with everything a trade card needs, plus a heartbeat and a snapshot."""
    cycle_id = "01CYCLETEST000000000000000"
    intent_id = "01INTENTTEST00000000000000"
    thesis_id = "01THESISTEST00000000000000"
    plan = make_event(
        clock,
        type=Topics.CYCLE_PLAN,
        payload={
            "cycle_id": cycle_id,
            "trigger": "cli",
            "dry_run": False,
            "plan": {
                "market_view": "Red tape, energy bid.",
                "risk_note": "Election night.",
                "decisions": [
                    {
                        "symbol": "CVX",
                        "action": "buy",
                        "quantity": 8,
                        "thesis": "Pullback in an intact uptrend with two catalysts.",
                        "evidence_ids": ["111489553"],
                        "expected_return_pct": 3.0,
                        "horizon_days": 10,
                        "stop_pct": 4.0,
                        "target_pct": 7.0,
                        "invalidation": ["Close below the 20-day average"],
                        "conviction": 0.5,
                    }
                ],
            },
            "challenge": {
                "overall": "Disciplined.",
                "objections": [
                    {"symbol": "CVX", "severity": "concern", "argument": "The edge is thin."}
                ],
            },
        },
    ).model_copy(update={"correlation_id": cycle_id})
    await db.insert_event(plan)
    await db.insert_event(
        make_event(
            clock,
            type=Topics.NEWS_ARTICLE_RAW,
            symbol="CVX",
            payload={
                "article_id": "111489553",
                "published_at": (T0 - timedelta(hours=1)).isoformat(),
                "source": "benzinga",
                "url": "https://example.com/evercore",
                "title": "Evercore ISI raises Chevron target to $250",
                "symbols": ["CVX"],
            },
        )
    )
    await db.insert_thesis(
        ThesisPayload(
            thesis_id=thesis_id,
            symbol="CVX",
            book="swing",
            direction="long",
            status="active",
            cycle_id=cycle_id,
            narrative="Pullback in an intact uptrend with two catalysts.",
            evidence_ids=["111489553"],
            expected_return_pct=3.0,
            horizon_days=10,
            conviction=0.5,
            quantity=Decimal(8),
            entry_price=Decimal("226.06"),
            stop_price=Decimal("217.01"),
            target_price=Decimal("241.88"),
            stop_pct=4.0,
            target_pct=7.0,
            invalidation=["Close below the 20-day average"],
            created_at=T0,
            updated_at=T0,
        )
    )
    intent = make_event(
        clock,
        type=Topics.ORDER_INTENT,
        symbol="CVX",
        payload={
            "intent_id": intent_id,
            "symbol": "CVX",
            "book": "swing",
            "side": "buy",
            "quantity": "8",
            "thesis_id": thesis_id,
            "rationale_event_ids": ["111489553"],
        },
    ).model_copy(update={"correlation_id": cycle_id})
    await db.insert_event(intent)
    await db.insert_event(
        make_event(
            clock,
            type=Topics.RISK_APPROVED,
            symbol="CVX",
            payload={
                "intent_id": intent_id,
                "intent": intent.payload,
                "decision": "approved",
                "approved_quantity": "8",
                "rules": [{"rule": "kill_switch", "passed": True, "detail": "off"}],
                "reason": "all rules passed",
            },
        )
    )
    await db.insert_event(
        make_event(
            clock,
            type=Topics.ORDER_FILLED,
            symbol="CVX",
            payload={
                "order_id": "o1",
                "intent_id": intent_id,
                "side": "buy",
                "status": "filled",
                "filled_quantity": "8",
                "avg_price": "226.06",
            },
        )
    )
    await db.insert_event(
        make_event(
            clock,
            type=Topics.PORTFOLIO_RECONCILED,
            payload={
                "equity": "100000",
                "cash": "98191",
                "buying_power": "390000",
                "positions": [
                    {"symbol": "CVX", "qty": "8", "avg_entry": "226.06", "market_value": "1794"}
                ],
                "as_of": T0.isoformat(),
            },
        )
    )
    await db.insert_event(
        make_event(
            clock,
            type=Topics.SYSTEM_HEARTBEAT,
            payload={
                "status": "ok",
                "kill_switch": False,
                "open_theses": 1,
                "next_cycle": "post_close at 21:10 UTC",
            },
        )
    )


@pytest.fixture
async def api(database_url):
    db = Database(database_url)
    await db.open()
    await db.migrate()
    async with db.pool.connection() as conn:
        await conn.execute("DELETE FROM theses WHERE thesis_id LIKE '01THESISTEST%'")
    clock = SimClock(T0)
    await seed(db, clock)
    users = Users(db.pool)
    await users.migrate()
    await users.add("ops", "operator", "operator-pass")
    await users.add("client", "viewer", "viewer-pass!")
    bus = InMemoryBus(clock)
    live = LiveFeed("redis://unused")
    app = create_app(
        ApiSettings(_env_file=None, database_url=database_url, web_dist="/nonexistent"),
        db=db,
        live=live,
        commands=Commands(bus, clock),
        run_live=False,
    )
    with TestClient(app) as client:
        yield client, bus, live
    await db.close()


def login(client: TestClient, name: str, password: str) -> None:
    r = client.post("/api/login", json={"name": name, "password": password})
    assert r.status_code == 200, r.text


def test_sessions_and_roles(api):
    client, _, _ = api
    assert client.get("/api/me").status_code == 401
    r = client.post("/api/login", json={"name": "ops", "password": "wrong"})
    assert r.status_code == 401
    login(client, "ops", "operator-pass")
    assert client.get("/api/me").json() == {"name": "ops", "role": "operator"}
    client.post("/api/logout")
    assert client.get("/api/me").status_code == 401


def test_state_trades_journal_and_cycles(api):
    client, _, _ = api
    login(client, "client", "viewer-pass!")
    state = client.get("/api/state").json()
    assert state["book"]["equity"] == 100000 and state["user"]["role"] == "viewer"
    cvx = next(p for p in state["book"]["positions"] if p["symbol"] == "CVX")
    assert cvx["qty"] == 8 and cvx["thesis"]["stop"] == 217.01 and cvx["thesis"]["target"] == 241.88
    assert state["health"]["bot"]["next_cycle"] == "post_close at 21:10 UTC"

    trades = client.get("/api/trades").json()
    card = next(t for t in trades if t["symbol"] == "CVX")
    assert card["what"] == "BUY 8 CVX at 226.06" and card["when"]["trigger"] == "cli"
    assert card["why"].startswith("Pullback") and card["exit_plan"]["stop"] == 217.01
    assert card["skeptic"]["severity"] == "concern" and card["risk"]["decision"] == "approved"
    assert (
        card["evidence"][0]["title"].startswith("Evercore")
        and card["evidence"][0]["kind"] == "news"
    )

    journal = client.get("/api/journal?limit=20").json()
    assert any(j["type"] == Topics.ORDER_FILLED and "BUY 8 CVX" in j["text"] for j in journal)
    cycles = client.get("/api/cycles").json()
    assert cycles[0]["trigger"] == "cli" and cycles[0]["decisions"][0]["symbol"] == "CVX"
    bars = client.get("/api/bars?symbol=CVX&interval=1d&n=5").json()
    assert isinstance(bars, list)


def test_commands_are_relayed_to_the_bus_with_roles(api):
    client, bus, _ = api
    login(client, "client", "viewer-pass!")
    r = client.post("/api/ask", json={"text": "why did we buy CVX?"})
    assert r.status_code == 200 and r.json()["request_id"]
    assert client.post("/api/commands", json={"command": "cycle"}).status_code == 403
    assert client.post("/api/commands", json={"command": "status"}).status_code == 200
    assert client.post("/api/commands", json={"command": "format-disk"}).status_code == 400
    login(client, "ops", "operator-pass")
    assert (
        client.post("/api/commands", json={"command": "kill", "args": "testing"}).status_code == 200
    )
    published = [e.payload for e in bus.log if e.type == Topics.DESK_COMMAND]
    assert [p["command"] for p in published] == ["ask", "status", "kill"]
    assert published[0]["role"] == "viewer" and published[2]["user"] == "ops"


def test_websocket_needs_a_session_and_relays_live_events(api):
    client, _, live = api
    with pytest.raises(Exception), client.websocket_connect("/ws") as ws:  # noqa: B017
        ws.receive_json()  # the server closes an unauthenticated socket
    login(client, "ops", "operator-pass")
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["user"]["name"] == "ops"
        event = make_event(
            SimClock(T0),
            type=Topics.CONSOLE_ANSWER_DELTA,
            payload={"request_id": "q1", "seq": 1, "text": "The book "},
        )
        live.broadcast(message_for(event))
        msg = ws.receive_json()
        assert msg["type"] == Topics.CONSOLE_ANSWER_DELTA and msg["payload"]["text"] == "The book "


async def test_users_table_round_trip(database_url):
    db = Database(database_url)
    await db.open()
    users = Users(db.pool)
    await users.migrate()
    await users.add("temp-user", "viewer", "a-long-password")
    assert await users.verify("temp-user", "a-long-password") is not None
    assert await users.verify("temp-user", "nope") is None
    assert await users.verify("nobody", "nope") is None
    assert await users.set_password("temp-user", "another-long-one")
    assert await users.verify("temp-user", "another-long-one") is not None
    assert await users.remove("temp-user")
    await db.close()
    await asyncio.sleep(0)
