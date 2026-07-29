"""Miles's Slack rhythm: the internal team posts India used to carry (Jul 10 meeting).

Two scheduled jobs, both posting to SLACK_AGENDA_CHANNEL (#exec-daily):
  morning_agenda : daily 07:40 Europe/Zurich. What's moving today, next actions
                   owed, anything cold. 3 to 5 bullets, phone readable.
  eod_summary    : daily 18:30 Europe/Zurich. What moved today (Notion edits),
                   what's open for tomorrow, plus tomorrow's real calendar.

v6.5 (2026-07-28 incident fix): the context block now reads Kas's LIVE Google
Calendar across every calendar via calendar_upcoming_v2, the same tested
connector the interactive bot and the Sentinel forward probe use. The old ICS
feed path (CALENDAR_ICS_URLS) is retired here: in production it saw a single
stale "Elite Coaching" feed, so the 28 Jul EOD post told Kas "Calendar
Tomorrow: Nothing scheduled" on a back to back day. The block also now includes
live Gmail (last 24h) so the "Kas Emails" section is grounded in real data
instead of being invented, and every source that cannot be read injects an
explicit UNAVAILABLE marker: the composer must say "could not verify" rather
than claiming an empty day or an empty inbox it never saw.

Env gated: both jobs skip silently unless SLACK_BOT_TOKEN and SLACK_AGENDA_CHANNEL
are set. Internal team only: no client facing content, no pricing, ever. Posts go
out as single messages in Miles's calm voice.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

import httpx

import ai
import config_loader as cfg
import notion_watch
from connectors import LOCAL_TZ, GoogleConnector, NotionConnector, SlackConnector

log = logging.getLogger("slack_rhythm")

_NOTION_API = "https://api.notion.com/v1"
MAX_POST_TOKENS = 700


def enabled() -> bool:
    return bool(os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_AGENDA_CHANNEL"))


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
        log.warning("Slack rhythm: calendar read failed for %s: %s", day_iso, e)
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
        log.warning("Slack rhythm: gmail read failed: %s", e)
        return (
            "KAS INBOX: UNAVAILABLE (live Gmail could not be read just now). "
            "You must say emails could not be verified. Do NOT claim there were no emails."
        )


def _context_block(kind: str) -> str:
    """Everything the model needs to write the post, as plain text sections.

    Every section is pulled LIVE at post time. A source that cannot be read is
    represented by an explicit UNAVAILABLE marker instead of silence, so the
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
        log.warning("Slack rhythm: Notion projects unavailable: %s", e)
    if rows:
        lines = []
        for row in rows:
            keep = {
                k: v
                for k, v in row["properties"].items()
                if any(w in k.lower() for w in ("stage", "status", "health", "next", "action", "due", "date", "owner"))
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
            "Cold items (no activity for a while):\n"
            + "\n".join(f"- {c['title']}: quiet {c['days_quiet']} days" for c in cold)
        )

    # Calendar + inbox: LIVE Google, single tenant token, the same connector the
    # interactive bot uses (and the Sentinel forward probe health checks).
    gc = GoogleConnector()
    if gc.configured():
        parts.append(_calendar_section(gc, "CALENDAR TODAY (live, all calendars merged)", today_iso))
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

    return "\n\n".join(parts)


# ───────────────────────── composition + posting ─────────────────────────
def _compose(kind: str, context_text: str) -> str:
    s = cfg.settings().get("ai", {})
    client = ai._get_client()
    if kind == "morning":
        brief = (
            "Write the morning agenda post for the private #agenda channel, exactly "
            "the way Kas's human EA India writes it. Open with 'Hi Kas, good morning - "
            "my agenda includes:' then 3 to 5 bullets: what's moving today, next "
            "actions owed and by whom, anything cold that needs a nudge. Ground "
            "today's meetings in the CALENDAR TODAY section. Keep it under 150 words."
        )
        header = "*Morning agenda*"
    else:
        brief = (
            "Write the end of day summary post for the private #agenda channel, exactly "
            "the way Kas's human EA India writes it. Open with 'Hi Kas, here is the EOD "
            "summary for <weekday, day month>:' then short sections with bold labels "
            "such as *Kas Emails :* and *Leads / Clients:*, one bullet per item, ending "
            "urgent items with a red circle emoji and waiting items with a yellow circle "
            "emoji. Cover what moved today (items tagged EDITED TODAY on the board) and "
            "what's open for tomorrow. Close with a *Calendar Tomorrow:* section built "
            "ONLY from the CALENDAR TOMORROW context section: list each real event with "
            "its start time; if there are more than 6 events, give the day's span, the "
            "event count and the most important ones by name. Keep it under 220 words."
        )
        header = "*End of day*"
    prompt = (
        f"You are Miles, Kas Bordier's AI executive assistant at MAVI. {brief}\n"
        "Rules: Slack mrkdwn only (*bold*, bullets with •). Calm, concise, internal "
        "team audience. NEVER include client pricing or anything client facing. "
        "No em dashes. Start the post with the header line "
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


def _run(kind: str) -> None:
    if not enabled():
        return
    text = _compose(kind, _context_block(kind))
    if not text:
        return
    result = SlackConnector().run(
        "slack_post_message",
        {"channel": os.environ["SLACK_AGENDA_CHANNEL"], "text": text},
    )
    log.info("Slack rhythm %s post: %s", kind, result[:120])


def _alert_operator(kind: str, e: Exception) -> None:
    """v6.6: a rhythm post that fails must never fail silently. Kas notices a
    missing agenda before anyone reads the logs. Alert Brandon (never Kas) via
    the Sentinel ops channel; never raise from here."""
    try:
        import sentinel
        sentinel.send_ops_alert(
            f"Miles ops: the Slack {kind} post FAILED to publish just now. "
            f"Reason: {str(e)[:200]}. Kas did not get the post. Check /api/health."
        )
    except Exception:  # noqa: BLE001
        pass


# ───────────────────────── JobQueue callbacks ─────────────────────────
async def morning_agenda(context) -> None:
    """JobQueue callback: 07:40 Europe/Zurich morning agenda in #exec-daily."""
    if not enabled():
        return
    import asyncio as _aio

    try:
        await _aio.to_thread(_run, "morning")
    except Exception as e:  # noqa: BLE001  (a Slack hiccup must never crash the bot)
        log.warning("Morning agenda failed: %s", e)
        _alert_operator("morning agenda", e)


async def eod_summary(context) -> None:
    """JobQueue callback: 18:30 Europe/Zurich end of day summary in #exec-daily."""
    if not enabled():
        return
    import asyncio as _aio

    try:
        await _aio.to_thread(_run, "eod")
    except Exception as e:  # noqa: BLE001
        log.warning("EOD summary failed: %s", e)
        _alert_operator("EOD summary", e)
