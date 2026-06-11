"""
Personal Rubika sender — controlled from a Telegram panel.
==========================================================

What it does (and ONLY this):
  * lets the owner log into THEIR OWN Rubika account (phone + code + 2FA),
  * forwards a message the owner marked in their OWN Saved Messages
    (e.g. caption ending in `کد135`) to their OWN contacts,
  * recipients are ordered: chat-first, then online, then last-seen,
  * configurable delay between sends (0.2 - 10s),
  * stops the whole run after MAX_ERRORS failed sends,
  * posts styled log cards to a private Telegram report group.

What it deliberately does NOT do: proxies, multi-account orchestration,
batch broadcasting, or "send to everyone" automation.

Panel text is Persian. Only the configured owner id may use it.
"""
import asyncio
import os
import random
import tempfile
import time
import zipfile
from datetime import datetime

from telethon import TelegramClient, events, Button
from telethon.errors import MessageNotModifiedError

import config
import crypto_util
import db
import rubika_client as rb
import worker
import account_conn
import features

# Make sure the data dir exists BEFORE the Telethon session file is created.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ---- counter (total sends the bot has done), persisted in a small file ----
COUNTER_FILE = os.path.join(DATA_DIR, "send_count.txt")


def _read_counter() -> int:
    try:
        with open(COUNTER_FILE) as f:
            return int(f.read().strip() or "0")
    except Exception:
        return 0


def _next_counter() -> int:
    n = _read_counter() + 1
    try:
        os.makedirs(os.path.dirname(COUNTER_FILE), exist_ok=True)
        with open(COUNTER_FILE, "w") as f:
            f.write(str(n))
    except Exception:
        pass
    return n


def now() -> str:
    # Timezone-aware (config.TIMEZONE, default Asia/Tehran) so log timestamps
    # are correct even when a worker runs on a foreign server.
    return config.now_str()


LINE = "━━━━━━━━━━━━━━━━"


def card(title: str, rows: list) -> str:
    return f"{title}\n{LINE}\n" + "\n".join(rows)


bot = TelegramClient(os.path.join(DATA_DIR, "panel_bot"), config.API_ID, config.API_HASH)

# conversation state per owner: {"step": "..."}
state: dict = {}
# rubpy login clients mid-flow (waiting for code / password)
pending: dict = {}
# prepared sends waiting for confirmation: owner_id -> payload
pending_send: dict = {}
# prepared channels waiting for the "add members" step: owner_id -> payload
pending_channel: dict = {}
# stop flags per account id
stop_flags: dict = {}
# accounts currently running a send/channel job (in-memory busy lock)
active_jobs: set = set()
# owner_id -> [account_id, ...] dead accounts awaiting delete confirmation
pending_dead_accounts: dict = {}
# running LOCAL automation tasks: account_id -> {"task":Task, "state":dict}
automation_tasks: dict = {}
# running LOCAL automation-EXTRAS tasks: account_id -> {"task":Task, "state":dict}
secretary_tasks: dict = {}
reply_tasks: dict = {}
channelreport_tasks: dict = {}


def _alert_word(n: int) -> str:
    return {1: "ONE", 2: "TWO", 3: "THREE"}.get(n, str(n))


async def _wait_or_stop(account_id: int, seconds: float, step: float = 2.0) -> bool:
    """Sleep up to `seconds`; return True early if a manual stop was requested."""
    waited = 0.0
    while waited < seconds:
        if stop_flags.get(account_id):
            return True
        d = min(step, seconds - waited)
        await asyncio.sleep(d)
        waited += d
    return False


def automation_on(account_id: int) -> bool:
    try:
        return bool(db.get_automation(account_id).get("enabled"))
    except Exception:
        return False


def secretary_on(account_id: int) -> bool:
    try:
        return bool(db.get_secretary(account_id).get("enabled"))
    except Exception:
        return False


def channelreport_on(account_id: int) -> bool:
    try:
        return bool(db.get_channel_report(account_id).get("enabled"))
    except Exception:
        return False


def reply_on(account_id: int) -> bool:
    try:
        return bool(db.get_reply_responder(account_id).get("enabled"))
    except Exception:
        return False


def continuous_busy(account_id: int) -> bool:
    """True if ANY always-on feature (automation / secretary / channel report /
    reply responder) is active on the account. One-shot manual operations
    (send / channel / join) are blocked while this is True, so a one-shot never
    opens a second connection alongside the shared one (Feature 6)."""
    return (automation_on(account_id) or secretary_on(account_id)
            or channelreport_on(account_id) or reply_on(account_id))


def _pick_text(texts: list, last_idx):
    """Random text index, avoiding the same one as last time (if possible)."""
    if not texts:
        return None, None
    if len(texts) == 1:
        return 0, texts[0]
    choices = [i for i in range(len(texts)) if i != last_idx]
    i = random.choice(choices)
    return i, texts[i]


def is_owner(event) -> bool:
    """Allowed to USE the bot = the owner OR an admin added from the panel.
    (Name kept for minimal churn across existing handlers.)
    """
    try:
        allowed = set(config.ALLOWED_IDS) | set(db.list_admin_ids())
    except Exception:
        allowed = set(config.ALLOWED_IDS)
    return event.sender_id in allowed


def is_real_owner(event) -> bool:
    """Only the configured OWNER (used for admin/worker management)."""
    return config.OWNER_ID and event.sender_id == config.OWNER_ID


async def log(text: str):
    """Post a report card to the log group (never crash the bot)."""
    try:
        await bot.send_message(config.LOG_GROUP_ID, text)
    except Exception as e:  # noqa: BLE001
        print(f"[log error] {e}")


async def _log_invalid_auth(phone: str, detail: str = ""):
    """Log that an account's session is truly invalid (device kicked out / login
    revoked) AFTER a fresh-connection retry already failed. Per the owner this is
    expected, not a bug. Marks the account inactive so the panel shows a
    one-tap re-login button, and tells the user how to recover it.

    ``detail`` carries the REAL underlying error text so we stop guessing why a
    session was rejected.
    """
    try:
        for a in db.list_accounts():
            if rb.normalize_phone(a["phone"]) == rb.normalize_phone(phone):
                db.set_status(a["id"], "inactive")
                break
    except Exception:
        pass
    rows = [
        f"👤 Account : {phone}",
        "📵 این سشن از روبیکا بیرون انداخته شده (دیوایس logout شده).",
        "همه‌ی قابلیت‌های این اکانت موقتاً متوقف شدن.",
        "🔁 برای ریکاوری: «👤 اکانت‌های من» → همین اکانت → «🔁 لاگین مجدد».",
    ]
    if detail:
        rows.append(f"🧩 جزئیات خطا: {detail[:200]}")
    rows.append(f"🕒 {now()}")
    await log(card("🔐 INVALID_AUTH — نیاز به لاگین مجدد", rows))


async def _on_invalid_auth(phone: str):
    """account_conn handler: only MARK the account inactive (the feature loops
    do the logging when they catch InvalidAuthError, so we don't double-post)."""
    try:
        for a in db.list_accounts():
            if rb.normalize_phone(a["phone"]) == rb.normalize_phone(phone):
                db.set_status(a["id"], "inactive")
                break
    except Exception:
        pass


async def safe_edit(obj, *args, **kwargs):
    """Edit a message/callback, ignoring Telegram's 'content not modified'
    error (raised when the new text+buttons equal what's already shown)."""
    try:
        return await obj.edit(*args, **kwargs)
    except MessageNotModifiedError:
        return None


# --------------------------------------------------------------------------- #
# Menus
# --------------------------------------------------------------------------- #
def main_menu(owner: bool = True):
    rows = [
        [Button.inline("🚀 ارسال", b"send_menu"),
         Button.inline("🔁 اتومیشن", b"automation")],
        [Button.inline("➕ افزودن اکانت", b"add_account"),
         Button.inline("👤 اکانت‌های من", b"accounts")],
        [Button.inline("📌 مارکر", b"marker"),
         Button.inline("⚙️ سرعت ارسال", b"speed")],
        [Button.inline("🛠 ورکرها", b"workers"),
         Button.inline("💾 بکاپ", b"backup")],
        [Button.inline("🏭 موتور مولد", b"generator")],
        [Button.inline("🖼 آرشیو عکس پیوی (PDF)", b"pvexport")],
    ]
    if owner:
        rows.append([Button.inline("👥 مدیریت ادمین", b"admins")])
    return rows


WELCOME = (
    "🤖 روبیکا تولز\n"
    "خوش اومدی 👋 یکی از گزینه‌ها رو انتخاب کن:"
)


@bot.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    if not is_owner(event):
        await event.respond("⛔ شما به این ربات دسترسی ندارید.")
        return
    state.pop(event.sender_id, None)
    await event.respond(WELCOME, buttons=main_menu(is_real_owner(event)))


@bot.on(events.CallbackQuery(data=b"home"))
async def home_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, WELCOME, buttons=main_menu(is_real_owner(event)))


@bot.on(events.CallbackQuery(data=b"cancel"))
async def cancel_cb(event):
    if not is_owner(event):
        return
    p = pending.pop(event.sender_id, None)
    if p:
        try:
            await p["client"].disconnect()
        except Exception:
            pass
    state.pop(event.sender_id, None)
    await safe_edit(event, "لغو شد. منوی اصلی:", buttons=main_menu(is_real_owner(event)))


# --------------------------------------------------------------------------- #
# Add account
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"add_account"))
async def add_account_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_phone"}
    await safe_edit(event, 
        "📱 شماره اکانت روبیکای خودت رو بفرست.\nمثال: `09123456789`",
        buttons=[[Button.inline("🔙 لغو", b"cancel")]],
    )


# --------------------------------------------------------------------------- #
# Accounts list / dashboard
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"accounts"))
async def accounts_cb(event):
    if not is_owner(event):
        return
    accounts = db.list_accounts()
    if not accounts:
        await safe_edit(event, 
            "هنوز اکانتی اضافه نکردی.",
            buttons=[[Button.inline("➕ افزودن اکانت", b"add_account")],
                     [Button.inline("🔙 بازگشت", b"home")]],
        )
        return
    buttons = []
    for i, acc in enumerate(accounts, start=1):
        mark = "" if acc["status"] == "active" else " ⚠️"
        buttons.append([Button.inline(f"{i}- {acc['phone']}{mark}",
                                      f"acc_{acc['id']}".encode())])
    buttons.append([Button.inline("🔄 بررسی و پاکسازی اکانت‌های پریده",
                                  b"acc_sweep")])
    buttons.append([Button.inline("🔙 بازگشت", b"home")])
    await safe_edit(event, "👤 اکانت‌های تو:", buttons=buttons)


@bot.on(events.CallbackQuery(data=b"acc_sweep"))
async def accounts_sweep_cb(event):
    """Update step: check every account's session and flag the dead ones
    (sessions that were kicked out / logged out) as inactive, so the panel
    clearly shows which accounts need a re-login. Healthy accounts that were
    wrongly marked inactive are restored to active."""
    if not is_owner(event):
        return
    await safe_edit(event, "🔄 در حال بررسی سشنِ همه‌ی اکانت‌ها ... (ممکنه کمی طول بکشه)")
    asyncio.create_task(run_accounts_sweep(event.sender_id))


async def run_accounts_sweep(owner_id: int):
    accounts = db.list_accounts()
    alive = 0
    dead_ids = []          # accounts whose session is confirmed dead
    restored = 0
    rows = []
    for acc in accounts:
        phone = acc["phone"]
        aid = acc["id"]
        w = worker.worker_for_account(acc)
        is_dead = False
        checked = True
        try:
            if w and not worker.is_local(w):
                # ask the owning worker to verify the session
                try:
                    res = await worker.api_call(
                        w, "POST", "/account/verify", {"phone": phone}, timeout=90)
                    is_dead = bool(res.get("dead"))
                except Exception:
                    checked = False           # worker unreachable -> don't touch
            else:
                is_dead = await account_conn.verify_session_dead(phone)
        except Exception:
            checked = False

        if not checked:
            rows.append(f"• {phone} : ❔ بررسی نشد (ورکر/اتصال در دسترس نبود)")
            continue
        if is_dead:
            dead_ids.append(aid)
            # stop any always-on features (kick it out) but DON'T delete yet —
            # deletion happens only after the owner confirms.
            try:
                db.set_secretary_enabled(aid, False)
                db.set_channel_report_enabled(aid, False)
                db.set_reply_enabled(aid, False)
                db.set_automation_enabled(aid, False)
            except Exception:
                pass
            for stopper in (stop_automation, stop_secretary, stop_channelreport,
                            stop_reply):
                try:
                    await stopper(acc)
                except Exception:
                    pass
            db.set_status(aid, "inactive")
            rows.append(f"• {phone} : 🔴 سشن پریده (شوت‌شده از سرور)")
        else:
            alive += 1
            if acc["status"] != "active":     # was wrongly inactive -> restore
                db.set_status(aid, "active")
                account_conn.reset_invalid(phone)
                restored += 1
                rows.append(f"• {phone} : 🟢 سالم (به فعال برگردانده شد)")
    await log(card("🔄 ACCOUNT SWEEP", [
        f"🟢 سالم: {alive}   🔴 پریده: {len(dead_ids)}   ♻️ بازگردانده: {restored}",
        LINE, *rows, LINE, f"🕒 {now()}"]))

    # remember the dead set so the confirm button can delete exactly these
    pending_dead_accounts[owner_id] = list(dead_ids)
    if dead_ids:
        dead_phones = []
        for aid in dead_ids:
            a = db.get_account(aid)
            if a:
                dead_phones.append(a["phone"])
        body = "\n".join(f"• {p}" for p in dead_phones)
        await bot.send_message(
            owner_id,
            f"🔄 بررسی تمام شد.\n🟢 سالم: {alive}   ♻️ بازگردانده: {restored}\n"
            f"🔴 {len(dead_ids)} اکانت از سرور شوت شده‌اند:\n{body}\n\n"
            "می‌خوای این اکانت‌ها کلاً از مدیریت اکانت حذف بشن؟",
            buttons=[[Button.inline(f"🗑 بله، حذف کن ({len(dead_ids)})", b"acc_sweep_del")],
                     [Button.inline("🔙 نه، فقط غیرفعال بمونن", b"accounts")]])
    else:
        try:
            await bot.send_message(
                owner_id,
                f"🔄 بررسی تمام شد.\n🟢 سالم: {alive}\n🔴 پریده: 0\n"
                f"♻️ بازگردانده‌شده: {restored}",
                buttons=[[Button.inline("👤 اکانت‌های من", b"accounts")],
                         [Button.inline("🏠 منوی اصلی", b"home")]])
        except Exception:
            pass


@bot.on(events.CallbackQuery(data=b"acc_sweep_del"))
async def accounts_sweep_delete_cb(event):
    """Confirmed deletion of the kicked-out accounts found by the last sweep."""
    if not is_owner(event):
        return
    dead_ids = pending_dead_accounts.pop(event.sender_id, [])
    if not dead_ids:
        await safe_edit(event, "لیست اکانت‌های پریده منقضی شده. دوباره «🔄 بررسی» رو بزن.",
                        buttons=[[Button.inline("👤 اکانت‌های من", b"accounts")]])
        return
    deleted = []
    for aid in dead_ids:
        acc = db.get_account(aid)
        if not acc:
            continue
        phone = acc["phone"]
        # make sure nothing is still running for it, then delete fully
        for stopper in (stop_automation, stop_secretary, stop_channelreport,
                        stop_reply):
            try:
                await stopper(acc)
            except Exception:
                pass
        try:
            await account_conn.close(phone)
        except Exception:
            pass
        try:
            db.delete_account(aid)
            deleted.append(phone)
        except Exception:
            pass
    await log(card("🗑 DEAD ACCOUNTS REMOVED", [
        f"🗑 حذف‌شده: {len(deleted)}",
        LINE, *[f"• {p}" for p in deleted], LINE, f"🕒 {now()}"]))
    await safe_edit(event,
        f"✅ {len(deleted)} اکانتِ پریده کاملاً حذف شدند.",
        buttons=[[Button.inline("👤 اکانت‌های من", b"accounts")],
                 [Button.inline("🏠 منوی اصلی", b"home")]])


# --------------------------------------------------------------------------- #
# Cleanup engine (موتور پاکسازی): groups where an account got banned/muted are
# recorded as candidates; the owner reviews them in a confirm/cancel panel and
# decides to leave them or keep them.
# --------------------------------------------------------------------------- #
async def _log_cleanup_candidate(account_id: int, phone: str, guid: str, name: str):
    """Log a freshly detected banned/muted group with a confirm/cancel panel."""
    rows = [
        f"👤 Account : {phone}",
        f"👥 Group : {name or guid}",
        f"🆔 {guid}",
        "⛔ این گروه اکانت رو بن/سکوت کرده (ارسال ممکن نیست).",
        "می‌خوای ازش خارج بشه؟",
        f"🕒 {now()}",
    ]
    try:
        await bot.send_message(
            config.LOG_GROUP_ID, card("🧹 موتور پاکسازی — گروه بن/سکوت", rows),
            buttons=[[Button.inline("✅ تأیید خروج",
                                    f"clnyes_{account_id}_{guid}".encode())],
                     [Button.inline("🚫 لغو (بمونه)",
                                    f"clnno_{account_id}_{guid}".encode())]])
    except Exception as e:  # noqa: BLE001
        print(f"[cleanup log] {e}")


