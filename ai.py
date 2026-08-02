"""Builds Miles's prompt, runs the tool loop, and maintains long term memory.

Persona lives in config/*.yaml; this file assembles it. Three jobs:
  1. build_system_prompt: persona + knowledge + LONG TERM MEMORY + live connector note
  2. coach_reply: the reply loop, with Anthropic tool use when connectors are wired
  3. maybe_extract_memories: a cheap background pass that distills durable facts
     from recent conversation into the memories table (survives restarts on the
     Railway volume via HERMES_DB_PATH)
"""
from __future__ import annotations

import json
import logging
import os
import re

from anthropic import Anthropic

import config_loader as cfg
import connectors
import database as db

log = logging.getLogger(__name__)

_client: Anthropic | None = None

CURATED_CHAR_CAP = 6000              # ~1500 tokens: the v1.3 curated-tier cap
EXTRACT_EVERY = 8                    # user messages between extraction passes
EXTRACT_MODEL = "claude-haiku-4-5-20251001"
MAX_TOOL_ROUNDS = 8

# Anti-fabrication gate: link domains that must be backed by a real tool result
# from THIS turn before Miles is allowed to show them to Kas.
_URL_RE = re.compile(r"https?://[^\s\"'<>\)\]\}]+")
_VERIFIED_LINK_DOMAINS = ("calendar.google.com", "www.google.com/calendar",
                          "docs.google.com", "drive.google.com",
                          "sheets.google.com", "meet.google.com", "notion.so", "notion.site")


def _harvest_urls(text: str) -> list[str]:
    """Every URL in a blob of text, with trailing punctuation stripped."""
    return [u.rstrip(".,;:!?") for u in _URL_RE.findall(text or "")]


def _enforce_link_integrity(reply: str, tool_urls: list[str]) -> str:
    """Replace any Google/Notion link in the final text that never appeared in an
    actual tool result this turn, and append one honest note. Prefix matching in
    both directions tolerates tracking params on either side."""
    flagged = []
    for u in set(_harvest_urls(reply)):
        low = u.lower()
        if not any(d in low for d in _VERIFIED_LINK_DOMAINS):
            continue
        backed = any(u.startswith(t) or t.startswith(u) for t in tool_urls)
        if not backed:
            flagged.append(u)
    if not flagged:
        return reply
    for u in sorted(flagged, key=len, reverse=True):
        reply = reply.replace(u, "[link removed, could not verify]")
    return reply + ("\n\nNote: I removed a link I could not verify against a real action. "
                    "If I did not show you a tool confirmed result, treat it as not done yet.")

# ── Truth gate v6.4 (2026-07-25, 10k harness findings pl-02 and book-04) ──
# 1. Verified action ledger: every SUCCESSFUL write tool result is logged to the
#    action_log table and injected into the system prompt as ground truth, so the
#    model always knows exactly what it has and has not done across turns.
# 2. Deterministic done-claim backstop: when a turn had ZERO successful writes
#    AND the ledger is empty (the exact pl-02 shape), a reply that asserts a
#    first-person completed action gets a plain-language correction appended.
# 3. Operator alert (Brandon only, never Kas) whenever either gate fires,
#    debounced to one per gate per 10 minutes.

_WRITE_TOOLS = {
    "calendar_create_event", "calendar_update_event", "calendar_delete_event",
    "gmail_draft", "gmail_send",
    "docs_create", "docs_append", "docs_replace",
    "sheets_create", "sheets_write", "sheets_append", "slides_create",
    "drive_create_folder", "drive_move", "drive_rename", "drive_trash", "drive_share",
    "notion_update_property", "notion_create_lead", "notion_create_page",
    "notion_append_content", "slack_post_message",
}


