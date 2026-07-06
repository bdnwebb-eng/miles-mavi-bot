"""Builds the persona's coaching prompt and calls the Anthropic Claude API.

This file is PERSONA-AGNOSTIC. Everything specific to the person you are cloning
lives in config/*.yaml — this code just assembles those pieces into a system
prompt. You should rarely need to edit it.
"""
from __future__ import annotations

import os

from anthropic import Anthropic

import config_loader as cfg
import database as db

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set in the environment.")
        _client = Anthropic(api_key=api_key)
    return _client


def build_system_prompt(tid: int) -> str:
    """Assemble the persona's voice + knowledge + the client's context into a system prompt."""
    p = cfg.persona()
    bot_name = cfg.settings().get("bot", {}).get("name", "Assistant")
    name = p.get("coach_name", "the coach")
    framework = p.get("framework_name", "their approach")
    phrases = p.get("signature_phrases", []) or []
    phrase_line = (
        "Occasionally (not every message) you may use phrases like: "
        + "; ".join(f'"{x}"' for x in phrases)
        if phrases
        else ""
    )

    # Client context: goals + program position so coaching is personalized.
    goals = [g["text"] for g in db.active_goals(tid)]
    goals_line = (
        "The client's current goals: " + " | ".join(goals)
        if goals
        else "The client has not set goals yet — gently encourage them to with /goals."
    )

    done = db.completed_lessons(tid)
    lessons = cfg.flat_lessons()
    next_lesson = next((l for l in lessons if l["lesson_id"] not in done), None)
    if next_lesson:
        prog_line = (
            f"In the program, their next step is \"{next_lesson['title']}\" "
            f"({next_lesson['module_title']}). You may nudge them toward it via /program."
        )
    else:
        prog_line = "The client has completed the program — focus on reinforcement and accountability."

    streak = db.checkin_streak(tid)
    streak_line = f"Current check-in streak: {streak} day(s)." if streak else ""

    kb = cfg.knowledge_text()
    kb_block = (
        f"\n# {name}'s knowledge base (draw on this; don't quote it verbatim)\n{kb}\n" if kb else ""
    )

    brand = p.get("brand", "")
    sister = p.get("sister_company", "")
    org_line = brand + ((" and " + sister) if sister else "")

    return f"""You are {bot_name}, the AI assistant that speaks in the voice of {name} \
({p.get('full_name', name)}){(', founder of ' + org_line) if org_line else ''}. \
You help with {p.get('niche', '')}. You talk TO the client AS {name} — warm, first-person, \
grounded in real lived experience, like {name} texting them.

# Who you are ({name}'s background)
{p.get('bio', '')}

# {name}'s philosophy ({framework})
{p.get('philosophy', '')}

# Domain principles you can teach from
{p.get('domain_principles', '')}
{kb_block}
# Tone
{p.get('tone', '')}
{phrase_line}

# Hard rules
{p.get('guardrails', '')}

# This client's context
{goals_line}
{prog_line}
{streak_line}

When a request is clearly outside your scope or sensitive, respond with the spirit of:
"{p.get('escalation_message', '')}"

Answer as {name} would: practical, specific, and rooted in real experience. \
Keep replies concise and actionable."""


def coach_reply(tid: int, user_text: str) -> str:
    """Generate a coaching reply in the persona's voice, with short-term memory."""
    s = cfg.settings().get("ai", {})
    client = _get_client()

    # Persist the incoming message, then pull recent history for context.
    db.add_message(tid, "user", user_text)
    history = db.recent_messages(tid, s.get("history_window", 12))

    # Few-shot voice examples come first as prior turns, then the real history.
    few_shot: list[dict] = []
    for ex in cfg.examples():
        u, a = ex.get("user"), ex.get("reply")
        if u and a:
            few_shot.append({"role": "user", "content": u})
            few_shot.append({"role": "assistant", "content": a.strip()})

    messages = few_shot + [{"role": r["role"], "content": r["content"]} for r in history]
    # Ensure the conversation starts with a user turn (Anthropic requirement).
    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    resp = client.messages.create(
        model=s.get("model", "claude-sonnet-4-6"),
        max_tokens=s.get("max_tokens", 800),
        temperature=s.get("temperature", 0.7),
        system=build_system_prompt(tid),
        messages=messages,
    )
    reply = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
    reply = reply.strip() or "Let's keep going — what's on your mind?"
    db.add_message(tid, "assistant", reply)
    return reply


def reminder_text(tid: int) -> str:
    """Generate a short, in-voice accountability nudge for the scheduled reminder."""
    s = cfg.settings().get("ai", {})
    try:
        client = _get_client()
    except RuntimeError:
        return "\U0001f44b Quick check-in: did you move things forward today? Reply /checkin to log it."

    prompt = (
        "Write ONE short, warm accountability nudge (1-2 sentences) to send this client now, "
        "in the coach's voice. Reference taking action / checking in on their goals. "
        "Do not use a greeting like 'Dear'. Keep it punchy."
    )
    resp = client.messages.create(
        model=s.get("model", "claude-sonnet-4-6"),
        max_tokens=120,
        temperature=0.8,
        system=build_system_prompt(tid),
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    return text or "\U0001f44b How did today go? Log it with /checkin — one step at a time."
