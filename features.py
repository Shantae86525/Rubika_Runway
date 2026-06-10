"""
features.py — LOCAL loops for the automation EXTRAS.
====================================================

Holds the long-running LOCAL (master-side) loops for:
  * Feature 1 — PV secretary (auto-reply to the FIRST message of each new PV),
  * Feature 2 — channel report (members + last-post views every interval),
  * Feature 5 — group-reply responder (reply when someone replies to us).

Connection model (Feature 6) — SAME shape as the project's original automation
and the working "send" path: each pass opens ONE connection via
``account_conn.connection(phone)``, does the WHOLE pass on that single client,
then closes it and sleeps OUTSIDE the block. The per-account lock inside
``connection()`` guarantees two features never hold a connection on the same
account at once, and opening once-per-pass (not once-per-call) means no
connect/disconnect churn — the two things that caused INVALID_AUTH.

This module never imports ``bot`` (to avoid a circular import). The master
injects its Telegram logger via ``set_logger()``; orchestration (local-vs-worker
routing, start/stop/recover) lives in ``bot.py``, exactly like the existing
automation feature.
"""
from __future__ import annotations

import asyncio

import account_conn
import config
import db
import rubika_client as rb

LINE = "━━━━━━━━━━━━━━━━"

_logger = None  # async callable(text)


def set_logger(fn):
    global _logger
    _logger = fn


async def _log(text: str):
    if _logger is None:
        return
    try:
        await _logger(text)
    except Exception:
        pass


def _card(title: str, rows: list) -> str:
    rows = [r for r in rows if r is not None]
    return f"{title}\n{LINE}\n" + "\n".join(rows)


def _now() -> str:
    return config.now_str()


async def _log_invalid(phone: str):
    await _log(_card("🔐 INVALID_AUTH", [
        f"👤 Account : {phone}",
        "سشن این اکانت باطل شده — باید دوباره لاگین شه (باگ نیست).",
        f"🕒 {_now()}",
    ]))


async def _sleep_interruptible(st: dict, seconds: float):
    waited = 0.0
    while waited < seconds and not st.get("stop"):
        await asyncio.sleep(1.0)
        waited += 1.0


# =========================================================================== #
# Feature 1 — PV secretary
# =========================================================================== #
async def run_secretary_local(account_id: int, phone: str, st: dict):
    """Poll new private chats every interval; reply ONCE to each person's first
    incoming message. Reply content is either the marked Saved-Messages post
    ('marker' mode) or a custom text ('text' mode). ONE connection per pass."""
    while not st.get("stop"):
        try:
            sec = db.get_secretary(account_id)
            state = sec.get("state") or ""
            mode = sec.get("mode") or "marker"
            # ---- one connection for this WHOLE pass ----
            async with account_conn.connection(phone) as client:
                result = await asyncio.wait_for(
                    rb.get_chats_updates(client, state), timeout=60)
                chats, new_state = rb.parse_chats_updates(result)
                if new_state:
                    db.set_secretary_state(account_id, new_state)

                # First run after enabling: prime the cursor only, do NOT reply
                # to the account's pre-existing chats.
                if not state:
                    chats = []

                if chats:
                    self_guid = st.get("self_guid")
                    if not self_guid:
                        self_guid = await asyncio.wait_for(
                            rb.get_self_guid(client), timeout=30)
                        st["self_guid"] = self_guid

                marker_ctx = None  # (saved_guid, mid) cached for this pass
                for chat in chats:
                    if st.get("stop"):
                        break
                    if rb.chat_type(chat) != "user":
                        continue
                    guid = rb.chat_object_guid(chat)
                    if not guid:
                        continue
                    author = rb.message_author_guid(rb.chat_last_message(chat))
                    if self_guid and author and author == self_guid:
                        continue
                    if db.secretary_already_replied(account_id, guid):
                        continue
                    try:
                        if mode == "text":
                            txt = sec.get("text") or ""
                            if not txt:
                                continue
                            await asyncio.wait_for(
                                rb.send_text(client, guid, txt),
                                timeout=config.SEND_TIMEOUT)
                        else:  # marker mode -> forward the marked Saved post
                            if marker_ctx is None:
                                marker = db.get_marker()
                                marker_ctx = await asyncio.wait_for(
                                    rb.find_marked_message(client, marker),
                                    timeout=60)
                            saved_guid, mid = marker_ctx
                            if not mid:
                                await _log(_card("🤖 منشی — مارکر پیدا نشد", [
                                    f"👤 Account : {phone}",
                                    f"📌 Marker : «{db.get_marker()}»",
                                    f"🕒 {_now()}"]))
                                break
                            await asyncio.wait_for(
                                rb.forward_to(client, saved_guid, guid, mid),
                                timeout=config.SEND_TIMEOUT)
                        db.mark_secretary_replied(account_id, guid)
                        db.incr_secretary_replied(account_id, 1)
                        st["replied"] = st.get("replied", 0) + 1
                        await _log(_card("🤖 منشی — جواب خودکار", [
                            f"👤 Account : {phone}",
                            f"🎯 To : {guid}",
                            f"✍️ Mode : {'متن دلخواه' if mode == 'text' else 'مارکر'}",
                            f"🕒 {_now()}"]))
                    except Exception as e:  # noqa: BLE001
                        if account_conn.is_auth_error(e):
                            raise
                        await _log(_card("⚠️ منشی — خطا در جواب", [
                            f"👤 Account : {phone}",
                            f"🎯 To : {guid}",
                            f"💥 {repr(e)[:160]}",
                            f"🕒 {_now()}"]))
                    await asyncio.sleep(config.SECRETARY_REPLY_DELAY)
        except account_conn.InvalidAuthError:
            await account_conn.notify_invalid(phone)
            await _log_invalid(phone)
            break
        except Exception as e:  # noqa: BLE001
            if account_conn.is_auth_error(e):
                await account_conn.notify_invalid(phone)
                await _log_invalid(phone)
                break
            await _log(_card("⚠️ منشی — خطای حلقه", [
                f"👤 Account : {phone}", f"💥 {repr(e)[:160]}", f"🕒 {_now()}"]))
        iv = (db.get_secretary(account_id).get("interval_sec")
              or config.SECRETARY_INTERVAL)
        await _sleep_interruptible(st, iv)