def _log_write_action(tid: int, tool: str, args: dict, out: str) -> bool:
    """Log a successful write tool result to the verified action ledger.
    Success is judged by each tool's own return shape (see connectors.py).
    Returns True when a real write landed this turn (whether or not the ledger
    insert itself succeeded: the write is real either way)."""
    if tool not in _WRITE_TOOLS or not isinstance(out, str):
        return False
    text = out.strip()
    if not text or text.startswith("Error"):
        return False
    args = args or {}
    link = ""
    summary = ""
    if tool == "calendar_create_event":
        try:
            j = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return False
        if not isinstance(j, dict) or not j.get("verified"):
            return False  # duplicate refusals and failed creates are not actions
        summary = (f"Created calendar event '{j.get('summary', '')}' starting "
                   f"{j.get('start', '')} on calendar '{j.get('calendar', '')}'")
        link = str(j.get("htmlLink", "") or "")
    elif tool == "calendar_delete_event":
        if not text.startswith("Deleted event"):
            return False
        summary = (f"Deleted calendar event {args.get('event_id', '')} from calendar "
                   f"'{args.get('calendar_id', 'primary') or 'primary'}'")
    elif tool == "gmail_draft":
        if not text.startswith("Draft saved"):
            return False
        summary = (f"Drafted email to {args.get('to', '')} subject "
                   f"'{args.get('subject', '')}' (saved to Drafts, NOT sent)")
    elif tool == "docs_create":
        summary = f"Created Google Doc '{args.get('title', '')}'"
    elif tool == "docs_append":
        summary = f"Appended text to Google Doc {args.get('document_id', '')}"
    elif tool == "docs_replace":
        summary = f"Edited Google Doc {args.get('document_id', '')} (find and replace)"
    elif tool == "sheets_create":
        summary = f"Created Google Sheet '{args.get('title', '')}'"
    elif tool == "sheets_write":
        summary = (f"Wrote cells to range {args.get('range', '')} of sheet "
                   f"{args.get('spreadsheet_id', '')}")
    elif tool == "sheets_append":
        summary = (f"Appended rows to range {args.get('range', '')} of sheet "
                   f"{args.get('spreadsheet_id', '')}")
    elif tool == "slides_create":
        summary = f"Created Google Slides deck '{args.get('title', '')}'"
    elif tool == "drive_create_folder":
        summary = f"Created Drive folder '{args.get('name', '')}'"
    elif tool == "drive_move":
        summary = (f"Moved Drive file {args.get('file_id', '')} into folder "
                   f"{args.get('new_parent_id', '')}")
    elif tool == "drive_rename":
        summary = f"Renamed Drive file {args.get('file_id', '')} to '{args.get('new_name', '')}'"
    elif tool == "drive_trash":
        summary = f"Moved Drive file {args.get('file_id', '')} to Trash (recoverable)"
    elif tool == "gmail_send":
        if not text.startswith("Sent email"):
            return False
        summary = (f"SENT email to {args.get('to', '')} subject "
                   f"'{args.get('subject', '')}' (approved by Kas this turn)")
    elif tool == "calendar_update_event":
        if not text.startswith("Updated event"):
            return False
        summary = (f"Updated calendar event {args.get('event_id', '')} "
                   f"({', '.join(k for k in ('start_iso', 'summary', 'location', 'description') if args.get(k)) or 'fields'})")
    elif tool == "drive_share":
        if not text.startswith("Shared"):
            return False
        summary = (f"Shared Drive file {args.get('file_id', '')} as viewer with "
                   f"{args.get('email', '') or 'anyone with the link'}")
    elif tool == "notion_create_lead":
        if not text.startswith("Created lead"):
            return False
        summary = (f"Created pipeline lead {args.get('project_code', '')} "
                   f"({args.get('client_name', '')}) on the MAVI Engagements Pipeline")
    elif tool == "notion_create_page":
        if not text.startswith("Created Notion page"):
            return False
        summary = f"Created Notion page '{args.get('title', '')}'"
    elif tool == "notion_append_content":
        if not text.startswith("Appended"):
            return False
        summary = f"Appended content to Notion page {args.get('page_id', '')}"
    elif tool == "notion_update_property":
        if not text.startswith("Updated"):
            return False
        summary = (f"Updated Notion property '{args.get('property', '')}' to "
                   f"'{args.get('value', '')}' on page {args.get('page_id', '')}")
    elif tool == "slack_post_message":
        if not text.startswith("Posted"):
            return False
        summary = f"Posted Slack message to channel {args.get('channel', '')}"
    if not summary:
        return False
    if not link:
        urls = _harvest_urls(text)
        link = urls[0] if urls else ""
    try:
        db.log_action(tid, tool, summary, link)
    except Exception:  # noqa: BLE001  (ledger bookkeeping must never break a reply)
        log.warning("[truthgate] failed to log action %s", tool)
    return True


