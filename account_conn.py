"""
account_conn.py — Feature #6: "one connection at a time, per account".
======================================================================

Why this exists
---------------
The continuous automation features (rotating-texts automation, the PV
"secretary", the group-reply responder and the channel report) may all be
active on the SAME Rubika account at the same time. rubpy keeps one network
session per account; if two coroutines touch that session concurrently — OR if
a second rubpy client connects while another is still connected — Rubika
invalidates the session (INVALID_AUTH). The old single-feature code avoided
that only because the panel made every operation mutually exclusive; that no
longer holds once several always-on features share an account.

The fix (matches the project's ORIGINAL safe pattern: open -> use -> close):
  * every call goes through ``account_conn.call(phone, fn, ...)``,
  * a per-account asyncio.Lock serialises calls so only ONE runs at a time,
  * INSIDE the lock we OPEN a fresh rubpy client, run the single op, then CLOSE
    it again before releasing the lock. We never keep a connection open across
    calls, so two rubpy clients can never be connected to the same session at
    once (which is exactly what produced the INVALID_AUTH storms).
  * callers keep ``fn`` a SHORT single operation and do sleeps/waits OUTSIDE
    the call, so the features interleave fairly instead of blocking each other.
  * INVALID_AUTH is surfaced as InvalidAuthError (the account must be
    re-logged-in; per the project owner this is expected, not a bug).

This module is imported by BOTH the master (bot.py) and the worker
(worker_api.py); it behaves identically on each because both run rubpy locally
for the accounts they own. It does NOT import bot/worker — the optional
"invalid auth" notifier is injected via set_invalid_auth_handler().
"""
from __future__ import annotations

import asyncio

import rubika_client as rb


# --------------------------------------------------------------------------- #
# Per-account serialisation state
# --------------------------------------------------------------------------- #
class _Conn:
    __slots__ = ("phone", "lock", "invalid")

    def __init__(self, phone: str):
        self.phone = phone
        self.lock = asyncio.Lock()
        self.invalid = False        # True once the session was invalidated


_conns: dict = {}                   # normalized phone -> _Conn

# optional async callback: async def handler(phone: str) -> None
_invalid_auth_handler = None


def set_invalid_auth_handler(fn):
    """Register an async callback invoked (once) when an account's session is
    found invalid. Used by the master to mark the account + post a log card."""
    global _invalid_auth_handler
    _invalid_auth_handler = fn


def _key(phone: str) -> str:
    return rb.normalize_phone(phone)


def _get_conn(phone: str) -> _Conn:
    k = _key(phone)
    c = _conns.get(k)
    if c is None:
        c = _Conn(k)
        _conns[k] = c
    return c


def _is_invalid_auth(err: Exception) -> bool:
    """True if the error means the account session is no longer valid."""
    s = (repr(err) + " " + str(err)).upper()
    return ("INVALID_AUTH" in s or "NOT_REGISTERED" in s
            or "INVALIDAUTH" in s or "AUTH_FROM_ANOTHER" in s)


class InvalidAuthError(RuntimeError):
    """Raised by call() when the account session is invalid (needs re-login)."""


async def _disconnect_quietly(client):
    if client is None:
        return
    try:
        await client.disconnect()
    except Exception:
        pass


async def call(phone: str, fn, *args, timeout: float = None, **kwargs):
    """Serialised, single-connection call.

    Holds the account's per-account lock, OPENS a fresh rubpy client, runs
    ``fn(client, *args, **kwargs)`` once, then CLOSES the client before
    releasing the lock. Because the connection never outlives the call, two
    clients can never be connected to the same session simultaneously, which is
    what caused the INVALID_AUTH storms when several always-on features shared
    one account.

    Raises InvalidAuthError if Rubika reports the session is invalid (the
    account must be re-logged-in). Keep ``fn`` SHORT and do sleeps OUTSIDE.
    """
    c = _get_conn(phone)
    async with c.lock:
        client = None
        try:
            client = rb.open_client(c.phone)
            await rb.connect_ready(client)
            if timeout:
                res = await asyncio.wait_for(fn(client, *args, **kwargs),
                                             timeout=timeout)
            else:
                res = await fn(client, *args, **kwargs)
            return res
        except Exception as e:  # noqa: BLE001
            if _is_invalid_auth(e):
                c.invalid = True
                await _notify_invalid(c.phone)
                raise InvalidAuthError(f"{c.phone}: session invalid") from e
            raise
        finally:
            await _disconnect_quietly(client)


async def _notify_invalid(phone: str):
    if _invalid_auth_handler is None:
        return
    try:
        await _invalid_auth_handler(phone)
    except Exception:
        pass


async def close(phone: str):
    """Acquire the account's lock briefly so any in-flight call finishes; there
    is no persistent connection to tear down (open->use->close model). Clearing
    the invalid flag lets a subsequent (re)login start clean."""
    c = _conns.get(_key(phone))
    if not c:
        return
    try:
        async with c.lock:
            c.invalid = False
    except Exception:
        pass


async def close_all():
    """Nothing persistent to close (open->use->close model). Kept for API
    compatibility with callers (e.g. bot shutdown)."""
    return


def reset_invalid(phone: str):
    """Clear the 'invalid' flag after a successful re-login."""
    c = _conns.get(_key(phone))
    if c:
        c.invalid = False


def is_invalid(phone: str) -> bool:
    c = _conns.get(_key(phone))
    return bool(c and c.invalid)


def start_janitor():
    """No-op in the open->use->close model (no idle sockets to reap). Kept so
    existing callers (bot.amain / worker startup) don't need to change."""
    return None
