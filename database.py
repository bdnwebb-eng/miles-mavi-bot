"""SQLite persistence for Hermes: users, program progress, goals, check-ins, chat history."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, date

# Honor HERMES_DB_PATH (Railway volume) so memory survives restarts and redeploys.
DB_PATH = os.environ.get("HERMES_DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "hermes.db"
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id   INTEGER PRIMARY KEY,
                name          TEXT,
                joined_at     TEXT,
                reminders_on  INTEGER DEFAULT 1,
                reminder_time TEXT,
                approved      INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS progress (
                telegram_id  INTEGER,
                lesson_id    TEXT,
                status       TEXT,
                completed_at TEXT,
                PRIMARY KEY (telegram_id, lesson_id)
            );
            CREATE TABLE IF NOT EXISTS goals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                text        TEXT,
                created_at  TEXT,
                active      INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS checkins (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                day         TEXT,
                note        TEXT,
                created_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS memories (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                category    TEXT DEFAULT 'fact',
                content     TEXT,
                source      TEXT DEFAULT 'auto',
                created_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                role        TEXT,
                content     TEXT,
                created_at  TEXT
            );
            """
        )


# ---------- users ----------
def get_user(tid: int) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE telegram_id=?", (tid,)).fetchone()


def upsert_user(tid: int, name: str, reminder_time: str) -> None:
    with _conn() as c:
        existing = c.execute("SELECT 1 FROM users WHERE telegram_id=?", (tid,)).fetchone()
        if existing:
            c.execute("UPDATE users SET name=? WHERE telegram_id=?", (name, tid))
        else:
            c.execute(
                "INSERT INTO users (telegram_id, name, joined_at, reminders_on, reminder_time) "
                "VALUES (?,?,?,?,?)",
                (tid, name, datetime.utcnow().isoformat(), 1, reminder_time),
            )


def set_approved(tid: int, approved: bool) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET approved=? WHERE telegram_id=?", (1 if approved else 0, tid))


def set_reminders(tid: int, on: bool, time_str: str | None = None) -> None:
    with _conn() as c:
        if time_str:
            c.execute(
                "UPDATE users SET reminders_on=?, reminder_time=? WHERE telegram_id=?",
                (1 if on else 0, time_str, tid),
            )
        else:
            c.execute("UPDATE users SET reminders_on=? WHERE telegram_id=?", (1 if on else 0, tid))


def users_with_reminders() -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM users WHERE reminders_on=1 AND reminder_time IS NOT NULL"
        ).fetchall()


# ---------- progress ----------
def complete_lesson(tid: int, lesson_id: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO progress (telegram_id, lesson_id, status, completed_at) "
            "VALUES (?,?,?,?)",
            (tid, lesson_id, "done", datetime.utcnow().isoformat()),
        )


def completed_lessons(tid: int) -> set[str]:
    with _conn() as c:
        rows = c.execute(
            "SELECT lesson_id FROM progress WHERE telegram_id=? AND status='done'", (tid,)
        ).fetchall()
        return {r["lesson_id"] for r in rows}


# ---------- goals ----------
def add_goal(tid: int, text: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO goals (telegram_id, text, created_at, active) VALUES (?,?,?,1)",
            (tid, text, datetime.utcnow().isoformat()),
        )


def active_goals(tid: int) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM goals WHERE telegram_id=? AND active=1 ORDER BY id", (tid,)
        ).fetchall()


def clear_goals(tid: int) -> None:
    with _conn() as c:
        c.execute("UPDATE goals SET active=0 WHERE telegram_id=?", (tid,))


# ---------- check-ins ----------
def add_checkin(tid: int, note: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO checkins (telegram_id, day, note, created_at) VALUES (?,?,?,?)",
            (tid, date.today().isoformat(), note, datetime.utcnow().isoformat()),
        )


def checkin_days(tid: int) -> list[str]:
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT day FROM checkins WHERE telegram_id=? ORDER BY day DESC", (tid,)
        ).fetchall()
        return [r["day"] for r in rows]


