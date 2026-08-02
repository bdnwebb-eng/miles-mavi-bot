"""SQLite persistence for Hermes: users, program progress, goals, check-ins, chat history."""
from __future__ import annotations

import os
import re
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
        # v7 transplant (2026-08-02): clean-head migration, one time. The old raw
        # memories move aside to an archive table (nothing destroyed); the fresh
        # memories table plus the curated tier get created below.
        try:
            has_old = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memories'").fetchone()
            has_arch = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memories_archive_v6'").fetchone()
            if has_old and not has_arch:
                n = c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                c.execute("ALTER TABLE memories RENAME TO memories_archive_v6")
                print(f"[db] transplant: archived {n} old memories to memories_archive_v6; clean head from here", flush=True)
        except sqlite3.Error as e:
            print(f"[db] transplant migration skipped: {e}", flush=True)
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
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id  INTEGER,
                category     TEXT DEFAULT 'fact',
                content      TEXT,
                source       TEXT DEFAULT 'auto',
                created_at   TEXT,
                consolidated INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS memories_curated (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                category    TEXT DEFAULT 'fact',
                content     TEXT,
                superseded  INTEGER DEFAULT 0,
                created_at  TEXT,
                updated_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS consolidation_log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at   TEXT,
                raw_seen INTEGER,
                kept     INTEGER,
                superseded INTEGER,
                dropped  INTEGER,
                note     TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                role        TEXT,
                content     TEXT,
                created_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS action_log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                tid     INTEGER,
                ts_utc  TEXT,
                tool    TEXT,
                summary TEXT,
                link    TEXT DEFAULT ''
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


# ───────────────────────── action log (verified action ledger, truth gate v6.4) ─────────────────────────
# Ground truth of every SUCCESSFUL write Miles has actually performed (calendar
# creates and deletes, Gmail drafts, Docs/Sheets/Slides/Drive writes, Notion
# property updates, Slack posts). ai.py injects the recent entries into the
# system prompt so Miles can never honestly claim an unperformed action and
# never denies one he actually performed. Survives restarts on the Railway
# volume via HERMES_DB_PATH.
def _ensure_action_log() -> None:
    with _conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS action_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "tid INTEGER, ts_utc TEXT, tool TEXT, summary TEXT, link TEXT DEFAULT '')"
        )


def log_action(tid: int, tool: str, summary: str, link: str = "") -> None:
    """Record one VERIFIED successful write action in the ledger."""
    _ensure_action_log()
    with _conn() as c:
        c.execute(
            "INSERT INTO action_log (tid, ts_utc, tool, summary, link) VALUES (?,?,?,?,?)",
            (tid, datetime.utcnow().isoformat(), tool,
             (summary or "").strip(), (link or "").strip()),
        )


def recent_actions(tid: int, limit: int = 30) -> list[sqlite3.Row]:
    """Most recent verified actions, newest first."""
    _ensure_action_log()
    with _conn() as c:
        return c.execute(
            "SELECT * FROM action_log WHERE tid=? ORDER BY id DESC LIMIT ?",
            (tid, limit),
        ).fetchall()


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


# ───────────────────────── google oauth (refresh token on the Railway volume) ─────────────────────────
# Miles serves exactly one principal (Kas), so Google is a single shared identity.
# The shared refresh token lives in one sentinel row keyed tid=0; any allowed user
# (Kas, Brandon the tester, anyone in allowed_ids) transparently uses the same token
# with no per-user re-auth.
SHARED_GOOGLE_TID = 0


def _ensure_google_auth() -> None:
    with _conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS google_auth (tid INTEGER PRIMARY KEY, "
            "refresh_token TEXT, scopes TEXT, created TEXT)"
        )


def set_google_token(refresh_token: str, scopes: str) -> None:
    """Store the single shared Google refresh token (upsert on the tid=0 sentinel row).
    Any allowed user running /connectgoogle updates the connection for everyone.
    Survives redeploys (Railway volume)."""
    _ensure_google_auth()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO google_auth (tid, refresh_token, scopes, created) "
            "VALUES (?,?,?,?)",
            (SHARED_GOOGLE_TID, refresh_token, scopes, datetime.utcnow().isoformat()),
        )


def get_google_token() -> str | None:
    """Return the single shared Google refresh token.

    Prefer the sentinel row (tid=0). If no sentinel row exists yet, fall back to the
    most recent legacy per-user row and adopt it as the shared token (copied into the
    sentinel once) so an already-authorized connection is NEVER lost on migration.
    """
    _ensure_google_auth()
    with _conn() as c:
        row = c.execute(
            "SELECT refresh_token FROM google_auth WHERE tid=?", (SHARED_GOOGLE_TID,)
        ).fetchone()
        if row and row[0]:
            return row[0]
        # Backward-compat: adopt the most recent existing per-tid token as the shared one.
        legacy = c.execute(
            "SELECT refresh_token, scopes FROM google_auth "
            "WHERE tid<>? AND refresh_token IS NOT NULL "
            "ORDER BY rowid DESC LIMIT 1",
            (SHARED_GOOGLE_TID,),
        ).fetchone()
        if legacy and legacy[0]:
            c.execute(
                "INSERT OR REPLACE INTO google_auth (tid, refresh_token, scopes, created) "
                "VALUES (?,?,?,?)",
                (SHARED_GOOGLE_TID, legacy[0], legacy[1], datetime.utcnow().isoformat()),
            )
            return legacy[0]
    return None


