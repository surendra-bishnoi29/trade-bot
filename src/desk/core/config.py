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

    # unprefixed on purpose: the Anthropic SDK reads ANTHROPIC_API_KEY itself
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    alpaca_api_key: SecretStr | None = Field(default=None, validation_alias="ALPACA_API_KEY")
    alpaca_secret_key: SecretStr | None = Field(default=None, validation_alias="ALPACA_SECRET_KEY")


def load_settings() -> Settings:
    return Settings()
