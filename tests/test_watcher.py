from datetime import UTC, datetime, timedelta
from decimal import Decimal

from desk.agents.watcher import WatcherConfig, WatcherState
from desk.core.payloads import MarketBar

T0 = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)


def bar(i: int, close: str, volume: int = 1000) -> MarketBar:
    c = Decimal(close)
    return MarketBar(
        symbol="AAPL",
        interval="1m",
        ts=T0 + timedelta(minutes=i),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        source="test",
    )


def test_abnormal_move_fires_on_a_one_percent_move_over_five_bars():
    state = WatcherState(WatcherConfig())
    alerts = []
    for i in range(6):
        alerts += state.on_bar(bar(i, "100.00"), T0 + timedelta(minutes=i))
    assert alerts == []
    alerts = state.on_bar(bar(6, "101.20"), T0 + timedelta(minutes=6))
    assert [a.kind for a in alerts] == ["abnormal_move"] and alerts[0].severity == "warning"
    assert alerts[0].details["pct_change"].startswith("1.2")


def test_volume_spike_against_the_trailing_median():
    state = WatcherState(WatcherConfig())
    for i in range(6):
        state.on_bar(bar(i, "100.00", volume=1000), T0 + timedelta(minutes=i))
    alerts = state.on_bar(bar(6, "100.00", volume=6000), T0 + timedelta(minutes=6))
    assert [a.kind for a in alerts] == ["volume_spike"] and alerts[0].details["multiple"] == "6.0"


def test_stale_data_alert_only_when_open_and_only_once():
    state = WatcherState(WatcherConfig(stale_after=timedelta(minutes=3)))
    assert (
        state.stale_checks(["AAPL", "MSFT"], T0, market_open=True) == []
    )  # the watcher just started
    state.on_bar(bar(0, "100.00"), T0)
    assert state.stale_checks(["AAPL", "MSFT"], T0 + timedelta(minutes=1), market_open=True) == []
    late = T0 + timedelta(minutes=5)
    first = [
        a
        for a in state.stale_checks(["AAPL", "MSFT"], late, market_open=True)
        if a.kind == "stale_data"
    ]
    assert sorted(a.symbol for a in first) == ["AAPL", "MSFT"] and all(
        a.severity == "info" for a in first
    )
    again = state.stale_checks(["AAPL", "MSFT"], late + timedelta(minutes=1), market_open=True)
    assert [a for a in again if a.kind == "stale_data"] == []
    assert state.stale_checks(["AAPL"], late, market_open=False) == []
    state.on_bar(bar(9, "100.00"), late + timedelta(minutes=2))
    assert "AAPL" not in state.stale_flagged


def test_stale_floor_ignores_time_before_the_open():
    state = WatcherState(WatcherConfig(stale_after=timedelta(minutes=3)))
    state.stale_checks(
        ["AAPL"], T0 - timedelta(hours=2), market_open=False
    )  # started long before the open
    assert state.stale_checks(["AAPL"], T0 + timedelta(minutes=1), market_open=True, floor=T0) == []
    late = state.stale_checks(["AAPL"], T0 + timedelta(minutes=4), market_open=True, floor=T0)
    assert [a.symbol for a in late if a.kind == "stale_data"] == ["AAPL"]


def test_feed_level_staleness_and_recovery():
    state = WatcherState(WatcherConfig())
    state.on_bar(bar(0, "100.00"), T0)
    assert state.stale_checks(["AAPL", "MSFT"], T0 + timedelta(minutes=2), market_open=True) == []
    alerts = state.stale_checks(["AAPL", "MSFT"], T0 + timedelta(minutes=4), market_open=True)
    assert (
        [a.kind for a in alerts] == ["feed_stale"]
        and alerts[0].symbol == "FEED"
        and alerts[0].severity == "critical"
    )
    assert (
        state.stale_checks(["AAPL", "MSFT"], T0 + timedelta(minutes=5), market_open=True) == []
    )  # flagged once
    state.on_bar(bar(6, "100.00"), T0 + timedelta(minutes=6))
    recovered = state.stale_checks(["AAPL", "MSFT"], T0 + timedelta(minutes=6), market_open=True)
    assert [a.kind for a in recovered] == ["feed_recovered"]
    quiet = state.stale_checks(["AAPL", "MSFT"], T0 + timedelta(minutes=13), market_open=True)
    assert [a.symbol for a in quiet if a.kind == "stale_data"] == [
        "MSFT"
    ]  # AAPL printed seven minutes ago
    assert [a.kind for a in quiet if a.symbol == "FEED"] == ["feed_stale"]
