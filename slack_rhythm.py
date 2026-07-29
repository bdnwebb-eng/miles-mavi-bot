"""Miles's daily rhythm: the internal posts India used to carry (Jul 10 meeting).

Two scheduled jobs (bot.py JobQueue):
  morning_agenda : daily 07:40 Europe/Zurich. What's moving today, each meeting
                   explained, next actions owed, anything cold.
  eod_summary    : daily 18:30 Europe/Zurich. What moved today, what's open for
                   tomorrow, tomorrow's real calendar, and the energy question.

v6.7 (2026-07-28, Kas's feedback via Brandon): DELIVERY IS TELEGRAM ONLY.
The scheduled Slack posts are OFF; Slack is still READ (last day of team
channels) so Miles knows what's going on, and he still replies when tagged
(slack_socket.py). Briefs go to every reminder-enabled allowed user via the
Telegram Bot API (Kas once her id is in allowed_ids; Brandon meanwhile).
The EOD brief ends by asking Kas how her energy was today (1 to 10, voice
note welcome); her reply is logged via the energy_log tool.

v6.5/v6.6 grounding stands: every section is LIVE at compose time (calendar
via calendar_upcoming_v2 across all calendars, Gmail last 24h, Notion board
with last-edited tags, Slack read) and any unreadable source injects an
explicit UNAVAILABLE marker. The composer must say "could not verify" rather
than claiming an empty day or an empty inbox it never saw.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

import httpx

import ai
import config_loader as cfg
import database as db
import notion_watch
from connectors import LOCAL_TZ, GoogleConnector, NotionConnector, SlackConnector

log = logging.getLogger("slack_rhythm")

_NOTION_API = "https://api.notion.com/v1"
MAX_POST_TOKENS = 700


def enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN"))


# ───────────────────────── source gathering ─────────────────────────
def _project_rows() -> list[dict]:
    """Rows of the projects DB with flattened properties + last_edited_time.

    Reuses the Notion connector's auth and property flattening; adds the page
    level last_edited_time the connector tool omits (the EOD post needs it).
    """
    notion = NotionConnector()
    dbid = os.environ.get("NOTION_PROJECTS_DB_ID", "")
    if not (notion.configured() and dbid):
        return []
    rows: list[dict] = []
    with httpx.Client(timeout=25) as http:
        r = http.post(
            f"{_NOTION_API}/databases/{dbid}/query",
            headers=notion._headers(),
            json={"page_size": 50},
        )
        r.raise_for_status()
        for pg in r.json().get("results", []):
            props = {}
            for pname, prop in (pg.get("properties") or {}).items():
                val = NotionConnector._prop_value(prop)
                if val not in (None, "", []):
                    props[pname] = val
            rows.append(
                {
                    "title": NotionConnector._title_of(pg),
                    "last_edited": pg.get("last_edited_time", ""),
                    "properties": props,
                }
            )
    return rows


def _calendar_section(gc: GoogleConnector, label: str, day_iso: str) -> str:
    """One day of Kas's LIVE calendar (all calendars merged), or an explicit
    UNAVAILABLE marker. Never returns silence: the composer always sees either
    real data (including a verified empty day) or a could-not-read marker."""
    try:
        raw = gc.run("calendar_upcoming_v2", {"start_date": day_iso, "end_date": day_iso})
        data = json.loads(raw)  # raises ValueError on an "Error: ..." string
        if not isinstance(data, dict):
            raise ValueError("unexpected payload shape")
        return f"{label}:\n{raw}"
    except Exception as e:  # noqa: BLE001
        log.warning("Rhythm: calendar read failed for %s: %s", day_iso, e)
        return (
            f"{label}: UNAVAILABLE (live calendar could not be read just now). "
            "You must say the calendar could not be verified. Do NOT claim the day is clear."
        )


def _gmail_section(gc: GoogleConnector) -> str:
    """Kas's live inbox, last 24h, as metadata only (from/subject/date/snippet),
    or an explicit UNAVAILABLE marker."""
    try:
        raw = gc.run("gmail_recent", {"limit": 15, "query": "in:inbox newer_than:1d"})
        data = json.loads(raw)  # raises ValueError on an "Error from Gmail" string
        if not isinstance(data, list):
            raise ValueError("unexpected payload shape")
        if not data:
            return "KAS INBOX, last 24h (live Gmail read, VERIFIED): no inbox messages in the last 24 hours."
        return "KAS INBOX, last 24h (live Gmail, from/subject/snippet):\n" + raw
    except Exception as e:  # noqa: BLE001
        log.warning("Rhythm: gmail read failed: %s", e)
        return (
            "KAS INBOX: UNAVAILABLE (live Gmail could not be read just now). "
            "You must say emails could not be verified. Do NOT claim there were no emails."
        )


def _slack_section() -> str:
    """Recent messages from the Slack channels Miles is a member of, READ ONLY.
    v6.7: Miles reads Slack for context but never posts scheduled updates there."""
    sc = SlackConnector()
    if not sc.configured():
        return "SLACK: not connected."
    try:
        chans_raw = sc.run("slack_channels", {})
        chans = [c for c in json.loads(chans_raw) if c.get("is_member")][:3]
        if not chans:
            return "SLACK (read only): Miles is not in any channels yet."
        parts = []
        for ch in chans:
            msgs = sc.run("slack_read_channel", {"channel": ch["id"], "limit": 12})
            parts.append(f"#{ch.get('name','?')}: {msgs}")
        return "SLACK ACTIVITY, recent messages (READ ONLY; never post updates to Slack):\n" + "\n".join(parts)
    except Exception as e:  # noqa: BLE001
        log.warning("Rhythm: slack read failed: %s", e)
        return "SLACK: UNAVAILABLE (could not be read just now). Do not claim Slack was quiet."


def _context_block(kind: str) -> str:
    """Everything the model needs to write the brief, as plain text sections.

    Every section is pulled LIVE at compose time. A source that cannot be read
    is represented by an explicit UNAVAILABLE marker instead of silence, so the
    composer can never mistake missing data for an empty day or an empty inbox.
    """
    parts: list[str] = []
    now = datetime.now(LOCAL_TZ)
    parts.append(f"Today is {now.strftime('%A %Y-%m-%d')}, time now {now.strftime('%H:%M')} (Europe/Zurich).")

    today_iso = now.strftime("%Y-%m-%d")
    tomorrow = now + timedelta(days=1)
    tomorrow_iso = tomorrow.strftime("%Y-%m-%d")
    tomorrow_name = tomorrow.strftime("%A %d %B")

    # Notion projects board (live), with a last edited date on every row so the
    # composer can tell today's movement from stale carry over.
    rows: list[dict] | None
    try:
        rows = _project_rows()
    except Exception as e:  # noqa: BLE001
        rows = None
        log.warning("Rhythm: Notion projects unavailable: %s", e)
    if rows:
        lines = []
        for row in rows:
            keep = {
                k: v
                for k, v in row["properties"].items()
                if any(w in k.lower() for w in ("stage", "status", "health", "next", "action", "due", "date", "owner", "client", "live"))
            }
            edited = (row.get("last_edited") or "")[:10]
            if edited == today_iso:
                tag = " [EDITED TODAY]"
            elif edited:
                tag = f" [last edited {edited}]"
            else:
                tag = ""
            lines.append(f"- {row['title']}{tag}: {keep}")
        parts.append("Notion projects board (live):\n" + "\n".join(lines))
    elif rows is None:
        parts.append(
            "LEADS BOARD: UNAVAILABLE (Notion could not be read just now). "
            "Say the board could not be verified. Do NOT guess lead statuses."
        )

    try:
        cold = notion_watch.cold_items() if notion_watch.enabled() else []
    except Exception:  # noqa: BLE001
        cold = []
    if cold:
        parts.append(
            "Cold items (no activity for a while; live projects are exempt):\n"
            + "\n".join(f"- {c['title']}: quiet {c['days_quiet']} days" for c in cold)
        )

    # Calendar + inbox: LIVE Google, single tenant token, the same connector the
    # interactive bot uses (and the Sentinel forward probe health checks).
    gc = GoogleConnector()
    if gc.configured():
        parts.append(_calendar_section(gc, "CALENDAR TODAY (live, all calendars merged; events carry 'notes')", today_iso))
        parts.append(
            _calendar_section(
                gc,
                f"CALENDAR TOMORROW, {tomorrow_name} (live, all calendars merged; "
                "the ONLY source for any claim about tomorrow's schedule)",
                tomorrow_iso,
            )
        )
        parts.append(_gmail_section(gc))
    else:
        parts.append(
            "CALENDAR: UNAVAILABLE (Google is not connected). Say the calendar could not "
            "be verified. Do NOT claim any day is clear or that nothing is scheduled."
        )
        parts.append(
            "KAS INBOX: UNAVAILABLE (Google is not connected). Say emails could not be "
            "verified. Do NOT claim there were no emails."
        )

    parts.append(_slack_section())

    return "\n\n".join(parts)


# ───────────────────────── composition + delivery ─────────────────────────
def _compose(kind: str, context_text: str) -> str:
    s = cfg.settings().get("ai", {})
    client = ai._get_client()
    if kind == "morning":
        brief = (
            "Write Kas's morning agenda as a Telegram message. Open with 'Good morning "
            "Kas - my agenda includes:' then short bullets: what's moving today, next "
            "actions owed and by whom, anything cold that needs a nudge. Ground today's "
            "meetings in the CALENDAR TODAY section, and for EACH meeting add one short "
            "line on who it is with and what it is about, using the event's notes and "
            "matching names to the pipeline board where they fit. Keep it under 170 words."
        )
        header = "Morning agenda"
    else:
        brief = (
            "Write Kas's end of day summary as a Telegram message. Open with 'Hi Kas, "
            "here is the EOD summary for <weekday, day month>:' then short labeled "
            "sections: Emails (from the live inbox section, only genuinely notable "
            "items), Leads / Clients (what moved today = rows tagged EDITED TODAY, then "
            "what's open), and Calendar Tomorrow built ONLY from the CALENDAR TOMORROW "
            "context section: each real event with its start time and a few words on "
            "what it is; if more than 6 events, give the day's span, the count and the "
            "key ones. Close with ONE warm question asking how her energy was today, "
            "1 to 10, mentioning a voice note with any notes about the day is perfect. "
            "Keep it under 230 words."
        )
        header = "End of day"
    prompt = (
        f"You are Miles, Kas Bordier's AI executive assistant at MAVI. {brief}\n"
        "Rules: PLAIN TEXT for Telegram (no markdown asterisks, no Slack mrkdwn). "
        "Simple lines and • bullets. Calm, concise. NEVER include client pricing. "
        "No em dashes. Start with the line "
        f"{header} and nothing before it.\n"
        "GROUNDING, absolute: every factual claim must come from the Context below. "
        "A section marked UNAVAILABLE means that data could not be read: say it could "
        "not be verified right now. NEVER write 'no emails', 'nothing scheduled' or "
        "any other empty state claim unless the matching Context section is present "
        "and shows a VERIFIED empty read (for the calendar: an events list that is "
        "empty with complete true, which you report as 'clear on the calendars I can "
        "see'). Only describe a board item as having moved or happened today if its "
        "row is tagged EDITED TODAY; untagged rows are open items, not today's news. "
        "Never invent an item that is not in the Context.\n\n"
        f"Context:\n{context_text}"
    )
    resp = client.messages.create(
        model=s.get("model", "claude-sonnet-4-6"),
        max_tokens=MAX_POST_TOKENS,
        temperature=0.5,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    return text


def _send_telegram(text: str) -> int:
    """Deliver a brief to every reminder-enabled user via the raw Bot API
    (no PTB context needed from a thread). Returns how many sends succeeded."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not (token and text):
        return 0
    sent = 0
    try:
        users = db.users_with_reminders()
    except Exception as e:  # noqa: BLE001
        log.warning("Rhythm: could not list recipients: %s", e)
        return 0
    with httpx.Client(timeout=20) as http:
        for u in users:
            try:
                r = http.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": u["telegram_id"], "text": text,
                          "disable_web_page_preview": True},
                )
                if r.status_code == 200:
                    sent += 1
                else:
                    log.warning("Rhythm: telegram send to %s HTTP %s", u["telegram_id"], r.status_code)
            except Exception as e:  # noqa: BLE001
                log.warning("Rhythm: telegram send failed: %s", e)
    return sent