# Strong first-person done-claim patterns (EN, FR, PL). Deliberately conservative:
# checked only when the turn had zero successful writes AND the ledger is empty,
# so legitimate references to past logged work are never touched. Negations
# ("I have not sent", "je n'ai pas envoye", "nie wyslalem") do not match, future
# tense ("I will draft") does not match, and question sentences are skipped.
_DONE_CLAIM_PATTERNS = [
    re.compile(r"\bI(?:'ve| have)\s+(?:already\s+)?(?:drafted|booked|created|sent|"
               r"scheduled|updated|added|saved|moved|deleted)\b", re.IGNORECASE),
    re.compile(r"\bit(?:'s| is)\s+(?:already\s+)?(?:in your drafts|booked|done|created)\b",
               re.IGNORECASE),
    re.compile(r"^\s*Done\b", re.IGNORECASE),
    re.compile("\\bj[\u2019']ai\\s+(?:d\u00e9j\u00e0\\s+)?(?:cr\u00e9\u00e9|r\u00e9dig\u00e9|"
               "r\u00e9serv\u00e9|envoy\u00e9|planifi\u00e9|mis \u00e0 jour)\\b", re.IGNORECASE),
    re.compile("\\bc[\u2019']est\\s+fait\\b", re.IGNORECASE),
    re.compile("(?<!nie )\\b(?:utworzy\u0142em|utworzy\u0142am|zapisa\u0142em|zapisa\u0142am|"
               "zarezerwowa\u0142em|zarezerwowa\u0142am|wys\u0142a\u0142em|wys\u0142a\u0142am|"
               "zaplanowa\u0142em|zaplanowa\u0142am)\\b", re.IGNORECASE),
    re.compile("(?<!nie )\\bgotow[eya]\\b", re.IGNORECASE),
]


# Future/conditional guard: "the moment it's done", "once it's booked", "będzie
# gotowe", "dès que ... c'est fait" are promises, not done-claims. Any sentence
# containing one of these markers is skipped (conservative: prefer under-firing).
_FUTURE_GUARD_RE = re.compile(
    "\\b(will|once|when|until|before|unless|as soon as|the moment|the second|if|"
    "d\u00e8s que|quand|une fois|si|sera|"
    "b\u0119dzie|gdy|kiedy|jak tylko)\\b", re.IGNORECASE)


def _enforce_action_integrity(reply: str, wrote_this_turn: bool, ledger_nonempty: bool) -> str:
    """Deterministic backstop for the pl-02 class: a first-person done-claim with
    zero successful write tool calls this turn and an empty verified ledger is a
    fabrication by definition. Appends a plain correction instead of letting the
    false claim stand. Never fires when anything was actually written (this turn
    or ever), so accurate references to real past work are untouched."""
    if wrote_this_turn or ledger_nonempty or not reply:
        return reply
    fired = False
    for chunk in re.split(r"[\n]+|(?<=[.!])\s+", reply):
        s = chunk.strip()
        if not s or "?" in s or _FUTURE_GUARD_RE.search(s):
            continue  # questions and future/conditional promises never count
        if any(p.search(s) for p in _DONE_CLAIM_PATTERNS):
            fired = True
            break
    if not fired:
        return reply
    log.warning("[truthgate] action integrity gate fired: done claim with zero "
                "writes this turn and an empty ledger.")
    return reply + ("\n\nCorrection: I have to be straight with you. I did not actually "
                    "perform that action. Nothing has been created or changed. Say the "
                    "word and I will do it for real right now.")


