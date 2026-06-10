"""
account_conn.py — Feature #6: ONE short-lived connection per "round", strictly
serialised per account.
============================================================================

Why this exists (and why it mirrors the project's PROVEN-working patterns)
--------------------------------------------------------------------------
rubpy keeps one network session per Rubika account. Rubika invalidates that
session (INVALID_AUTH) in two situations we must both avoid:

  (A) TWO clients connected to the same session at the same time.
  (B) MANY rapid connect/disconnect cycles on the same session (login flood).

Two patterns in THIS project provably do NOT trigger either problem:
  * "send to contacts": opens ONE client, does the WHOLE job on it, closes.
  * the original automation: opened ONE client and reused it for the whole loop.

Both share the shape: **open once, do a whole batch of work, close.** They are
NOT permanent (so rubpy's background update socket isn't held open forever) and
NOT per-operation (so there's no connect/disconnect churn).

This module generalises exactly that shape so the always-on features
(automation / secretary / reply responder / channel report) can share an
account safely:

  * ``async with account_conn.connection(phone) as client:`` — acquires the
    per-account lock, opens ONE client, yields it for a whole pass, then closes
    it on exit. Because the lock is held for the whole pass, two features can
    never have a connection open on the same account at the same time (avoids
    A); because a pass opens exactly one client, there's no churn (avoids B).
  * ``await account_conn.call(phone, fn, ...)`` — convenience for a single
    one-off operation (e.g. profile sync); internally just one ``connection()``.
  * INVALID_AUTH is surfaced as InvalidAuthError; the account must be
    re-logged-in (per the project owner this is expected, not a bug).

Callers do sleeps/waits OUTSIDE the ``connection()`` block so features
interleave at pass granularity. This module never imports bot/worker; the
optional invalid-auth notifier is injected via set_invalid_auth_handler().
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import rubika_client as rb


class _Conn:
    __slots__ = ("phone", "lock", "invalid")

    def __init__(self, phone: str):
        self.phone = phone
        self.lock = asyncio.Lock()
        self.invalid = False


_conns: dict = {}                   # normalized phone -> _Conn
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


def is_auth_error(err: Exception) -> bool:
    """True ONLY for explicit Rubika 'session invalid' signals.

    Deliberately narrow: a banned/muted group, a failed single send, a timeout,
    or a transient network/connection hiccup must NOT be treated as a dead
    session. We match the explicit auth tokens Rubika returns when the session
    itself is revoked, and (importantly) NOT generic words like "error" or
    "forbidden" that a group-level block would produce.
    """
    s = str(err).upper()
    return ("INVALID_AUTH" in s or "INVALIDAUTH" in s
            or "NOT_REGISTERED" in s or "AUTH_FROM_ANOTHER" in s)


async def verify_session_dead(phone: str) -> bool:
    """Confirm a suspected dead session with a FRESH connection before we ever
    declare the account invalid.

    A single auth-looking error can be transient (a wedged socket, a hiccup
    during connect, Rubika briefly rejecting a reused session). So when a loop
    sees one, it calls this: we open a brand-new client and do one cheap,
    read-only call (get_me / get_self_guid). If that succeeds, the session is
    ALIVE and the earlier error was transient -> return False (do NOT kill the
    account). Only if the fresh connection ALSO fails with an explicit auth
    error do we return True (truly dead).
    """
    c = _get_conn(phone)
    async with c.lock:
        client = None
        try:
            client = rb.open_client(c.phone)
            await rb.connect_ready(client)
            await asyncio.wait_for(rb.get_self_guid(client), timeout=30)
            return False                      # fresh connection works -> alive
        except Exception as e:  # noqa: BLE001
            return is_auth_error(e)           # dead only if fresh conn auth-fails
        finally:
            await _disconnect_quietly(client)


class InvalidAuthError(RuntimeError):
    """Raised when the account session is invalid (needs re-login)."""


async def _disconnect_quietly(client):
    if client is None:
        return
    try:
        await client.disconnect()
    except Exception:
        pass


async def notify_invalid(phone: str):
    """Mark + notify that a session is invalid (called by the feature loops)."""
    c = _get_conn(phone)
    c.invalid = True
    if _invalid_auth_handler is None:
        return
    try:
        await _invalid_auth_handler(_key(phone))
    except Exception:
        pass


@asynccontextmanager
async def connection(phone: str):
    """Hold the account's per-account lock, open ONE rubpy client for the whole
    block, and close it on exit. Use this around a WHOLE feature pass (e.g. all
    groups in one automation cycle), then sleep OUTSIDE the block.

    This is the same open->work->close shape as the working "send" path, so it
    never keeps a permanent socket and never churns connections.
    """
    c = _get_conn(phone)
    async with c.lock:
        client = None
        try:
            client = rb.open_client(c.phone)
            await rb.connect_ready(client)
            yield client
        finally:
            await _disconnect_quietly(client)


async def call(phone: str, fn, *args, timeout: float = None, **kwargs):
    """Run a SINGLE one-off ``fn(client, *args, **kwargs)`` inside one
    ``connection()``. On an auth-looking error, CONFIRM with a fresh connection
    before raising InvalidAuthError (so a transient hiccup never kills a healthy
    account)."""
    try:
        async with connection(phone) as client:
            if timeout:
                return await asyncio.wait_for(fn(client, *args, **kwargs),
                                              timeout=timeout)
            return await fn(client, *args, **kwargs)
    except InvalidAuthError:
        raise
    except Exception as e:  # noqa: BLE001
        if is_auth_error(e) and await verify_session_dead(phone):
            await notify_invalid(phone)
            raise InvalidAuthError(f"{_key(phone)}: session invalid") from e
        raise


async def close(phone: str):
    """Briefly acquire the account's lock so any in-flight pass finishes; there
    is no persistent connection to tear down. Clears the invalid flag so a
    subsequent (re)login starts clean."""
    c = _conns.get(_key(phone))
    if not c:
        return
    try:
        async with c.lock:
            c.invalid = False
    except Exception:
        pass


async def close_all():
    """Nothing persistent to close (open->work->close model)."""
    return


def reset_invalid(phone: str):
    c = _conns.get(_key(phone))
    if c:
        c.invalid = False


def is_invalid(phone: str) -> bool:
    c = _conns.get(_key(phone))
    return bool(c and c.invalid)


def start_janitor():
    """No-op: no idle sockets to reap in the open->work->close model."""
    return None
