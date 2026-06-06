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
import tempfile
import zipfile
from datetime import datetime

from telethon import TelegramClient, events, Button
from telethon.errors import MessageNotModifiedError

import config
import crypto_util
import db
import rubika_client as rb
import worker

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
        [Button.inline("➕ افزودن اکانت", b"add_account"),
         Button.inline("👤 اکانت من", b"accounts")],
        [Button.inline("🚀 ارسال", b"send_menu")],
        [Button.inline("🛠 مدیریت ورکر", b"workers"),
         Button.inline("📌 تنظیم مارکر", b"marker")],
        [Button.inline("⚙️ تنظیم سرعت ارسال", b"speed")],
        [Button.inline("💾 بکاپ", b"backup")],
    ]
    if owner:
        rows.append([Button.inline("👥 مدیریت ادمین", b"admins")])
    return rows


WELCOME = (
    "╭───────────────────╮\n"
    "     🤖  پنل روبیکا\n"
    "╰───────────────────╯\n"
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
    buttons.append([Button.inline("🔙 بازگشت", b"home")])
    await safe_edit(event, "👤 اکانت‌های تو:", buttons=buttons)


@bot.on(events.CallbackQuery(pattern=b"acc_(\\d+)"))
async def account_menu_cb(event):
    if not is_owner(event):
        return
    account_id = int(event.pattern_match.group(1))
    acc = db.get_account(account_id)
    if not acc:
        await event.answer("اکانت پیدا نشد.", alert=True)
        return
    status = "فعال ✅" if acc["status"] == "active" else "غیرفعال ⚠️"
    text = (
        "╭───── 👤 اکانت ─────╮\n"
        f"  نام    : {acc['name'] or '-'}\n"
        f"  شماره  : {acc['phone']}\n"
        f"  آیدی   : {acc['user_id']}\n"
        f"  وضعیت  : {status}\n"
        "╰────────────────────╯"
    )
    buttons = [
        [Button.inline("🚀 ارسال با این اکانت", f"send_{account_id}".encode())],
        [Button.inline("📢 ارسال به شیوه کانال", f"chan_{account_id}".encode())],
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
    elif step == "await_admin_id":
        await handle_admin_id(event)
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
    finally:
        try:
            await client.disconnect()
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
    marker = db.get_marker()
    # Route to the worker that OWNS this account (session affinity).
    w = worker.worker_for_account(acc)
    if w and not worker.is_local(w):
        await send_prepare_remote(event, acc, w, marker)
        return
    await safe_edit(event, "⏳ در حال آماده‌سازی (اتصال، پیدا کردن پیام نشان‌دار، خواندن مخاطب‌ها) ...")

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

    await log(card("SEND STARTED 🚀", [
        f"🛠 Count : {count:03d}",
        f"📱 Phone : {phone}",
        f"🕒 Started : {now()}",
        LINE,
        f"🎯 Targets : {total}",
        f"⏱ Delay : {delay}s",
        f"📌 Marker : «{marker}» Found ✅",
    ]))

    client = rb.open_client(phone)
    try:
        await rb.connect_ready(client)
        for guid in recipients:
            if stop_flags.get(account_id):
                reason = "توقف دستی توسط کاربر"
                break
            try:
                await asyncio.wait_for(
                    rb.forward_message(client, saved_guid, guid, mid),
                    timeout=config.SEND_TIMEOUT,
                )
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                await log(card("⚠️ SEND ERROR", [
                    f"📱 Phone : {phone}",
                    f"🎯 To : {guid}",
                    f"💥 Error : {repr(e)[:200]}",
                ]))
                if fail >= config.MAX_ERRORS:
                    reason = f"رسیدن به سقف خطا ({config.MAX_ERRORS})"
                    break
            await asyncio.sleep(delay)
    except Exception as e:  # noqa: BLE001
        reason = f"خطای کلی: {repr(e)[:200]}"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

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

    try:
        res = await worker.api_call(w, "POST", "/send/start", {
            "phone": phone, "marker": marker, "delay": delay,
            "max_errors": config.MAX_ERRORS, "send_timeout": config.SEND_TIMEOUT,
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
    await bot.start(bot_token=config.BOT_TOKEN)
    await log(card("Online", [f"Rubika Project {config.VERSION}", LINE, f"🕒 {now()}"]))
    print(f"Panel is running (version {config.VERSION}).")
    # background worker health monitor (alerts + periodic STATU WORKER ALL)
    asyncio.create_task(health_loop())
    try:
        await bot.run_until_disconnected()
    finally:
        await worker.shutdown()


if __name__ == "__main__":
    asyncio.run(amain())
