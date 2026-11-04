"""Interface accounts: operators may run the desk, viewers may watch and ask. Passwords are bcrypt
hashes in Postgres; there is no sign-up, an operator adds people with `desk-api users add`."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

import bcrypt

Role = Literal["operator", "viewer"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_users (
    name          TEXT PRIMARY KEY,
    role          TEXT NOT NULL CHECK (role IN ('operator', 'viewer')),
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login    TIMESTAMPTZ
);
"""


@dataclass(frozen=True)
class User:
    name: str
    role: Role


class Users:
    def __init__(self, pool) -> None:
        self.pool = pool

    async def migrate(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(SCHEMA)

    async def add(self, name: str, role: Role, password: str) -> None:
        name = name.strip().lower()
        if not name or len(password) < 8:
            raise ValueError("a name and a password of at least 8 characters are required")
        digest = await asyncio.to_thread(bcrypt.hashpw, password.encode(), bcrypt.gensalt())
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO api_users (name, role, password_hash) VALUES (%s, %s, %s) "
                "ON CONFLICT (name) DO UPDATE SET role = EXCLUDED.role, password_hash = EXCLUDED.password_hash",
                (name, role, digest.decode()),
            )

    async def set_password(self, name: str, password: str) -> bool:
        if len(password) < 8:
            raise ValueError("a password of at least 8 characters is required")
        digest = await asyncio.to_thread(bcrypt.hashpw, password.encode(), bcrypt.gensalt())
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE api_users SET password_hash = %s WHERE name = %s",
                (digest.decode(), name.lower()),
            )
            return cur.rowcount == 1

    async def remove(self, name: str) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute("DELETE FROM api_users WHERE name = %s", (name.lower(),))
            return cur.rowcount == 1

    async def list(self) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT name, role, created_at, last_login FROM api_users ORDER BY name"
            )
            return [
                {"name": r[0], "role": r[1], "created_at": r[2], "last_login": r[3]}
                for r in await cur.fetchall()
            ]

    async def verify(self, name: str, password: str) -> User | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT role, password_hash FROM api_users WHERE name = %s", (name.strip().lower(),)
            )
            row = await cur.fetchone()
        if row is None:
            await asyncio.to_thread(bcrypt.checkpw, b"x", _DUMMY)  # same cost for unknown names
            return None
        ok = await asyncio.to_thread(bcrypt.checkpw, password.encode(), row[1].encode())
        if not ok:
            return None
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE api_users SET last_login = now() WHERE name = %s", (name.strip().lower(),)
            )
        return User(name=name.strip().lower(), role=row[0])


_DUMMY = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt())
