"""The prompt registry: every prompt is a versioned file; its version is the file name plus a hash of
its text, stamped on the events the model produced, so a plan can always be traced to the exact
words that asked for it."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

PROMPTS = Path(__file__).resolve().parents[3] / "prompts"

NAMES = {
    "head_trader": "head_trader/v1.md",
    "plan": "head_trader/plan_v1.md",
    "brief": "head_trader/brief_v1.md",
    "morning": "head_trader/morning_v1.md",
    "skeptic": "skeptic/v1.md",
}


def path_of(name: str) -> Path:
    try:
        return PROMPTS / NAMES[name]
    except KeyError as exc:
        raise KeyError(f"unknown prompt {name!r}; known: {sorted(NAMES)}") from exc


@lru_cache(maxsize=32)
def load(name: str) -> str:
    return path_of(name).read_text()


def version(name: str) -> str:
    """'plan_v1@3f9c1a2b': the file's version name and a hash of its content."""
    text = load(name)
    digest = hashlib.sha256(text.encode()).hexdigest()[:8]
    return f"{path_of(name).stem}@{digest}"


def registry() -> dict[str, str]:
    return {name: version(name) for name in NAMES}


def render_registry() -> str:
    return "\n".join(f"{name:<12} {version(name)}  {NAMES[name]}" for name in NAMES)
