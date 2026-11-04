"""Close every open position in the paper account with market orders.

Operator tool, run by hand: `uv run python scripts/close_positions.py`. While the market is closed the
orders queue and fill at the next open. Prints symbols, order ids and statuses; never prints keys.
"""

import truststore
from dotenv import load_dotenv

truststore.inject_into_ssl()  # this machine's Python has no CA bundle of its own
load_dotenv()

from alpaca.trading.client import TradingClient

from desk.core.config import Settings


def main() -> None:
    settings = Settings()
    if settings.alpaca_api_key is None or settings.alpaca_secret_key is None:
        raise SystemExit("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env")
    client = TradingClient(
        settings.alpaca_api_key.get_secret_value(),
        settings.alpaca_secret_key.get_secret_value(),
        paper=settings.alpaca_paper,
    )
    positions = client.get_all_positions()
    if not positions:
        print("no open positions")
        return
    for p in positions:
        print(
            f"closing {p.symbol}: {p.qty} shares, market value {p.market_value}, P&L {p.unrealized_pl}"
        )
    for r in client.close_all_positions(cancel_orders=True):
        body = getattr(r, "body", None)
        order_id = getattr(body, "id", None)
        status = getattr(getattr(body, "status", None), "value", getattr(body, "status", None))
        print(f"{r.symbol}: http {r.status}, order {order_id}, status {status}")


if __name__ == "__main__":
    main()