def checkin_streak(tid: int) -> int:
    """Consecutive days (ending today or yesterday) with a check-in."""
    days = set(checkin_days(tid))
    if not days:
        return 0
    from datetime import timedelta

    streak = 0
    cursor = date.today()
    # Allow the streak to count if they checked in today OR yesterday (grace).
    if cursor.isoformat() not in days:
        cursor = cursor - timedelta(days=1)
        if cursor.isoformat() not in days:
            return 0
    while cursor.isoformat() in days:
        streak += 1
        cursor = cursor - timedelta(days=1)
    return streak


# ---------- messages (AI memory) ----------
def add_message(tid: int, role: str, content: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO messages (telegram_id, role, content, created_at) VALUES (?,?,?,?)",
            (tid, role, content, datetime.utcnow().isoformat()),
        )


def recent_messages(tid: int, limit: int) -> list[sqlite3.Row]:
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content FROM messages WHERE telegram_id=? ORDER BY id DESC LIMIT ?",
            (tid, limit),
        ).fetchall()
        return list(reversed(rows))


# ───────────────────────── prefs (generic per-user settings) ─────────────────────────
def _ensure_prefs() -> None:
    with _conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS prefs (tid INTEGER, key TEXT, value TEXT, "
            "PRIMARY KEY (tid, key))"
        )


def set_pref(tid: int, key: str, value: str) -> None:
    _ensure_prefs()
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO prefs (tid, key, value) VALUES (?,?,?)", (tid, key, value))


def get_pref(tid: int, key: str, default: str | None = None) -> str | None:
    _ensure_prefs()
    with _conn() as c:
        row = c.execute("SELECT value FROM prefs WHERE tid=? AND key=?", (tid, key)).fetchone()
    return row[0] if row else default


# ---------- long term memory ----------
def add_memory(tid: int, content: str, category: str = "fact", source: str = "auto") -> bool:
    """Store one durable fact. Returns False on empty or duplicate content."""
    content = (content or "").strip()
    if not content:
        return False
    with _conn() as c:
        dup = c.execute(
            "SELECT 1 FROM memories WHERE telegram_id=? AND lower(content)=lower(?)",
            (tid, content),
        ).fetchone()
        if dup:
            return False
        c.execute(
            "INSERT INTO memories (telegram_id, category, content, source, created_at) "
            "VALUES (?,?,?,?,?)",
            (tid, category, content, source, datetime.utcnow().isoformat()),
        )
    return True


def memories_for_prompt(tid: int, limit: int = 48) -> list[sqlite3.Row]:
    """Most recent N memories, oldest first, for prompt injection."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM memories WHERE telegram_id=? ORDER BY id DESC LIMIT ?",
            (tid, limit),
        ).fetchall()
    return list(reversed(rows))


def all_memories(tid: int) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM memories WHERE telegram_id=? ORDER BY id", (tid,)
        ).fetchall()


def delete_memory(tid: int, mem_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM memories WHERE telegram_id=? AND id=?", (tid, mem_id))
    return cur.rowcount > 0


# ───────────────────────── energy tracking (Jul 10 meeting: daily score, pattern mapping) ─────────────────────────
def _ensure_energy() -> None:
    with _conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS energy (tid INTEGER, day TEXT, score INTEGER, "
            "note TEXT DEFAULT '', PRIMARY KEY (tid, day))"
        )


def add_energy(tid: int, score: int, note: str = "") -> None:
    _ensure_energy()
    from datetime import datetime
    day = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO energy (tid, day, score, note) VALUES (?,?,?,?)", (tid, day, score, note))


def energy_history(tid: int, days: int = 30) -> list:
    _ensure_energy()
    with _conn() as c:
        rows = c.execute(
            "SELECT day, score, note FROM energy WHERE tid=? ORDER BY day DESC LIMIT ?", (tid, days)
        ).fetchall()
    return list(reversed(rows))