def _run(kind: str) -> None:
    if not enabled():
        return
    text = _compose(kind, _context_block(kind))
    if not text:
        return
    sent = _send_telegram(text)
    log.info("Rhythm %s brief: delivered on Telegram to %d recipient(s).", kind, sent)
    if sent == 0:
        raise RuntimeError("brief composed but delivered to zero Telegram recipients")


def _alert_operator(kind: str, e: Exception) -> None:
    """A rhythm brief that fails must never fail silently. Kas notices a missing
    brief before anyone reads the logs. Alert Brandon (never Kas) via the
    Sentinel ops channel; never raise from here."""
    try:
        import sentinel
        sentinel.send_ops_alert(
            f"Miles ops: the {kind} Telegram brief FAILED just now. "
            f"Reason: {str(e)[:200]}. Kas did not get the brief. Check /api/health."
        )
    except Exception:  # noqa: BLE001
        pass


# ───────────────────────── JobQueue callbacks ─────────────────────────
async def morning_agenda(context) -> None:
    """JobQueue callback: 07:40 Europe/Zurich morning agenda, Telegram only."""
    if not enabled():
        return
    import asyncio as _aio

    try:
        await _aio.to_thread(_run, "morning")
    except Exception as e:  # noqa: BLE001  (a delivery hiccup must never crash the bot)
        log.warning("Morning brief failed: %s", e)
        _alert_operator("morning agenda", e)


async def eod_summary(context) -> None:
    """JobQueue callback: 18:30 Europe/Zurich end of day summary + energy
    question, Telegram only."""
    if not enabled():
        return
    import asyncio as _aio

    try:
        await _aio.to_thread(_run, "eod")
    except Exception as e:  # noqa: BLE001
        log.warning("EOD brief failed: %s", e)
        _alert_operator("EOD summary", e)