_GATE_ALERT_DEBOUNCE_S = 600


def _operator_gate_alert(gate: str, excerpt: str) -> None:
    """Sentinel-style operator alert to Brandon when a truth gate fires. Debounced
    to one alert per gate per 10 minutes via sentinel_state. Never messages Kas,
    never raises."""
    try:
        from datetime import datetime as _adt, timezone as _tz
        key = f"truthgate_alert_{gate}"
        now = _adt.now(_tz.utc).timestamp()
        last = db.get_sentinel_state(key)
        if last:
            try:
                if now - float(last) < _GATE_ALERT_DEBOUNCE_S:
                    return
            except ValueError:
                pass
        db.set_sentinel_state(key, str(now))
        import sentinel
        one_line = " ".join((excerpt or "").split())[:160]
        sentinel.send_ops_alert(
            f"Miles truth gate: the {gate} gate fired and corrected a reply to Kas. "
            f"Excerpt: {one_line}"
        )
    except Exception as e:  # noqa: BLE001  (alerting must never break a reply)
        log.warning("[truthgate] operator alert failed: %s", e)


IMAGE_DEFAULT_INSTRUCTION = (
    "Kas sent this image. If it is a chat screenshot (WhatsApp, email, etc), identify "
    "the app, the chat or group name, the date, and what matters; extract any "
    "commitments, dates, or asks; propose the one next action and offer to draft it. "
    "Otherwise describe what matters and propose the next action."
)


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set in the environment.")
        _client = Anthropic(api_key=api_key)
    return _client


