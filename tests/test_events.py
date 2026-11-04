from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from ulid import ULID

from desk.core.events import Event, Topics, parse_payload, validate_payload
from desk.core.payloads import MarketBar

T = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)


def make(**kw) -> Event:
    base = {
        "type": Topics.MARKET_BAR_1M,
        "occurred_at": T,
        "ingested_at": T,
        "source": "test@0.1",
        "payload": {},
    }
    base.update(kw)
    return Event(**base)


def test_event_ids_are_ulids_and_json_round_trips():
    e = make(payload={"a": 1})
    assert ULID.from_str(e.event_id)
    assert Event.from_json(e.to_json()) == e


def test_naive_datetime_is_rejected():
    with pytest.raises(ValidationError):
        make(occurred_at=datetime(2026, 11, 3, 14, 30))


def test_non_utc_timestamps_are_normalized_to_utc():
    eastern = timezone(timedelta(hours=-5))
    e = make(occurred_at=datetime(2026, 11, 3, 9, 30, tzinfo=eastern))
    assert e.occurred_at == T and e.occurred_at.tzinfo == UTC


def test_bad_topic_and_bad_ulid_are_rejected():
    with pytest.raises(ValidationError):
        make(type="MarketBar")
    with pytest.raises(ValidationError):
        make(event_id="not-a-ulid")


def test_events_are_immutable():
    e = make()
    with pytest.raises(ValidationError):
        e.symbol = "AAPL"  # type: ignore[misc]


def test_registered_payload_is_validated_and_json_safe():
    bar = {
        "symbol": "AAPL",
        "interval": "1m",
        "ts": T.isoformat(),
        "open": "268.40",
        "high": "268.90",
        "low": "268.10",
        "close": "268.75",
        "volume": 12000,
        "source": "alpaca",
    }
    data = validate_payload(Topics.MARKET_BAR_1M, bar)
    assert data["close"] == "268.75" and isinstance(data["ts"], str)
    parsed = parse_payload(make(payload=data))
    assert isinstance(parsed, MarketBar)
    assert parsed.close == Decimal("268.75")
    assert parsed.closes_at == T + timedelta(minutes=1)


def test_invalid_payload_is_rejected():
    with pytest.raises(ValidationError):
        validate_payload(Topics.MARKET_BAR_1M, {"symbol": "AAPL"})


def test_unregistered_topic_passes_through():
    assert validate_payload("custom.topic", {"x": 1}) == {"x": 1}


def test_every_topic_name_is_well_formed():
    assert len(Topics.all()) > 20
    for t in Topics.all():
        make(type=t)
