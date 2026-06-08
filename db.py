"""SQLite storage: the user's own Rubika account(s) + panel settings.

Deliberately minimal: just `accounts` and a single-row `settings`.
No proxy tables, no broadcast queues — this is a small personal tool.
"""
import os
import sqlite3
from datetime import datetime

import config

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "data.db")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    conn = _conn()
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            phone     TEXT UNIQUE,
            name      TEXT,
            user_id   TEXT,
            session   TEXT,
            added_at  TEXT,
            status    TEXT DEFAULT 'active'
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            send_delay REAL,
            marker     TEXT
        )
        """
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (id, send_delay, marker) VALUES (1, ?, ?)",
        (config.DEFAULT_DELAY, config.FORWARD_MARKER),
    )

    # ---- Worker subsystem tables (additive; never touches the originals) ----
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS workers (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            tag          TEXT UNIQUE,
            ip           TEXT,
            ssh_port     INTEGER DEFAULT 22,
            ssh_user     TEXT,
            ssh_pass_enc TEXT,
            api_port     INTEGER,
            api_token_enc TEXT,
            is_master    INTEGER DEFAULT 0,
            enabled      INTEGER DEFAULT 1,
            status       TEXT DEFAULT 'unknown',
            ping_ms      INTEGER DEFAULT -1,
            file_ok      INTEGER DEFAULT 0,
            last_checked TEXT,
            created_at   TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS admins (
            user_id  INTEGER PRIMARY KEY,
            name     TEXT,
            added_at TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_daily (
            worker_id INTEGER,
            day       TEXT,
            sent      INTEGER DEFAULT 0,
            PRIMARY KEY (worker_id, day)
        )
        """
    )

    # ---- Automation tables (rotating texts to an account's groups) ----
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS automation (
            account_id   INTEGER PRIMARY KEY,
            enabled      INTEGER DEFAULT 0,
            interval_sec INTEGER DEFAULT 30,
            sent_total   INTEGER DEFAULT 0,
            updated_at   TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS automation_texts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            text       TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS automation_links (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            link       TEXT
        )
        """
    )

    # ---- migration: add accounts.worker_id (account -> worker affinity) ----
    cols = [r["name"] for r in c.execute("PRAGMA table_info(accounts)").fetchall()]
    if "worker_id" not in cols:
        c.execute("ALTER TABLE accounts ADD COLUMN worker_id INTEGER")

    conn.commit()
    conn.close()


# ---------- accounts ----------

def add_account(phone: str, name: str, user_id: str, session: str) -> int:
    conn = _conn()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO accounts (phone, name, user_id, session, added_at, status)
        VALUES (?, ?, ?, ?, ?, 'active')
        ON CONFLICT(phone) DO UPDATE SET
            name=excluded.name,
            user_id=excluded.user_id,
            session=excluded.session,
            status='active'
        """,
        (phone, name, user_id, session, _now()),
    )
    conn.commit()
    row = c.execute("SELECT id FROM accounts WHERE phone = ?", (phone,)).fetchone()
    conn.close()
    return row["id"]


def list_accounts() -> list:
    conn = _conn()
    rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_account(account_id: int):
    conn = _conn()
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_account(account_id: int):
    conn = _conn()
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.execute("DELETE FROM automation WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM automation_texts WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM automation_links WHERE account_id = ?", (account_id,))
    conn.commit()
    conn.close()


def set_status(account_id: int, status: str):
    conn = _conn()
    conn.execute("UPDATE accounts SET status = ? WHERE id = ?", (status, account_id))
    conn.commit()
    conn.close()


# ---------- settings ----------

def get_settings() -> dict:
    conn = _conn()
    row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    conn.close()
    if not row:
        return {"send_delay": config.DEFAULT_DELAY, "marker": config.FORWARD_MARKER}
    return dict(row)


def get_delay() -> float:
    return config.clamp_delay(get_settings().get("send_delay"))


def set_delay(value: float):
    conn = _conn()
    conn.execute("UPDATE settings SET send_delay = ? WHERE id = 1",
                 (config.clamp_delay(value),))
    conn.commit()
    conn.close()


def get_marker() -> str:
    return (get_settings().get("marker") or config.FORWARD_MARKER).strip()


