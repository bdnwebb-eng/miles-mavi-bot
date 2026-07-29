"""Miles: Kas Bordier's AI executive assistant bot. Entry point: wires handlers, schedules reminders, starts polling."""
from __future__ import annotations

import logging
import os
from datetime import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram.ext import Application, ContextTypes

import ai
import config_loader as cfg
import database as db
import handlers
import notion_watch
import sentinel
import slack_rhythm
import slack_socket
import web_api

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("hermes")


async def send_daily_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: nudge every opted-in user whose reminder_time matches now (bot tz)."""
    tz = ZoneInfo(cfg.settings()["bot"]["timezone"])
    from datetime import datetime

    now_hhmm = datetime.now(tz).strftime("%H:%M")
    for user in db.users_with_reminders():
        if (user["reminder_time"] or "")[:5] == now_hhmm:
            try:
                text = ai.reminder_text(user["telegram_id"])
                await context.bot.send_message(chat_id=user["telegram_id"], text=text)
            except Exception as e:  # noqa: BLE001
                log.warning("Reminder to %s failed: %s", user["telegram_id"], e)


async def send_cold_flags(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: daily proactive cold-project scan (SOW: message Kas when
    anything goes cold). Silent unless Notion is wired AND something is actually cold."""
    if not notion_watch.enabled():
        return
    import asyncio as _aio

    report = await _aio.to_thread(notion_watch.cold_report)
    if not report:
        return
    for user in db.users_with_reminders():
        try:
            await context.bot.send_message(chat_id=user["telegram_id"], text=report)
        except Exception as e:  # noqa: BLE001
            log.warning("Cold flag to %s failed: %s", user["telegram_id"], e)


async def sentinel_watchdog(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: run the Sentinel end-to-end health probe, alert Brandon
    (the builder, never Kas) on any new or persisting RED and on recovery. Runs the
    synchronous probe off the event loop so polling is never blocked."""
    import asyncio as _aio
    try:
        await _aio.to_thread(sentinel.run_watchdog, "scheduled")
    except Exception as e:  # noqa: BLE001
        log.warning("[sentinel] watchdog run failed: %s", e)


def main() -> None:
    load_dotenv()
    print("[bot] main() entered", flush=True)

    # v5.2: read-only live JSON API for the Miles command dashboard. Started FIRST
    # (daemon thread, stdlib http.server) so /health and /api/dashboard are reachable
    # regardless of the rest of boot. Never touches the PTB asyncio loop.
    try:
        web_api.start()
    except Exception as e:  # noqa: BLE001
        log.warning("Dashboard API failed to start: %s", e)
        print(f"[web_api] FAILED to start: {e}", flush=True)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY not set — AI replies will error until it is.")

    db.init_db()

    # Sentinel boot self-test: end-to-end probe before polling. Logs one line
    # per check and, if overall RED, fires a private alert to Brandon while the
    # bot still starts (so /health and Telegram keep working). Never crashes boot.
    try:
        sentinel.boot_selftest()
    except Exception as e:  # noqa: BLE001
        log.warning("[sentinel] boot self-test failed to run: %s", e)

    app = Application.builder().token(token).build()
    handlers.register(app)

    # Schedule the reminder sweep once a minute; each user fires at their chosen HH:MM.
    if cfg.settings().get("reminders", {}).get("enabled", True):
        if app.job_queue is not None:
            app.job_queue.run_repeating(send_daily_reminders, interval=60, first=10)
            tz = ZoneInfo(cfg.settings()["bot"]["timezone"])
            app.job_queue.run_daily(send_cold_flags, time=time(hour=7, minute=35, tzinfo=tz))
            # Daily rhythm (v6.7): morning agenda + EOD summary w/ energy question,
            # delivered on TELEGRAM only (Kas's decision, Jul 28). Slack is read
            # for context but never posted to on a schedule.
            app.job_queue.run_daily(slack_rhythm.morning_agenda, time=time(hour=7, minute=40, tzinfo=tz))
            app.job_queue.run_daily(slack_rhythm.eod_summary, time=time(hour=18, minute=30, tzinfo=tz))
            # Sentinel watchdog: every 30 minutes, compare live health to last state
            # and alert Brandon on new/persisting RED or recovery. First run at +2 min.
            app.job_queue.run_repeating(sentinel_watchdog, interval=1800, first=120)
            log.info(
                "Reminder scheduler active (cold scan 07:35, Telegram morning brief 07:40, Telegram EOD + energy 18:30 %s). Rhythm %s.",
                tz, "armed" if slack_rhythm.enabled() else "dormant (env not set)",
            )
        else:
            log.warning("JobQueue unavailable — install python-telegram-bot[job-queue] for reminders.")

    # v3.3: Slack Socket Mode responder (team mentions + DMs), env gated.
    if slack_socket.start():
        log.info("Slack socket: armed (mentions + DMs via Socket Mode).")

    log.info("Hermes is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
