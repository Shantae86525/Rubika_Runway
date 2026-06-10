"""
account_conn.py — Feature #6: ONE persistent connection per account, reused
and strictly serialised.
===========================================================================

Why this exists (and why it is shaped EXACTLY like the project's original
working automation loop)
------------------------------------------------------------------------
rubpy keeps one network session per Rubika account. Rubika invalidates that
session (INVALID_AUTH) in TWO situations we must both avoid:

  (A) TWO clients connected to the same session at the same time
      -> happens if several always-on features each open their own client.

  (B) MANY rapid connect/disconnect cycles on the same session in a short time
      -> Rubika reads it as login-flooding and kicks the session. This is what
         a naive "open a fresh client for every single call" design causes
         (e.g. automation sending to 20 groups = 20 logins in seconds).

The project's ORIGINAL automation avoided BOTH by opening ONE connection and
REUSING it for the whole run. This module generalises exactly that pattern so
the secretary / reply responder / channel report / automation can all share an
account safely:

  * keep ONE warm rubpy client per account (lazily opened, REUSED across calls),
  * a per-account asyncio.Lock serialises calls so only ONE call ever touches
    the session at a time (never two in parallel -> avoids (A)),
  * the connection is REUSED, not reopened per call -> no connect churn -> (B),
  * a transient connection error reconnects ONCE (sequentially, never parallel),
  * INVALID_AUTH is surfaced as InvalidAuthError; the account must be
    re-logged-in (per the project owner this is expected, not a bug),
  * an idle janitor closes a connection unused for a while so many accounts
    don't keep N sockets open forever.

Callers keep ``fn`` a SHORT single operation and do sleeps/waits OUTSIDE the
call, so features interleave fairly. This module never imports bot/worker; the
optional invalid-auth notifier is injected via set_invalid_auth_handler().
"""
from __future__ import annotations

import asyncio
import time

import config
import rubika_client as rb


class _Conn:
    __slots__ = ("phone", "lock", "client", "last_used", "invalid")

    def __init__(self, phone: str):
        self.phone = phone
        self.lock = asyncio.Lock()
        self.client = None          # rubpy Client or None (the ONE warm client)
        self.last_used = 0.0
        self.invalid = False


_conns: dict = {}                   # normalized phone -> _Conn
_janitor_task = None
_invalid_auth_handler = None        # async def handler(phone) -> None


def set_invalid_auth_handler(fn):
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
    s = (repr(err) + " " + str(err)).upper()
    return ("INVALID_AUTH" in s or "NOT_REGISTERED" in s
            or "INVALIDAUTH" in s or "AUTH_FROM_ANOTHER" in s)


class InvalidAuthError(RuntimeError):
    """Raised by call() when the account session is invalid (needs re-login)."""


async def _ensure_connected(c: _Conn):
    """Return the warm client, opening+connecting it ONCE if needed."""
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


async def call(phone: str, fn, *args, timeout: float = None, **kwargs):
    """Run ``fn(client, *args, **kwargs)`` on the account's ONE warm connection
    while holding its per-account lock, so only one call touches the session at
    a time and the connection is reused (no churn, no parallel clients).

    Reconnects ONCE on a transient connection error. Raises InvalidAuthError if
    Rubika reports the session is invalid.

    Keep ``fn`` a SHORT single operation; sleep OUTSIDE this call so the other
    features on the same account get their turn quickly.
    """
    c = _get_conn(phone)
    async with c.lock:
        c.last_used = time.monotonic()
        for attempt in (1, 2):
            try:
                client = await _ensure_connected(c)
                if timeout:
                    res = await asyncio.wait_for(fn(client, *args, **kwargs),
                                                 timeout=timeout)
                else:
                    res = await fn(client, *args, **kwargs)
                c.last_used = time.monotonic()
                return res
            except asyncio.TimeoutError:
                await _drop(c)          # stuck socket: drop so next call reopens
                raise
            except Exception as e:  # noqa: BLE001
                if _is_invalid_auth(e):
                    c.invalid = True
                    await _drop(c)
                    await _notify_invalid(c.phone)
                    raise InvalidAuthError(f"{c.phone}: session invalid") from e
                # transient/connection error -> drop and retry ONCE (sequential)
                await _drop(c)
                if attempt == 2:
                    raise
                await asyncio.sleep(1.0)


async def _notify_invalid(phone: str):
    if _invalid_auth_handler is None:
        return
    try:
        await _invalid_auth_handler(phone)
    except Exception:
        pass


async def close(phone: str):
    """Force-close an account's warm connection (e.g. before a fresh login, or
    before a one-shot send/channel/join that opens its own client). Guarantees
    no second connection can exist for that account."""
    c = _conns.get(_key(phone))
    if not c:
        return
    async with c.lock:
        await _drop(c)
        c.invalid = False


async def close_all():
    for c in list(_conns.values()):
        try:
            async with c.lock:
                await _drop(c)
        except Exception:
            pass


def reset_invalid(phone: str):
    c = _conns.get(_key(phone))
    if c:
        c.invalid = False


def is_invalid(phone: str) -> bool:
    c = _conns.get(_key(phone))
    return bool(c and c.invalid)


# --------------------------------------------------------------------------- #
# Idle janitor: close connections unused for a while (keeps socket count low).
# --------------------------------------------------------------------------- #
async def _janitor_loop():
    idle = max(60, int(config.CONN_IDLE_CLOSE_SEC))
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        for c in list(_conns.values()):
            if c.client is None or c.lock.locked():
                continue
            if (now - c.last_used) < idle:
                continue
            try:
                await asyncio.wait_for(c.lock.acquire(), timeout=0.1)
            except Exception:
                continue
            try:
                if c.client is not None and (time.monotonic() - c.last_used) >= idle:
                    await _drop(c)
            finally:
                c.lock.release()


def start_janitor():
    global _janitor_task
    if _janitor_task is None or _janitor_task.done():
        try:
            _janitor_task = asyncio.create_task(_janitor_loop())
        except RuntimeError:
            _janitor_task = None
    return _janitor_task