@bot.on(events.CallbackQuery(pattern=b"clnyes_(\\d+)_(.+)"))
async def cleanup_confirm_cb(event):
    """Owner confirmed leaving a banned/muted group."""
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    guid = event.pattern_match.group(2).decode()
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    await event.answer("در حال خروج از گروه ...")
    phone = acc["phone"]
    ok = False
    try:
        w = worker.worker_for_account(acc)
        if w and not worker.is_local(w):
            res = await worker.api_call(w, "POST", "/group/leave",
                                        {"phone": phone, "group_guid": guid},
                                        timeout=90)
            ok = bool(res.get("ok"))
        else:
            await account_conn.call(phone, rb.leave_group, guid, timeout=60)
            ok = True
    except Exception as e:  # noqa: BLE001
        await safe_edit(event, f"❌ خروج ناموفق: {repr(e)[:120]}")
        return
    db.remove_cleanup_candidate(account_id, guid)
    await safe_edit(event, card("🧹 موتور پاکسازی", [
        f"👤 Account : {phone}",
        f"👥 Group : {guid}",
        ("✅ از گروه خارج شد." if ok else "⚠️ خروج نامشخص بود."),
        f"🕒 {now()}",
    ]))


@bot.on(events.CallbackQuery(pattern=b"clnno_(\\d+)_(.+)"))
async def cleanup_cancel_cb(event):
    """Owner chose to keep the group; just drop it from the candidate list."""
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    guid = event.pattern_match.group(2).decode()
    db.remove_cleanup_candidate(account_id, guid)
    await safe_edit(event, card("🧹 موتور پاکسازی", [
        f"👥 Group : {guid}",
        "🚫 لغو شد — گروه می‌مونه (دیگه تو این لیست نیست).",
        f"🕒 {now()}",
    ]))


@bot.on(events.CallbackQuery(pattern=b"acc_(\\d+)"))
async def account_menu_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    status = "فعال ✅" if acc["status"] == "active" else "غیرفعال ⚠️ (سشن باطل)"
    text = card("👤 اکانت", [
        f"📛 نام : {acc['name'] or '-'}",
        f"📱 شماره : {acc['phone']}",
        f"🆔 آیدی : {acc['user_id']}",
        f"⭐️ وضعیت : {status}",
    ])
    buttons = []
    if acc["status"] != "active":
        buttons.append([Button.inline("🔁 لاگین مجدد (ریکاوری سشن)",
                                      f"relogin_{account_id}".encode())])
    buttons += [
        [Button.inline("🚀 ارسال", f"send_{account_id}".encode()),
         Button.inline("📢 کانال", f"chan_{account_id}".encode())],
        [Button.inline("🗑 حذف اکانت", f"del_{account_id}".encode())],
        [Button.inline("🔙 بازگشت", b"accounts")],
    ]
    await safe_edit(event, text, buttons=buttons)


@bot.on(events.CallbackQuery(pattern=b"del_(\\d+)"))
async def delete_confirm_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    await safe_edit(event, 
        "از حذف این اکانت مطمئنی؟",
        buttons=[[Button.inline("✅ بله، حذف کن", f"delyes_{account_id}".encode())],
                 [Button.inline("🔙 خیر", f"acc_{account_id}".encode())]],
    )


@bot.on(events.CallbackQuery(pattern=b"delyes_(\\d+)"))
async def delete_do_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    db.delete_account(account_id)
    await safe_edit(event, "اکانت حذف شد. ✅",
                     buttons=[[Button.inline("🔙 بازگشت", b"accounts")]])


@bot.on(events.CallbackQuery(pattern=b"relogin_(\\d+)"))
async def relogin_cb(event):
    """Re-login an account whose session was invalidated (device kicked out).
    Reuses the normal login flow; on success its active features auto-recover."""
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    phone = acc["phone"]
    await safe_edit(event, "⏳ در حال آماده‌سازی لاگین مجدد ...")
    w = worker.worker_for_account(acc) or worker.ensure_master_worker()
    if w and not worker.is_local(w):
        # health-check the owning worker first, like the normal remote login
        try:
            await worker.check_worker(w)
        except Exception:
            pass
        w = db.get_worker(w["id"])
        if not (w and w["enabled"] and w["status"] == "ok"):
            await safe_edit(event,
                "❌ ورکر این اکانت الان سالم نیست. اول وضعیت ورکر رو درست کن.",
                buttons=[[Button.inline("🔙 بازگشت", f"acc_{account_id}".encode())]])
            return
        await handle_phone_remote(event, phone, w)
    else:
        await _begin_local_login(event, phone, w)


async def _recover_account_features(account_id: int, settle_delay: float = 0.0):
    """After a (re)login, relaunch every always-on feature that is still marked
    enabled for this account, so recovery is automatic.

    ``settle_delay``: wait this many seconds before relaunching, so a freshly
    created session has time to fully settle on Rubika's side. Relaunching heavy
    activity on a brand-new session immediately can make Rubika reject it with a
    (transient) INVALID_AUTH right after adding the account.
    """
    if settle_delay:
        await asyncio.sleep(settle_delay)
    acc = db.get_account(account_id)
    if not acc:
        return
    if automation_on(account_id):
        try:
            await start_automation(acc)
        except Exception as e:  # noqa: BLE001
            await log(f"⚠️ ریکاوری اتومیشن {acc['phone']} ناموفق: {repr(e)[:120]}")
    if secretary_on(account_id):
        try:
            await start_secretary(acc)
        except Exception as e:  # noqa: BLE001
            await log(f"⚠️ ریکاوری منشی {acc['phone']} ناموفق: {repr(e)[:120]}")
    if channelreport_on(account_id):
        try:
            await start_channelreport(acc)
        except Exception as e:  # noqa: BLE001
            await log(f"⚠️ ریکاوری گزارش‌کانال {acc['phone']} ناموفق: {repr(e)[:120]}")
    if reply_on(account_id):
        try:
            await start_reply(acc)
        except Exception as e:  # noqa: BLE001
            await log(f"⚠️ ریکاوری ریپلای {acc['phone']} ناموفق: {repr(e)[:120]}")
    if automation_on(account_id) or secretary_on(account_id) or \
            channelreport_on(account_id) or reply_on(account_id):
        await log(card("♻️ FEATURES RECOVERED", [
            f"👤 Account : {acc['phone']}",
            "قابلیت‌های فعالِ این اکانت بعد از لاگین مجدد دوباره راه افتادن.",
            f"🕒 {now()}"]))


# --------------------------------------------------------------------------- #
# Speed (delay) setting
# --------------------------------------------------------------------------- #
def speed_buttons():
    return [
        [Button.inline("0.2s", b"sp_0.2"), Button.inline("0.5s", b"sp_0.5"),
         Button.inline("1s", b"sp_1")],
        [Button.inline("2s", b"sp_2"), Button.inline("5s", b"sp_5"),
         Button.inline("10s", b"sp_10")],
        [Button.inline("🔙 بازگشت", b"home")],
    ]


@bot.on(events.CallbackQuery(data=b"speed"))
async def speed_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_delay"}
    await safe_edit(event, 
        f"⏱ تأخیر فعلی: {db.get_delay()} ثانیه\n{LINE}\n"
        "یک سرعت انتخاب کن، یا یک عدد بین ۰.۲ تا ۱۰ بفرست:",
        buttons=speed_buttons(),
    )


@bot.on(events.CallbackQuery(pattern=b"sp_([0-9.]+)"))
async def speed_set_cb(event):
    if not is_owner(event):
        return
    value = config.clamp_delay(event.pattern_match.group(1).decode())
    db.set_delay(value)
    state.pop(event.sender_id, None)
    await safe_edit(event, f"✅ تأخیر روی {value} ثانیه تنظیم شد.",
                     buttons=[[Button.inline("🔙 منوی اصلی", b"home")]])


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"backup"))
async def backup_cb(event):
    if not is_owner(event):
        return
    await event.answer("در حال ساخت بکاپ کامل ...")
    try:
        archive = await build_backup_archive()
    except Exception as e:  # noqa: BLE001
        await event.answer(f"خطا در ساخت بکاپ: {repr(e)[:120]}", alert=True)
        return
    if not archive:
        await event.answer("هنوز چیزی برای بکاپ وجود ندارد.", alert=True)
        return
    try:
        await bot.send_file(
            event.sender_id, archive,
            caption=("💾 بکاپ کامل • " + now() +
                     "\nشامل: دیتابیس + سشن همه‌ی اکانت‌ها + شمارنده"),
            force_document=True,
        )
        await event.answer("بکاپ ارسال شد.")
    finally:
        try:
            os.remove(archive)
        except Exception:
            pass


def _add_dir_to_zip(zf: zipfile.ZipFile, src_dir: str, arc_prefix: str):
    """Recursively add every file under src_dir into the zip under arc_prefix/."""
    if not os.path.isdir(src_dir):
        return
    for root, _dirs, files in os.walk(src_dir):
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, src_dir)
            zf.write(full, arcname=os.path.join(arc_prefix, rel))


async def _add_worker_sessions(zf: zipfile.ZipFile):
    """Worker-aware hook.

    When the Worker subsystem exists, this pulls each registered worker's
    session files over its SSH tunnel and stores them under
    `sessions/<worker_tag>/` inside the same archive. It is a safe no-op until
    the Worker module is added, so the backup never breaks.
    """
    try:
        import worker  # added together with the Worker subsystem
    except ImportError:
        return
    try:
        await worker.collect_sessions_into_zip(zf)  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        await log(f"⚠️ بکاپ سشن ورکرها ناقص ماند: {repr(e)[:150]}")


async def build_backup_archive():
    """Bundle the master DB + all local session files + counter into one zip.

    Returns the path to a temporary .zip (caller deletes it) or None if there
    is nothing to back up.
    """
    has_db = os.path.exists(db.DB_PATH)
    has_sessions = os.path.isdir(rb.SESSIONS_DIR) and any(os.scandir(rb.SESSIONS_DIR))
    if not has_db and not has_sessions:
        return None

    fd, zip_path = tempfile.mkstemp(prefix="rubika_backup_", suffix=".zip", dir=DATA_DIR)
    os.close(fd)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if has_db:
            zf.write(db.DB_PATH, arcname="data.db")
        if os.path.exists(COUNTER_FILE):
            zf.write(COUNTER_FILE, arcname="send_count.txt")
        # local session files (master-side accounts)
        _add_dir_to_zip(zf, rb.SESSIONS_DIR, "sessions/local")
        # worker session files (no-op until the Worker subsystem is added)
        await _add_worker_sessions(zf)
    return zip_path


# --------------------------------------------------------------------------- #
# Send menu (pick which account)
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"send_menu"))
async def send_menu_cb(event):
    if not is_owner(event):
        return
    accounts = db.list_accounts()
    if not accounts:
        await safe_edit(event, "اول یک اکانت اضافه کن.",
                         buttons=[[Button.inline("➕ افزودن اکانت", b"add_account")],
                                  [Button.inline("🔙 بازگشت", b"home")]])
        return
    buttons = [[Button.inline(f"🚀 {a['phone']}", f"sm_{a['id']}".encode())]
               for a in accounts]
    buttons.append([Button.inline("🔙 بازگشت", b"home")])
    await safe_edit(event, "با کدوم اکانت ارسال بشه؟", buttons=buttons)


@bot.on(events.CallbackQuery(pattern=b"sm_(\\d+)"))
async def send_mode_cb(event):
    """Choose HOW to send with this account: normal forward, or channel mode."""
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    await safe_edit(event, 
        f"📤 نوع ارسال با اکانت {acc['phone']} رو انتخاب کن:",
        buttons=[
            [Button.inline("🚀 ارسال معمولی (به مخاطبین)", f"send_{account_id}".encode())],
            [Button.inline("📢 ارسال به شیوه کانال", f"chan_{account_id}".encode())],
            [Button.inline("🔙 بازگشت", b"send_menu")],
        ],
    )


# --------------------------------------------------------------------------- #
# Message router (conversation steps)
# --------------------------------------------------------------------------- #
@bot.on(events.NewMessage)
async def message_router(event):
    if not is_owner(event):
        return
    if event.raw_text.startswith("/start"):
        return
    st = state.get(event.sender_id)
    if not st:
        return
    step = st.get("step")
    if step == "await_phone":
        await handle_phone(event)
    elif step == "await_code":
        await handle_code(event)
    elif step == "await_password":
        await handle_password(event)
    elif step == "await_delay":
        await handle_delay(event)
    elif step == "await_marker":
        await handle_marker(event)
    elif step == "await_channel_name":
        await handle_channel_name(event)
    elif step == "await_auto_text":
        await handle_auto_text(event)
    elif step == "await_auto_interval":
        await handle_auto_interval(event)
    elif step == "await_auto_link":
        await handle_auto_link(event)
    elif step == "await_admin_id":
        await handle_admin_id(event)
    elif step == "await_sec_text":
        await handle_sec_text(event)
    elif step == "await_sec_interval":
        await handle_sec_interval(event)
    elif step == "await_cr_channel":
        await handle_cr_channel(event)
    elif step == "await_cr_interval":
        await handle_cr_interval(event)
    elif step == "await_rp_text":
        await handle_rp_text(event)
    elif step == "await_rp_delay":
        await handle_rp_delay(event)
    elif step == "await_psync":
        await handle_psync_input(event)
    elif step == "await_bc_title":
        await handle_bc_title(event)
    elif step == "await_bc_target":
        await handle_bc_target(event)
    elif step == "await_bc_gap":
        await handle_bc_gap(event)
    elif step in ("wk_ip", "wk_port", "wk_user", "wk_pass"):
        await handle_worker_step(event, step)


async def handle_delay(event):
    value = config.clamp_delay(event.raw_text.strip())
    db.set_delay(value)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ تأخیر روی {value} ثانیه تنظیم شد.",
                        buttons=main_menu(is_real_owner(event)))


async def handle_phone(event):
    phone = event.raw_text.strip()
    await event.respond("⏳ در حال انتخاب ورکر سالم و اتصال به روبیکا ...")
    # Pick the worker that will OWN this account (round-robin + health check).
    try:
        w = await worker.pick_worker_for_login()
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ خطا در انتخاب ورکر: {repr(e)[:150]}")
        return
    if not w:
        await event.respond(
            "❌ هیچ ورکر سالمی در دسترس نیست.\n"
            "از «🛠 مدیریت ورکر» وضعیت رو چک کن یا یک ورکر اضافه کن.")
        return
    if not worker.is_local(w):
        await handle_phone_remote(event, phone, w)
        return
    await _begin_local_login(event, phone, w)


async def _begin_local_login(event, phone, w):
    """Local (master) login flow — shared by first-time add AND re-login."""
    # closing any warm connection guarantees the fresh login isn't fighting an
    # old socket for the same session (Feature 6).
    try:
        await account_conn.close(phone)
    except Exception:
        pass
    # ----- LOCAL master worker: ORIGINAL login logic, unchanged -------------
    try:
        ctx = await rb.start_login(phone)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ خطا در ارسال کد: {e}\nدوباره شماره را بفرست یا لغو کن.")
        return
    ctx["worker"] = w
    pending[event.sender_id] = ctx
    status = str(ctx.get("status") or "").upper()
    if "PASS" in status:
        hint = ctx.get("hint") or ""
        state[event.sender_id] = {"step": "await_password"}
        await event.respond(
            "🔐 این اکانت رمز دومرحله‌ای دارد." + (f"\nراهنما: {hint}" if hint else "") +
            "\nرمز را بفرست.",
            buttons=[[Button.inline("🔙 لغو", b"cancel")]],
        )
        return
    if not ctx.get("phone_code_hash"):
        try:
            await ctx["client"].disconnect()
        except Exception:
            pass
        pending.pop(event.sender_id, None)
        await event.respond(f"❌ روبیکا کد نفرستاد (status: {status or 'نامشخص'}). دوباره تلاش کن.")
        return
    state[event.sender_id] = {"step": "await_code"}
    await event.respond("📩 کد ورود در اپ روبیکا اومد. کد رو بفرست.",
                        buttons=[[Button.inline("🔙 لغو", b"cancel")]])


async def handle_code(event):
    ctx = pending.get(event.sender_id)
    if not ctx:
        state.pop(event.sender_id, None)
        return
    if ctx.get("remote"):
        await handle_code_remote(event, ctx)
        return
    code = "".join(ch for ch in event.raw_text if ch.isdigit())
    try:
        await rb.finish_login(ctx, code)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ کد اشتباه یا خطا: {e}\nدوباره کد را بفرست یا لغو کن.")
        return
    await complete_account(event)


async def handle_password(event):
    ctx = pending.get(event.sender_id)
    if not ctx:
        state.pop(event.sender_id, None)
        return
    if ctx.get("remote"):
        await handle_password_remote(event, ctx)
        return
    password = event.raw_text.strip()
    try:
        new_ctx = await rb.start_login(ctx["phone"], pass_key=password)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ رمز اشتباه یا خطا: {e}\nدوباره رمز را بفرست.")
        return
    pending[event.sender_id] = new_ctx
    state[event.sender_id] = {"step": "await_code"}
    await event.respond("🔓 رمز پذیرفته شد. حالا کد ورود را بفرست.",
                        buttons=[[Button.inline("🔙 لغو", b"cancel")]])


