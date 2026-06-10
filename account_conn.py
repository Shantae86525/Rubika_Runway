"""
account_conn.py — Feature #6: "one warm connection + a fine-grained queue per
account".
=============================================================================

Why this exists
---------------
The continuous automation features (rotating-texts automation, the PV
"secretary", the group-reply responder and the channel report) may all be
active on the SAME Rubika account at the same time. rubpy keeps one network
session per account; if two coroutines fire `await client.xxx()` on that
session concurrently their round-trips interleave and Rubika invalidates the
session (INVALID_AUTH). The old code avoided that only because the panel made
every operation mutually exclusive — that no longer holds once several
always-on features share an account.

The fix (kept deliberately simple):
  * keep ONE warm rubpy client per account (lazily opened, auto-reconnect),
  * guard every single rubpy call with a per-account asyncio.Lock so only one
    call ever touches the session at a time,
  * callers do `await account_conn.call(phone, rb.send_text, guid, txt)` — the
    lock is held only for that one short call, sleeps/waits happen OUTSIDE it,
    so the features interleave fairly instead of blocking each other,
  * a tiny janitor closes a connection that has been idle for a while (so many
    accounts don't keep N sockets open forever),
  * INVALID_AUTH is detected and surfaced (the account must be re-logged-in;
    per the project owner this is expected, not a bug).

This module is imported by BOTH the master (bot.py) and the worker
(worker_api.py); it behaves identically on each because both run rubpy locally
for the accounts they own. It does NOT import bot/worker — the optional
"invalid auth" notifier is injected via set_invalid_auth_handler().
"""
from __future__ import annotations

import asyncio
import time

import config
import rubika_client as rb


# --------------------------------------------------------------------------- #
# Per-account connection state
# --------------------------------------------------------------------------- #
class _Conn:
    __slots__ = ("phone", "lock", "client", "last_used", "invalid")

    def __init__(self, phone: str):
        self.phone = phone
        self.lock = asyncio.Lock()
        self.client = None          # rubpy Client or None
        self.last_used = 0.0        # monotonic timestamp of last use
        self.invalid = False        # True once the session was invalidated


_conns: dict = {}                   # normalized phone -> _Conn
_janitor_task = None

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


async def _ensure_connected(c: _Conn):
    if c.client is not None:
        return c.client
    client = rb.open_client(c.phone)
    await rb.connect_ready(client)
    c.client = client
    return client


async def _drop(c: _Conn):
    cl = c.client
    c.client = None
    if cl is not None:
        try:
            await cl.disconnect()
        except Exception:
            pass


class InvalidAuthError(RuntimeError):
    """Raised by call() when the account session is invalid (needs re-login)."""


async def call(phone: str, fn, *args, timeout: float = None, **kwargs):
    """Run ``fn(client, *args, **kwargs)`` on the account's warm connection
    while holding its per-account lock (so only one call touches the session at
    a time). Raises InvalidAuthError ONLY if the session is truly invalid.

    Robustness: a single INVALID_AUTH on an old/stale warm connection does NOT
    mean the session is dead — Rubika sometimes returns it on a wedged socket.
    So on INVALID_AUTH we drop the connection, build a COMPLETELY FRESH one and
    retry; only if a fresh connection ALSO reports INVALID_AUTH do we declare
    the session invalid. This stops false "session expired" alarms while the
    account is in fact still logged in.

    NOTE: keep ``fn`` a SHORT single operation and do any sleeping OUTSIDE this
    call, so the other features on the same account get their turn quickly.
    """
    c = _get_conn(phone)
    async with c.lock:
        c.last_used = time.monotonic()
        last_err = None
        auth_failures = 0
        # up to 3 tries: enough for one stale-conn retry + one transient retry
        for attempt in range(1, 4):
            try:
                client = await _ensure_connected(c)
                if timeout:
                    res = await asyncio.wait_for(fn(client, *args, **kwargs),
                                                 timeout=timeout)
                else:
                    res = await fn(client, *args, **kwargs)
                c.last_used = time.monotonic()
                return res
            except asyncio.TimeoutError as e:
                # a stuck call: drop the (maybe wedged) connection and bubble up
                last_err = e
                await _drop(c)
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                if _is_invalid_auth(e):
                    auth_failures += 1
                    await _drop(c)                 # throw away the stale socket
                    if auth_failures >= 2:
                        # a FRESH connection still rejected -> really invalid
                        c.invalid = True
                        await _notify_invalid(c.phone)
                        raise InvalidAuthError(f"{c.phone}: session invalid") from e
                    # first time: rebuild a brand-new connection and retry
                    await asyncio.sleep(1.0)
                    continue
                # transient/connection error -> drop and retry
                await _drop(c)
                if attempt >= 3:
                    raise
                await asyncio.sleep(1.0)
        if last_err:
            raise last_err


async def _notify_invalid(phone: str):
    if _invalid_auth_handler is None:
        return
    try:
        await _invalid_auth_handler(phone)
    except Exception:
        pass


async def close(phone: str):
    """Force-close a single account's warm connection (e.g. on delete/re-login).
    Safe to call even if nothing is open."""
    c = _conns.get(_key(phone))
    if not c:
        return
    async with c.lock:
        await _drop(c)
        c.invalid = False


async def close_all():
    """Close every warm connection (call on shutdown)."""
    for c in list(_conns.values()):
        try:
            async with c.lock:
                await _drop(c)
        except Exception:
            pass


def reset_invalid(phone: str):
    """Clear the 'invalid' flag after a successful re-login."""
    c = _conns.get(_key(phone))
    if c:
        c.invalid = False


def is_invalid(phone: str) -> bool:
    c = _conns.get(_key(phone))
    return bool(c and c.invalid)


# --------------------------------------------------------------------------- #
# Idle janitor: close connections that have not been used for a while so a
# large number of accounts does not keep N sockets open forever.
# --------------------------------------------------------------------------- #
async def _janitor_loop():
    idle = max(60, int(config.CONN_IDLE_CLOSE_SEC))
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        for c in list(_conns.values()):
            if c.client is None:
                continue
            if c.lock.locked():
                continue  # in use right now
            if (now - c.last_used) < idle:
                continue
            try:
                # acquire briefly; skip if a caller grabbed it meanwhile
                await asyncio.wait_for(c.lock.acquire(), timeout=0.1)
            except Exception:
                continue
            try:
                if c.client is not None and (time.monotonic() - c.last_used) >= idle:
                    await _drop(c)
            finally:
                c.lock.release()


def start_janitor():
    """Start the idle-connection janitor (idempotent). Returns the task."""
    global _janitor_task
    if _janitor_task is None or _janitor_task.done():
        _janitor_task = asyncio.create_task(_janitor_loop())
    return _janitor_task
