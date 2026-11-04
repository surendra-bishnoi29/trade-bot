"""desk-api: `serve` runs the backend, `users` manages interface accounts."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys

from dotenv import load_dotenv

from desk.storage.db import Database
from desk_api.settings import ApiSettings
from desk_api.users import Users


def _serve(host: str, port: int) -> None:
    import uvicorn

    from desk_api.main import create_app

    settings = ApiSettings()
    if settings.session_secret is None:
        logging.getLogger(__name__).warning(
            "DESK_SESSION_SECRET is not set: sessions will not survive a restart"
        )
    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


async def _users(args: argparse.Namespace) -> int:
    settings = ApiSettings()
    db = Database(settings.database_url)
    await db.open()
    try:
        users = Users(db.pool)
        await users.migrate()
        if args.action == "list":
            rows = await users.list()
            if not rows:
                print("no users yet")
            for r in rows:
                last = (
                    r["last_login"].strftime("%Y-%m-%d %H:%M UTC") if r["last_login"] else "never"
                )
                print(f"{r['name']:<20} {r['role']:<9} last login {last}")
        elif args.action == "add":
            password = args.password or getpass.getpass(f"password for {args.name}: ")
            await users.add(args.name, args.role, password)
            print(f"{args.name} added as {args.role}")
        elif args.action == "passwd":
            password = args.password or getpass.getpass(f"new password for {args.name}: ")
            print("updated" if await users.set_password(args.name, password) else "no such user")
        elif args.action == "remove":
            print("removed" if await users.remove(args.name) else "no such user")
    finally:
        await db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(
        prog="desk-api", description="the trading desk interface backend"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the backend")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    users = sub.add_parser("users", help="manage interface accounts")
    actions = users.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add", help="add or replace a user")
    add.add_argument("name")
    add.add_argument("--role", choices=["operator", "viewer"], default="viewer")
    add.add_argument("--password", help="omit to be prompted")
    actions.add_parser("list", help="list users")
    passwd = actions.add_parser("passwd", help="change a password")
    passwd.add_argument("name")
    passwd.add_argument("--password")
    remove = actions.add_parser("remove", help="remove a user")
    remove.add_argument("name")
    args = parser.parse_args(argv)
    if args.command == "serve":
        _serve(args.host, args.port)
        return 0
    return asyncio.run(_users(args))


if __name__ == "__main__":
    sys.exit(main())