async def complete_account(event):
    ctx = pending.pop(event.sender_id, None)
    state.pop(event.sender_id, None)
    if not ctx:
        return
    client = ctx["client"]
    phone = ctx["phone"]
    w = ctx.get("worker") or worker.ensure_master_worker() or {}
    wtag = w.get("tag", "-")
    try:
        me = await client.get_me()
        guid = rb._guid_of(me) or "-"
        name = rb._name_of(me)
        ordered, stats = await rb.get_ordered_recipients(client)
        account_id = db.add_account(phone, name, str(guid), rb.session_path(phone))
        if w.get("id"):
            db.set_account_worker(account_id, w["id"])

        await log(card("LOGIN SUCCESS ✅", [
            f"This Account : {phone}",
            LINE,
            f"Name : {name}",
            f"ID   : {guid}",
            LINE,
            f"📇 Contacts : {stats['contacts']}",
            f"👥 Groups   : {stats['groups']}",
            f"🎯 Contact with chat : {stats['with_chat']}",
            LINE,
            f"👨‍🔧 Worker : {wtag}",
        ]))
        await event.respond(
            "✅ اکانت با موفقیت اضافه شد!\n"
            f"👤 {name} | 📱 {phone}\n"
            f"📇 مخاطبین: {stats['contacts']} | 👥 گروه‌ها: {stats['groups']} | "
            f"💬 چت‌دار: {stats['with_chat']}",
            buttons=[[Button.inline("🚀 ارسال", f"send_{account_id}".encode())],
                     [Button.inline("🏠 منوی اصلی", b"home")]],
        )
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ خطا بعد از ورود: {e}")
        account_id = None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    # re-login recovery runs ONLY after the login client is fully disconnected,
    # so the recovered features never open a second connection alongside it. We
    # run it in the BACKGROUND with a settle delay so the freshly created
    # session has time to stabilise on Rubika's side (relaunching heavy activity
    # immediately can trigger a transient INVALID_AUTH right after adding).
    if account_id:
        try:
            account_conn.reset_invalid(phone)
            asyncio.create_task(_recover_account_features(account_id, settle_delay=8.0))
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Send: prepare -> confirm -> run
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(pattern=b"send_(\\d+)"))
async def send_prepare_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    if continuous_busy(account_id):
        await safe_edit(event,
            "🔁 یک قابلیت اتومیشن (اتومیشن/منشی/ریپلای/گزارش) روی این اکانت روشنه. "
            "اول از بخش «🔁 اتومیشن» خاموشش کن، بعد ارسال بزن.",
            buttons=[[Button.inline("🔙 بازگشت", f"acc_{account_id}".encode())]])
        return
    marker = db.get_marker()
    # Route to the worker that OWNS this account (session affinity).
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        await send_prepare_remote(event, acc, w, marker)
        return
    await safe_edit(event, "⏳ در حال آماده‌سازی (اتصال، پیدا کردن پیام نشان‌دار، خواندن مخاطب‌ها) ...")

    await account_conn.close(acc["phone"])   # ensure single connection (Feature 6)
    client = rb.open_client(acc["phone"])
    try:
        await rb.connect_ready(client)
        saved_guid, mid = await rb.find_marked_message(client, marker)
        if not mid:
            await safe_edit(event, 
                f"❌ توی Saved Messages پیامی با مارکر «{marker}» پیدا نشد.\n"
                "یه پیام (متن/عکس/فایل) توی Saved Messages بذار که آخر کپشنش این مارکر باشه.",
                buttons=[[Button.inline("🔙 بازگشت", f"acc_{account_id}".encode())]],
            )
            return
        ordered, stats = await rb.get_ordered_recipients(client)
    except Exception as e:  # noqa: BLE001
        await safe_edit(event, f"❌ خطا در آماده‌سازی: {e}",
                         buttons=[[Button.inline("🔙 بازگشت", f"acc_{account_id}".encode())]])
        return
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    if not ordered:
        await safe_edit(event, "هیچ مخاطبی برای ارسال پیدا نشد.",
                         buttons=[[Button.inline("🔙 بازگشت", f"acc_{account_id}".encode())]])
        return

    pending_send[event.sender_id] = {
        "account_id": account_id,
        "phone": acc["phone"],
        "saved_guid": saved_guid,
        "mid": mid,
        "recipients": [r["guid"] for r in ordered],
    }

    await safe_edit(event, 
        card("🚀 آماده‌ی ارسال", [
            f"📎 محتوا : پیام نشان‌دار «{marker}» ✅",
            f"🎯 گیرنده‌ها : {len(ordered)} مخاطب",
            "ترتیب : چت‌دار ← آنلاین ← Last Seen",
            LINE,
            "به این مخاطب‌ها ارسال بشه؟",
        ]),
        buttons=[[Button.inline("✅ تأیید و ارسال", f"go_{account_id}".encode())],
                 [Button.inline("🔙 لغو", f"acc_{account_id}".encode())]],
    )


@bot.on(events.CallbackQuery(pattern=b"go_(\\d+)"))
async def send_go_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    payload = pending_send.get(event.sender_id)
    if not payload or payload["account_id"] != account_id:
        await event.answer("اطلاعات ارسال منقضی شده. دوباره «ارسال» رو بزن.", alert=True)
        return
    stop_flags[account_id] = False
    total = payload.get("total")
    if total is None:
        total = len(payload.get("recipients", []))
    await safe_edit(event, 
        f"⏳ شروع ارسال به {total} مخاطب ... گزارش‌ها در گروه لاگ میاد.",
        buttons=[[Button.inline("⏹ توقف ارسال", f"stop_{account_id}".encode())]],
    )
    # run the send in the background so the handler returns quickly
    if payload.get("remote"):
        asyncio.create_task(run_send_remote(event.sender_id, payload))
    else:
        asyncio.create_task(run_send(event.sender_id, payload))


@bot.on(events.CallbackQuery(pattern=b"stop_(\\d+)"))
async def stop_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    stop_flags[account_id] = True
    await event.answer("درخواست توقف ثبت شد. بعد از پیام جاری متوقف می‌شود.", alert=True)


async def run_send(owner_id: int, payload: dict):
    account_id = payload["account_id"]
    phone = payload["phone"]
    saved_guid = payload["saved_guid"]
    mid = payload["mid"]
    recipients = payload["recipients"]
    marker = db.get_marker()
    delay = db.get_delay()

    count = _next_counter()
    total = len(recipients)
    ok = 0
    fail = 0
    started = datetime.now()
    reason = None
    active_jobs.add(account_id)

    await log(card("SEND STARTED 🚀", [
        f"🛠 Count : {count:03d}",
        f"📱 Phone : {phone}",
        f"🕒 Started : {now()}",
        LINE,
        f"🎯 Targets : {total}",
        f"⏱ Delay : {delay}s",
        f"📌 Marker : «{marker}» Found ✅",
    ]))

    n = total
    idx = 0
    retry_count = 0
    await account_conn.close(phone)          # ensure single connection (Feature 6)
    client = rb.open_client(phone)
    try:
        await rb.connect_ready(client)
        while True:
            attempt_fail = 0
            hit_max = False
            while idx < n:
                if stop_flags.get(account_id):
                    reason = "توقف دستی توسط کاربر"
                    break
                guid = recipients[idx]
                idx += 1
                try:
                    await asyncio.wait_for(
                        rb.forward_message(client, saved_guid, guid, mid),
                        timeout=config.SEND_TIMEOUT,
                    )
                    ok += 1
                except Exception as e:  # noqa: BLE001
                    fail += 1
                    attempt_fail += 1
                    await log(card("⚠️ SEND ERROR", [
                        f"📱 Phone : {phone}",
                        f"🎯 To : {guid}",
                        f"💥 Error : {repr(e)[:200]}",
                    ]))
                    if attempt_fail >= config.MAX_ERRORS:
                        hit_max = True
                        break
                await asyncio.sleep(delay)

            if reason:                       # manual stop
                break
            if not hit_max:                  # whole list finished
                break
            if retry_count >= config.RESUME_MAX_RETRIES:
                reason = f"رسیدن به سقف خطا ({config.MAX_ERRORS})"
                break

            # ---- auto-resume: wait, then continue from the rest of the list ----
            retry_count += 1
            remaining = max(0, total - ok - fail)
            await log(card(f"🚨 ALERT 5 MINUTE {_alert_word(retry_count)}", [
                f"✅ {ok}",
                f"⏳ {remaining}",
                f"👤 Account : {phone}",
            ]))
            if await _wait_or_stop(account_id, config.RESUME_WAIT):
                reason = "توقف دستی توسط کاربر"
                break
            try:
                await client.disconnect()
            except Exception:
                pass
            client = rb.open_client(phone)
            await rb.connect_ready(client)
    except Exception as e:  # noqa: BLE001
        reason = f"خطای کلی: {repr(e)[:200]}"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        active_jobs.discard(account_id)

    dur = str(datetime.now() - started).split(".")[0]
    pending_send.pop(owner_id, None)

    if reason:
        await log(card("⛔ SEND STOPPED", [
            f"👤 Account : {phone}",
            f"📊 ✅ {ok}   ❌ {fail}   📁 {total}",
            f"⚠️ Reason : {reason}",
            f"⏱ Duration : {dur}",
            f"🕒 {now()}",
        ]))
        try:
            await bot.send_message(owner_id, f"⛔ ارسال متوقف شد. ✅ {ok} / ❌ {fail} از {total}\nدلیل: {reason}",
                                   buttons=main_menu(owner_id == config.OWNER_ID))
        except Exception:
            pass
    else:
        await log(card("SEND FINISHED ✅", [
            "🟢 Status : Completed",
            f"👤 Account : {phone}",
            LINE,
            f"✅ {ok}   ❌ {fail}   📁 {total}",
            f"⏱ Duration : {dur}",
        ]))
        try:
            await bot.send_message(owner_id, f"✅ ارسال تمام شد. ✅ {ok} / ❌ {fail} از {total}",
                                   buttons=main_menu(owner_id == config.OWNER_ID))
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Channel send mode: create a channel, forward the marked file into it, then
# add the account's own contacts as members (in batches, up to a target).
# Works for both local (master) accounts and accounts owned by a remote worker.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(pattern=b"chan_(\\d+)"))
async def channel_start_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    if continuous_busy(account_id):
        await event.answer("🔁 یک قابلیت اتومیشن روی این اکانت روشنه. اول خاموشش کن.",
                           alert=True)
        return
    state[event.sender_id] = {"step": "await_channel_name", "account_id": account_id}
    await safe_edit(event, 
        "📢 اسم کانالی که می‌خوای ساخته بشه رو بفرست:\nمثال: `تست ۱`",
        buttons=[[Button.inline("🔙 لغو", f"acc_{account_id}".encode())]],
    )


async def handle_channel_name(event):
    st = state.get(event.sender_id)
    if not st:
        return
    account_id = st.get("account_id")
    name = event.raw_text.strip()
    state.pop(event.sender_id, None)
    if not name:
        await event.respond("اسم کانال نمی‌تونه خالی باشه. دوباره از «ارسال کانالی» شروع کن.",
                            buttons=main_menu(is_real_owner(event)))
        return
    acc = db.get_account(account_id)
    if not acc:
        await event.respond("اکانت پیدا نشد.", buttons=main_menu(is_real_owner(event)))
        return
    marker = db.get_marker()
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        await channel_create_remote(event, acc, w, name, marker)
    else:
        await channel_create_local(event, acc, name, marker)


def _channel_ready_buttons(account_id):
    return [[Button.inline("👥 شروع عضو کردن مخاطبین", f"chadd_{account_id}".encode())],
            [Button.inline("🏠 منوی اصلی", b"home")]]


def _channel_ready_card(name, marker, forwarded):
    return card("📢 کانال ساخته شد ✅", [
        f"🎛 کانال : {name}",
        (f"📎 فایل نشان‌دار «{marker}» ارسال شد ✅" if forwarded
         else f"⚠️ فایل نشان‌دار «{marker}» ارسال نشد (کانال ساخته شد)"),
        LINE,
        f"حالا می‌تونی مخاطب‌ها رو {config.CHANNEL_ADD_BATCH}تا‌{config.CHANNEL_ADD_BATCH}تا "
        f"تا سقف {config.CHANNEL_MEMBER_TARGET} عضو کنی.",
    ])


async def channel_create_local(event, acc, name, marker):
    msg = await event.respond(f"⏳ در حال ساخت کانال «{name}» و ارسال فایل نشان‌دار ...")
    await account_conn.close(acc["phone"])   # ensure single connection (Feature 6)
    client = rb.open_client(acc["phone"])
    channel_guid = None
    forwarded = False
    try:
        await rb.connect_ready(client)
        saved_guid, mid = await rb.find_marked_message(client, marker)
        channel_guid = await rb.create_channel(client, name)
        if mid:
            try:
                await rb.forward_message(client, saved_guid, channel_guid, mid)
                forwarded = True
            except Exception:
                forwarded = False
    except Exception as e:  # noqa: BLE001
        await safe_edit(msg, f"❌ خطا در ساخت کانال: {repr(e)[:160]}",
                       buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
        try:
            await client.disconnect()
        except Exception:
            pass
        return
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    pending_channel[event.sender_id] = {
        "account_id": acc["id"], "phone": acc["phone"], "channel_name": name,
        "channel_guid": channel_guid, "remote": False,
    }
    await safe_edit(msg, _channel_ready_card(name, marker, forwarded),
                   buttons=_channel_ready_buttons(acc["id"]))


async def channel_create_remote(event, acc, w, name, marker):
    msg = await event.respond(f"⏳ بررسی ورکر {w['tag']} و ساخت کانال «{name}» ...")
    try:
        await worker.check_worker(w)
    except Exception:
        pass
    w = db.get_worker(w["id"])
    if not (w and w["enabled"] and w["status"] == "ok"):
        await safe_edit(msg, 
            f"❌ ورکر {w['tag'] if w else '?'} الان سالم/فعال نیست"
            f" (وضعیت: {w['status'] if w else 'نامشخص'}).\n"
            "این اکانت روی همین ورکر لاگین شده و فقط از همین‌جا می‌تونه کانال بسازه.",
            buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
        return
    try:
        res = await worker.api_call(w, "POST", "/channel/create",
                                    {"phone": acc["phone"], "marker": marker,
                                     "title": name}, timeout=120)
    except Exception as e:  # noqa: BLE001
        await safe_edit(msg, f"❌ خطا در ساخت کانال روی ورکر: {repr(e)[:150]}",
                       buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
        return
    if not res.get("ok") or not res.get("channel_guid"):
        await safe_edit(msg, "❌ ساخت کانال روی ورکر ناموفق بود.",
                       buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
        return
    pending_channel[event.sender_id] = {
        "account_id": acc["id"], "phone": acc["phone"], "channel_name": name,
        "channel_guid": res["channel_guid"], "remote": True, "worker_id": w["id"],
    }
    await safe_edit(msg, _channel_ready_card(name, marker, res.get("forwarded")),
                   buttons=_channel_ready_buttons(acc["id"]))


@bot.on(events.CallbackQuery(pattern=b"chadd_(\\d+)"))
async def channel_add_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    payload = pending_channel.get(event.sender_id)
    if not payload or payload["account_id"] != account_id:
        await event.answer("اطلاعات کانال منقضی شده. دوباره از «ارسال کانالی» شروع کن.",
                           alert=True)
        return
    await safe_edit(event, 
        f"⏳ شروع عضو کردن مخاطبین (دسته‌های {config.CHANNEL_ADD_BATCH}تایی تا سقف "
        f"{config.CHANNEL_MEMBER_TARGET}) ... گزارش در گروه لاگ میاد.")
    if payload.get("remote"):
        asyncio.create_task(run_channel_add_remote(event.sender_id, payload))
    else:
        asyncio.create_task(run_channel_add_local(event.sender_id, payload))


def _channel_done_card(phone, name, added):
    return card("⏳ CHANNEL WILL BE CREATED", [
        f"☎️ACCOUNT : {phone}",
        f"🎛CHANNEL : {name}",
        f"✅ADD : {added}",
        LINE,
        f"⏰ : {now()}",
    ])


async def run_channel_add_local(owner_id: int, payload: dict):
    phone = payload["phone"]
    name = payload["channel_name"]
    channel_guid = payload["channel_guid"]
    added = 0
    await account_conn.close(phone)          # ensure single connection (Feature 6)
    client = rb.open_client(phone)
    try:
        await rb.connect_ready(client)
        added = await rb.seed_channel_with_contacts(
            client, channel_guid,
            target=config.CHANNEL_MEMBER_TARGET,
            batch=config.CHANNEL_ADD_BATCH,
            delay=config.CHANNEL_ADD_DELAY)
    except Exception as e:  # noqa: BLE001
        await log(f"⚠️ عضو کردن مخاطبین کانال «{name}» ناقص ماند: {repr(e)[:150]}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    pending_channel.pop(owner_id, None)
    await log(_channel_done_card(phone, name, added))
    try:
        await bot.send_message(owner_id,
                               f"✅ عضو کردن مخاطبین کانال «{name}» تمام شد. تعداد: {added}",
                               buttons=main_menu(owner_id == config.OWNER_ID))
    except Exception:
        pass


async def run_channel_add_remote(owner_id: int, payload: dict):
    phone = payload["phone"]
    name = payload["channel_name"]
    w = db.get_worker(payload["worker_id"])
    added = 0
    if not w:
        await log("⛔ ورکر صاحب این کانال پیدا نشد.")
        pending_channel.pop(owner_id, None)
        return
    try:
        # member-adding can take a while (batches + delays) -> generous timeout
        res = await worker.api_call(w, "POST", "/channel/add", {
            "phone": phone, "channel_guid": payload["channel_guid"],
            "target": config.CHANNEL_MEMBER_TARGET,
            "batch": config.CHANNEL_ADD_BATCH,
            "delay": config.CHANNEL_ADD_DELAY,
        }, timeout=600)
        added = res.get("added", 0)
    except Exception as e:  # noqa: BLE001
        await log(f"⚠️ عضو کردن مخاطبین کانال «{name}» روی ورکر ناقص ماند: {repr(e)[:150]}")
    pending_channel.pop(owner_id, None)
    await log(_channel_done_card(phone, name, added))
    try:
        await bot.send_message(owner_id,
                               f"✅ عضو کردن مخاطبین کانال «{name}» تمام شد. تعداد: {added}",
                               buttons=main_menu(owner_id == config.OWNER_ID))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Remote login relay (account lives on a remote worker)
# --------------------------------------------------------------------------- #
async def handle_phone_remote(event, phone, w):
    try:
        res = await worker.api_call(w, "POST", "/login/start", {"phone": phone})
    except Exception as e:  # noqa: BLE001
        pending.pop(event.sender_id, None)
        await event.respond(f"❌ ارتباط با ورکر {w['tag']} برقرار نشد: {repr(e)[:150]}")
        return
    pending[event.sender_id] = {"remote": True, "worker": w, "phone": phone}
    if res.get("needs_password"):
        state[event.sender_id] = {"step": "await_password"}
        await event.respond("🔐 این اکانت رمز دومرحله‌ای دارد. رمز را بفرست.",
                            buttons=[[Button.inline("🔙 لغو", b"cancel")]])
        return
    if res.get("needs_code"):
        state[event.sender_id] = {"step": "await_code"}
        await event.respond(f"📩 کد ورود اومد (ورکر {w['tag']}). کد رو بفرست.",
                            buttons=[[Button.inline("🔙 لغو", b"cancel")]])
        return
    pending.pop(event.sender_id, None)
    await event.respond(f"❌ ورکر کد نفرستاد (status: {res.get('status')}). دوباره تلاش کن.")


async def handle_code_remote(event, ctx):
    w = ctx["worker"]
    code = "".join(ch for ch in event.raw_text if ch.isdigit())
    try:
        res = await worker.api_call(w, "POST", "/login/code",
                                    {"phone": ctx["phone"], "code": code}, timeout=120)
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ کد اشتباه یا خطا: {repr(e)[:150]}\nدوباره کد را بفرست یا لغو کن.")
        return
    if not res.get("ok"):
        await event.respond("❌ ورود ناموفق بود. دوباره تلاش کن یا لغو کن.")
        return
    await complete_account_remote(event, ctx, res)


async def handle_password_remote(event, ctx):
    w = ctx["worker"]
    password = event.raw_text.strip()
    try:
        await worker.api_call(w, "POST", "/login/password",
                              {"phone": ctx["phone"], "password": password})
    except Exception as e:  # noqa: BLE001
        await event.respond(f"❌ رمز اشتباه یا خطا: {repr(e)[:150]}\nدوباره رمز را بفرست.")
        return
    state[event.sender_id] = {"step": "await_code"}
    await event.respond("🔓 رمز پذیرفته شد. حالا کد ورود را بفرست.",
                        buttons=[[Button.inline("🔙 لغو", b"cancel")]])


async def complete_account_remote(event, ctx, res):
    pending.pop(event.sender_id, None)
    state.pop(event.sender_id, None)
    w = ctx["worker"]
    phone = res.get("phone") or ctx["phone"]
    name = res.get("name", "-")
    guid = res.get("guid", "-")
    contacts = res.get("contacts", 0)
    groups = res.get("groups", 0)
    with_chat = res.get("with_chat", 0)
    # session file lives ON THE WORKER, so store an empty local session path.
    account_id = db.add_account(phone, name, str(guid), "")
    db.set_account_worker(account_id, w["id"])

    # re-login recovery: relaunch any always-on feature this account had.
    try:
        account_conn.reset_invalid(phone)
        await _recover_account_features(account_id)
    except Exception:
        pass

    await log(card("LOGIN SUCCESS ✅", [
        f"This Account : {phone}",
        LINE,
        f"Name : {name}",
        f"ID   : {guid}",
        LINE,
        f"📇 Contacts : {contacts}",
        f"👥 Groups   : {groups}",
        f"🎯 Contact with chat : {with_chat}",
        LINE,
        f"👨‍🔧 Worker : {w['tag']}",
    ]))
    await event.respond(
        f"✅ اکانت اضافه شد (ورکر {w['tag']})!\n"
        f"👤 {name} | 📱 {phone}\n"
        f"📇 مخاطبین: {contacts} | 👥 گروه‌ها: {groups} | 💬 چت‌دار: {with_chat}",
        buttons=[[Button.inline("🚀 ارسال", f"send_{account_id}".encode())],
                 [Button.inline("🏠 منوی اصلی", b"home")]],
    )


# --------------------------------------------------------------------------- #
# Marker setting
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"marker"))
async def marker_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_marker"}
    await safe_edit(event, 
        f"📌 مارکر فعلی: «{db.get_marker()}»\n{LINE}\n"
        "مارکر جدید رو بفرست (متنی که آخر کپشن پیام نشان‌دارت می‌ذاری):",
        buttons=[[Button.inline("🔙 بازگشت", b"home")]],
    )


async def handle_marker(event):
    marker = event.raw_text.strip()
    if not marker:
        await event.respond("مارکر نمی‌تونه خالی باشه. دوباره بفرست.")
        return
    db.set_marker(marker)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ مارکر روی «{marker}» تنظیم شد.",
                        buttons=main_menu(is_real_owner(event)))


# --------------------------------------------------------------------------- #
# Admin management (OWNER ONLY)
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"admins"))
async def admins_cb(event):
    if not is_real_owner(event):
        await event.answer("فقط مالک ربات به این بخش دسترسی دارد.", alert=True)
        return
    admins = db.list_admins()
    rows = [[Button.inline(f"🗑 {a['name'] or a['user_id']}",
                           f"deladmin_{a['user_id']}".encode())] for a in admins]
    rows.append([Button.inline("➕ افزودن ادمین", b"admin_add")])
    rows.append([Button.inline("🔙 بازگشت", b"home")])
    body = "\n".join(f"• {a['name'] or '-'} ({a['user_id']})" for a in admins) \
        if admins else "هنوز ادمینی اضافه نشده."
    await safe_edit(event, "👥 مدیریت ادمین‌ها:\n" + body, buttons=rows)