def clear_google_token() -> None:
    """Drop the shared Google connection (all rows), forcing a fresh /connectgoogle."""
    _ensure_google_auth()
    with _conn() as c:
        c.execute("DELETE FROM google_auth")


# ---- legacy per-tid helpers (kept for backward compatibility) ----
def set_google_auth(tid: int, refresh_token: str, scopes: str) -> None:
    """Deprecated: writes the shared token regardless of tid (single-tenant bot)."""
    set_google_token(refresh_token, scopes)


def get_google_refresh_token(tid: int) -> str | None:
    """Deprecated: returns the shared token regardless of tid (single-tenant bot)."""
    return get_google_token()


def get_google_auth(tid: int) -> sqlite3.Row | None:
    _ensure_google_auth()
    with _conn() as c:
        return c.execute("SELECT * FROM google_auth WHERE tid=?", (tid,)).fetchone()


def clear_google_auth(tid: int) -> None:
    _ensure_google_auth()
    with _conn() as c:
        c.execute("DELETE FROM google_auth WHERE tid=?", (tid,))


# ───────────────────────── sentinel state (self-monitoring watchdog) ─────────────────────────
# Small key/value store so the Sentinel watchdog remembers the last per-check status
# and last-alert timestamps across restarts (no alert spam on redeploy).
def _ensure_sentinel_state() -> None:
    with _conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS sentinel_state (key TEXT PRIMARY KEY, "
            "value TEXT, updated TEXT)"
        )


def set_sentinel_state(key: str, value: str) -> None:
    _ensure_sentinel_state()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO sentinel_state (key, value, updated) VALUES (?,?,?)",
            (key, value, datetime.utcnow().isoformat()),
        )


def get_sentinel_state(key: str, default: str | None = None) -> str | None:
    _ensure_sentinel_state()
    with _conn() as c:
        row = c.execute("SELECT value FROM sentinel_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


# ───────────────────────── dashboard notes (Kas flags things for Brandon) ─────────────────────────
# v8: every dashboard section carries a small Add note affordance. Notes land here
# (on the Railway volume via HERMES_DB_PATH, so they survive redeploys), surface in
# the /api/status payload under "notes", and fire an operator alert to Brandon.
def _ensure_dashboard_notes() -> None:
    with _conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS dashboard_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts_utc TEXT, section TEXT, text TEXT)"
        )


def add_note(section: str, text: str) -> int:
    """Store one dashboard note. Caps section and text length. Returns the row id."""
    _ensure_dashboard_notes()
    section = (section or "General").strip()[:120]
    text = (text or "").strip()[:2000]
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO dashboard_notes (ts_utc, section, text) VALUES (?,?,?)",
            (datetime.utcnow().isoformat(), section, text),
        )
        return int(cur.lastrowid or 0)