# =========================================================================== #
# Feature 2 — channel report
# =========================================================================== #
async def run_channel_report_local(account_id: int, phone: str, st: dict):
    """Every interval, log the target channel's member count + last-post views.
    ONE connection per pass."""
    while not st.get("stop"):
        cr = db.get_channel_report(account_id)
        guid = cr.get("channel_guid") or ""
        title = cr.get("channel_title") or ""
        iv = cr.get("interval_sec") or config.CHANNEL_REPORT_INTERVAL
        if guid:
            try:
                async with account_conn.connection(phone) as client:
                    # resolve @username / link -> guid once, then persist it
                    if not str(guid).startswith("c0"):
                        rguid, rtitle = await asyncio.wait_for(
                            rb.resolve_channel(client, guid), timeout=60)
                        if rguid:
                            guid = rguid
                            if rtitle and not title:
                                title = rtitle
                            try:
                                db.set_channel_report_target(account_id, guid, title)
                            except Exception:
                                pass
                    info = await asyncio.wait_for(
                        rb.get_channel_info(client, guid), timeout=60)
                    members = rb.channel_member_count(info)
                    if not title:
                        title = rb.channel_title_of(info)
                    views, _mid = await asyncio.wait_for(
                        rb.get_last_post_views(client, guid), timeout=60)
                await _log(_card("📊 گزارش کانال", [
                    f"👤 Account : {phone}",
                    f"🆔 Channel : {guid}",
                    (f"🏷 Title : {title}" if title else None),
                    f"👥 Members : {members}",
                    f"👁 Last post views : "
                    f"{views if views is not None else 'نامشخص'}",
                    f"🕒 {_now()}"]))
            except account_conn.InvalidAuthError:
                await account_conn.notify_invalid(phone)
                await _log_invalid(phone)
                break
            except Exception as e:  # noqa: BLE001
                if account_conn.is_auth_error(e):
                    await account_conn.notify_invalid(phone)
                    await _log_invalid(phone)
                    break
                await _log(_card("⚠️ گزارش کانال — خطا", [
                    f"👤 Account : {phone}",
                    f"🆔 Channel : {guid}",
                    f"💥 {repr(e)[:160]}",
                    f"🕒 {_now()}"]))
        await _sleep_interruptible(st, iv)


