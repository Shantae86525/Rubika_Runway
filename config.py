"""All settings are loaded from the .env file (never hard-coded)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# ---- Telegram ----
API_ID = _int("API_ID")
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = _int("OWNER_ID")
LOG_GROUP_ID = _int("LOG_GROUP_ID")

# ---- Sending ----
# Hard bounds for the per-message delay (seconds).
MIN_DELAY = 0.2
MAX_DELAY = 10.0
DEFAULT_DELAY = _float("SEND_DELAY", 1.0)

# Marker at the end of the caption of the message in the account's Saved Messages.
FORWARD_MARKER = os.getenv("FORWARD_MARKER", "کد135").strip()

# Stop the whole run after this many failed sends.
MAX_ERRORS = _int("MAX_ERRORS", 3)

# Per-send timeout so a single stuck send can never hang the whole run.
SEND_TIMEOUT = _int("SEND_TIMEOUT", 60)

# ---- Channel send mode ----
# When sending "via channel", the bot creates a channel, forwards the marked
# message into it, then adds the account's OWN contacts as members.
# Contacts are added in chunks of CHANNEL_ADD_BATCH, up to CHANNEL_MEMBER_TARGET
# in total. (Rubika lists contacts ~100 at a time; reading is paginated.)
CHANNEL_MEMBER_TARGET = _int("CHANNEL_MEMBER_TARGET", 300)
CHANNEL_ADD_BATCH = _int("CHANNEL_ADD_BATCH", 80)
# Pause (seconds) between member-add batches to stay gentle on Rubika.
CHANNEL_ADD_DELAY = _float("CHANNEL_ADD_DELAY", 2.0)

# ---- Auto-resume (continue a send after an error/crash) ----
# When a send stops because of errors (NOT a manual stop), wait this many
# seconds and resume from the rest of the list, up to RESUME_MAX_RETRIES times.
RESUME_WAIT = _int("RESUME_WAIT", 300)            # 5 minutes
RESUME_MAX_RETRIES = _int("RESUME_MAX_RETRIES", 2)

# ---- Automation (rotating texts to the account's own groups) ----
# Every interval (clamped to [MIN,MAX]) the bot sends a random text to each of
# the account's groups. A tiny random pause between groups avoids a spam burst.
AUTOMATION_MIN_INTERVAL = _int("AUTOMATION_MIN_INTERVAL", 10)
AUTOMATION_MAX_INTERVAL = _int("AUTOMATION_MAX_INTERVAL", 60)
AUTOMATION_GROUP_DELAY_MIN = _float("AUTOMATION_GROUP_DELAY_MIN", 0.5)
AUTOMATION_GROUP_DELAY_MAX = _float("AUTOMATION_GROUP_DELAY_MAX", 2.0)
# How often (seconds) the master posts the per-account automation summary.
AUTOMATION_SUMMARY_INTERVAL = _int("AUTOMATION_SUMMARY_INTERVAL", 1200)  # 20 min

# Pause (seconds) between joining each personal group from the link list.
GROUP_JOIN_DELAY = _float("GROUP_JOIN_DELAY", 3.0)

# --------------------------------------------------------------------------- #
# Automation EXTRAS (additive — Feature set: secretary / channel report /
# profile sync / reply responder / shared connection). All optional with sane
# defaults; nothing here changes existing behaviour.
# --------------------------------------------------------------------------- #
# Feature 6: close an account's warm connection after this many idle seconds.
CONN_IDLE_CLOSE_SEC = _int("CONN_IDLE_CLOSE_SEC", 600)        # 10 min

# Feature 1 (PV secretary): how often to poll for new private chats, and bounds.
SECRETARY_INTERVAL = _int("SECRETARY_INTERVAL", 600)         # 10 min default
SECRETARY_MIN_INTERVAL = _int("SECRETARY_MIN_INTERVAL", 60)
SECRETARY_MAX_INTERVAL = _int("SECRETARY_MAX_INTERVAL", 3600)
# Pause between individual auto-replies in one secretary pass.
SECRETARY_REPLY_DELAY = _float("SECRETARY_REPLY_DELAY", 2.0)

# Feature 2 (channel report): how often to post the channel stats, and bounds.
CHANNEL_REPORT_INTERVAL = _int("CHANNEL_REPORT_INTERVAL", 600)   # 10 min
CHANNEL_REPORT_MIN_INTERVAL = _int("CHANNEL_REPORT_MIN_INTERVAL", 120)
CHANNEL_REPORT_MAX_INTERVAL = _int("CHANNEL_REPORT_MAX_INTERVAL", 3600)

# Feature 3 (profile sync): pause between applying the profile to each account.
PROFILE_SYNC_DELAY = _float("PROFILE_SYNC_DELAY", 1.0)

# Feature 5 (group-reply responder): default + bounds for the reply delay, and
# how often the responder polls the account's chats for new replies.
REPLY_DELAY = _float("REPLY_DELAY", 2.0)
REPLY_MIN_DELAY = _float("REPLY_MIN_DELAY", 0.0)
REPLY_MAX_DELAY = _float("REPLY_MAX_DELAY", 60.0)
REPLY_POLL_INTERVAL = _int("REPLY_POLL_INTERVAL", 20)        # seconds between polls


def clamp_secretary_interval(value) -> int:
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return SECRETARY_INTERVAL
    return max(SECRETARY_MIN_INTERVAL, min(SECRETARY_MAX_INTERVAL, value))


def clamp_channel_report_interval(value) -> int:
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return CHANNEL_REPORT_INTERVAL
    return max(CHANNEL_REPORT_MIN_INTERVAL, min(CHANNEL_REPORT_MAX_INTERVAL, value))


def clamp_reply_delay(value) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return REPLY_DELAY
    return max(REPLY_MIN_DELAY, min(REPLY_MAX_DELAY, value))

# Version label shown in the startup "Online" log card.
VERSION = os.getenv("VERSION", "V1")

# Only this id may control the bot.
# NOTE: extra admins added from the panel are stored in the DB and merged at
# runtime (see db.list_admin_ids); OWNER_ID can never be removed.
ALLOWED_IDS = [i for i in [OWNER_ID] if i]

# --------------------------------------------------------------------------- #
# Worker / distributed-mode settings (all OPTIONAL — single-server still works)
# --------------------------------------------------------------------------- #
# Run mode: "master" = the Telegram panel (this machine orchestrates),
#           "worker" = headless API node that only executes login/send jobs.
MODE = (os.getenv("MODE", "master") or "master").strip().lower()

# Fernet key used to encrypt worker SSH passwords / tokens at rest in the DB.
# Generate once with:  python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
WORKER_SECRET = os.getenv("WORKER_SECRET", "").strip()

# Git repo the master clones onto a worker server during provisioning.
GIT_REPO_URL = os.getenv("GIT_REPO_URL", "https://github.com/Shantae86525/Rubika_Runway").strip()
GIT_BRANCH = os.getenv("GIT_BRANCH", "main").strip()

# The worker API listens ONLY on loopback inside the worker; the master reaches
# it through an SSH tunnel, so this port is never exposed to the internet.
WORKER_API_PORT = _int("WORKER_API_PORT", 8765)

# Address the worker API binds to. Inside Docker this MUST be 0.0.0.0, because
# Docker's published port (`-p 127.0.0.1:8765:8765`) forwards to the container's
# network interface, NOT the container's loopback — so binding 127.0.0.1 inside
# the container makes the API unreachable. Host-side exposure stays loopback-only
# (enforced by the `-p 127.0.0.1:...` publish), so this is still not public.
WORKER_BIND_HOST = os.getenv("WORKER_BIND_HOST", "0.0.0.0").strip()

# Shared bearer token the worker API expects. In worker mode it is read from
# the environment; on the master it is generated per-worker and stored (enc.).
WORKER_API_TOKEN = os.getenv("WORKER_API_TOKEN", "").strip()

# Should the master machine itself also act as a (local) worker?
MASTER_AS_WORKER = (os.getenv("MASTER_AS_WORKER", "true").strip().lower()
                    in ("1", "true", "yes", "on"))

# Health check: each worker performs a GET to this Rubika upload endpoint.
#   HTTP 200 / 404  -> "File ok"  (route is healthy)
#   HTTP 503        -> "Blocked"  (route is rate-limited / blocked)
HEALTH_URL = os.getenv("HEALTH_URL", "https://upmessenger490.iranlms.ir/UploadFile.ashx").strip()
HEALTH_TIMEOUT = _int("HEALTH_TIMEOUT", 15)

# How often (seconds) the master posts the "STATU WORKER ALL" report.
HEALTH_INTERVAL = _int("HEALTH_INTERVAL", 1800)  # 30 minutes

# Ping colour thresholds in milliseconds:
#   ping <= GREEN          -> 🟢
#   GREEN < ping <= YELLOW -> 🟡
#   ping > YELLOW / blocked -> 🔴
PING_GREEN_MS = _int("PING_GREEN_MS", 800)
PING_YELLOW_MS = _int("PING_YELLOW_MS", 2000)

# All log timestamps use this timezone regardless of the server location.
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tehran").strip()


def clamp_delay(value) -> float:
    """Keep the configured delay inside [MIN_DELAY, MAX_DELAY]."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_DELAY
    return max(MIN_DELAY, min(MAX_DELAY, value))


