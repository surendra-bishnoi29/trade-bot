"""One tiny call to confirm the Anthropic key in .env works. Costs a fraction of a cent.

Run:  uv run python scripts/check_anthropic.py
"""

from __future__ import annotations

import sys

import anthropic
from dotenv import load_dotenv


def main() -> int:
    load_dotenv()
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    response = client.messages.create(
        model="claude-opus-5-5",
        max_tokens=32,
        messages=[{"role": "user", "content": "Reply with the single word OK."}],
        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
        extra_body={"fallbacks": "default"},
    )
    if response.stop_reason == "refusal":
        print("the request was refused:", response.stop_details)
        return 1
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    usage = response.usage
    print(
        f"model {response.model} replied {text!r}; tokens in {usage.input_tokens}, out {usage.output_tokens}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
