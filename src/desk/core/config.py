"""Settings. Secrets come from the environment or a .env file, never from code."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Thirty liquid large caps across all eleven sectors, plus SPY and QQQ as references (decision A3).
DEFAULT_UNIVERSE: tuple[str, ...] = (
    "AAPL",
    "MSFT",
    "NVDA",
    "AVGO",  # information technology
    "GOOGL",
    "META",
    "NFLX",  # communication services
    "AMZN",
    "TSLA",
    "HD",  # consumer discretionary
    "PG",
    "COST",
    "KO",  # consumer staples
    "LLY",
    "UNH",
    "JNJ",  # health care
    "JPM",
    "V",
    "BRK.B",
    "GS",  # financials
    "CAT",
    "GE",
    "UNP",  # industrials
    "XOM",
    "CVX",  # energy
    "LIN",  # materials
    "NEE",  # utilities
    "PLD",  # real estate
    "SPY",
    "QQQ",  # market references
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DESK_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: str = "dev"
    autonomy_level: int = Field(default=1, ge=0, le=3)
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "postgresql://localhost:5432/desk"
    paper_capital: Decimal = Decimal(100000)
    core_universe: list[str] = Field(default_factory=lambda: list(DEFAULT_UNIVERSE))
    alpaca_paper: bool = True

    # the autonomous swing cycle (decision: slice 3 safeguards)
    position_pct: Decimal = Decimal("0.02")  # of equity per new position
    max_open_positions: int = 10
    max_new_per_cycle: int = 3
    cycle_mid_session: bool = True
    cycle_news_trigger: bool = True
    cycle_every_minutes: int = (
        0  # 0 = scheduled cycles only; N = also every N minutes while the market is open
    )
    min_conviction_with_concern: float = 0.5  # a buy the skeptic has a concern about needs this

    # the risk officer's portfolio rules (architecture section 8)
    max_gross_pct: Decimal = Decimal("1.0")  # account gross exposure, of equity
    max_sector_pct: Decimal = Decimal("0.30")  # one sector's gross exposure, of equity
    max_daily_loss_pct: Decimal = Decimal("0.02")  # of the day's starting equity; then exits only
    drawdown_derisk_pct: Decimal = Decimal(
        "0.03"
    )  # from the equity peak: new positions at half size
    drawdown_halt_pct: Decimal = Decimal("0.05")  # no new positions
    drawdown_kill_pct: Decimal = Decimal("0.08")  # kill switch, released by a human
    intraday_max_gross_pct: Decimal = Decimal("0.5")
    intraday_allow_short: bool = True
    intraday_max_short_pct: Decimal = Decimal("0.25")

    # the intraday book (phase 4): off until a strategy beats costs in `desk intraday-eval`
    intraday_enabled: bool = False
    intraday_strategy: Literal["opening_range", "vwap_reversion", "momentum"] = "vwap_reversion"
    intraday_position_pct: Decimal = Decimal("0.01")  # of equity per name
    intraday_watchlist_size: int = 10
    intraday_max_positions: int = 5

    # operations (slice 4: the desk runs on a server)
    require_redis: bool = (
        False  # production: refuse to start without Redis instead of the in-memory bus
    )
    alpaca_feed: Literal["iex", "sip"] = "iex"  # sip needs the paid Alpaca data plan
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    heartbeat_minutes: int = 5
    sec_user_agent: str | None = (
        None  # EDGAR asks for a descriptive agent with a contact; unset = off
    )
    models_dir: str = "data/models"  # trained forecasters (git-ignored)
    forecast_horizon: int = 5  # sessions ahead the champion forecaster predicts (1, 5 or 20)
    watch_markets: bool = True  # keep oil, gold and crypto prices fresh for the funnel (watch only)

    # the lab desk (branch `predictor`): one eye per instrument, scored calls, lines on every position.
    # It replaces the swing cycle, the thesis monitor and the intraday book when switched on.
    lab_enabled: bool = False
    # "alpaca": orders go to the Alpaca account in .env. "sim": the in-process simulated broker fills
    # them at the next live minute bar (data still comes from Alpaca); for a local run without a
    # second paper account. The journal, the funnel and the scores look the same either way.
    broker: Literal["alpaca", "sim"] = "alpaca"
    # Live stock bars: the websocket (true) or a REST poll every 20 seconds (false). Alpaca's free
    # plan allows one stream per login, so the default is the stream for the main desk and the
    # poll for the lab desk, which runs beside it on the same login. Set it to say otherwise.
    live_stream: bool | None = None
    sim_crypto_fee_pct: Decimal = Decimal("0.25")  # Alpaca's taker fee on crypto, each side
    lab_markets: list[str] = Field(default_factory=lambda: ["stocks", "commodities", "crypto"])
    predictor_min_confidence: float = 0.6
    predictor_max_calls_per_hour: int = 40
    lab_position_pct: Decimal = Decimal("0.01")  # of equity per position
    lab_min_first_profit_pct: float = 0.5
    lab_min_loss_pct: float = 0.5
    commodities_max_gross_pct: Decimal = Decimal("0.10")
    commodities_allow_short: bool = True
    crypto_max_gross_pct: Decimal = Decimal("0.05")
    sentinel_review_minutes: int = 45
    review_narrative: bool = True  # one model call per review day for the post-mortem lessons
    rss_news: bool = True  # keyless RSS headlines per symbol behind the same scoring
    fred_api_key: SecretStr | None = None  # macro strategist; free key from fred.stlouisfed.org
    finnhub_api_key: SecretStr | None = (
        None  # fundamentals and earnings dates; free key from finnhub.io
    )

    # unprefixed on purpose: the Anthropic SDK reads ANTHROPIC_API_KEY itself
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    alpaca_api_key: SecretStr | None = Field(default=None, validation_alias="ALPACA_API_KEY")
    alpaca_secret_key: SecretStr | None = Field(default=None, validation_alias="ALPACA_SECRET_KEY")


def load_settings() -> Settings:
    return Settings()
