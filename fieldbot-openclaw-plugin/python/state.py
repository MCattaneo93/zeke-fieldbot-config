"""SQLite state: last timesheet entries, claim timestamps, pending button
choices, and misc metadata (e.g. last announced ticket id)."""
import datetime
import json
import sqlite3
import uuid
from zoneinfo import ZoneInfo

import config


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS last_entry (
            telegram_id INTEGER PRIMARY KEY, ts_id INTEGER, task_id INTEGER);
        CREATE TABLE IF NOT EXISTS claims (
            telegram_id INTEGER, task_id INTEGER, claimed_at TEXT,
            PRIMARY KEY (telegram_id, task_id));
        CREATE TABLE IF NOT EXISTS pending_choice (
            id TEXT PRIMARY KEY, telegram_id INTEGER, action TEXT,
            params TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS sweep_snooze (
            task_id INTEGER PRIMARY KEY, until TEXT, by_telegram_id INTEGER,
            created_at TEXT);
        CREATE TABLE IF NOT EXISTS last_event (
            telegram_id INTEGER PRIMARY KEY, ics TEXT, title TEXT);
        """
    )
    return conn


def _now() -> datetime.datetime:
    return datetime.datetime.now(ZoneInfo(config.TZNAME))


# --- last timesheet entry (for "actually make that 3 hours") ----------------
def remember_entry(telegram_id: int, ts_id: int | None, task_id: int):
    if ts_id is None:
        return
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO last_entry VALUES (?,?,?)",
            (telegram_id, ts_id, task_id),
        )


def last_entry(telegram_id: int) -> int | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT ts_id FROM last_entry WHERE telegram_id=?", (telegram_id,)
        ).fetchone()
    return row[0] if row else None


# --- claim timestamps (for auto-computed hours + EOD nudge) -----------------
def record_claim(telegram_id: int, task_id: int):
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO claims VALUES (?,?,?)",
            (telegram_id, task_id, _now().isoformat()),
        )


def claim_elapsed_hours(telegram_id: int, task_id: int) -> float | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT claimed_at FROM claims WHERE telegram_id=? AND task_id=?",
            (telegram_id, task_id),
        ).fetchone()
    if not row:
        return None
    claimed = datetime.datetime.fromisoformat(row[0])
    return (_now() - claimed).total_seconds() / 3600


def claims_today(telegram_id: int) -> list[int]:
    today = _now().date().isoformat()
    with _db() as conn:
        rows = conn.execute(
            "SELECT task_id FROM claims WHERE telegram_id=? AND claimed_at >= ?",
            (telegram_id, today),
        ).fetchall()
    return [r[0] for r in rows]


def clear_claim(telegram_id: int, task_id: int):
    with _db() as conn:
        conn.execute(
            "DELETE FROM claims WHERE telegram_id=? AND task_id=?",
            (telegram_id, task_id),
        )


def claim_owner(task_id: int) -> int | None:
    """Telegram id of whoever most recently took this ticket."""
    with _db() as conn:
        row = conn.execute(
            "SELECT telegram_id FROM claims WHERE task_id=? "
            "ORDER BY claimed_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    return row[0] if row else None


def clear_claims_for_task(task_id: int):
    with _db() as conn:
        conn.execute("DELETE FROM claims WHERE task_id=?", (task_id,))


# --- pending inline-button choices ------------------------------------------
def save_choice(telegram_id: int, action: str, params: dict) -> str:
    pid = uuid.uuid4().hex[:12]
    with _db() as conn:
        conn.execute(
            "INSERT INTO pending_choice VALUES (?,?,?,?,?)",
            (pid, telegram_id, action, json.dumps(params), _now().isoformat()),
        )
        # drop anything older than a day
        cutoff = (_now() - datetime.timedelta(days=1)).isoformat()
        conn.execute("DELETE FROM pending_choice WHERE created_at < ?", (cutoff,))
    return pid


def pop_choice(pid: str) -> tuple[int, str, dict] | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT telegram_id, action, params FROM pending_choice WHERE id=?",
            (pid,),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM pending_choice WHERE id=?", (pid,))
    return (row[0], row[1], json.loads(row[2])) if row else None


# --- sweep snoozes -----------------------------------------------------------
# Snooze deliberately lives HERE and not in Odoo. It used to create a "Check in"
# mail.activity, which meant the ticket then had a next step and vanished from
# the sweep permanently - and came back a week later as an overdue item in
# everyone's dispatch and recaps. 32 of 58 open activities were that string.
# A snooze is a private "not now", not a commitment; it belongs in the bot.
def snooze_task(task_id: int, until: str, telegram_id: int | None = None):
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sweep_snooze VALUES (?,?,?,?)",
            (task_id, until, telegram_id, _now().isoformat()),
        )


def snoozed_task_ids() -> set[int]:
    """Tasks still snoozed as of today. Expired rows are dropped on read."""
    today = _now().date().isoformat()
    with _db() as conn:
        conn.execute("DELETE FROM sweep_snooze WHERE until <= ?", (today,))
        rows = conn.execute("SELECT task_id FROM sweep_snooze").fetchall()
    return {r[0] for r in rows}


def snooze_until(task_id: int) -> str | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT until FROM sweep_snooze WHERE task_id=?", (task_id,)
        ).fetchone()
    return row[0] if row else None


def clear_snooze(task_id: int):
    with _db() as conn:
        conn.execute("DELETE FROM sweep_snooze WHERE task_id=?", (task_id,))


# --- last created calendar event (for "invite Mike" follow-ups) -------------
def remember_event(telegram_id: int, ics: str, title: str):
    with _db() as conn:
        conn.execute("INSERT OR REPLACE INTO last_event VALUES (?,?,?)",
                     (telegram_id, ics, title))


def last_event(telegram_id: int) -> tuple[str, str] | None:
    """Returns (ics_text, title) of the sender's most recent event."""
    with _db() as conn:
        row = conn.execute(
            "SELECT ics, title FROM last_event WHERE telegram_id=?",
            (telegram_id,),
        ).fetchone()
    return (row[0], row[1]) if row else None


# --- meta key/value ----------------------------------------------------------
def get_meta(key: str) -> str | None:
    with _db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(key: str, value: str):
    with _db() as conn:
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
