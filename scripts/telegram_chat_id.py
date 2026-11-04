"""Print the chat ids that have messaged the desk's Telegram bot, so one can go into .env as
DESK_TELEGRAM_CHAT_ID. Reads the token from .env; never prints it."""

import json
import urllib.request

from dotenv import load_dotenv

load_dotenv()

from desk.core.config import Settings


def main() -> None:
    token = Settings().telegram_bot_token
    if token is None:
        raise SystemExit("DESK_TELEGRAM_BOT_TOKEN is not set in .env")
    url = f"https://api.telegram.org/bot{token.get_secret_value()}/getUpdates"
    with urllib.request.urlopen(url, timeout=15) as resp:
        updates = json.load(resp).get("result", [])
    seen: dict[str, str] = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            name = chat.get("title") or " ".join(
                x for x in (chat.get("first_name"), chat.get("last_name")) if x
            )
            seen[str(chat["id"])] = f"{chat.get('type', '')} {name}".strip()
    if not seen:
        print(
            "no messages yet: open the bot in Telegram, press Start, send any text, then run again"
        )
    for chat_id, who in seen.items():
        print(f"DESK_TELEGRAM_CHAT_ID={chat_id}   # {who}")


if __name__ == "__main__":
    main()