def build_system_prompt(tid: int) -> str:
    """Assemble persona + knowledge + long term memory + connector context."""
    # Ground the model in real time. Without this the model has no clock and
    # will guess weekday-to-date mappings (this booked a "Friday 25 July" that
    # does not exist). Injected fresh on every call.
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    _now = _dt.now(ZoneInfo("Europe/Zurich"))
    date_line = (
        f"CURRENT DATE AND TIME: {_now.strftime('%A %d %B %Y, %H:%M')} in Geneva "
        "(Europe/Zurich). Anchor every date, weekday, and relative expression "
        "(today, tomorrow, next Friday) to this. When a request contains a weekday "
        "and a date number that disagree (for example Friday 25 July when Friday is "
        "the 24th), do NOT pick one: point out the mismatch and ask which she means "
        "before booking anything."
    )
    # Truth gate v6.4: inject the verified action ledger as ground truth right
    # after the date line, so the model always knows what it has and has not done.
    try:
        _acts = db.recent_actions(tid, 30)
    except Exception:  # noqa: BLE001
        _acts = []
    if _acts:
        _rows = []
        for _a in _acts:
            try:
                _ts = _dt.fromisoformat(str(_a["ts_utc"]))
                if _ts.tzinfo is None:
                    _ts = _ts.replace(tzinfo=ZoneInfo("UTC"))
                _when = _ts.astimezone(ZoneInfo("Europe/Zurich")).strftime("%a %d %b %H:%M")
            except (ValueError, TypeError):
                _when = str(_a["ts_utc"])[:16]
            _line = f"- [{_when} Geneva] {_a['summary']}"
            if _a["link"]:
                _line += f" ({_a['link']})"
            _rows.append(_line)
        ledger_block = (
            "VERIFIED ACTION LEDGER (ground truth). These are the ONLY actions you have "
            "actually performed recently (newest first, times in Geneva):\n"
            + "\n".join(_rows) + "\n"
            "If an action is not in this ledger and not confirmed by a tool result in "
            "THIS turn, you did NOT do it. Never state, imply, or agree that you sent, "
            "drafted, booked, created, updated, or deleted anything that is not on this "
            "list. If asked about something not on the list, say plainly you have not "
            "done it yet and offer to do it now. This rule outranks politeness and "
            "confidence."
        )
    else:
        ledger_block = ("VERIFIED ACTION LEDGER: empty. You have performed no actions "
                        "recently. Any claim otherwise is false.")

    soul = cfg.soul()
    sources = cfg.sources()
    kindex = cfg.knowledge_index()

    goals = [g["text"] for g in db.active_goals(tid)]
    goals_line = ("Current tracked goals: " + " | ".join(goals)) if goals else ""
    streak = db.checkin_streak(tid)
    streak_line = f"Current check-in streak: {streak} day(s)." if streak else ""

    # Curated memory: the ONLY memory tier that rides in the head (v1.3 cap).
    mems = db.curated_for_prompt(CURATED_CHAR_CAP)
    mem_block = ""
    if mems:
        mem_lines = "\n".join(f"- ({m['category']}) {m['content']}" for m in mems)
        mem_block = (
            "\n# Curated memory (small and distilled; the only memory you carry)\n"
            "Durable facts distilled nightly from past conversations. Use them naturally; "
            "never recite the list. For anything older or more specific, call search_history.\n"
            f"{mem_lines}\n"
        )

    # Live connectors: tell the model what real data it can reach.
    tools_block = ""
    acts = connectors.active()
    if acts:
        names = ", ".join(c.name for c in acts)
        tools_block = (
            "\n# Live connectors\n"
            f"You have live tools wired: {names}. Use them whenever a question needs "
            "real current data (inbox, Notion projects) instead of guessing. Summarize "
            "what you find in your own voice; never dump raw output. Email is read only. "
            "In Notion you may update ONE property at a time on a project page (health, "
            "next action, dates) and you must tell the principal exactly what you "
            "changed. You can never send or publish anything: drafts stay drafts until the "
            "principal approves. When a tool returns an error or an incomplete window, "
            "tell Kas plainly what you could and could not see, and what you are doing "
            "about it. Never present partial data as complete.\n"
        )
        if any(c.name == "google" for c in acts):
            tools_block += (
                "Google is fully live (Gmail, Calendar, Drive, Docs, Sheets, Slides). You have "
                "full Google access and can now "
                "CREATE and EDIT Google Docs, Sheets and Slides and fully manage Drive "
                "(create folders, move, rename, organize files) inside Kas's Google. Whenever "
                "you create or change anything, always tell Kas exactly what you did and share "
                "the link. GMAIL STAYS DRAFT ONLY: you create drafts in Kas's Drafts for her to "
                "review and send, and you never send. DRIVE DELETES ARE TRASH ONLY: you move "
                "items to Trash (recoverable), never permanently delete, and you say so. Calendar "
                "writes still CONFIRM FIRST: only create or change an event once Kas has explicitly "
                "approved it, and always tell her exactly what you booked, naming the calendar it "
                "landed on and sharing the event htmlLink the tool returns as proof. Never reveal or repeat "
                "the contents of any Google auth code or token.\n"
                "SEARCH BEFORE CREATE: before creating events from an email or brief, FIRST "
                "search the target dates with calendar_upcoming_v2 (use q and "
                "start_date/end_date) for existing or duplicate entries. Create only what is "
                "missing. After creating, list exactly what was created, with links from tool "
                "results, and what was skipped as already present. If duplicates exist, list "
                "them and confirm with Kas before deleting. TIMEZONES: for events happening in "
                "another city, pass that city's IANA timezone to calendar_create_event with the "
                "local wall time (e.g. America/Chicago for Dallas); when confirming to Kas, "
                "state BOTH the event's local time and the Geneva time.\n"
            )
        tools_block += (
            "HARD ANTI FABRICATION RULES: NEVER write a link, an event confirmation, a "
            "document name, or any 'done' statement unless it came from a tool result in "
            "THIS conversation turn or is listed in the VERIFIED ACTION LEDGER above. If "
            "you did not run the tool, say plainly that you have not done it yet and what "
            "you need. Fabricating a link or a confirmation "
            "is the single worst thing you can do. "
            "Claiming an unperformed action as done is the single worst failure you can "
            "commit. When uncertain whether you did something, check the ledger; if "
            "absent, say you have not.\n"
        )

    return f"""You are Miles. WHO YOU ARE below is law; everything else serves it.

# {date_line}

# {ledger_block}

# WHO YOU ARE
{soul}

# WHERE TRUTH LIVES
{sources}

# YOUR KNOWLEDGE LIBRARY
The menu below lists your drawers. Open one with the read_knowledge tool the moment a task needs depth (pricing, proposals, booking doctrine, walls). Never guess what a drawer would say.
{kindex}
{mem_block}{tools_block}
# This person's context
{goals_line}
{streak_line}

Answer as Miles: practical, specific, grounded, concise."""