def recent_notes(limit: int = 50) -> list[sqlite3.Row]:
    """Most recent dashboard notes, newest first."""
    _ensure_dashboard_notes()
    with _conn() as c:
        return c.execute(
            "SELECT * FROM dashboard_notes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


# ---------- curated memory + recall (v7 transplant) ----------

def curated_for_prompt(char_cap: int = 6000) -> list[sqlite3.Row]:
    """Live curated tier, oldest first, trimmed to the char cap (newest win)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM memories_curated WHERE superseded=0 ORDER BY id DESC"
        ).fetchall()
    out, total = [], 0
    for r in rows:
        ln = len(r["content"] or "")
        if total + ln > char_cap:
            break
        out.append(r)
        total += ln
    return list(reversed(out))


def curated_all() -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute("SELECT * FROM memories_curated WHERE superseded=0 ORDER BY id").fetchall()


def add_curated(category: str, content: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO memories_curated (category, content, created_at, updated_at) VALUES (?,?,?,?)",
            (category, content, _now(), _now()),
        )


def supersede_curated(ids: list[int]) -> int:
    if not ids:
        return 0
    with _conn() as c:
        q = ",".join("?" for _ in ids)
        cur = c.execute(f"UPDATE memories_curated SET superseded=1, updated_at=? WHERE id IN ({q})", (_now(), *ids))
        return cur.rowcount


def enforce_curated_cap(char_cap: int = 6000) -> int:
    """Machine-enforced v1.3 cap: oldest entries beyond the cap get superseded."""
    with _conn() as c:
        rows = c.execute("SELECT id, LENGTH(content) AS ln FROM memories_curated WHERE superseded=0 ORDER BY id DESC").fetchall()
        total, cut = 0, []
        for r in rows:
            total += r["ln"] or 0
            if total > char_cap:
                cut.append(r["id"])
        if cut:
            q = ",".join("?" for _ in cut)
            c.execute(f"UPDATE memories_curated SET superseded=1, updated_at=? WHERE id IN ({q})", (_now(), *cut))
        return len(cut)


def unconsolidated_memories() -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute("SELECT * FROM memories WHERE consolidated=0 ORDER BY id LIMIT 120").fetchall()


def archive_consolidated(ids: list[int]) -> None:
    if not ids:
        return
    with _conn() as c:
        q = ",".join("?" for _ in ids)
        c.execute(f"UPDATE memories SET consolidated=1 WHERE id IN ({q})", ids)


def log_consolidation(summary: dict, note: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO consolidation_log (ran_at, raw_seen, kept, superseded, dropped, note) VALUES (?,?,?,?,?,?)",
            (_now(), summary.get("raw_seen", 0), summary.get("kept", 0),
             summary.get("superseded", 0), summary.get("dropped", 0), note[:1500]),
        )


def search_history(query: str, limit: int = 8) -> list[dict]:
    """Recall over the message log and every memory tier (raw, archive, curated).
    Simple term match, recency weighted (newest first). Returns dicts with
    source, when, and text."""
    terms = [t for t in re.split(r"[^\w]+", (query or "").lower()) if len(t) >= 3][:6]
    if not terms:
        return []
    like = " AND ".join("LOWER(content) LIKE ?" for _ in terms)
    args = tuple(f"%{t}%" for t in terms)
    out: list[dict] = []
    with _conn() as c:
        def _grab(sql: str, source: str, a=args):
            try:
                for r in c.execute(sql, a).fetchall():
                    out.append({"source": source, "when": r["created_at"], "text": r["content"]})
            except sqlite3.Error:
                pass
        _grab(f"SELECT content, created_at FROM messages WHERE {like} ORDER BY id DESC LIMIT {int(limit)}", "conversation")
        _grab(f"SELECT content, created_at FROM memories WHERE {like} ORDER BY id DESC LIMIT {int(limit)}", "memory")
        _grab(f"SELECT content, created_at FROM memories_archive_v6 WHERE {like} ORDER BY id DESC LIMIT {int(limit)}", "memory-archive")
        _grab(f"SELECT content, created_at AS created_at FROM memories_curated WHERE {like} ORDER BY id DESC LIMIT {int(limit)}", "curated")
    out.sort(key=lambda r: str(r.get("when") or ""), reverse=True)
    return out[: int(limit) * 2]


# ────────────────────────── connection vault (additive) ──────────────────────────
# Keys Kas adds from the dashboard Settings page. ADDITIVE ONLY: connectors
# resolve credentials environment-first (connectors.cred), so nothing stored
# here can ever override a credential the operator set in Railway. Secrets stay
# in this database on the bot's private volume and are never echoed back in
# full by any API.

def _ensure_connections() -> None:
    with _conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS connections (name TEXT PRIMARY KEY, kind TEXT, "
            "secret TEXT, status TEXT, detail TEXT, added_by TEXT, "
            "added_utc TEXT, checked_utc TEXT)")


def vault_set(name: str, kind: str, secret: str, status: str, detail: str = "",
              added_by: str = "dashboard") -> None:
    _ensure_connections()
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with _conn() as c:
        c.execute(
            "INSERT INTO connections (name, kind, secret, status, detail, added_by, added_utc, checked_utc) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, secret=excluded.secret, "
            "status=excluded.status, detail=excluded.detail, checked_utc=excluded.checked_utc",
            (name, kind, secret, status, detail, added_by, now, now))


def vault_all() -> list[sqlite3.Row]:
    _ensure_connections()
    with _conn() as c:
        return list(c.execute(
            "SELECT name, kind, status, detail, added_by, added_utc, checked_utc "
            "FROM connections ORDER BY added_utc"))


def vault_secret(env_or_name: str) -> str | None:
    """Look a secret up by the env-style slot for its kind (e.g. NOTION_API_KEY)
    or by the row's own name. Used by connectors.cred() as the fallback AFTER
    the environment, so vault entries only ever fill empty slots."""
    _ensure_connections()
    kind_by_env = {
        "NOTION_API_KEY": "notion", "SLACK_BOT_TOKEN": "slack",
        "ELEVENLABS_API_KEY": "elevenlabs", "OPENAI_API_KEY": "openai",
        "COMPOSIO_API_KEY": "composio",
    }
    with _conn() as c:
        row = c.execute("SELECT secret FROM connections WHERE name = ?", (env_or_name,)).fetchone()
        if row:
            return row["secret"]
        kind = kind_by_env.get(env_or_name)
        if kind:
            row = c.execute(
                "SELECT secret FROM connections WHERE kind = ? AND status = 'live' "
                "ORDER BY added_utc DESC LIMIT 1", (kind,)).fetchone()
            if row:
                return row["secret"]
    return None
