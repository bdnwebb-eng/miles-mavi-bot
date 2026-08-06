"""All Telegram handlers: commands, menus, resources, accountability, assistant replies.

Persona-agnostic. Any name shown to users is pulled from config/persona.yaml
(coach_name), so this file works for any clone without edits.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import asyncio
import base64
import logging

import ai
import config_loader as cfg
import connectors
import database as db
import tts

log = logging.getLogger("hermes.handlers")

# Conversation states tracked in context.user_data
AWAITING_GOAL = "awaiting_goal"
AWAITING_CHECKIN = "awaiting_checkin"
AWAITING_CODE = "awaiting_code"


def _coach_name() -> str:
    return cfg.persona().get("coach_name", "the assistant")


# ───────────────────────── access control ─────────────────────────
def _is_allowed(tid: int) -> bool:
    access = cfg.settings().get("access", {})
    if not access.get("restrict", False):
        return True
    if tid in (access.get("allowed_ids") or []):
        return True
    user = db.get_user(tid)
    return bool(user and user["approved"])


# ───────────────────────── menus ─────────────────────────
def main_menu() -> InlineKeyboardMarkup:
    feats = cfg.settings().get("features", {})
    rows = []
    if feats.get("coaching", True):
        rows.append([InlineKeyboardButton(f"💬 Ask {_coach_name()}", callback_data="menu:coach")])
    if feats.get("program", True):
        rows.append([InlineKeyboardButton("📚 My program", callback_data="menu:program")])
    if feats.get("resources", True):
        rows.append([InlineKeyboardButton("🧰 Resources", callback_data="menu:resources")])
    if feats.get("accountability", True):
        rows.append(
            [
                InlineKeyboardButton("🎯 Goals", callback_data="menu:goals"),
                InlineKeyboardButton("✅ Check-in", callback_data="menu:checkin"),
            ]
        )
    return InlineKeyboardMarkup(rows)


# ───────────────────────── /start ─────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    settings = cfg.settings()
    reminder_time = settings.get("reminders", {}).get("default_time", "18:00")
    db.upsert_user(user.id, user.first_name or "there", reminder_time)

    access = settings.get("access", {})
    if access.get("restrict", False) and not _is_allowed(user.id):
        context.user_data["state"] = AWAITING_CODE
        await update.message.reply_text(
            f"👋 Welcome! This is {_coach_name()}, a private assistant. "
            "Please enter your access code to continue."
        )
        return

    p = cfg.persona()
    await update.message.reply_text(
        f"👋 *Welcome, {user.first_name or 'there'}!*\n\n{p.get('intro', '')}\n\n"
        "Tap a button below, or just type your question anytime.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu(),
    )


# ───────────────────────── /menu & /help ─────────────────────────
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("What would you like to do?", reply_markup=main_menu())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = _coach_name()
    await update.message.reply_text(
        "Here's what I can do:\n\n"
        f"💬 *Ask anything*: just type and {name} handles it.\n"
        "📚 /program — work through the program step by step.\n"
        f"🧰 /resources — browse {name}'s videos, PDFs & templates.\n"
        "🎯 /goals — set or view your goals.\n"
        "✅ /checkin — log a check-in and build your streak.\n"
        "⏰ /reminders — turn daily nudges on/off.\n"
        "🔋 /energy — log today's energy 1 to 10, I map the pattern.\n"
        "🧠 /memories — what I hold in long term memory (/remember, /forget).\n"
        "🔌 /connectors — which live accounts I am wired into.\n"
        "🟢 /connectgoogle — connect your Google: Gmail, Calendar, and full Docs, Sheets, "
        "Slides & Drive create/edit (one time).\n"
        "🩺 /sentinel — operator health check: is every system green (owner only).\n"
        "📋 /menu — show the button menu.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ───────────────────────── program ─────────────────────────
def _program_view(tid: int) -> tuple[str, InlineKeyboardMarkup]:
    done = db.completed_lessons(tid)
    prog = cfg.program()
    lines = [f"📚 *{prog.get('title','Program')}*", prog.get("description", ""), ""]
    buttons = []
    next_locked = False
    for m in prog.get("modules", []):
        lines.append(f"*{m['title']}*")
        for lesson in m.get("lessons", []):
            lid = lesson["id"]
            if lid in done:
                lines.append(f"  ✅ {lesson['title']}")
            elif not next_locked:
                lines.append(f"  ▶️ {lesson['title']}  ← next")
                buttons.append(
                    [InlineKeyboardButton(f"Open: {lesson['title']}", callback_data=f"lesson:{lid}")]
                )
                next_locked = True
            else:
                lines.append(f"  🔒 {lesson['title']}")
        lines.append("")
    if not buttons:
        lines.append("🎉 You've completed every lesson. Keep up the accountability with /checkin!")
    return "\n".join(lines), InlineKeyboardMarkup(buttons or [[InlineKeyboardButton("📋 Menu", callback_data="menu:home")]])


async def program(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = _program_view(update.effective_user.id)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def _show_lesson(query, tid: int, lesson_id: str) -> None:
    lesson = cfg.lesson_by_id(lesson_id)
    if not lesson:
        await query.edit_message_text("That lesson isn't available anymore.")
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Mark complete & continue", callback_data=f"done:{lesson_id}")]]
    )
    await query.edit_message_text(
        f"*{lesson['title']}*\n\n{lesson['body']}\n\n_You can ask me anything about this lesson — just type._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


# ───────────────────────── resources ─────────────────────────
def _resources_view() -> tuple[str, InlineKeyboardMarkup]:
    res = cfg.resources()
    buttons = [
        [InlineKeyboardButton(cat["name"], callback_data=f"rescat:{i}")]
        for i, cat in enumerate(res.get("categories", []))
    ]
    return (
        "🧰 *Resource Library*\nPick a category, or just ask me for what you need in plain text.",
        InlineKeyboardMarkup(buttons),
    )


async def resources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = _resources_view()
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def _show_category(query, idx: int) -> None:
    cats = cfg.resources().get("categories", [])
    if idx < 0 or idx >= len(cats):
        await query.edit_message_text("Category not found.")
        return
    cat = cats[idx]
    lines = [f"🧰 *{cat['name']}*", ""]
    for item in cat.get("items", []):
        lines.append(f"• [{item['title']}]({item['url']}) — {item.get('description','')}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu:resources")]])
    await query.edit_message_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
        disable_web_page_preview=False,
    )


# ───────────────────────── goals ─────────────────────────
async def goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    current = db.active_goals(tid)
    if current:
        body = "🎯 *Your current goals:*\n" + "\n".join(f"• {g['text']}" for g in current)
    else:
        body = "🎯 You haven't set any goals yet."
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("➕ Add a goal", callback_data="goal:add")],
         [InlineKeyboardButton("🗑 Clear goals", callback_data="goal:clear")]]
    )
    await update.message.reply_text(body, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


# ───────────────────────── check-in ─────────────────────────
async def checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["state"] = AWAITING_CHECKIN
    await update.message.reply_text(
        "✅ *Check-in time.* How did today go? One line is plenty — what did you do (or what got in the way)?",
        parse_mode=ParseMode.MARKDOWN,
    )


# ───────────────────────── reminders ─────────────────────────
async def reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    user = db.get_user(tid)
    on = bool(user and user["reminders_on"])
    t = (user["reminder_time"] if user else None) or cfg.settings()["reminders"]["default_time"]
    status = f"⏰ Daily nudges are *{'ON' if on else 'OFF'}* at *{t}* ({cfg.settings()['bot']['timezone']})."
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Turn OFF" if on else "Turn ON", callback_data="rem:toggle")]]
    )
    await update.message.reply_text(status, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


# ───────────────────────── plain text router ─────────────────────────
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    text = update.message.text.strip()
    state = context.user_data.get("state")

    # Access gate
    if state == AWAITING_CODE:
        code = cfg.settings().get("access", {}).get("access_code", "")
        if text == code:
            db.set_approved(tid, True)
            context.user_data.pop("state", None)
            p = cfg.persona()
            await update.message.reply_text(
                f"✅ You're in! {p.get('intro','')}", reply_markup=main_menu()
            )
        else:
            await update.message.reply_text(
                "That code didn't match. Try again."
            )
        return

    if not _is_allowed(tid):
        await update.message.reply_text("Please /start and enter your access code first.")
        return

    # Google OAuth code capture: after /connectgoogle, the next code-looking message
    # is the authorization code the user pasted back. exchange_code stores it as the
    # single SHARED Google token (single-tenant bot), so ANY allowed user connecting
    # updates the one connection everyone uses. No per-user re-auth.
    if db.get_pref(tid, "awaiting_google_code", "0") == "1":
        t = text.strip()
        codelike = ("code=" in t) or t.startswith("4/") or (len(t) > 15 and " " not in t)
        if codelike:
            ok, msg = await asyncio.to_thread(connectors.GoogleConnector.exchange_code, tid, text)
            if ok:
                db.set_pref(tid, "awaiting_google_code", "0")
                await update.message.reply_text(
                    "✅ Google connected. I can now read your inbox and draft replies, see and "
                    "book your calendar, and fully work in your Drive: create and edit Google "
                    "Docs, Sheets and Slides, and make, move, rename and organize files. Gmail "
                    "stays draft only (I never send). Drive deletes are trash only (recoverable, "
                    "never permanent). I'll always confirm before I put anything on your calendar, "
                    "and I'll tell you exactly what I create or change and share the link."
                )
            else:
                await update.message.reply_text(msg)
            return

    if state == AWAITING_GOAL:
        db.add_goal(tid, text)
        context.user_data.pop("state", None)
        await update.message.reply_text(
            f"🎯 Locked in: *{text}*\nI'll hold you to it.", parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu(),
        )
        return

    if state == AWAITING_CHECKIN:
        db.add_checkin(tid, text)
        context.user_data.pop("state", None)
        streak = db.checkin_streak(tid)
        await update.message.reply_text(
            f"✅ Logged. 🔥 *{streak}-day streak.* Proud of you — see you tomorrow.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Default: AI assistant reply
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        reply = await asyncio.to_thread(ai.coach_reply, tid, text)
    except Exception as e:  # noqa: BLE001
        reply = ("I hit a snag reaching my brain just now. "
                 "Try again in a moment.")
        context.application.logger.error("AI error: %s", e) if hasattr(context.application, "logger") else None
    await update.message.reply_text(reply)

    # Long term memory upkeep (self-gated, cheap, never blocks the reply)
    asyncio.create_task(asyncio.to_thread(ai.maybe_extract_memories, tid))

    # Voice note (opt-in via /voice) when ElevenLabs is configured
    if tts.enabled() and db.get_pref(tid, "voice", "off") == "on":
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action="record_voice"
        )
        audio = await asyncio.to_thread(tts.synthesize, reply)
        if audio:
            await update.message.reply_voice(voice=audio)


# ───────────────────────── photos & screenshots ─────────────────────────
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kas forwards a screenshot (WhatsApp, email, anything); Miles reads it, names
    the app / group / date, pulls out commitments and asks, proposes the next action."""
    tid = update.effective_user.id
    if context.user_data.get("state") == AWAITING_CODE or not _is_allowed(tid):
        await update.message.reply_text("Please /start and enter your access code first.")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    caption = (update.message.caption or "").strip()
    try:
        photo = update.message.photo[-1]  # largest rendition Telegram offers
        file = await photo.get_file()
        data = await file.download_as_bytearray()
        if len(data) > 4 * 1024 * 1024:
            await update.message.reply_text(
                "That image is heavier than I can read here. Send it a touch smaller."
            )
            return
        image_b64 = base64.b64encode(bytes(data)).decode("ascii")
        reply = await asyncio.to_thread(ai.coach_reply, tid, caption, image_b64, "image/jpeg")
    except Exception as e:  # noqa: BLE001
        reply = "I hit a snag reading that image. Try sending it again in a moment."
        context.application.logger.error("Photo error: %s", e) if hasattr(context.application, "logger") else None
    await update.message.reply_text(reply)

    # Long term memory upkeep (self-gated, cheap, never blocks the reply)
    asyncio.create_task(asyncio.to_thread(ai.maybe_extract_memories, tid))

    # Voice note (opt-in via /voice) when ElevenLabs is configured
    if tts.enabled() and db.get_pref(tid, "voice", "off") == "on":
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action="record_voice"
        )
        audio = await asyncio.to_thread(tts.synthesize, reply)
        if audio:
            await update.message.reply_voice(voice=audio)


