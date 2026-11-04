"""Tell systemd the desk is alive: READY=1 at start, WATCHDOG=1 on a timer. A no-op outside systemd."""

from __future__ import annotations

import os
import socket


def _send(state: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
        return True
    except OSError:
        return False


def ready() -> bool:
    return _send("READY=1")


def watchdog() -> bool:
    return _send("WATCHDOG=1")


def status(text: str) -> bool:
    return _send("STATUS=" + text.replace("\n", " ")[:200])


def watchdog_interval() -> float | None:
    """Seconds between pings, half of systemd's WatchdogSec, or None when no watchdog is armed."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return None
    return max(1.0, int(usec) / 1_000_000 / 2)