def set_marker(marker: str):
    conn = _conn()
    conn.execute("UPDATE settings SET marker = ? WHERE id = 1", (marker.strip(),))
    conn.commit()
    conn.close()



# --------------------------------------------------------------------------- #
# Admins (extra Telegram ids allowed to use the panel, added by the owner).
# OWNER_ID is always allowed and is NOT stored here.
# --------------------------------------------------------------------------- #
def add_admin(user_id: int, name: str = ""):
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO admins (user_id, name, added_at) VALUES (?, ?, ?)",
        (int(user_id), name or "", _now()),
    )
    conn.commit()
    conn.close()


def remove_admin(user_id: int):
    conn = _conn()
    conn.execute("DELETE FROM admins WHERE user_id = ?", (int(user_id),))
    conn.commit()
    conn.close()


def list_admins() -> list:
    conn = _conn()
    rows = conn.execute("SELECT * FROM admins ORDER BY added_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_admin_ids() -> list:
    return [int(a["user_id"]) for a in list_admins()]


# --------------------------------------------------------------------------- #
# Workers
# --------------------------------------------------------------------------- #
def add_worker(tag: str, ip: str, ssh_port: int, ssh_user: str,
               ssh_pass_enc: str, api_port: int, api_token_enc: str,
               is_master: int = 0) -> int:
    conn = _conn()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO workers (tag, ip, ssh_port, ssh_user, ssh_pass_enc,
                             api_port, api_token_enc, is_master, enabled,
                             status, ping_ms, file_ok, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'unknown', -1, 0, ?)
        """,
        (tag, ip, int(ssh_port or 22), ssh_user, ssh_pass_enc,
         int(api_port), api_token_enc, int(is_master), _now()),
    )
    conn.commit()
    wid = c.lastrowid
    conn.close()
    return wid


def list_workers() -> list:
    conn = _conn()
    rows = conn.execute("SELECT * FROM workers ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_enabled_workers() -> list:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM workers WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_worker(worker_id: int):
    conn = _conn()
    row = conn.execute("SELECT * FROM workers WHERE id = ?", (int(worker_id),)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_worker_by_tag(tag: str):
    conn = _conn()
    row = conn.execute("SELECT * FROM workers WHERE tag = ?", (tag,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_master_worker():
    conn = _conn()
    row = conn.execute("SELECT * FROM workers WHERE is_master = 1 LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None


def delete_worker(worker_id: int):
    conn = _conn()
    conn.execute("DELETE FROM workers WHERE id = ?", (int(worker_id),))
    conn.execute("DELETE FROM worker_daily WHERE worker_id = ?", (int(worker_id),))
    # detach accounts that were bound to this worker
    conn.execute("UPDATE accounts SET worker_id = NULL WHERE worker_id = ?",
                 (int(worker_id),))
    conn.commit()
    conn.close()


def set_worker_enabled(worker_id: int, enabled: bool):
    conn = _conn()
    conn.execute("UPDATE workers SET enabled = ? WHERE id = ?",
                 (1 if enabled else 0, int(worker_id)))
    conn.commit()
    conn.close()


def update_worker_health(worker_id: int, status: str, ping_ms: int, file_ok: bool):
    conn = _conn()
    conn.execute(
        "UPDATE workers SET status = ?, ping_ms = ?, file_ok = ?, last_checked = ? "
        "WHERE id = ?",
        (status, int(ping_ms), 1 if file_ok else 0, _now(), int(worker_id)),
    )
    conn.commit()
    conn.close()


def count_accounts_on_worker(worker_id: int) -> int:
    conn = _conn()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM accounts WHERE worker_id = ?", (int(worker_id),)
    ).fetchone()
    conn.close()
    return int(row["n"]) if row else 0


def set_account_worker(account_id: int, worker_id):
    conn = _conn()
    conn.execute("UPDATE accounts SET worker_id = ? WHERE id = ?",
                 (worker_id, int(account_id)))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# Per-worker daily send counter (no cap; informational + routing hint).
# --------------------------------------------------------------------------- #
def _today() -> str:
    return config.now_dt().strftime("%Y-%m-%d")


def incr_worker_sent(worker_id: int, n: int = 1):
    conn = _conn()
    day = _today()
    conn.execute(
        "INSERT INTO worker_daily (worker_id, day, sent) VALUES (?, ?, ?) "
        "ON CONFLICT(worker_id, day) DO UPDATE SET sent = sent + ?",
        (int(worker_id), day, int(n), int(n)),
    )
    conn.commit()
    conn.close()


def worker_sent_today(worker_id: int) -> int:
    conn = _conn()
    row = conn.execute(
        "SELECT sent FROM worker_daily WHERE worker_id = ? AND day = ?",
        (int(worker_id), _today()),
    ).fetchone()
    conn.close()
    return int(row["sent"]) if row else 0


# --------------------------------------------------------------------------- #
# Automation (rotating texts to an account's groups). One row per account.
# --------------------------------------------------------------------------- #
def _ensure_automation_row(c, account_id: int):
    c.execute(
        "INSERT OR IGNORE INTO automation (account_id, enabled, interval_sec, "
        "sent_total, updated_at) VALUES (?, 0, 30, 0, ?)",
        (int(account_id), _now()),
    )


def get_automation(account_id: int) -> dict:
    conn = _conn()
    c = conn.cursor()
    _ensure_automation_row(c, account_id)
    conn.commit()
    row = c.execute("SELECT * FROM automation WHERE account_id = ?",
                    (int(account_id),)).fetchone()
    conn.close()
    return dict(row) if row else {"account_id": account_id, "enabled": 0,
                                  "interval_sec": 30, "sent_total": 0}


def set_automation_enabled(account_id: int, enabled: bool):
    conn = _conn()
    c = conn.cursor()
    _ensure_automation_row(c, account_id)
    c.execute("UPDATE automation SET enabled = ?, updated_at = ? WHERE account_id = ?",
              (1 if enabled else 0, _now(), int(account_id)))
    conn.commit()
    conn.close()


def set_automation_interval(account_id: int, interval_sec: int):
    conn = _conn()
    c = conn.cursor()
    _ensure_automation_row(c, account_id)
    c.execute("UPDATE automation SET interval_sec = ?, updated_at = ? WHERE account_id = ?",
              (config.clamp_interval(interval_sec), _now(), int(account_id)))
    conn.commit()
    conn.close()


def incr_automation_sent(account_id: int, n: int = 1):
    conn = _conn()
    c = conn.cursor()
    _ensure_automation_row(c, account_id)
    c.execute("UPDATE automation SET sent_total = sent_total + ? WHERE account_id = ?",
              (int(n), int(account_id)))
    conn.commit()
    conn.close()


def list_enabled_automations() -> list:
    conn = _conn()
    rows = conn.execute("SELECT * FROM automation WHERE enabled = 1").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_automation_text(account_id: int, text: str):
    conn = _conn()
    conn.execute("INSERT INTO automation_texts (account_id, text) VALUES (?, ?)",
                 (int(account_id), text))
    conn.commit()
    conn.close()


def list_automation_texts(account_id: int) -> list:
    conn = _conn()
    rows = conn.execute(
        "SELECT text FROM automation_texts WHERE account_id = ? ORDER BY id",
        (int(account_id),)).fetchall()
    conn.close()
    return [r["text"] for r in rows]


def clear_automation_texts(account_id: int):
    conn = _conn()
    conn.execute("DELETE FROM automation_texts WHERE account_id = ?", (int(account_id),))
    conn.commit()
    conn.close()


def add_automation_link(account_id: int, link: str):
    conn = _conn()
    conn.execute("INSERT INTO automation_links (account_id, link) VALUES (?, ?)",
                 (int(account_id), link))
    conn.commit()
    conn.close()


def list_automation_links(account_id: int) -> list:
    conn = _conn()
    rows = conn.execute(
        "SELECT link FROM automation_links WHERE account_id = ? ORDER BY id",
        (int(account_id),)).fetchall()
    conn.close()
    return [r["link"] for r in rows]


def clear_automation_links(account_id: int):
    conn = _conn()
    conn.execute("DELETE FROM automation_links WHERE account_id = ?", (int(account_id),))
    conn.commit()
    conn.close()
