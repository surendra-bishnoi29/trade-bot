"""Sessions: a signed, expiring cookie carrying the user's name and role."""

from __future__ import annotations

import secrets
import time

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from desk_api.users import User

COOKIE = "desk_session"


class Sessions:
    def __init__(self, secret: str | None, hours: int = 72) -> None:
        self.secret = secret or secrets.token_hex(32)
        self.max_age = hours * 3600
        self._signer = URLSafeTimedSerializer(self.secret, salt="desk-session")

    def issue(self, user: User) -> str:
        return self._signer.dumps({"n": user.name, "r": user.role})

    def read(self, token: str | None) -> User | None:
        if not token:
            return None
        try:
            data = self._signer.loads(token, max_age=self.max_age)
        except BadSignature:
            return None
        if data.get("r") not in ("operator", "viewer"):
            return None
        return User(name=str(data.get("n", "")), role=data["r"])


class LoginLimiter:
    """At most `attempts` failed logins per address per `window` seconds."""

    def __init__(self, attempts: int = 10, window: float = 300.0) -> None:
        self.attempts, self.window = attempts, window
        self._fails: dict[str, list[float]] = {}

    def blocked(self, key: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self._fails.get(key, []) if now - t < self.window]
        self._fails[key] = recent
        return len(recent) >= self.attempts

    def failed(self, key: str) -> None:
        self._fails.setdefault(key, []).append(time.monotonic())

    def clear(self, key: str) -> None:
        self._fails.pop(key, None)


async def current_user(request: Request) -> User:
    user = request.app.state.sessions.read(request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="sign in first")
    return user


async def require_operator(request: Request) -> User:
    user = await current_user(request)
    if user.role != "operator":
        raise HTTPException(status_code=403, detail="operators only")
    return user