# =========================================================================== #
# Feature 5 — group-reply responder
# =========================================================================== #
async def run_reply_local(account_id: int, phone: str, st: dict):
    """Poll the account's chats; when someone replies (in a group) to one of the
    account's OWN messages, auto-reply with the configured text after a delay.
    Each replied-to message is handled at most once. ONE connection per pass."""
    while not st.get("stop"):
        try:
            rr = db.get_reply_responder(account_id)
            text = rr.get("text") or ""
            delay = rr.get("delay_sec")
            if delay is None:
                delay = config.REPLY_DELAY
            if not text:
                await _sleep_interruptible(st, config.REPLY_POLL_INTERVAL)
                continue

            state = st.get("state") or ""
            async with account_conn.connection(phone) as client:
                self_guid = st.get("self_guid")
                if not self_guid:
                    self_guid = await asyncio.wait_for(
                        rb.get_self_guid(client), timeout=30)
                    st["self_guid"] = self_guid

                result = await asyncio.wait_for(
                    rb.get_chats_updates(client, state), timeout=60)
                chats, new_state = rb.parse_chats_updates(result)
                if new_state:
                    st["state"] = new_state
                if not state:           # prime only on first run
                    chats = []

                for chat in chats:
                    if st.get("stop"):
                        break
                    if rb.chat_type(chat) != "group":
                        continue
                    gguid = rb.chat_object_guid(chat)
                    if not gguid:
                        continue
                    try:
                        msgs = await asyncio.wait_for(
                            rb.get_recent_messages(client, gguid, 20), timeout=60)
                    except Exception as e:  # noqa: BLE001
                        if account_conn.is_auth_error(e):
                            raise
                        msgs = []
                    for m in msgs:
                        if st.get("stop"):
                            break
                        mid = rb._msg_id_of(m)
                        rtid = rb.message_reply_to_id(m)
                        if not mid or not rtid:
                            continue
                        if db.reply_already_done(account_id, mid):
                            continue
                        author = rb.message_author_guid(m)
                        if self_guid and author == self_guid:
                            db.mark_reply_done(account_id, mid)
                            continue
                        # confirm the replied-to message belongs to us
                        try:
                            parents = await asyncio.wait_for(
                                rb.get_messages_by_id(client, gguid, [rtid]),
                                timeout=60)
                        except Exception as e:  # noqa: BLE001
                            if account_conn.is_auth_error(e):
                                raise
                            parents = []
                        if not parents:
                            continue        # cannot verify parent -> skip safely
                        parent_author = rb.message_author_guid(parents[0])
                        if not (self_guid and parent_author == self_guid):
                            db.mark_reply_done(account_id, mid)
                            continue
                        # it's a reply to US -> respond after the configured delay
                        await asyncio.sleep(max(0.0, float(delay)))
                        try:
                            await asyncio.wait_for(
                                rb.send_reply(client, gguid, text, mid),
                                timeout=config.SEND_TIMEOUT)
                            db.mark_reply_done(account_id, mid)
                            db.incr_reply_replied(account_id, 1)
                            st["replied"] = st.get("replied", 0) + 1
                            await _log(_card("↩️ پاسخ‌گوی ریپلای", [
                                f"یک ریپلای جواب داده شد توسط [{phone}]",
                                f"👥 Group : {gguid}",
                                f"🕒 {_now()}"]))
                        except Exception as e:  # noqa: BLE001
                            if account_conn.is_auth_error(e):
                                raise
                            await _log(_card("⚠️ ریپلای — خطا در جواب", [
                                f"👤 Account : {phone}",
                                f"👥 Group : {gguid}",
                                f"💥 {repr(e)[:160]}",
                                f"🕒 {_now()}"]))
        except account_conn.InvalidAuthError:
            await account_conn.notify_invalid(phone)
            await _log_invalid(phone)
            break
        except Exception as e:  # noqa: BLE001
            if account_conn.is_auth_error(e):
                await account_conn.notify_invalid(phone)
                await _log_invalid(phone)
                break
            await _log(_card("⚠️ ریپلای — خطای حلقه", [
                f"👤 Account : {phone}", f"💥 {repr(e)[:160]}", f"🕒 {_now()}"]))
        await _sleep_interruptible(st, config.REPLY_POLL_INTERVAL)
