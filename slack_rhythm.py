"""Miles's Slack rhythm: the internal team posts India used to carry (Jul 10 meeting).

Two scheduled jobs, both posting to SLACK_AGENDA_CHANNEL (#exec-daily):
  morning_agenda : daily 07:40 Europe/Zurich. What's moving today, next actions
                   owed, anything cold. 3 to 5 bullets, phone readable.
  eod_summary    : daily 18:30 Europe/Zurich. What moved today (Notion edits),
                   what's open for tomorrow.

Env gated: both jobs skip silently unless SLACK_BOT_TOKEN and SLACK_AGENDA_CHANNEL
are set. Internal team only: no client facing content, no pricing, ever. Posts go
out as single messages in Miles's calm voice.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

import httpx

import ai
import config_loader as cfg
import notion_watch
from connectors import LOCAL_TZ, CalendarConnector, NotionConnector, SlackConnector

log = logging.getLogger("slack_rhythm")

_NOTION_API = "https://api.notion.com/v1"
MAX_POST_TOKENS = 400


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


def _context_block(kind: str) -> str:
    """Everything the model needs to write the post, as plain text sections."""
    parts: list[str] = []
    today = datetime.now(LOCAL_TZ).strftime("%A %Y-%m-%d")
    parts.append(f"Today is {today} (Europe/Zurich).")

    try:
        rows = _project_rows()
    except Exception as e:  # noqa: BLE001
        rows = []
        log.warning("Slack rhythm: Notion projects unavailable: %s", e)
    if rows:
        lines = []
        today_iso = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        for row in rows:
            keep = {
                k: v
                for k, v in row["properties"].items()
                if any(w in k.lower() for w in ("stage", "status", "health", "next", "action", "due", "date", "owner"))
            }
            edited = (row.get("last_edited") or "")[:10]
            tag = " [edited today]" if edited == today_iso else ""
            lines.append(f"- {row['title']}{tag}: {keep}")
        parts.append("Notion projects board:\n" + "\n".join(lines))

    try:
        cold = notion_watch.cold_items() if notion_watch.enabled() else []
    except Exception:  # noqa: BLE001
        cold = []
    if cold:
        parts.append(
            "Cold items (no activity for a while):\n"
            + "\n".join(f"- {c['title']}: quiet {c['days_quiet']} days" for c in cold)
        )

    cal = CalendarConnector()
    if cal.configured():
        try:
            parts.append("Calendar, next 1 day:\n" + cal.run("calendar_upcoming", {"days": 1}))
        except Exception as e:  # noqa: BLE001
            log.warning("Slack rhythm: calendar unavailable: %s", e)

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
            "actions owed and by whom, anything cold that needs a nudge."
        )
        header = "*Morning agenda*"
    else:
        brief = (
            "Write the end of day summary post for the private #agenda channel, exactly "
            "the way Kas's human EA India writes it. Open with 'Hi Kas, here is the EOD "
            "summary for <weekday, day month>:' then short sections with bold labels "
            "such as *Kas Emails :* and *Leads / Clients:*, one bullet per item, ending "
            "urgent items with a red circle emoji and waiting items with a yellow circle "
            "emoji. Cover what moved today (items edited today on the board) and what's "
            "open for tomorrow."
        )
        header = "*End of day*"
    prompt = (
        f"You are Miles, Kas Bordier's AI executive assistant at MAVI. {brief}\n"
        "Rules: Slack mrkdwn only (*bold*, bullets with •). Calm, concise, internal "
        "team audience. NEVER include client pricing or anything client facing. "
        "No em dashes. Keep it under 120 words. Start the post with the header line "
        f"{header} and nothing before it.\n\n"
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


async def eod_summary(context) -> None:
    """JobQueue callback: 18:30 Europe/Zurich end of day summary in #exec-daily."""
    if not enabled():
        return
    import asyncio as _aio

    try:
        await _aio.to_thread(_run, "eod")
    except Exception as e:  # noqa: BLE001
        log.warning("EOD summary failed: %s", e)