@bot.on(events.CallbackQuery(data=b"admin_add"))
async def admin_add_cb(event):
    if not is_real_owner(event):
        await event.answer("فقط مالک.", alert=True)
        return
    state[event.sender_id] = {"step": "await_admin_id"}
    await safe_edit(event, 
        "🆔 آیدی عددی تلگرام ادمین جدید رو بفرست (مثلاً `123456789`).\n"
        "می‌تونی اسم رو هم با فاصله بعدش بدی: `123456789 علی`",
        buttons=[[Button.inline("🔙 بازگشت", b"admins")]],
    )


async def handle_admin_id(event):
    if not is_real_owner(event):
        state.pop(event.sender_id, None)
        return
    parts = event.raw_text.strip().split(maxsplit=1)
    try:
        uid = int(parts[0])
    except (ValueError, IndexError):
        await event.respond("آیدی باید عدد باشه. دوباره بفرست.")
        return
    name = parts[1] if len(parts) > 1 else ""
    db.add_admin(uid, name)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ ادمین {uid} اضافه شد. حالا می‌تونه با ربات کار کنه.",
                        buttons=main_menu(is_real_owner(event)))


@bot.on(events.CallbackQuery(pattern=b"deladmin_(\\d+)"))
async def deladmin_cb(event):
    if not is_real_owner(event):
        await event.answer("فقط مالک.", alert=True)
        return
    uid = int(event.pattern_match.group(1))
    db.remove_admin(uid)
    await event.answer("ادمین حذف شد.")
    await admins_cb(event)


# --------------------------------------------------------------------------- #
# Worker panel: status cards
# --------------------------------------------------------------------------- #
def _ping_text(w) -> str:
    p = w.get("ping_ms", -1)
    return f"{p}ms" if (p is not None and p >= 0) else "—"


def worker_status_all_card(workers) -> str:
    lines = ["🛠 STATU WORKER ALL", LINE]
    for w in workers:
        lines.append(f"🖥 {w['ip']} {w['tag']}")
        lines.append(LINE)
        lines.append(f"{worker.status_emoji(w)} {w['ip']} -{_ping_text(w)} - {worker.file_label(w)}")
        # When unhealthy, show the diagnostic reason so the cause is visible.
        if not w.get("file_ok"):
            d = worker.health_detail(w["id"])
            if d:
                lines.append(f"ℹ️ {d}")
        lines.append(LINE)
    lines.append(f"🕒 {now()}")
    return "\n".join(lines)


def added_worker_card(w) -> str:
    rows = [
        "🛠 ADDED WORKER", LINE,
        f"🖥 {w['ip']} {w['tag']}", LINE,
        "🛠 Statu Worker", LINE,
        f"{worker.status_emoji(w)} {w['ip']} -{_ping_text(w)} - {worker.file_label(w)}",
    ]
    if not w.get("file_ok"):
        d = worker.health_detail(w["id"])
        if d:
            rows.append(f"ℹ️ {d}")
    rows += [LINE, f"🕒 {now()}"]
    return "\n".join(rows)


async def log_status_all(refresh: bool = True):
    workers = db.list_workers()
    if not workers:
        return
    if refresh:
        try:
            await worker.check_all(workers)
        except Exception:
            pass
        workers = db.list_workers()
    await log(worker_status_all_card(workers))


# --------------------------------------------------------------------------- #
# Worker panel: menu + per-worker management
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"workers"))
async def workers_cb(event):
    if not is_owner(event):
        return
    worker.ensure_master_worker()
    workers = db.list_workers()
    rows = []
    for w in workers:
        off = "" if w["enabled"] else " (خاموش)"
        kind = "🏠" if w["is_master"] else "🖥"
        rows.append([Button.inline(
            f"{worker.status_emoji(w)} {kind} {w['tag']} • {w['ip']}{off}",
            f"wk_{w['id']}".encode())])
    rows.append([Button.inline("➕ افزودن ورکر", b"wk_add"),
                 Button.inline("🔄 رفرش وضعیت", b"wk_refresh")])
    rows.append([Button.inline("🔙 بازگشت", b"home")])
    await safe_edit(event, "🛠 مدیریت ورکرها\n(روی هر کدوم بزن برای جزئیات و مدیریت)", buttons=rows)


@bot.on(events.CallbackQuery(data=b"wk_refresh"))
async def wk_refresh_cb(event):
    if not is_owner(event):
        return
    await event.answer("در حال بررسی هم‌زمان همه‌ی ورکرها ...")
    await log_status_all(refresh=True)
    await workers_cb(event)


@bot.on(events.CallbackQuery(data=b"wk_add"))
async def wk_add_cb(event):
    if not is_owner(event):
        return
    if not crypto_util.is_configured():
        await event.answer("اول WORKER_SECRET رو توی .env تنظیم کن (راهنما در README).",
                           alert=True)
        return
    state[event.sender_id] = {"step": "wk_ip", "wk": {}}
    await safe_edit(event, "🖥 آی‌پی سرور ورکر رو بفرست:",
                     buttons=[[Button.inline("🔙 لغو", b"workers")]])


async def handle_worker_step(event, step):
    st = state.get(event.sender_id)
    if not st:
        return
    wk = st.setdefault("wk", {})
    val = event.raw_text.strip()
    if step == "wk_ip":
        wk["ip"] = val
        st["step"] = "wk_port"
        await event.respond("🔌 پورت SSH رو بفرست (پیش‌فرض 22 — اگه همونه فقط `22` بفرست):",
                            buttons=[[Button.inline("🔙 لغو", b"workers")]])
    elif step == "wk_port":
        try:
            wk["port"] = int(val)
        except ValueError:
            wk["port"] = 22
        st["step"] = "wk_user"
        await event.respond("👤 یوزرنیم SSH (مثلاً `root`):",
                            buttons=[[Button.inline("🔙 لغو", b"workers")]])
    elif step == "wk_user":
        wk["user"] = val
        st["step"] = "wk_pass"
        await event.respond("🔑 پسورد SSH رو بفرست:",
                            buttons=[[Button.inline("🔙 لغو", b"workers")]])
    elif step == "wk_pass":
        wk["pass"] = val
        state.pop(event.sender_id, None)
        await provision_and_register(event, wk)


async def provision_and_register(event, wk):
    msg = await event.respond("🚀 شروع نصب ورکر روی سرور ...")

    # Reserve the worker tag up-front so the "building" log and the final
    # "added" card share the SAME tag.
    tag = worker.gen_tag()
    await log(card("🛠 WORKER BUilDING....", [
        f"🖥 {wk['ip']} {tag}",
        LINE,
        f"🕒 {now()}",
    ]))

    async def progress(text):
        try:
            await safe_edit(msg, text)
        except Exception:
            pass

    prov = await worker.provision_worker(wk["ip"], wk.get("port", 22),
                                         wk["user"], wk["pass"],
                                         tag=tag, on_progress=progress)
    if not prov.get("ok"):
        await safe_edit(msg, f"❌ نصب ناموفق: {prov.get('error')}",
                       buttons=[[Button.inline("🔙 بازگشت", b"workers")]])
        return
    wid = await worker.register_provisioned(wk["ip"], wk.get("port", 22),
                                            wk["user"], wk["pass"], prov)
    w = db.get_worker(wid)
    # Give the freshly started container time to fully come up before the
    # first health check; checking immediately on connect gave a misleading
    # status. Wait 30s, then verify.
    await safe_edit(msg, "⏳ ورکر نصب شد. ۳۰ ثانیه صبر برای آماده‌شدن کامل و بررسی وضعیت ...")
    await asyncio.sleep(30)
    try:
        await worker.check_worker(w)
    except Exception:
        pass
    w = db.get_worker(wid)
    await safe_edit(msg, f"✅ ورکر {w['tag']} اضافه و بررسی شد.",
                   buttons=[[Button.inline("🛠 مدیریت ورکر", b"workers")],
                            [Button.inline("🏠 منوی اصلی", b"home")]])
    await log(added_worker_card(w))
    await log_status_all(refresh=False)


@bot.on(events.CallbackQuery(pattern=b"wk_(\\d+)"))
async def wk_detail_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        await event.answer("ورکر پیدا نشد.", alert=True)
        return
    n_acc = db.count_accounts_on_worker(wid)
    sent = db.worker_sent_today(wid)
    lines = [
        f"🛠 ورکر {w['tag']}", LINE,
        f"🖥 IP : {w['ip']}",
        f"نوع : {'Master (محلی)' if w['is_master'] else 'Worker'}",
        f"وضعیت : {worker.status_emoji(w)} {w['status']}",
        f"پینگ : {_ping_text(w)}",
        f"فایل : {worker.file_label(w)}",
        f"اکانت‌ها : {n_acc}",
        f"ارسال امروز : {sent}",
        f"فعال : {'بله' if w['enabled'] else 'خیر'}",
        f"آخرین بررسی : {w.get('last_checked') or '—'}",
    ]
    rows = []
    if not w["is_master"]:
        toggle = "⏸ قطع" if w["enabled"] else "▶️ وصل"
        rows.append([Button.inline(toggle, f"wktog_{wid}".encode()),
                     Button.inline("♻️ ری‌استارت", f"wkrst_{wid}".encode())])
        rows.append([Button.inline("⬆️ آپدیت", f"wkupd_{wid}".encode()),
                     Button.inline("🗑 حذف", f"wkdel_{wid}".encode())])
    else:
        # Local master worker: only allow enabling/disabling it as a worker
        # (no remote restart/update/teardown — it runs in-process).
        toggle = "⏸ خاموش‌کردن لوکال" if w["enabled"] else "▶️ روشن‌کردن لوکال"
        rows.append([Button.inline(toggle, f"wktog_{wid}".encode())])
    rows.append([Button.inline("🔄 بررسی این ورکر", f"wkchk_{wid}".encode())])
    rows.append([Button.inline("🔙 بازگشت", b"workers")])
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"wktog_(\\d+)"))
async def wk_toggle_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        return
    db.set_worker_enabled(wid, not w["enabled"])
    await event.answer("وضعیت تغییر کرد.")
    await wk_detail_cb(event)


@bot.on(events.CallbackQuery(pattern=b"wkrst_(\\d+)"))
async def wk_restart_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w or w["is_master"]:
        await event.answer("روی مستر قابل اجرا نیست.", alert=True)
        return
    await event.answer("در حال ری‌استارت ...")
    try:
        await worker.close_tunnel(wid)
        await worker.restart_worker(w)
    except Exception as e:  # noqa: BLE001
        await safe_edit(event, f"❌ خطا در ری‌استارت: {repr(e)[:150]}",
                         buttons=[[Button.inline("🔙 بازگشت", f"wk_{wid}".encode())]])
        return
    await wk_detail_cb(event)


@bot.on(events.CallbackQuery(pattern=b"wkupd_(\\d+)"))
async def wk_update_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w or w["is_master"]:
        await event.answer("روی مستر قابل اجرا نیست.", alert=True)
        return
    await safe_edit(event, f"⬆️ در حال آپدیت ورکر {w['tag']} (git pull + rebuild) ...")
    try:
        await worker.close_tunnel(wid)
        await worker.update_worker(w)
    except Exception as e:  # noqa: BLE001
        await safe_edit(event, f"❌ خطا در آپدیت: {repr(e)[:150]}",
                         buttons=[[Button.inline("🔙 بازگشت", f"wk_{wid}".encode())]])
        return
    await wk_detail_cb(event)


@bot.on(events.CallbackQuery(pattern=b"wkchk_(\\d+)"))
async def wk_check_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        return
    await event.answer("در حال بررسی ...")
    try:
        await worker.check_worker(w)
    except Exception:
        pass
    await wk_detail_cb(event)


@bot.on(events.CallbackQuery(pattern=b"wkdel_(\\d+)"))
async def wk_del_confirm_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    await safe_edit(event, 
        "حذف کامل این ورکر؟ (کانتینر و سورس روی سرور هم پاک می‌شه)",
        buttons=[[Button.inline("✅ بله، حذف کن", f"wkdely_{wid}".encode())],
                 [Button.inline("🔙 خیر", f"wk_{wid}".encode())]],
    )


@bot.on(events.CallbackQuery(pattern=b"wkdely_(\\d+)"))
async def wk_del_do_cb(event):
    if not is_owner(event):
        return
    wid = int(event.pattern_match.group(1))
    w = db.get_worker(wid)
    if not w:
        return
    await safe_edit(event, "🗑 در حال پاک‌سازی سرور و حذف ورکر ...")
    if not w["is_master"]:
        try:
            await worker.teardown_worker(w)
        except Exception:
            pass
    db.delete_worker(wid)
    await safe_edit(event, f"✅ ورکر {w['tag']} حذف شد.",
                     buttons=[[Button.inline("🔙 بازگشت", b"workers")]])


