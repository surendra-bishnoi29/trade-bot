"""Cache of structured model outputs keyed by a hash of the whole prompt, so a replay can rerun a day
without paying for the same plans twice. A miss is billed and stored; the key never changes
unless the prompt, the material or the model does."""

from __future__ import annotations

import hashlib
from typing import Any

from desk.storage.db import Database


def cache_key(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class LLMCache:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.hits = 0
        self.misses = 0

    async def get(self, key: str) -> dict[str, Any] | None:
        found = await self.db.llm_cache_get(key)
        if found is None:
            self.misses += 1
        else:
            self.hits += 1
        return found

    async def put(self, key: str, kind: str, model: str, response: dict[str, Any]) -> None:
        await self.db.llm_cache_put(key, kind, model, response)
