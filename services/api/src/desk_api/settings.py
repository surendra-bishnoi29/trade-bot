"""Settings for the api service. Same DESK_ prefix and .env file as the bot, its own keys."""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DESK_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://localhost:5432/desk"
    redis_url: str = "redis://localhost:6379/0"
    session_secret: SecretStr | None = None  # required in production; random per process otherwise
    session_hours: int = 72
    cookie_secure: bool = False  # true once the interface is served over https
    heartbeat_minutes: int = 5
    web_dist: str = "web/dist"  # served by the api in development; Caddy serves it in production