# --------------------------------------------------------------------------- #
# Remote send (account owned by a remote worker)
# --------------------------------------------------------------------------- #
async def send_prepare_remote(event, acc, w, marker):
    if continuous_busy(acc["id"]):
        await safe_edit(event,
            "🔁 یک قابلیت اتومیشن (اتومیشن/منشی/ریپلای/گزارش) روی این اکانت روشنه. "
            "اول از بخش «🔁 اتومیشن» خاموشش کن، بعد ارسال بزن.",
            buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
        return
    await safe_edit(event, f"⏳ بررسی ورکر {w['tag']} و آماده‌سازی ...")
    # CHECK the worker right before using it.
    try:
        await worker.check_worker(w)
    except Exception:
        pass
    w = db.get_worker(w["id"])
    if not (w and w["enabled"] and w["status"] == "ok"):
        await safe_edit(event, 
            f"❌ ورکر {w['tag'] if w else '?'} الان سالم/فعال نیست"
            f" (وضعیت: {w['status'] if w else 'نامشخص'}).\n"
            "این اکانت روی همین ورکر لاگین شده و فقط از همین‌جا می‌تونه بفرسته.",
            buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
        return
    try:
        res = await worker.api_call(w, "POST", "/prepare",
                                    {"phone": acc["phone"], "marker": marker})
    except Exception as e:  # noqa: BLE001
        await safe_edit(event, f"❌ خطا در آماده‌سازی روی ورکر: {repr(e)[:150]}",
                         buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
        return
    if not res.get("marker_found"):
        await safe_edit(event, 
            f"❌ توی Saved Messages ورکر پیامی با مارکر «{marker}» نبود.",
            buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
        return
    total = res.get("total", 0)
    if total == 0:
        await safe_edit(event, "هیچ مخاطبی پیدا نشد.",
                         buttons=[[Button.inline("🔙 بازگشت", f"acc_{acc['id']}".encode())]])
        return
    pending_send[event.sender_id] = {
        "account_id": acc["id"], "phone": acc["phone"],
        "remote": True, "worker_id": w["id"], "total": total,
    }
    await safe_edit(event, 
        card(f"🚀 آماده‌ی ارسال (ورکر {w['tag']})", [
            f"📎 محتوا : پیام نشان‌دار «{marker}» ✅",
            f"🎯 گیرنده‌ها : {total} مخاطب",
            "ترتیب : چت‌دار ← آنلاین ← Last Seen",
            LINE,
            "به این مخاطب‌ها ارسال بشه؟",
        ]),
        buttons=[[Button.inline("✅ تأیید و ارسال", f"go_{acc['id']}".encode())],
                 [Button.inline("🔙 لغو", f"acc_{acc['id']}".encode())]],
    )


async def run_send_remote(owner_id: int, payload: dict):
    account_id = payload["account_id"]
    phone = payload["phone"]
    w = db.get_worker(payload["worker_id"])
    marker = db.get_marker()
    delay = db.get_delay()
    count = _next_counter()
    total = payload.get("total", 0)
    ok = 0
    fail = 0
    reason = None
    started = datetime.now()

    if not w:
        await log("⛔ ورکر صاحب این اکانت پیدا نشد.")
        pending_send.pop(owner_id, None)
        return

    active_jobs.add(account_id)
    await log(card("SEND STARTED 🚀", [
        f"🛠 Count : {count:03d}",
        f"📱 Phone : {phone}",
        f"👨‍🔧 Worker : {w['tag']}",
        f"🕒 Started : {now()}",
        LINE,
        f"🎯 Targets : {total}",
        f"⏱ Delay : {delay}s",
        f"📌 Marker : «{marker}» Found ✅",
    ]))

    prev_retry = 0
    try:
        res = await worker.api_call(w, "POST", "/send/start", {
            "phone": phone, "marker": marker, "delay": delay,
            "max_errors": config.MAX_ERRORS, "send_timeout": config.SEND_TIMEOUT,
            "resume_wait": config.RESUME_WAIT, "max_retries": config.RESUME_MAX_RETRIES,
        })
        if not res.get("ok") or not res.get("marker_found"):
            reason = "مارکر روی ورکر پیدا نشد"
        else:
            job_id = res["job_id"]
            total = res.get("total", total)
            while True:
                if stop_flags.get(account_id):
                    try:
                        await worker.api_call(w, "POST", f"/send/stop/{job_id}")
                    except Exception:
                        pass
                await asyncio.sleep(2)
                try:
                    stt = await worker.api_call(w, "GET", f"/send/status/{job_id}")
                except Exception as e:  # noqa: BLE001
                    reason = f"قطع ارتباط با ورکر: {repr(e)[:120]}"
                    break
                ok = stt.get("ok", 0)
                fail = stt.get("fail", 0)
                # auto-resume happening on the worker -> master posts the ALERT
                rc = stt.get("retry_count", 0)
                if rc > prev_retry:
                    prev_retry = rc
                    remaining = max(0, total - ok - fail)
                    await log(card(f"🚨 ALERT 5 MINUTE {_alert_word(rc)}", [
                        f"✅ {ok}",
                        f"⏳ {remaining}",
                        f"👤 Account : {phone}",
                    ]))
                if stt.get("done"):
                    r = stt.get("reason")
                    if r == "manual_stop":
                        reason = "توقف دستی توسط کاربر"
                    elif r and str(r).startswith("max_errors"):
                        reason = f"رسیدن به سقف خطا ({config.MAX_ERRORS})"
                    elif r:
                        reason = str(r)
                    break
    except Exception as e:  # noqa: BLE001
        reason = f"خطای کلی: {repr(e)[:150]}"

    try:
        db.incr_worker_sent(w["id"], ok)
    except Exception:
        pass
    active_jobs.discard(account_id)
    dur = str(datetime.now() - started).split(".")[0]
    pending_send.pop(owner_id, None)
    is_owner_user = owner_id == config.OWNER_ID

    if reason:
        await log(card("⛔ SEND STOPPED", [
            f"👤 Account : {phone}",
            f"👨‍🔧 Worker : {w['tag']}",
            f"📊 ✅ {ok}   ❌ {fail}   📁 {total}",
            f"⚠️ Reason : {reason}",
            f"⏱ Duration : {dur}",
            f"🕒 {now()}",
        ]))
        try:
            await bot.send_message(owner_id, f"⛔ ارسال متوقف شد. ✅ {ok} / ❌ {fail} از {total}\nدلیل: {reason}",
                                   buttons=main_menu(is_owner_user))
        except Exception:
            pass
    else:
        await log(card("SEND FINISHED ✅", [
            "🟢 Status : Completed",
            f"👤 Account : {phone}",
            f"👨‍🔧 Worker : {w['tag']}",
            LINE,
            f"✅ {ok}   ❌ {fail}   📁 {total}",
            f"⏱ Duration : {dur}",
        ]))
        try:
            await bot.send_message(owner_id, f"✅ ارسال تمام شد. ✅ {ok} / ❌ {fail} از {total}",
                                   buttons=main_menu(is_owner_user))
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Automation: rotate texts to an account's groups, repeatedly.
# Works for local (master) accounts and accounts owned by a remote worker.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"automation"))
async def automation_menu_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    accounts = db.list_accounts()
    if not accounts:
        await safe_edit(event, "اول یک اکانت اضافه کن.",
                        buttons=[[Button.inline("➕ افزودن اکانت", b"add_account")],
                                 [Button.inline("🔙 بازگشت", b"home")]])
        return
    rows = []
    for a in accounts:
        on = automation_on(a["id"])
        rows.append([Button.inline(f"{'🟢' if on else '⚪️'} {a['phone']}",
                                   f"auto_{a['id']}".encode())])
    rows.append([Button.inline("🪪 سینک اسم/بیو همه اکانت‌ها", b"psync")])
    rows.append([Button.inline("🔙 بازگشت", b"home")])
    await safe_edit(event, "🔁 اتومیشن — یک اکانت انتخاب کن:", buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"auto_(\\d+)"))
async def automation_account_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    au = db.get_automation(account_id)
    texts = db.list_automation_texts(account_id)
    on = bool(au["enabled"])
    lines = [
        f"🔁 اتومیشن — {acc['phone']}", LINE,
        f"وضعیت : {'🟢 روشن' if on else '⚪️ خاموش'}",
        f"فاصله : {au['interval_sec']} ثانیه",
        f"تعداد متن‌ها : {len(texts)}",
        f"مجموع ارسال : {au['sent_total']}",
        LINE,
        f"🤖 منشی : {'🟢' if secretary_on(account_id) else '⚪️'}   "
        f"📊 گزارش کانال : {'🟢' if channelreport_on(account_id) else '⚪️'}   "
        f"↩️ ریپلای : {'🟢' if reply_on(account_id) else '⚪️'}",
    ]
    rows = [
        [Button.inline("➕ افزودن متن", f"auadd_{account_id}".encode()),
         Button.inline("🗑 پاک‌کردن متن‌ها", f"auclr_{account_id}".encode())],
        [Button.inline("🔗 لیست گروه‌ها", f"aulnk_{account_id}".encode())],
        [Button.inline("⏱ تنظیم فاصله", f"auint_{account_id}".encode())],
        [Button.inline("⏹ خاموش‌کردن" if on else "▶️ روشن‌کردن",
                       f"autog_{account_id}".encode())],
        [Button.inline("🤖 منشی پیوی", f"secm_{account_id}".encode()),
         Button.inline("📊 گزارش کانال", f"crm_{account_id}".encode())],
        [Button.inline("↩️ پاسخ‌گوی ریپلای", f"rpm_{account_id}".encode())],
        [Button.inline("🔙 بازگشت", b"automation")],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"auadd_(\\d+)"))
async def automation_add_text_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_auto_text", "account_id": account_id}
    await safe_edit(event, "✍️ متنی که می‌خوای به گروه‌ها بره رو بفرست (می‌تونی چند تا پشت‌هم بفرستی):",
                    buttons=[[Button.inline("✅ تمام / بازگشت", f"auto_{account_id}".encode())]])


async def handle_auto_text(event):
    st = state.get(event.sender_id)
    if not st:
        return
    account_id = st.get("account_id")
    text = event.raw_text.strip()
    if not text:
        await event.respond("متن خالیه. دوباره بفرست.")
        return
    db.add_automation_text(account_id, text)
    n = len(db.list_automation_texts(account_id))
    await event.respond(
        f"✅ متن اضافه شد (مجموع: {n}). متن بعدی رو بفرست یا برگرد.",
        buttons=[[Button.inline("✅ تمام / بازگشت", f"auto_{account_id}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"auclr_(\\d+)"))
async def automation_clear_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    db.clear_automation_texts(account_id)
    await event.answer("همه‌ی متن‌ها پاک شد.")
    await automation_account_cb(event)


@bot.on(events.CallbackQuery(pattern=b"auint_(\\d+)"))
async def automation_interval_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_auto_interval", "account_id": account_id}
    await safe_edit(event,
        f"⏱ یک عدد بین {config.AUTOMATION_MIN_INTERVAL} تا {config.AUTOMATION_MAX_INTERVAL} "
        "بفرست (فاصله‌ی هر دور به ثانیه):",
        buttons=[[Button.inline("🔙 بازگشت", f"auto_{account_id}".encode())]])


async def handle_auto_interval(event):
    st = state.get(event.sender_id)
    if not st:
        return
    account_id = st.get("account_id")
    db.set_automation_interval(account_id, event.raw_text.strip())
    iv = db.get_automation(account_id)["interval_sec"]
    state.pop(event.sender_id, None)
    acc = db.get_account(account_id)
    if acc and automation_on(account_id):   # apply new interval to a live loop
        await stop_automation(acc)
        await start_automation(acc)
    await event.respond(f"✅ فاصله روی {iv} ثانیه تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت", f"auto_{account_id}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"autog_(\\d+)"))
async def automation_toggle_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    au = db.get_automation(account_id)
    if not au["enabled"]:                       # turning ON
        if not db.list_automation_texts(account_id):
            await event.answer("اول حداقل یک متن اضافه کن.", alert=True)
            return
        if account_id in active_jobs:
            await event.answer("این اکانت الان در حال ارساله. صبر کن تموم شه.", alert=True)
            return
        # start FIRST; only mark enabled if it actually launched (so a dead/old
        # worker can't leave the account stuck in a broken "on" state).
        try:
            await start_automation(acc)
        except Exception as e:  # noqa: BLE001
            await event.answer(f"شروع اتومیشن ناموفق: {repr(e)[:120]}\n"
                               "اگه اکانت روی ورکره، اول ورکر رو آپدیت کن.", alert=True)
            return
        db.set_automation_enabled(account_id, True)
        await log(card("🔁 AUTOMATION ON", [
            f"👤 Account : {acc['phone']}",
            f"⏱ Interval : {au['interval_sec']}s",
            f"🕒 {now()}",
        ]))
    else:                                       # turning OFF
        db.set_automation_enabled(account_id, False)
        await stop_automation(acc)
        await log(card("🔁 AUTOMATION OFF", [
            f"👤 Account : {acc['phone']}",
            f"🕒 {now()}",
        ]))
    await automation_account_cb(event)


# ---- per-account group-link list: this ONE account joins your personal groups ----
@bot.on(events.CallbackQuery(pattern=b"aulnk_(\\d+)"))
async def automation_links_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    links = db.list_automation_links(account_id)
    body = "\n".join(f"• {ln}" for ln in links) if links else "هنوز لینکی اضافه نشده."
    lines = [f"🔗 لیست گروه‌های {acc['phone']}", LINE, body, LINE,
             "می‌تونی لینک گروه‌های شخصی‌ت رو اضافه کنی، بعد «عضو شو» بزنی تا "
             "همین اکانت عضوشون بشه."]
    rows = [
        [Button.inline("➕ افزودن لینک", f"auladd_{account_id}".encode()),
         Button.inline("🗑 پاک‌کردن", f"aulclr_{account_id}".encode())],
        [Button.inline("✅ عضو شو (و ذخیره در لیست مشترک)", f"auljoin_{account_id}".encode())],
        [Button.inline(f"📥 عضو از لیست مشترک ({db.count_verified_group_links()})",
                       f"aushared_{account_id}".encode())],
        [Button.inline("🔙 بازگشت", f"auto_{account_id}".encode())],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"auladd_(\\d+)"))
async def automation_link_add_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_auto_link", "account_id": account_id}
    await safe_edit(event, "🔗 لینک گروه روبیکا رو بفرست (می‌تونی چند تا پشت‌هم بفرستی):",
                    buttons=[[Button.inline("✅ تمام / بازگشت", f"aulnk_{account_id}".encode())]])


async def handle_auto_link(event):
    st = state.get(event.sender_id)
    if not st:
        return
    account_id = st.get("account_id")
    link = event.raw_text.strip()
    if not link.startswith("http"):
        await event.respond("یه لینکِ معتبر بفرست (با https شروع شه).")
        return
    db.add_automation_link(account_id, link)
    n = len(db.list_automation_links(account_id))
    await event.respond(
        f"✅ لینک اضافه شد (مجموع: {n}). لینک بعدی رو بفرست یا برگرد.",
        buttons=[[Button.inline("✅ تمام / بازگشت", f"aulnk_{account_id}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"aulclr_(\\d+)"))
async def automation_link_clear_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    db.clear_automation_links(account_id)
    await event.answer("لینک‌ها پاک شد.")
    await automation_links_cb(event)


@bot.on(events.CallbackQuery(pattern=b"auljoin_(\\d+)"))
async def automation_link_join_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    links = db.list_automation_links(account_id)
    if not links:
        await event.answer("اول حداقل یه لینک اضافه کن.", alert=True)
        return
    if continuous_busy(account_id):
        await event.answer("🔁 یک قابلیت اتومیشن روی این اکانت روشنه. اول خاموشش کن، بعد «عضو شو» بزن.",
                           alert=True)
        return
    if account_id in active_jobs:
        await event.answer("این اکانت الان مشغوله. صبر کن.", alert=True)
        return
    await safe_edit(event, f"⏳ {acc['phone']} داره عضو {len(links)} گروه می‌شه ... "
                    "گزارش در گروه لاگ میاد.")
    asyncio.create_task(run_group_join(acc, links))


async def run_group_join(acc: dict, links: list):
    account_id = acc["id"]
    phone = acc["phone"]
    active_jobs.add(account_id)
    joined = 0
    failed = 0
    joined_links = []
    try:
        w = worker.worker_for_account(acc)
        if w and not worker.is_local(w):
            res = await worker.api_call(w, "POST", "/group/join",
                                        {"phone": phone, "links": links}, timeout=600)
            joined = res.get("joined", 0)
            failed = res.get("failed", 0)
            joined_links = res.get("joined_links", []) or []
        else:
            await account_conn.close(phone)   # ensure single connection (Feature 6)
            client = rb.open_client(phone)
            try:
                await rb.connect_ready(client)
                for link in links:
                    try:
                        await asyncio.wait_for(rb.join_group_by_link(client, link),
                                               timeout=60)
                        joined += 1
                        joined_links.append(link)
                    except Exception:
                        failed += 1
                    await asyncio.sleep(config.GROUP_JOIN_DELAY)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        # Feature 4: remember every successfully joined link in the SHARED
        # verified list so the other accounts can re-use it.
        for ln in joined_links:
            try:
                db.add_verified_group_link(ln, added_by=phone)
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        await log(f"⚠️ عضو شدن در گروه‌های «{phone}» ناقص ماند: {repr(e)[:150]}")
    finally:
        active_jobs.discard(account_id)
    await log(card("🔗 GROUP JOIN", [
        f"👤 Account : {phone}",
        f"✅ Joined : {joined}",
        f"❌ Failed : {failed}",
        f"💾 Saved to shared : {len(joined_links)}",
        f"🕒 {now()}",
    ]))


async def run_automation_local(account_id: int, phone: str, st: dict):
    """Local automation loop — ONE connection per pass (Feature 6), the same
    open->work->close shape as the original source automation and the working
    "send" path. Every interval we open one connection, send a random text to
    each group on it (tiny random pause between groups), then close it and
    sleep. The per-account lock inside connection() means secretary / reply /
    channel report on the SAME account never hold a connection at the same time
    -> no parallel clients, and opening once-per-pass -> no connect churn."""
    fails: dict = {}          # guid -> consecutive failures
    last_text: dict = {}
    try:
        while not st["stop"]:
            st["heartbeat"] = time.monotonic()   # watchdog: prove we're alive
            try:
                # ONE connection for this whole pass (per-account lock inside
                # connection() prevents parallel use by secretary/reply).
                async with account_conn.connection(phone) as client:
                    try:
                        groups = await asyncio.wait_for(
                            rb.get_group_guids(client), timeout=60)
                    except Exception:
                        # could not read groups this pass -> drop the (maybe
                        # wedged) socket and try again next round. NEVER kill the
                        # loop, NEVER open a second connection to "verify".
                        groups = []
                        account_conn.drop_connection(phone)
                    st["groups"] = len(groups)
                    for g in groups:
                        if st["stop"]:
                            break
                        guid = g["guid"]
                        if guid in st["skipped"]:
                            continue
                        idx, txt = _pick_text(st["texts"], last_text.get(guid))
                        if txt is None:
                            break
                        try:
                            await asyncio.wait_for(
                                rb.send_text(client, guid, txt),
                                timeout=config.SEND_TIMEOUT)
                        except Exception:
                            # ANY send failure (banned/muted group, transient
                            # auth hiccup, timeout, ...) is treated EXACTLY like
                            # the original code: count it against THIS group and
                            # mute the group after 3 strikes. We do NOT declare
                            # the account dead and do NOT stop the loop — that
                            # false-positive was what silently halted automation.
                            fails[guid] = fails.get(guid, 0) + 1
                            if fails[guid] >= 3:
                                st["skipped"].add(guid)
                                # cleanup engine: record this banned/muted group
                                # as a candidate (logged once) for the owner to
                                # review + confirm leaving.
                                try:
                                    is_new = db.add_cleanup_candidate(
                                        account_id, guid, g.get("name", ""),
                                        reason="بن/سکوت یا عدم امکان ارسال")
                                    if is_new:
                                        await _log_cleanup_candidate(
                                            account_id, phone, guid, g.get("name", ""))
                                except Exception:
                                    pass
                        else:
                            st["sent"] += 1
                            last_text[guid] = idx
                            fails[guid] = 0
                            try:              # a brief DB lock must NOT count as a send error
                                db.incr_automation_sent(account_id, 1)
                            except Exception:
                                pass
                        await asyncio.sleep(random.uniform(
                            config.AUTOMATION_GROUP_DELAY_MIN,
                            config.AUTOMATION_GROUP_DELAY_MAX))
                    # recovery: if every group ended up muted, reset + reconnect
                    if groups and all(g["guid"] in st["skipped"] for g in groups):
                        st["skipped"].clear()
                        fails.clear()
                        account_conn.drop_connection(phone)
            except Exception as e:  # noqa: BLE001
                # a whole-pass error: drop the connection so the next pass is
                # fresh, log once, and CONTINUE (never kill automation).
                account_conn.drop_connection(phone)
                await log(f"⚠️ اتومیشن «{phone}» خطای دور (ادامه می‌دهد): {repr(e)[:150]}")
            st["heartbeat"] = time.monotonic()
            waited = 0
            while waited < st["interval"] and not st["stop"]:
                await asyncio.sleep(1)
                waited += 1
    except Exception as e:  # noqa: BLE001
        await log(f"⚠️ اتومیشن «{phone}» با خطا متوقف شد: {repr(e)[:150]}")


async def start_automation(acc: dict):
    """Start the automation loop for an account (local task or remote worker job)."""
    account_id = acc["id"]
    texts = db.list_automation_texts(account_id)
    interval = db.get_automation(account_id)["interval_sec"]
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        await worker.api_call(w, "POST", "/automation/start",
                              {"phone": acc["phone"], "texts": texts, "interval": interval})
        return
    # local
    old = automation_tasks.pop(account_id, None)
    if old:
        old["state"]["stop"] = True
        try:                                  # let the old loop fully stop first
            await asyncio.wait_for(old["task"], timeout=5)
        except Exception:
            pass
    st = {"stop": False, "sent": 0, "groups": 0, "skipped": set(),
          "texts": texts, "interval": interval, "heartbeat": time.monotonic()}
    task = asyncio.create_task(run_automation_local(account_id, acc["phone"], st))
    automation_tasks[account_id] = {"task": task, "state": st}


async def stop_automation(acc: dict):
    account_id = acc["id"]
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        try:
            await worker.api_call(w, "POST", "/automation/stop", {"phone": acc["phone"]})
        except Exception:
            pass
        return
    t = automation_tasks.pop(account_id, None)
    if t:
        t["state"]["stop"] = True


async def automation_summary_loop():
    """Every AUTOMATION_SUMMARY_INTERVAL, post a per-account total. Also self-
    heals automations that stopped: relaunches a worker automation whose
    container restarted AND a LOCAL automation whose task died or hung (no
    heartbeat within 3x its interval)."""
    while True:
        await asyncio.sleep(config.AUTOMATION_SUMMARY_INTERVAL)
        try:
            for au in db.list_enabled_automations():
                acc = db.get_account(au["account_id"])
                if not acc:
                    continue
                w = worker.worker_for_account(acc)
                sent = au["sent_total"]
                groups = None
                if w and not worker.is_local(w):
                    try:
                        stt = await worker.api_call(
                            w, "GET", f"/automation/status?phone={acc['phone']}")
                        if not stt.get("running"):   # worker restarted -> relaunch
                            await start_automation(acc)
                        sent = stt.get("sent", sent)
                        groups = stt.get("groups")
                    except Exception:
                        pass
                else:
                    # LOCAL self-heal: relaunch if the task is gone, finished,
                    # or hung (heartbeat older than 3x interval -> silent stall).
                    t = automation_tasks.get(au["account_id"])
                    interval = au.get("interval_sec") or config.AUTOMATION_MIN_INTERVAL
                    stale = (3 * max(interval, 10)) + 120
                    dead = (not t) or t["task"].done()
                    hung = False
                    if t and not dead:
                        hb = t["state"].get("heartbeat", 0)
                        hung = (time.monotonic() - hb) > stale
                    if dead or hung:
                        if hung:                       # a hung task must be cancelled
                            try:
                                t["state"]["stop"] = True
                                t["task"].cancel()
                            except Exception:
                                pass
                        await log(card("♻️ AUTOMATION SELF-HEAL", [
                            f"👤 Account : {acc['phone']}",
                            ("علت: تسک متوقف شده بود" if dead else "علت: هنگ بی‌صدا (بدون فعالیت)"),
                            "اتومیشن دوباره راه‌اندازی شد.",
                            f"🕒 {now()}"]))
                        try:
                            await start_automation(acc)
                        except Exception as e:  # noqa: BLE001
                            await log(f"⚠️ self-heal اتومیشن {acc['phone']} ناموفق: {repr(e)[:120]}")
                    if t and t["state"].get("groups") is not None:
                        groups = t["state"].get("groups")
                rows = [f"👤 Account : {acc['phone']}", f"✅ مجموع ارسال : {sent}"]
                if groups is not None:
                    rows.append(f"👥 گروه‌ها : {groups}")
                rows.append(f"🕒 {now()}")
                await log(card("🔁 AUTOMATION SUMMARY", rows))
        except Exception as e:  # noqa: BLE001
            print(f"[automation_summary] {e}")


async def recover_automations():
    """On boot, relaunch every automation that was enabled before restart."""
    for au in db.list_enabled_automations():
        acc = db.get_account(au["account_id"])
        if not acc:
            continue
        try:
            await start_automation(acc)
        except Exception as e:  # noqa: BLE001
            await log(f"⚠️ بازگردانی اتومیشن {acc['phone']} ناموفق: {repr(e)[:120]}")


# --------------------------------------------------------------------------- #
# Automation EXTRAS — start/stop + panel UI (secretary / channel report /
# reply responder), profile sync, shared-list join, recovery + worker relay.
# All LOCAL loops run on the shared connection (account_conn); remote accounts
# are driven through new worker endpoints (see worker_api.py).
# --------------------------------------------------------------------------- #
async def _start_local(tasks: dict, account_id: int, factory):
    """(Re)start a local feature loop, replacing any previous one."""
    old = tasks.pop(account_id, None)
    if old:
        old["state"]["stop"] = True
        try:
            await asyncio.wait_for(old["task"], timeout=5)
        except Exception:
            pass
    st = {"stop": False, "replied": 0}
    task = asyncio.create_task(factory(st))
    tasks[account_id] = {"task": task, "state": st}


def _stop_local(tasks: dict, account_id: int):
    t = tasks.pop(account_id, None)
    if t:
        t["state"]["stop"] = True
        # also cancel the task so it stops promptly even if it is mid-sleep or
        # mid-call; otherwise a long interval could keep it running one more pass
        # after the user turned the feature off in the panel.
        task = t.get("task")
        if task and not task.done():
            task.cancel()


# ---- Feature 1: secretary ----
async def start_secretary(acc: dict):
    aid = acc["id"]
    phone = acc["phone"]
    try:                                   # prime cursor: don't reply to old PVs
        db.set_secretary_state(aid, "")
    except Exception:
        pass
    sec = db.get_secretary(aid)
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        await worker.api_call(w, "POST", "/secretary/start", {
            "phone": phone, "mode": sec.get("mode") or "marker",
            "text": sec.get("text") or "", "marker": db.get_marker(),
            "interval": sec.get("interval_sec") or config.SECRETARY_INTERVAL})
        return
    await _start_local(secretary_tasks, aid,
                       lambda st: features.run_secretary_local(aid, phone, st))


async def stop_secretary(acc: dict):
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        try:
            await worker.api_call(w, "POST", "/secretary/stop", {"phone": acc["phone"]})
        except Exception:
            pass
        return
    _stop_local(secretary_tasks, acc["id"])


# ---- Feature 2: channel report ----
async def start_channelreport(acc: dict):
    aid = acc["id"]
    phone = acc["phone"]
    cr = db.get_channel_report(aid)
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        await worker.api_call(w, "POST", "/channelreport/start", {
            "phone": phone, "channel_guid": cr.get("channel_guid") or "",
            "channel_title": cr.get("channel_title") or "",
            "interval": cr.get("interval_sec") or config.CHANNEL_REPORT_INTERVAL})
        return
    await _start_local(channelreport_tasks, aid,
                       lambda st: features.run_channel_report_local(aid, phone, st))


async def stop_channelreport(acc: dict):
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        try:
            await worker.api_call(w, "POST", "/channelreport/stop", {"phone": acc["phone"]})
        except Exception:
            pass
        return
    _stop_local(channelreport_tasks, acc["id"])


# ---- Feature 5: reply responder ----
async def start_reply(acc: dict):
    aid = acc["id"]
    phone = acc["phone"]
    rr = db.get_reply_responder(aid)
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        await worker.api_call(w, "POST", "/reply/start", {
            "phone": phone, "text": rr.get("text") or "",
            "delay": rr.get("delay_sec") or config.REPLY_DELAY})
        return
    await _start_local(reply_tasks, aid,
                       lambda st: features.run_reply_local(aid, phone, st))


async def stop_reply(acc: dict):
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        try:
            await worker.api_call(w, "POST", "/reply/stop", {"phone": acc["phone"]})
        except Exception:
            pass
        return
    _stop_local(reply_tasks, acc["id"])


# --------------------------------------------------------------------------- #
# Secretary panel
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(pattern=b"secm_(\\d+)"))
async def secretary_menu_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    sec = db.get_secretary(aid)
    on = bool(sec["enabled"])
    mode = sec.get("mode") or "marker"
    lines = [
        f"🤖 منشی پیوی — {acc['phone']}", LINE,
        f"وضعیت : {'🟢 روشن' if on else '⚪️ خاموش'}",
        f"حالت جواب : {'متن دلخواه' if mode == 'text' else 'مارکر (پیام نشان‌دار)'}",
        f"متن دلخواه : {((sec.get('text') or '—')[:40])}",
        f"فاصله چک : {sec.get('interval_sec')} ثانیه",
        f"مجموع جواب‌ها : {sec.get('replied_total')}",
        LINE,
        "فقط به «اولین پیامِ» هر نفر جواب داده می‌شه.",
    ]
    rows = [
        [Button.inline("📌 حالت مارکر" + (" ✅" if mode == "marker" else ""),
                       f"secmodem_{aid}".encode()),
         Button.inline("✍️ حالت متن" + (" ✅" if mode == "text" else ""),
                       f"secmodet_{aid}".encode())],
        [Button.inline("✍️ تنظیم متن دلخواه", f"sectext_{aid}".encode())],
        [Button.inline("⏱ تنظیم فاصله", f"secint_{aid}".encode())],
        [Button.inline("⏹ خاموش‌کردن" if on else "▶️ روشن‌کردن",
                       f"sectog_{aid}".encode())],
        [Button.inline("🔙 بازگشت", f"auto_{aid}".encode())],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"secmodem_(\\d+)"))
async def secretary_mode_marker_cb(event):
    if not is_owner(event):
        return
    db.set_secretary_mode(int(event.pattern_match.group(1)), "marker")
    await event.answer("حالت: مارکر")
    await secretary_menu_cb(event)


@bot.on(events.CallbackQuery(pattern=b"secmodet_(\\d+)"))
async def secretary_mode_text_cb(event):
    if not is_owner(event):
        return
    db.set_secretary_mode(int(event.pattern_match.group(1)), "text")
    await event.answer("حالت: متن دلخواه")
    await secretary_menu_cb(event)


@bot.on(events.CallbackQuery(pattern=b"sectext_(\\d+)"))
async def secretary_set_text_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_sec_text", "account_id": aid}
    await safe_edit(event, "✍️ متنِ جوابِ منشی رو بفرست:",
                    buttons=[[Button.inline("🔙 بازگشت", f"secm_{aid}".encode())]])


async def handle_sec_text(event):
    st = state.get(event.sender_id)
    if not st:
        return
    aid = st["account_id"]
    txt = event.raw_text.strip()
    if not txt:
        await event.respond("متن خالیه. دوباره بفرست.")
        return
    db.set_secretary_text(aid, txt)
    db.set_secretary_mode(aid, "text")
    state.pop(event.sender_id, None)
    await event.respond("✅ متن منشی تنظیم شد و حالت روی «متن دلخواه» رفت.",
                        buttons=[[Button.inline("🔙 بازگشت", f"secm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"secint_(\\d+)"))
async def secretary_interval_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_sec_interval", "account_id": aid}
    await safe_edit(event,
        f"⏱ فاصله‌ی چک پیوی (ثانیه) بین {config.SECRETARY_MIN_INTERVAL} تا "
        f"{config.SECRETARY_MAX_INTERVAL} بفرست:",
        buttons=[[Button.inline("🔙 بازگشت", f"secm_{aid}".encode())]])


async def handle_sec_interval(event):
    st = state.get(event.sender_id)
    if not st:
        return
    aid = st["account_id"]
    db.set_secretary_interval(aid, event.raw_text.strip())
    state.pop(event.sender_id, None)
    acc = db.get_account(aid)
    if acc and secretary_on(aid):          # apply new interval to a live loop
        await stop_secretary(acc)
        await start_secretary(acc)
    iv = db.get_secretary(aid)["interval_sec"]
    await event.respond(f"✅ فاصله روی {iv} ثانیه تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت", f"secm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"sectog_(\\d+)"))
async def secretary_toggle_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    sec = db.get_secretary(aid)
    if not sec["enabled"]:
        if aid in active_jobs:
            await event.answer("این اکانت الان مشغول یک عملیات تک‌باریه. صبر کن.", alert=True)
            return
        if (sec.get("mode") or "marker") == "text" and not (sec.get("text") or "").strip():
            await event.answer("اول متن دلخواه رو تنظیم کن یا حالت مارکر رو انتخاب کن.",
                               alert=True)
            return
        try:
            await start_secretary(acc)
        except Exception as e:  # noqa: BLE001
            await event.answer(f"شروع منشی ناموفق: {repr(e)[:110]}\n"
                               "اگه اکانت روی ورکره، اول ورکر رو آپدیت کن.", alert=True)
            return
        db.set_secretary_enabled(aid, True)
        await log(card("🤖 SECRETARY ON", [f"👤 Account : {acc['phone']}", f"🕒 {now()}"]))
    else:
        db.set_secretary_enabled(aid, False)
        await stop_secretary(acc)
        await log(card("🤖 SECRETARY OFF", [f"👤 Account : {acc['phone']}", f"🕒 {now()}"]))
    await secretary_menu_cb(event)


# --------------------------------------------------------------------------- #
# Channel report panel
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(pattern=b"crm_(\\d+)"))
async def channelreport_menu_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    cr = db.get_channel_report(aid)
    on = bool(cr["enabled"])
    lines = [
        f"📊 گزارش کانال — {acc['phone']}", LINE,
        f"وضعیت : {'🟢 روشن' if on else '⚪️ خاموش'}",
        f"کانال : {cr.get('channel_guid') or '—'}",
        f"عنوان : {cr.get('channel_title') or '—'}",
        f"فاصله : {cr.get('interval_sec')} ثانیه",
        LINE,
        "هر بازه: تعداد اعضا + بازدید آخرین پست → گروه لاگ.",
    ]
    rows = [
        [Button.inline("📢 تنظیم کانال (لینک/یوزرنیم/گایید)", f"crset_{aid}".encode())],
        [Button.inline("⏱ تنظیم فاصله", f"crint_{aid}".encode())],
        [Button.inline("⏹ خاموش‌کردن" if on else "▶️ روشن‌کردن",
                       f"crtog_{aid}".encode())],
        [Button.inline("🔙 بازگشت", f"auto_{aid}".encode())],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"crset_(\\d+)"))
async def channelreport_set_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_cr_channel", "account_id": aid}
    await safe_edit(event,
        "📢 لینک یا یوزرنیم یا گاییدِ کانال رو بفرست:\n"
        "مثال: `@my_channel` یا `https://rubika.ir/my_channel` یا `c0...`",
        buttons=[[Button.inline("🔙 بازگشت", f"crm_{aid}".encode())]])


async def handle_cr_channel(event):
    st = state.get(event.sender_id)
    if not st:
        return
    aid = st["account_id"]
    ref = event.raw_text.strip()
    if not ref:
        await event.respond("خالیه. دوباره بفرست.")
        return
    db.set_channel_report_target(aid, ref, "")
    state.pop(event.sender_id, None)
    await event.respond("✅ کانال ثبت شد. (موقع گزارش، یوزرنیم/لینک خودکار به گایید تبدیل می‌شه)",
                        buttons=[[Button.inline("🔙 بازگشت", f"crm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"crint_(\\d+)"))
async def channelreport_interval_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_cr_interval", "account_id": aid}
    await safe_edit(event,
        f"⏱ فاصله‌ی گزارش (ثانیه) بین {config.CHANNEL_REPORT_MIN_INTERVAL} تا "
        f"{config.CHANNEL_REPORT_MAX_INTERVAL} بفرست:",
        buttons=[[Button.inline("🔙 بازگشت", f"crm_{aid}".encode())]])


async def handle_cr_interval(event):
    st = state.get(event.sender_id)
    if not st:
        return
    aid = st["account_id"]
    db.set_channel_report_interval(aid, event.raw_text.strip())
    state.pop(event.sender_id, None)
    acc = db.get_account(aid)
    if acc and channelreport_on(aid):
        await stop_channelreport(acc)
        await start_channelreport(acc)
    iv = db.get_channel_report(aid)["interval_sec"]
    await event.respond(f"✅ فاصله روی {iv} ثانیه تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت", f"crm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"crtog_(\\d+)"))
async def channelreport_toggle_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    cr = db.get_channel_report(aid)
    if not cr["enabled"]:
        if aid in active_jobs:
            await event.answer("این اکانت الان مشغول یک عملیات تک‌باریه. صبر کن.", alert=True)
            return
        if not (cr.get("channel_guid") or "").strip():
            await event.answer("اول کانال رو تنظیم کن.", alert=True)
            return
        try:
            await start_channelreport(acc)
        except Exception as e:  # noqa: BLE001
            await event.answer(f"شروع گزارش ناموفق: {repr(e)[:110]}\n"
                               "اگه اکانت روی ورکره، اول ورکر رو آپدیت کن.", alert=True)
            return
        db.set_channel_report_enabled(aid, True)
        await log(card("📊 CHANNEL REPORT ON", [
            f"👤 Account : {acc['phone']}",
            f"🆔 Channel : {cr.get('channel_guid')}",
            f"🕒 {now()}"]))
    else:
        db.set_channel_report_enabled(aid, False)
        await stop_channelreport(acc)
        await log(card("📊 CHANNEL REPORT OFF", [f"👤 Account : {acc['phone']}", f"🕒 {now()}"]))
    await channelreport_menu_cb(event)


# --------------------------------------------------------------------------- #
# Reply responder panel
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(pattern=b"rpm_(\\d+)"))
async def reply_menu_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    rr = db.get_reply_responder(aid)
    on = bool(rr["enabled"])
    lines = [
        f"↩️ پاسخ‌گوی ریپلای — {acc['phone']}", LINE,
        f"وضعیت : {'🟢 روشن' if on else '⚪️ خاموش'}",
        f"متن جواب : {((rr.get('text') or '—')[:40])}",
        f"تأخیر : {rr.get('delay_sec')} ثانیه",
        f"مجموع جواب‌ها : {rr.get('replied_total')}",
        LINE,
        "وقتی توی گروه به این اکانت ریپلای بزنن، جواب خودکار می‌ده (فعلاً فقط متن).",
    ]
    rows = [
        [Button.inline("✍️ تنظیم متن", f"rptext_{aid}".encode())],
        [Button.inline("⏱ تنظیم تأخیر", f"rpdelay_{aid}".encode())],
        [Button.inline("⏹ خاموش‌کردن" if on else "▶️ روشن‌کردن",
                       f"rptog_{aid}".encode())],
        [Button.inline("🔙 بازگشت", f"auto_{aid}".encode())],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"rptext_(\\d+)"))
async def reply_set_text_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_rp_text", "account_id": aid}
    await safe_edit(event, "✍️ متنِ جوابِ ریپلای رو بفرست:",
                    buttons=[[Button.inline("🔙 بازگشت", f"rpm_{aid}".encode())]])


async def handle_rp_text(event):
    st = state.get(event.sender_id)
    if not st:
        return
    aid = st["account_id"]
    txt = event.raw_text.strip()
    if not txt:
        await event.respond("متن خالیه. دوباره بفرست.")
        return
    db.set_reply_text(aid, txt)
    state.pop(event.sender_id, None)
    await event.respond("✅ متن ریپلای تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت", f"rpm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"rpdelay_(\\d+)"))
async def reply_set_delay_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    state[event.sender_id] = {"step": "await_rp_delay", "account_id": aid}
    await safe_edit(event,
        f"⏱ تأخیرِ جواب (ثانیه) بین {config.REPLY_MIN_DELAY} تا "
        f"{config.REPLY_MAX_DELAY} بفرست:",
        buttons=[[Button.inline("🔙 بازگشت", f"rpm_{aid}".encode())]])


async def handle_rp_delay(event):
    st = state.get(event.sender_id)
    if not st:
        return
    aid = st["account_id"]
    db.set_reply_delay(aid, event.raw_text.strip())
    state.pop(event.sender_id, None)
    acc = db.get_account(aid)
    if acc and reply_on(aid):
        await stop_reply(acc)
        await start_reply(acc)
    d = db.get_reply_responder(aid)["delay_sec"]
    await event.respond(f"✅ تأخیر روی {d} ثانیه تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت", f"rpm_{aid}".encode())]])


@bot.on(events.CallbackQuery(pattern=b"rptog_(\\d+)"))
async def reply_toggle_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    rr = db.get_reply_responder(aid)
    if not rr["enabled"]:
        if aid in active_jobs:
            await event.answer("این اکانت الان مشغول یک عملیات تک‌باریه. صبر کن.", alert=True)
            return
        if not (rr.get("text") or "").strip():
            await event.answer("اول متن جواب رو تنظیم کن.", alert=True)
            return
        try:
            await start_reply(acc)
        except Exception as e:  # noqa: BLE001
            await event.answer(f"شروع ریپلای ناموفق: {repr(e)[:110]}\n"
                               "اگه اکانت روی ورکره، اول ورکر رو آپدیت کن.", alert=True)
            return
        db.set_reply_enabled(aid, True)
        await log(card("↩️ REPLY RESPONDER ON", [f"👤 Account : {acc['phone']}", f"🕒 {now()}"]))
    else:
        db.set_reply_enabled(aid, False)
        await stop_reply(acc)
        await log(card("↩️ REPLY RESPONDER OFF", [f"👤 Account : {acc['phone']}", f"🕒 {now()}"]))
    await reply_menu_cb(event)


# --------------------------------------------------------------------------- #
# Feature 3: profile (name + bio) sync across ALL accounts
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"psync"))
async def psync_menu_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    p = db.get_profile_sync()
    name = (str(p.get("first_name") or "") + " " + str(p.get("last_name") or "")).strip()
    lines = [
        "🪪 سینک اسم/بیو همه اکانت‌ها", LINE,
        f"نام : {name or '—'}",
        f"بیو : {p.get('bio') or '—'}",
        LINE,
        "این مقدار روی همه‌ی اکانت‌ها اعمال می‌شه (عکس لازم نیست).",
    ]
    rows = [
        [Button.inline("✏️ تنظیم نام/بیو", b"psyncset")],
        [Button.inline("🚀 اعمال روی همه", b"psyncgo")],
        [Button.inline("🔙 بازگشت", b"automation")],
    ]
    await safe_edit(event, "\n".join(lines), buttons=rows)


@bot.on(events.CallbackQuery(data=b"psyncset"))
async def psync_set_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_psync"}
    await safe_edit(event,
        "✏️ نام رو در خط اول و بیو رو در خط دوم بفرست:\n"
        "خط اول = نام کامل (با اولین فاصله به نام/نام‌خانوادگی تقسیم می‌شه)\n"
        "خط دوم = بیو\n\nمثال:\nعلی رضایی\nسلام، خوش اومدی 🌹",
        buttons=[[Button.inline("🔙 بازگشت", b"psync")]])


async def handle_psync_input(event):
    txt = event.raw_text
    parts = txt.split("\n", 1)
    name_line = parts[0].strip()
    bio = parts[1].strip() if len(parts) > 1 else ""
    np = name_line.split(" ", 1)
    first = np[0].strip() if np else ""
    last = np[1].strip() if len(np) > 1 else ""
    db.set_profile_sync(first, last, bio)
    state.pop(event.sender_id, None)
    await event.respond(
        f"✅ ثبت شد:\nنام: {name_line or '—'}\nبیو: {bio or '—'}\n"
        "حالا «🚀 اعمال روی همه» رو بزن.",
        buttons=[[Button.inline("🔙 بازگشت", b"psync")]])


async def _apply_profile_local(client, first, last, bio):
    """Compare current profile to target; update only if different. Returns
    True if changed, False if already identical."""
    cur = await rb.get_my_profile(client)
    same = ((cur.get("first_name") or "") == first
            and (cur.get("last_name") or "") == last
            and (cur.get("bio") or "") == bio)
    if same:
        return False
    await rb.update_profile(client, first_name=first, last_name=last, bio=bio)
    return True


@bot.on(events.CallbackQuery(data=b"psyncgo"))
async def psync_go_cb(event):
    if not is_owner(event):
        return
    accounts = db.list_accounts()
    if not accounts:
        await event.answer("هیچ اکانتی نیست.", alert=True)
        return
    p = db.get_profile_sync()
    if not (p.get("first_name") or p.get("last_name") or p.get("bio")):
        await event.answer("اول نام/بیو رو تنظیم کن.", alert=True)
        return
    await safe_edit(event,
        f"⏳ در حال اعمال نام/بیو روی {len(accounts)} اکانت ... گزارش در گروه لاگ میاد.")
    asyncio.create_task(run_profile_sync())


async def run_profile_sync():
    p = db.get_profile_sync()
    first = p.get("first_name") or ""
    last = p.get("last_name") or ""
    bio = p.get("bio") or ""
    accounts = db.list_accounts()
    changed = unchanged = failed = 0
    rows = []
    for acc in accounts:
        phone = acc["phone"]
        try:
            w = worker.worker_for_account(acc)
            if w and not worker.is_local(w):
                res = await worker.api_call(w, "POST", "/profile/update", {
                    "phone": phone, "first_name": first, "last_name": last,
                    "bio": bio}, timeout=120)
                ch = res.get("changed")
            else:
                ch = await account_conn.call(phone, _apply_profile_local,
                                             first, last, bio, timeout=60)
            if ch:
                changed += 1
                rows.append(f"• {phone} : ✅ عوض شد")
            else:
                unchanged += 1
                rows.append(f"• {phone} : ⏸ بدون تغییر")
        except account_conn.InvalidAuthError:
            failed += 1
            rows.append(f"• {phone} : 🔐 سشن باطل (لاگین مجدد)")
        except Exception as e:  # noqa: BLE001
            failed += 1
            rows.append(f"• {phone} : ❌ {repr(e)[:60]}")
        await asyncio.sleep(config.PROFILE_SYNC_DELAY)
    await log(card("🪪 PROFILE SYNC", [
        f"✅ تغییر: {changed}   ⏸ بدون تغییر: {unchanged}   ❌ خطا: {failed}",
        LINE, *rows, LINE, f"🕒 {now()}"]))
    try:
        await bot.send_message(config.OWNER_ID,
                               f"🪪 سینک پروفایل تمام شد. ✅ {changed} / ⏸ {unchanged} / ❌ {failed}",
                               buttons=main_menu(True))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Feature 4: a single account joins the SHARED verified group-link list.
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(pattern=b"aushared_(\\d+)"))
async def automation_shared_join_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    links = db.list_verified_group_links()
    if not links:
        await event.answer("لیست مشترک خالیه. اول با یک اکانت «عضو شو» بزن تا پر شه.",
                           alert=True)
        return
    if continuous_busy(aid):
        await event.answer("یک قابلیت اتومیشن روی این اکانت روشنه. اول خاموشش کن.", alert=True)
        return
    if aid in active_jobs:
        await event.answer("این اکانت مشغوله. صبر کن.", alert=True)
        return
    await safe_edit(event,
        f"⏳ {acc['phone']} داره از لیست مشترک ({len(links)}) عضو می‌شه ... "
        "گزارش در گروه لاگ میاد.")
    asyncio.create_task(run_group_join(acc, links))


# --------------------------------------------------------------------------- #
# 🏭 Generator engine (موتور مولد): one account creates a channel/group, the
# others join it, the owner makes them admins (we poll user_is_admin), then all
# accounts seed their contacts (sequentially, anti-duplicate). Fully logged.
# Local accounts go through account_conn; worker accounts via /gen/* endpoints.
# Never touches the automation logic or the base source.
# --------------------------------------------------------------------------- #
def generator_menu_text():
    b = db.get_broadcaster()
    sel = db.list_broadcaster_account_ids()
    return card("📢 پخش کانالی (موتور مولد)", [
        f"اسم مشترک کانال‌ها : {b.get('title') or '—'}",
        f"اکانت‌های انتخاب‌شده : {len(sel)}",
        f"سقف عضوگیری (هر کانال) : {b.get('member_target')}",
        f"فاصله بین اکانت‌ها : {b.get('gap_seconds')} ثانیه",
        LINE,
        "هر اکانتِ انتخابی، کانالِ خودش رو می‌سازه (با اسم مشترک + یوزرنیم رندوم)، "
        "پیامِ مارکر رو می‌فرسته، و مخاطبینِ خودش رو عضو می‌کنه. نوبتی + لاگ کامل.",
    ])


def generator_menu_buttons():
    return [
        [Button.inline("✏️ اسم مشترک کانال", b"bc_title")],
        [Button.inline("👥 انتخاب اکانت‌ها", b"bc_accounts"),
         Button.inline("🎯 سقف عضوگیری", b"bc_target")],
        [Button.inline("⏱ فاصله بین اکانت‌ها", b"bc_gap")],
        [Button.inline("▶️ شروع پخش کانالی", b"bc_start")],
        [Button.inline("🔙 بازگشت", b"home")],
    ]


@bot.on(events.CallbackQuery(data=b"generator"))
async def generator_menu_cb(event):
    if not is_owner(event):
        return
    state.pop(event.sender_id, None)
    await safe_edit(event, generator_menu_text(), buttons=generator_menu_buttons())


@bot.on(events.CallbackQuery(data=b"bc_title"))
async def bc_title_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_bc_title"}
    await safe_edit(event, "✏️ اسمِ مشترکِ کانال‌ها رو بفرست (همه‌ی کانال‌ها این اسم رو می‌گیرن):",
                    buttons=[[Button.inline("🔙 بازگشت", b"generator")]])


async def handle_bc_title(event):
    title = event.raw_text.strip()
    if not title:
        await event.respond("اسم خالیه. دوباره بفرست.")
        return
    db.set_broadcaster(title=title)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ اسم مشترک روی «{title}» تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت", b"generator")]])


@bot.on(events.CallbackQuery(data=b"bc_accounts"))
async def bc_accounts_cb(event):
    if not is_owner(event):
        return
    accounts = db.list_accounts()
    if not accounts:
        await event.answer("اول یک اکانت اضافه کن.", alert=True)
        return
    sel = set(db.list_broadcaster_account_ids())
    rows = []
    for a in accounts:
        mark = "✅" if a["id"] in sel else "⬜️"
        rows.append([Button.inline(f"{mark} {a['phone']}",
                                   f"bcacc_{a['id']}".encode())])
    rows.append([Button.inline("🔙 بازگشت", b"generator")])
    await safe_edit(event, "👥 اکانت‌هایی که کانال می‌سازن رو انتخاب کن (بزن تا تیک بخوره):",
                    buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"bcacc_(\\d+)"))
async def bc_acc_toggle_cb(event):
    if not is_owner(event):
        return
    db.toggle_broadcaster_account(int(event.pattern_match.group(1)))
    await bc_accounts_cb(event)


@bot.on(events.CallbackQuery(data=b"bc_target"))
async def bc_target_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_bc_target"}
    await safe_edit(event, "🎯 سقفِ عضوگیری برای هر کانال رو بفرست (عدد، مثلاً 300):",
                    buttons=[[Button.inline("🔙 بازگشت", b"generator")]])


async def handle_bc_target(event):
    try:
        n = max(1, int(event.raw_text.strip()))
    except ValueError:
        await event.respond("یه عدد بفرست.")
        return
    db.set_broadcaster(member_target=n)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ سقف عضوگیری روی {n} تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت", b"generator")]])


@bot.on(events.CallbackQuery(data=b"bc_gap"))
async def bc_gap_cb(event):
    if not is_owner(event):
        return
    state[event.sender_id] = {"step": "await_bc_gap"}
    await safe_edit(event, "⏱ فاصله بین اکانت‌ها رو به ثانیه بفرست (مثلاً 8):",
                    buttons=[[Button.inline("🔙 بازگشت", b"generator")]])


async def handle_bc_gap(event):
    try:
        n = max(1, int(event.raw_text.strip()))
    except ValueError:
        await event.respond("یه عدد (ثانیه) بفرست.")
        return
    db.set_broadcaster(gap_seconds=n)
    state.pop(event.sender_id, None)
    await event.respond(f"✅ فاصله روی {n} ثانیه تنظیم شد.",
                        buttons=[[Button.inline("🔙 بازگشت", b"generator")]])


@bot.on(events.CallbackQuery(data=b"bc_start"))
async def bc_start_cb(event):
    if not is_owner(event):
        return
    b = db.get_broadcaster()
    sel = db.list_broadcaster_account_ids()
    if not b.get("title"):
        await event.answer("اول اسمِ مشترک رو تنظیم کن.", alert=True)
        return
    if not sel:
        await event.answer("اول حداقل یک اکانت انتخاب کن.", alert=True)
        return
    await safe_edit(event,
        f"📢 پخش کانالی شروع شد روی {len(sel)} اکانت. نوبتی و با لاگ کامل پیش می‌ره.",
        buttons=[[Button.inline("🏠 منوی اصلی", b"home")]])
    asyncio.create_task(run_broadcaster(event.sender_id))


async def _broadcast_one(acc, title, member_target, marker):
    """Make this account's OWN channel, set a random username, forward the
    marked post, then seed its OWN contacts. Local or worker."""
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        res = await worker.api_call(w, "POST", "/broadcast/run", {
            "phone": acc["phone"], "title": title, "marker": marker,
            "member_target": member_target}, timeout=900)
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "broadcast failed"))
        return res.get("object_guid"), res.get("username", ""), \
            res.get("forwarded", False), res.get("added", 0)

    async def _do(client):
        guid = await rb.create_channel(client, title)
        username = ""
        try:
            username = await rb.assign_random_channel_username(client, guid)
        except Exception:
            username = ""
        forwarded = False
        try:
            saved_guid, mid = await rb.find_marked_message(client, marker)
            if mid:
                await rb.forward_message(client, saved_guid, guid, mid)
                forwarded = True
        except Exception:
            forwarded = False
        added = 0
        try:
            added = await rb.seed_channel_with_contacts(
                client, guid, target=member_target,
                batch=config.CHANNEL_ADD_BATCH, delay=config.CHANNEL_ADD_DELAY)
        except Exception:
            added = 0
        return guid, username, forwarded, added
    return await account_conn.call(acc["phone"], _do, timeout=900)


async def run_broadcaster(owner_id: int):
    b = db.get_broadcaster()
    title = b.get("title")
    member_target = int(b.get("member_target") or config.CHANNEL_MEMBER_TARGET)
    gap = int(b.get("gap_seconds") or config.BROADCAST_GAP_SECONDS)
    marker = db.get_marker()
    ids = db.list_broadcaster_account_ids()
    accounts = [db.get_account(i) for i in ids]
    accounts = [a for a in accounts if a]

    await log(card("📢 پخش کانالی — شروع", [
        f"🎛 اسم مشترک : {title}",
        f"👥 اکانت‌ها : {len(accounts)}",
        f"🎯 سقف هر کانال : {member_target}",
        f"⏱ فاصله : {gap}s",
        f"🕒 {now()}"]))

    made = 0
    total_added = 0
    # SEQUENTIAL — never parallel (safe for worker accounts too)
    for acc in accounts:
        phone = acc["phone"]
        try:
            guid, username, forwarded, added = await _broadcast_one(
                acc, title, member_target, marker)
            made += 1
            total_added += added
            await log(card("📢 پخش کانالی — کانال ساخته شد ✅", [
                f"👤 {phone}",
                f"🆔 {guid}",
                (f"🔗 @{username}" if username else "⚠️ یوزرنیم ست نشد"),
                ("📎 پیام مارکر فرستاده شد" if forwarded
                 else f"⚠️ مارکر «{marker}» پیدا/فرستاده نشد"),
                f"➕ مخاطبینِ اضافه‌شده : {added}",
                f"🕒 {now()}"]))
        except account_conn.InvalidAuthError:
            await _log_invalid_auth(phone)
        except Exception as e:  # noqa: BLE001
            await log(card("📢 پخش کانالی — خطا", [
                f"👤 {phone}", f"💥 {repr(e)[:160]}", f"🕒 {now()}"]))
        await asyncio.sleep(gap)

    await log(card("📢 پخش کانالی — پایان ✅", [
        f"🎛 اسم : {title}",
        f"✅ کانال‌های ساخته‌شده : {made}/{len(accounts)}",
        f"➕ کلِ مخاطبینِ اضافه‌شده : {total_added}",
        f"🕒 {now()}"]))
    try:
        await bot.send_message(owner_id,
                               f"📢 پخش کانالی تمام شد.\nکانال: {made}/{len(accounts)} | "
                               f"مخاطب اضافه‌شده: {total_added}",
                               buttons=main_menu(owner_id == config.OWNER_ID))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 🖼 PV image -> PDF export: download EVERY photo from an account's private
# (user) chats and send them back as a single PDF. Local or worker. No photo
# is skipped (only photos — videos/gifs/files are ignored, as requested).
# --------------------------------------------------------------------------- #
@bot.on(events.CallbackQuery(data=b"pvexport"))
async def pvexport_menu_cb(event):
    if not is_owner(event):
        return
    accounts = db.list_accounts()
    if not accounts:
        await event.answer("اول یک اکانت اضافه کن.", alert=True)
        return
    rows = [[Button.inline(f"🖼 {a['phone']}", f"pvx_{a['id']}".encode())]
            for a in accounts]
    rows.append([Button.inline("🔙 بازگشت", b"home")])
    await safe_edit(event,
        "🖼 از کدوم اکانت عکس‌های پیوی‌ها رو جمع کنم و PDF بفرستم؟\n"
        "(فقط عکس — فیلم/گیف نه. هیچ عکسی جا نمی‌مونه.)", buttons=rows)


@bot.on(events.CallbackQuery(pattern=b"pvx_(\\d+)"))
async def pvexport_run_cb(event):
    if not is_owner(event):
        return
    aid = int(event.pattern_match.group(1))
    acc = db.get_account(aid)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    await safe_edit(event,
        f"⏳ شروع جمع‌آوری عکس‌های پیویِ {acc['phone']} ... این ممکنه چند دقیقه طول بکشه. "
        "وقتی آماده شد، PDF برات ارسال می‌شه.",
        buttons=[[Button.inline("🏠 منوی اصلی", b"home")]])
    asyncio.create_task(run_pv_export(event.sender_id, acc))


async def _pv_collect_photos(acc) -> list:
    """Return a list of raw image byte-blobs from the account's PV chats.
    Local: download directly. Worker: ask worker, decode base64."""
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        import base64
        res = await worker.api_call(w, "POST", "/pvexport/run", {
            "phone": acc["phone"], "max_chats": config.PV_EXPORT_MAX_CHATS,
            "max_photos": config.PV_EXPORT_MAX_PHOTOS}, timeout=1800)
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "pvexport failed"))
        return [base64.b64decode(x) for x in (res.get("photos_b64") or [])]

    async def _do(client):
        out = []
        guids = await rb.get_chat_list_guids(client, only_users=True)
        for g in guids[:config.PV_EXPORT_MAX_CHATS]:
            async for _mid, fi in rb.iter_chat_photos(client, g):
                try:
                    blob = await rb.download_photo(client, fi)
                    if blob:
                        out.append(blob)
                except Exception:
                    continue
                if len(out) >= config.PV_EXPORT_MAX_PHOTOS:
                    return out
        return out
    return await account_conn.call(acc["phone"], _do, timeout=1800)


async def run_pv_export(owner_id: int, acc):
    phone = acc["phone"]
    try:
        photos = await _pv_collect_photos(acc)
    except account_conn.InvalidAuthError:
        await _log_invalid_auth(phone)
        return
    except Exception as e:  # noqa: BLE001
        await log(card("🖼 آرشیو عکس پیوی — خطا", [
            f"👤 {phone}", f"💥 {repr(e)[:160]}", f"🕒 {now()}"]))
        try:
            await bot.send_message(owner_id, f"❌ جمع‌آوری عکس‌های {phone} ناموفق: {repr(e)[:120]}")
        except Exception:
            pass
        return

    if not photos:
        await bot.send_message(owner_id, f"ℹ️ هیچ عکسی در پیوی‌های {phone} پیدا نشد.",
                               buttons=main_menu(owner_id == config.OWNER_ID))
        return

    import pdf_export
    out_path = os.path.join(DATA_DIR, f"pv_{phone}_{int(datetime.now().timestamp())}.pdf")
    try:
        n = await asyncio.to_thread(pdf_export.build_pdf, photos, out_path)
    except Exception as e:  # noqa: BLE001
        await bot.send_message(owner_id, f"❌ ساخت PDF ناموفق: {repr(e)[:120]}")
        return

    await log(card("🖼 آرشیو عکس پیوی ✅", [
        f"👤 {phone}",
        f"🖼 تعداد عکس : {n}",
        f"🕒 {now()}"]))
    try:
        await bot.send_file(owner_id, out_path,
                            caption=f"🖼 آرشیو عکس‌های پیویِ {phone}\nتعداد: {n} عکس",
                            force_document=True)
        await bot.send_message(owner_id, "✅ آرشیو ارسال شد.",
                               buttons=main_menu(owner_id == config.OWNER_ID))
    finally:
        try:
            os.remove(out_path)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Recovery (on boot) + worker relay loop for the EXTRAS.
# --------------------------------------------------------------------------- #
async def recover_extras():
    """Relaunch every EXTRA feature that was enabled before a restart."""
    for sec in db.list_enabled_secretaries():
        acc = db.get_account(sec["account_id"])
        if acc:
            try:
                await start_secretary(acc)
            except Exception as e:  # noqa: BLE001
                await log(f"⚠️ بازگردانی منشی {acc['phone']} ناموفق: {repr(e)[:120]}")
    for cr in db.list_enabled_channel_reports():
        acc = db.get_account(cr["account_id"])
        if acc:
            try:
                await start_channelreport(acc)
            except Exception as e:  # noqa: BLE001
                await log(f"⚠️ بازگردانی گزارش‌کانال {acc['phone']} ناموفق: {repr(e)[:120]}")
    for rr in db.list_enabled_reply_responders():
        acc = db.get_account(rr["account_id"])
        if acc:
            try:
                await start_reply(acc)
            except Exception as e:  # noqa: BLE001
                await log(f"⚠️ بازگردانی ریپلای {acc['phone']} ناموفق: {repr(e)[:120]}")


async def _heal_remote_extra(acc, status_path, starter):
    w = worker.worker_for_account(acc)
    if not (w and not worker.is_local(w)):
        return
    try:
        stt = await worker.api_call(w, "GET", f"{status_path}?phone={acc['phone']}")
        if not stt.get("running"):           # worker container restarted
            await starter(acc)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Health & self-heal engine (موتور سلامت و خودتعمیر).
# Periodically: verify every account's session, optionally deactivate the dead
# ones, relaunch any enabled-but-stopped automation, and post one overall
# system-health card. This is the automatic counterpart of the manual sweep.
# --------------------------------------------------------------------------- #
async def health_engine_loop():
    while True:
        await asyncio.sleep(max(300, config.HEALTH_ENGINE_INTERVAL))
        try:
            await run_health_engine()
        except Exception as e:  # noqa: BLE001
            print(f"[health_engine] {e}")


async def run_health_engine():
    accounts = db.list_accounts()
    alive = 0
    dead = 0
    skipped = 0
    healed = 0
    dead_rows = []
    for acc in accounts:
        phone = acc["phone"]
        aid = acc["id"]
        w = worker.worker_for_account(acc)
        is_dead = False
        checked = True
        try:
            if w and not worker.is_local(w):
                try:
                    res = await worker.api_call(
                        w, "POST", "/account/verify", {"phone": phone}, timeout=90)
                    is_dead = bool(res.get("dead"))
                except Exception:
                    checked = False
            else:
                is_dead = await account_conn.verify_session_dead(phone)
        except Exception:
            checked = False

        if not checked:
            skipped += 1
            continue
        if is_dead:
            dead += 1
            dead_rows.append(f"• {phone} : 🔴 شوت‌شده")
            if config.HEALTH_ENGINE_AUTODISABLE_DEAD and acc["status"] == "active":
                # stop features + flag inactive (never auto-delete; that's manual)
                try:
                    db.set_secretary_enabled(aid, False)
                    db.set_channel_report_enabled(aid, False)
                    db.set_reply_enabled(aid, False)
                    db.set_automation_enabled(aid, False)
                except Exception:
                    pass
                for stopper in (stop_automation, stop_secretary,
                                stop_channelreport, stop_reply):
                    try:
                        await stopper(acc)
                    except Exception:
                        pass
                db.set_status(aid, "inactive")
        else:
            alive += 1
            # self-heal: an account that is healthy AND has automation enabled
            # but whose local task is gone -> relaunch it.
            if automation_on(aid):
                t = automation_tasks.get(aid)
                w2 = worker.worker_for_account(acc)
                local = not (w2 and not worker.is_local(w2))
                if local and ((not t) or t["task"].done()):
                    try:
                        await start_automation(acc)
                        healed += 1
                    except Exception:
                        pass

    rows = [
        f"🟢 سالم : {alive}",
        f"🔴 شوت‌شده : {dead}",
        f"♻️ اتومیشن‌های ترمیم‌شده : {healed}",
    ]
    if skipped:
        rows.append(f"❔ بررسی‌نشده (ورکر در دسترس نبود) : {skipped}")
    if dead_rows:
        rows.append(LINE)
        rows.extend(dead_rows)
        rows.append("برای حذفِ کامل: «👤 اکانت‌های من» → «🔄 بررسی و پاکسازی».")
    rows.append(LINE)
    rows.append(f"🕒 {now()}")
    await log(card("🩺 موتور سلامت و خودتعمیر", rows))


async def extras_worker_loop():
    """Every 30s: drain queued log lines from each remote worker (so worker-side
    secretary/reply/report events show up in the master log group), and relaunch
    any remote EXTRA whose worker restarted."""
    while True:
        await asyncio.sleep(30)
        try:
            for w in db.list_enabled_workers():
                if worker.is_local(w):
                    continue
                try:
                    res = await worker.api_call(w, "GET", "/extras/logs", timeout=30)
                    for line in (res.get("logs") or []):
                        await log(line)
                except Exception:
                    pass
            for sec in db.list_enabled_secretaries():
                acc = db.get_account(sec["account_id"])
                if acc:
                    await _heal_remote_extra(acc, "/secretary/status", start_secretary)
            for cr in db.list_enabled_channel_reports():
                acc = db.get_account(cr["account_id"])
                if acc:
                    await _heal_remote_extra(acc, "/channelreport/status", start_channelreport)
            for rr in db.list_enabled_reply_responders():
                acc = db.get_account(rr["account_id"])
                if acc:
                    await _heal_remote_extra(acc, "/reply/status", start_reply)
        except Exception as e:  # noqa: BLE001
            print(f"[extras_worker_loop] {e}")


# --------------------------------------------------------------------------- #
# Background health monitor: immediate alerts + periodic STATU WORKER ALL.
# --------------------------------------------------------------------------- #
async def health_loop():
    import time as _t
    prev_status: dict = {}
    last_report = 0.0
    quick = min(300, max(60, config.HEALTH_INTERVAL))
    while True:
        try:
            workers = db.list_workers()
            if workers:
                results = await worker.check_all(workers)
                for r in results:
                    old = prev_status.get(r["id"])
                    if old == "ok" and r["status"] != "ok":
                        kind = "بلاک" if r["status"] == "blocked" else "قطع"
                        await log(card("🚨 WORKER ALERT", [
                            f"👨‍🔧 {r['tag']} • {r['ip']}",
                            f"وضعیت: 🟢 سالم  ←  🔴 {kind}",
                            f"🕒 {now()}",
                        ]))
                    prev_status[r["id"]] = r["status"]
                now_t = _t.monotonic()
                if now_t - last_report >= config.HEALTH_INTERVAL:
                    await log(worker_status_all_card(db.list_workers()))
                    last_report = now_t
        except Exception as e:  # noqa: BLE001
            print(f"[health_loop] {e}")
        await asyncio.sleep(quick)


# --------------------------------------------------------------------------- #
# Boot
# --------------------------------------------------------------------------- #
async def amain():
    problems = config.validate()
    if problems:
        print("Missing settings in .env: " + ", ".join(problems))
        return
    db.init()
    worker.ensure_master_worker()
    # Feature 6 wiring: shared-connection logger + invalid-auth handler + janitor
    features.set_logger(log)
    account_conn.set_invalid_auth_handler(_on_invalid_auth)
    account_conn.start_janitor()
    await bot.start(bot_token=config.BOT_TOKEN)
    await log(card("Online", [f"Rubika Project {config.VERSION}", LINE, f"🕒 {now()}"]))
    print(f"Panel is running (version {config.VERSION}).")
    # background worker health monitor (alerts + periodic STATU WORKER ALL)
    asyncio.create_task(health_loop())
    # automation: periodic summary log + relaunch any automation enabled before restart
    asyncio.create_task(automation_summary_loop())
    await recover_automations()
    # automation EXTRAS: relaunch enabled features + drain remote worker logs/heal
    asyncio.create_task(extras_worker_loop())
    # health & self-heal engine (verify sessions, relaunch stalled automation,
    # post overall health card) — موتور سلامت و خودتعمیر
    asyncio.create_task(health_engine_loop())
    await recover_extras()
    try:
        await bot.run_until_disconnected()
    finally:
        try:
            await account_conn.close_all()
        except Exception:
            pass
        await worker.shutdown()


if __name__ == "__main__":
    asyncio.run(amain())