# ───────────────────────── voice notes in (ElevenLabs STT) ─────────────────────────
async def voice_note_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kas sends a voice note (or an audio file); Miles transcribes it with
    ElevenLabs Speech to Text and handles it exactly like a typed message,
    and always answers a voice note with a voice note as well as text. Never
    silent: any failure gets a warm retry line."""
    tid = update.effective_user.id
    if context.user_data.get("state") == AWAITING_CODE or not _is_allowed(tid):
        await update.message.reply_text("Please /start and enter your access code first.")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    retry_line = "I could not make that one out, mind sending it again or typing it?"
    transcript = None
    try:
        media = update.message.voice or update.message.audio
        if media is None:
            await update.message.reply_text(retry_line)
            return
        if not tts.stt_enabled():
            log.warning("Voice note received but ELEVENLABS_API_KEY is missing.")
            await update.message.reply_text(retry_line)
            return
        file = await media.get_file()
        data = await file.download_as_bytearray()
        mime = getattr(media, "mime_type", None) or "audio/ogg"
        fname = ("voice.ogg" if update.message.voice
                 else (getattr(media, "file_name", None) or "audio.bin"))
        transcript = await asyncio.to_thread(tts.transcribe, bytes(data), mime, fname)
    except Exception as e:  # noqa: BLE001
        log.warning("Voice note error: %s", e)
        transcript = None
    if not transcript:
        await update.message.reply_text(retry_line)
        return

    try:
        reply = await asyncio.to_thread(ai.coach_reply, tid, "(voice note) " + transcript)
    except Exception as e:  # noqa: BLE001
        log.warning("AI error on voice note: %s", e)
        reply = "I hit a snag reaching my brain just now. Try again in a moment."
    await update.message.reply_text(reply)

    # Long term memory upkeep (self-gated, cheap, never blocks the reply)
    asyncio.create_task(asyncio.to_thread(ai.maybe_extract_memories, tid))

    # Voice in always gets voice back: she spoke to Miles, Miles speaks in reply
    # (plus the text above). The /voice toggle only governs typed messages.
    if tts.enabled():
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action="record_voice"
        )
        audio = await asyncio.to_thread(tts.synthesize, reply)
        if audio:
            await update.message.reply_voice(voice=audio)


# ───────────────────────── voice toggle ─────────────────────────
async def voice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    if not _is_allowed(tid):
        await update.message.reply_text("Please /start and enter your access code first.")
        return
    if not tts.enabled():
        await update.message.reply_text(
            "Voice isn't switched on for this deployment yet (missing voice key)."
        )
        return
    now_on = db.get_pref(tid, "voice", "off") != "on"
    db.set_pref(tid, "voice", "on" if now_on else "off")
    if now_on:
        await update.message.reply_text(
            "🎙 Voice notes ON. I'll speak my replies as well as type them. /voice to turn off."
        )
    else:
        await update.message.reply_text("💬 Voice notes OFF. Text only. /voice to turn back on.")



# ───── memory ─────
async def memories_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    if not _is_allowed(tid):
        await update.message.reply_text("Please /start and enter your access code first.")
        return
    mems = db.all_memories(tid)
    if not mems:
        await update.message.reply_text(
            "🧠 Nothing in long term memory yet. It builds itself as we talk, "
            "or teach me directly: /remember <something worth keeping>."
        )
        return
    lines = ["🧠 *What I'm holding in long term memory:*", ""]
    for m in mems[-30:]:
        lines.append(f"#{m['id']} ({m['category']}) {m['content']}")
    lines.append("")
    lines.append("_/remember <text> to add · /forget <number> to drop one._")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def remember_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    if not _is_allowed(tid):
        await update.message.reply_text("Please /start and enter your access code first.")
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text("Tell me what to keep: /remember <the thing>.")
        return
    if db.add_memory(tid, text, "fact", "manual"):
        await update.message.reply_text("🧠 Kept. I won't lose it.")
    else:
        await update.message.reply_text("Already holding that one.")


async def forget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    if not _is_allowed(tid):
        await update.message.reply_text("Please /start and enter your access code first.")
        return
    arg = (context.args or [""])[0].lstrip("#")
    if not arg.isdigit():
        await update.message.reply_text("Give me the number from /memories: /forget 12")
        return
    if db.delete_memory(tid, int(arg)):
        await update.message.reply_text("🗑 Forgotten.")
    else:
        await update.message.reply_text("No memory with that number.")


# ───── google oauth (self-serve connect) ─────
async def connectgoogle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    if not _is_allowed(tid):
        await update.message.reply_text("Please /start and enter your access code first.")
        return
    if not connectors.GoogleConnector._has_app():
        await update.message.reply_text(
            "Google isn't set up on my side yet (missing app credentials). Flag it to Brandon."
        )
        return
    url = connectors.GoogleConnector.auth_url()
    db.set_pref(tid, "awaiting_google_code", "1")
    await update.message.reply_text(
        "Let's connect your Google. Three quick steps:\n\n"
        "1. Open this and approve as kas@maviliving.com:\n"
        f"{url}\n\n"
        "2. After you approve, the browser will try to open a localhost page that "
        "won't load. That's expected. Copy the whole address from the address bar "
        "(or just the part after code=).\n\n"
        "3. Send it back to me here and I'll finish connecting.",
        disable_web_page_preview=True,
    )


# ───── connectors ─────
async def connectors_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    if not _is_allowed(tid):
        await update.message.reply_text("Please /start and enter your access code first.")
        return
    lines = ["🔌 *Connectors*:", ""]
    lines.extend(connectors.status_lines())
    lines.append("")
    lines.append(
        "_Email, calendar feeds, Slack read and Notion are read-first. Google is full: Gmail "
        "draft-only, Calendar read+write (confirm first), and full create/edit on Docs, Sheets, "
        "Slides and Drive (deletes are trash-only). I always tell you what I create or change._"
    )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ───────────────────────── callback buttons ─────────────────────────
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tid = query.from_user.id
    data = query.data

    if data == "menu:home":
        await query.edit_message_text("What would you like to do?", reply_markup=main_menu())
    elif data == "menu:coach":
        await query.edit_message_text("💬 Go ahead: type what you need and I'm on it.")
    elif data == "menu:program":
        text, kb = _program_view(tid)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    elif data == "menu:resources":
        text, kb = _resources_view()
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    elif data == "menu:goals":
        current = db.active_goals(tid)
        body = ("🎯 *Your goals:*\n" + "\n".join(f"• {g['text']}" for g in current)) if current else "🎯 No goals set yet."
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("➕ Add a goal", callback_data="goal:add")],
             [InlineKeyboardButton("🗑 Clear goals", callback_data="goal:clear")]]
        )
        await query.edit_message_text(body, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    elif data == "menu:checkin":
        context.user_data["state"] = AWAITING_CHECKIN
        await query.edit_message_text("✅ How did today go? Reply with one line.")
    elif data.startswith("lesson:"):
        await _show_lesson(query, tid, data.split(":", 1)[1])
    elif data.startswith("done:"):
        db.complete_lesson(tid, data.split(":", 1)[1])
        text, kb = _program_view(tid)
        await query.edit_message_text("✅ Nice work.\n\n" + text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    elif data.startswith("rescat:"):
        await _show_category(query, int(data.split(":", 1)[1]))
    elif data == "goal:add":
        context.user_data["state"] = AWAITING_GOAL
        await query.edit_message_text("🎯 What's the goal? Write it in one specific sentence.")
    elif data == "goal:clear":
        db.clear_goals(tid)
        await query.edit_message_text("🗑 Goals cleared. Set a fresh one anytime with /goals.")
    elif data == "rem:toggle":
        user = db.get_user(tid)
        new_on = not bool(user and user["reminders_on"])
        db.set_reminders(tid, new_on)
        await query.edit_message_text(f"⏰ Daily nudges are now *{'ON' if new_on else 'OFF'}*.", parse_mode=ParseMode.MARKDOWN)


# ───────────────────────── registration ─────────────────────────

async def energy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tid = update.effective_user.id
    if not _is_allowed(tid):
        return
    args = context.args or []
    if args and args[0].isdigit() and 1 <= int(args[0]) <= 10:
        score = int(args[0])
        note = " ".join(args[1:])[:200]
        db.add_energy(tid, score, note)
        hist = db.energy_history(tid, 30)
        avg = round(sum(r[1] for r in hist) / len(hist), 1)
        low_days = [r[0][5:] for r in hist if r[1] <= 4][-3:]
        msg = f"Logged: {score}/10 today."
        if note:
            msg += f" Noted: {note}."
        msg += f"\n30 day average: {avg}/10 across {len(hist)} day(s)."
        if low_days:
            msg += f" Low days recently: {', '.join(low_days)}."
        if score <= 4:
            msg += "\nLow energy day, Kas. Tell me what to lighten and I'll move what can move."
        await update.message.reply_text(msg)
        return
    hist = db.energy_history(tid, 30)
    if not hist:
        await update.message.reply_text("Log your first score: /energy 7 (add a note if you like: /energy 7 long client day).")
        return
    lines = [f"{r[0][5:]}: {'▪' * r[1]} {r[1]}/10" + (f" · {r[2]}" if r[2] else "") for r in hist[-14:]]
    avg = round(sum(r[1] for r in hist) / len(hist), 1)
    await update.message.reply_text("Energy, last 14 logged days:\n" + "\n".join(lines) + f"\n\nAverage: {avg}/10. Log today: /energy 1-10.")


# ───────────────────────── /sentinel (operator health, owner gated) ─────────────────────────
async def sentinel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """On demand Sentinel health summary. Owner gated: only allowed operators can
    ask Miles if it is healthy. Runs the read only probe off the event loop."""
    tid = update.effective_user.id
    if not _is_allowed(tid):
        return
    await update.message.reply_text("Running Sentinel diagnostics, one moment.")
    import sentinel
    try:
        report = await asyncio.to_thread(sentinel.run_diagnostics, False)
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"Sentinel failed to run: {e}")
        return
    dot = {"green": "🟢", "amber": "🟡", "red": "🔴"}
    counts = report.get("summary_counts", {})
    head = (f"{dot.get(report['overall'], '⚪')} Miles Sentinel: "
            f"{report['overall'].upper()}  "
            f"({counts.get('green', 0)} green, {counts.get('amber', 0)} amber, "
            f"{counts.get('red', 0)} red)")
    lines = [head, ""]
    for c in report["checks"]:
        lines.append(f"{dot.get(c['status'], '⚪')} {c['label']}: {c['detail']}")
        if c["status"] in ("red", "amber") and c.get("fix"):
            lines.append(f"   Fix: {c['fix']}")
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n..."
    await update.message.reply_text(text, disable_web_page_preview=True)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("program", program))
    app.add_handler(CommandHandler("resources", resources))
    app.add_handler(CommandHandler("goals", goals))
    app.add_handler(CommandHandler("checkin", checkin))
    app.add_handler(CommandHandler("reminders", reminders))
    app.add_handler(CommandHandler("voice", voice_cmd))
    app.add_handler(CommandHandler("memories", memories_cmd))
    app.add_handler(CommandHandler("remember", remember_cmd))
    app.add_handler(CommandHandler("forget", forget_cmd))
    app.add_handler(CommandHandler("connectors", connectors_cmd))
    app.add_handler(CommandHandler("connectgoogle", connectgoogle_cmd))
    app.add_handler(CommandHandler("energy", energy_cmd))
    app.add_handler(CommandHandler("sentinel", sentinel_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, voice_note_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    if tts.stt_enabled():
        log.info("Voice input: armed (ElevenLabs STT)")
    else:
        log.info("Voice input: dormant (ELEVENLABS_API_KEY missing); voice notes get a friendly retry line")