def clamp_interval(value) -> int:
    """Keep the automation interval inside [AUTOMATION_MIN_INTERVAL, MAX]."""
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return AUTOMATION_MIN_INTERVAL
    return max(AUTOMATION_MIN_INTERVAL, min(AUTOMATION_MAX_INTERVAL, value))


def validate() -> list:
    """Return a list of missing required settings (empty list = OK)."""
    problems = []
    if not API_ID:
        problems.append("API_ID")
    if not API_HASH:
        problems.append("API_HASH")
    if not BOT_TOKEN:
        problems.append("BOT_TOKEN")
    if not OWNER_ID:
        problems.append("OWNER_ID")
    if not LOG_GROUP_ID:
        problems.append("LOG_GROUP_ID")
    return problems


def validate_worker() -> list:
    """Required settings when this process runs in MODE=worker."""
    problems = []
    if not WORKER_API_TOKEN:
        problems.append("WORKER_API_TOKEN")
    return problems


# --------------------------------------------------------------------------- #
# Timezone-aware "now" (used by every log card so timestamps are Iran time
# even when a worker runs on a foreign server).
# --------------------------------------------------------------------------- #
def _tzinfo():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TIMEZONE)
    except Exception:
        try:
            import pytz
            return pytz.timezone(TIMEZONE)
        except Exception:
            return None


def now_dt():
    """Current datetime in the configured timezone (naive fallback)."""
    from datetime import datetime
    tz = _tzinfo()
    return datetime.now(tz) if tz else datetime.now()


def now_str() -> str:
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")
