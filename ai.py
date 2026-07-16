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
import os

from anthropic import Anthropic

import config_loader as cfg
import connectors
import database as db

_client: Anthropic | None = None

MEMORY_LIMIT = 48                    # memories injected into the prompt
EXTRACT_EVERY = 8                    # user messages between extraction passes
EXTRACT_MODEL = "claude-haiku-4-5-20251001"
MAX_TOOL_ROUNDS = 5

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
    p = cfg.persona()
    bot_name = cfg.settings().get("bot", {}).get("name", "Assistant")
    name = p.get("coach_name", "the assistant")
    framework = p.get("framework_name", "their approach")
    phrases = p.get("signature_phrases", []) or []
    phrase_line = (
        "Occasionally (not every message) you may use phrases like: "
        + "; ".join(f'"{x}"' for x in phrases)
        if phrases
        else ""
    )

    # Principal context: goals and streaks still apply (they become Kas's tracked items).
    goals = [g["text"] for g in db.active_goals(tid)]
    goals_line = (
        "Current tracked goals: " + " | ".join(goals)
        if goals
        else "No tracked goals yet; offer /goals when it fits naturally."
    )

    done = db.completed_lessons(tid)
    lessons = cfg.flat_lessons()
    next_lesson = next((l for l in lessons if l["lesson_id"] not in done), None)
    prog_line = (
        f"In the program, their next step is \"{next_lesson['title']}\" "
        f"({next_lesson['module_title']}). You may nudge them toward it via /program."
        if next_lesson
        else ""
    )

    streak = db.checkin_streak(tid)
    streak_line = f"Current check-in streak: {streak} day(s)." if streak else ""

    kb = cfg.knowledge_text()
    kb_block = (
        f"\n# {name}'s knowledge base (draw on this; don't quote it verbatim)\n{kb}\n" if kb else ""
    )

    # Long term memory: durable facts distilled from every past conversation.
    mems = db.memories_for_prompt(tid, MEMORY_LIMIT)
    mem_block = ""
    if mems:
        mem_lines = "\n".join(f"- ({m['category']}) {m['content']}" for m in mems)
        mem_block = (
            "\n# Long term memory\n"
            "Things you know from past conversations with this person. Use them naturally "
            "in your answers; never recite this list or mention that you keep one unless asked.\n"
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
            "changed. You can never send, delete, or publish anything: drafts stay "
            "drafts until the principal approves.\n"
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
                "approved it, and always tell her exactly what you booked. Never reveal or repeat "
                "the contents of any Google auth code or token.\n"
            )

    brand = p.get("brand", "")
    sister = p.get("sister_company", "")
    org_line = brand + ((" and " + sister) if sister else "")

    return f"""You are {bot_name}, the AI assistant that speaks in the voice of {name} \
({p.get('full_name', name)}){(', of ' + org_line) if org_line else ''}. \
You help with {p.get('niche', '')}. You talk TO the principal AS {name} — warm, first-person, \
like {name} texting them.

# Who you are ({name}'s background)
{p.get('bio', '')}

# {name}'s philosophy ({framework})
{p.get('philosophy', '')}

# Domain principles you can draw on
{p.get('domain_principles', '')}
{kb_block}{mem_block}{tools_block}
# Tone
{p.get('tone', '')}
{phrase_line}

# Hard rules
{p.get('guardrails', '')}

# This person's context
{goals_line}
{prog_line}
{streak_line}

When a request is clearly outside your scope or sensitive, respond with the spirit of:
"{p.get('escalation_message', '')}"

Answer as {name} would: practical, specific, and grounded. Keep replies concise."""


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
        max_tokens=s.get("max_tokens", 800),
        temperature=s.get("temperature", 0.7),
        system=build_system_prompt(tid),
    )
    tools = connectors.active_tools()
    if tools:
        kwargs["tools"] = tools

    resp = client.messages.create(messages=messages, **kwargs)

    # Tool loop: let the model read email/Notion/etc. before it answers.
    rounds = 0
    while getattr(resp, "stop_reason", "") == "tool_use" and rounds < MAX_TOOL_ROUNDS:
        messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
        results = []
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use":
                out = connectors.dispatch(block.name, block.input or {})
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
        messages.append({"role": "user", "content": results})
        resp = client.messages.create(messages=messages, **kwargs)
        rounds += 1

    reply = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    reply = reply.strip() or "On it — give me one more line of detail and I'll sort it."
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
