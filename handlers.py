"""All Telegram handlers: commands, menus, program, resources, accountability, coaching.

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

import ai
import config_loader as cfg
import database as db

# Conversation states tracked in context.user_data
AWAITING_GOAL = "awaiting_goal"
AWAITING_CHECKIN = "awaiting_checkin"
AWAITING_CODE = "awaiting_code"


def _coach_name() -> str:
    return cfg.persona().get("coach_name", "the coach")


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
        rows.append([InlineKeyboardButton("💬 Ask the coach", callback_data="menu:coach")])
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
            f"👋 Welcome! This bot is for {_coach_name()}'s clients. "
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
        f"💬 *Ask anything* — just type, and I'll coach you in {name}'s voice.\n"
        "📚 /program — work through the program step by step.\n"
        f"🧰 /resources — browse {name}'s videos, PDFs & templates.\n"
        "🎯 /goals — set or view your goals.\n"
        "✅ /checkin — log a check-in and build your streak.\n"
        "⏰ /reminders — turn daily nudges on/off.\n"
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
                f"That code didn't match. Try again, or contact {_coach_name()}."
            )
        return

    if not _is_allowed(tid):
        await update.message.reply_text("Please /start and enter your access code first.")
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

    # Default: AI coaching
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        reply = ai.coach_reply(tid, text)
    except Exception as e:  # noqa: BLE001
        reply = ("I hit a snag reaching the coaching brain just now. "
                 "Try again in a moment.")
        context.application.logger.error("AI error: %s", e) if hasattr(context.application, "logger") else None
    await update.message.reply_text(reply)


# ───────────────────────── callback buttons ─────────────────────────
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tid = query.from_user.id
    data = query.data

    if data == "menu:home":
        await query.edit_message_text("What would you like to do?", reply_markup=main_menu())
    elif data == "menu:coach":
        await query.edit_message_text("💬 Go ahead — type your question and I'll coach you through it.")
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
def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("program", program))
    app.add_handler(CommandHandler("resources", resources))
    app.add_handler(CommandHandler("goals", goals))
    app.add_handler(CommandHandler("checkin", checkin))
    app.add_handler(CommandHandler("reminders", reminders))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