def coach_reply(tid: int, user_text: str, image_b64: str | None = None,
                image_media_type: str = "image/jpeg") -> str:
    """Generate a reply in Miles's voice, with short-term history, long term memory,
    and live connector tools when they're wired. When image_b64 is provided (photo
    handler), the current turn is sent as image + text content blocks."""
    s = cfg.settings().get("ai", {})
    client = _get_client()

    user_text = (user_text or "").strip()
    if image_b64 and not user_text:
        user_text = IMAGE_DEFAULT_INSTRUCTION
    db.add_message(tid, "user", user_text)
    history = db.recent_messages(tid, s.get("history_window", 12))

    few_shot: list[dict] = []
    for ex in cfg.examples():
        u, a = ex.get("user"), ex.get("reply")
        if u and a:
            few_shot.append({"role": "user", "content": u})
            few_shot.append({"role": "assistant", "content": a.strip()})

    messages = few_shot + [{"role": r["role"], "content": r["content"]} for r in history]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    # Photo turn: swap the just-stored text for image + text content blocks.
    if image_b64 and messages and messages[-1]["role"] == "user":
        messages[-1] = {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image_media_type,
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": user_text},
            ],
        }

    kwargs: dict = dict(
        model=s.get("model", "claude-sonnet-4-6"),
        max_tokens=s.get("max_tokens", 3000),
        temperature=s.get("temperature", 0.7),
        system=build_system_prompt(tid),
    )
    tools = connectors.active_tools()
    if tools:
        kwargs["tools"] = tools

    resp = client.messages.create(messages=messages, **kwargs)

    # Tool loop: let the model read email/Notion/etc. before it answers.
    # tool_urls collects every URL that appeared in a REAL tool result this turn;
    # the anti-fabrication gate below only lets those through to Kas. Ledger links
    # are pre-seeded: they came from real past tool results, so Miles may repeat
    # them when Kas asks about work already done (book-04 coherence).
    rounds = 0
    tool_urls: list[str] = []
    wrote_this_turn = False
    try:
        _ledger_rows = db.recent_actions(tid, 30)
    except Exception:  # noqa: BLE001
        _ledger_rows = []
    ledger_nonempty = bool(_ledger_rows)
    for _a in _ledger_rows:
        if _a["link"]:
            tool_urls.append(str(_a["link"]))
    while getattr(resp, "stop_reason", "") == "tool_use" and rounds < MAX_TOOL_ROUNDS:
        messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
        results = []
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use":
                out = connectors.dispatch(block.name, block.input or {})
                out_text = out if isinstance(out, str) else str(out)
                tool_urls.extend(_harvest_urls(out_text))
                if _log_write_action(tid, block.name, block.input or {}, out_text):
                    wrote_this_turn = True
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
        messages.append({"role": "user", "content": results})
        resp = client.messages.create(messages=messages, **kwargs)
        rounds += 1

    if getattr(resp, "stop_reason", "") == "tool_use":
        # Ran out of tool rounds with actions still pending: never imply success.
        reply = ("I hit my tool step limit before finishing that, so the last action was "
                 "NOT completed. Tell me to continue and I will pick it up from there.")
    else:
        reply = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        reply = reply.strip() or "On it. Give me one more line of detail and I'll sort it."
        _pre_link = reply
        reply = _enforce_link_integrity(reply, tool_urls)
        if reply != _pre_link:
            _operator_gate_alert("link", reply)
        _pre_action = reply
        reply = _enforce_action_integrity(reply, wrote_this_turn, ledger_nonempty)
        if reply != _pre_action:
            _operator_gate_alert("action", reply)
    db.add_message(tid, "assistant", reply)
    return reply


def maybe_extract_memories(tid: int) -> int:
    """Every EXTRACT_EVERY user messages, distill new durable facts into long term
    memory using a cheap fast model. Safe to call after every reply (it self-gates).
    Returns how many memories were added."""
    try:
        n = int(db.get_pref(tid, "msgs_since_extract", "0") or 0) + 1
        if n < EXTRACT_EVERY:
            db.set_pref(tid, "msgs_since_extract", str(n))
            return 0
        db.set_pref(tid, "msgs_since_extract", "0")

        history = db.recent_messages(tid, EXTRACT_EVERY * 2 + 4)
        if not history:
            return 0
        convo = "\n".join(f"{r['role'].upper()}: {r['content']}" for r in history)[-8000:]
        known = [m["content"] for m in db.all_memories(tid)]
        known_block = "\n".join(f"- {k}" for k in known[-80:]) or "(nothing yet)"

        client = _get_client()
        prompt = (
            "You maintain the long term memory of Miles, an executive assistant bot.\n"
            "From the conversation below, extract NEW durable facts worth remembering for "
            "weeks or months: stable facts about the principal and her world, preferences, "
            "standing commitments or deadlines, projects and their state, and people (who "
            "they are, why they matter). Skip small talk, one-off logistics, and anything "
            "already known.\n\n"
            f"Already known (do NOT repeat or rephrase):\n{known_block}\n\n"
            f"Conversation:\n{convo}\n\n"
            'Reply with ONLY a JSON array, max 5 items, each item exactly: '
            '{"category": "fact|preference|commitment|project|person", '
            '"content": "one specific, self contained sentence"}. '
            "If nothing new is worth keeping, reply []."
        )
        resp = client.messages.create(
            model=EXTRACT_MODEL,
            max_tokens=600,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            return 0
        items = json.loads(text[start : end + 1])
        added = 0
        valid = {"fact", "preference", "commitment", "project", "person"}
        for it in items[:5]:
            if isinstance(it, dict) and (it.get("content") or "").strip():
                cat = it.get("category", "fact")
                if cat not in valid:
                    cat = "fact"
                if db.add_memory(tid, it["content"].strip(), cat, "auto"):
                    added += 1
        return added
    except Exception:  # noqa: BLE001  (memory upkeep must never break the bot)
        return 0


def reminder_text(tid: int) -> str:
    """Generate the short in-voice morning nudge for the scheduled reminder."""
    s = cfg.settings().get("ai", {})
    try:
        client = _get_client()
    except RuntimeError:
        return "\U0001f44b Morning. Ask me 'what needs me today' when you're ready."

    prompt = (
        "Write ONE short morning message (1-2 sentences) to send the principal now, in the "
        "assistant's voice. Invite them to get today's brief (the three things that need "
        "them). No greeting like 'Dear'. Keep it calm and punchy."
    )
    resp = client.messages.create(
        model=s.get("model", "claude-sonnet-4-6"),
        max_tokens=120,
        temperature=0.8,
        system=build_system_prompt(tid),
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    return text or "\U0001f44b Morning. Say the word and I'll run today's three things."
