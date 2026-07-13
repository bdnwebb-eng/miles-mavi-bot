"""Miles v3.3: Slack Socket Mode responder (mentions + DMs).

Team members can tag @Miles in a channel (app_mention) or DM him (message.im)
and get an AI reply in the same channel, threaded per the workspace thread rule.

Runs the slack_sdk builtin SocketModeClient (stdlib websocket, sync) in a
daemon thread started from bot.py post-init. Env gated: needs SLACK_APP_TOKEN
(xapp, connections:write) and SLACK_BOT_TOKEN (xoxb). Reconnects are handled
by SocketModeClient(auto_reconnect_enabled=True).

Internal team audience, NOT Kas: no client pricing, quotes, invoices, margins,
or tier sheet ever leaves this module. Confidential asks get deferred to Kas.
"""
from __future__ import annotations

import logging
import os
import threading

import config_loader as cfg

log = logging.getLogger("slack_socket")

BOT_USER_ID = "U0BGSQ58ETW"          # Miles's Slack bot user (maviliving.slack.com)
MAX_REPLY_TOKENS = 500
_SEEN_MAX = 500                      # cap on the in-memory event_id dedupe set

_seen_events: set[str] = set()
_seen_lock = threading.Lock()
_user_cache: dict[str, str] = {}     # user id -> display name
_channel_cache: dict[str, str] = {}  # channel id -> #name
_socket_client = None                # keeps the SocketModeClient alive


def enabled() -> bool:
    return bool(os.environ.get("SLACK_APP_TOKEN") and os.environ.get("SLACK_BOT_TOKEN"))


def start() -> bool:
    """Start the Socket Mode listener in a daemon thread. Returns True if armed."""
    if not enabled():
        log.info("Slack socket: not armed (SLACK_APP_TOKEN or SLACK_BOT_TOKEN missing).")
        return False
    threading.Thread(target=_run, name="slack-socket", daemon=True).start()
    return True


def _run() -> None:
    """Daemon thread body: connect and stay alive. Never raises."""
    global _socket_client
    try:
        from slack_sdk import WebClient
        from slack_sdk.socket_mode.builtin import SocketModeClient

        web = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
        _socket_client = SocketModeClient(
            app_token=os.environ["SLACK_APP_TOKEN"],
            web_client=web,
            auto_reconnect_enabled=True,
        )
        _socket_client.socket_mode_request_listeners.append(_on_request)
        _socket_client.connect()
        log.info("Slack socket: connected, listening for mentions and DMs.")
        threading.Event().wait()  # park forever; the client's own threads do the work
    except Exception as e:  # noqa: BLE001  (a Slack failure must never crash the bot)
        log.error("Slack socket: listener stopped: %s", e)


def _on_request(client, req) -> None:
    """SocketModeClient listener: ACK immediately, then handle events_api envelopes."""
    try:
        from slack_sdk.socket_mode.response import SocketModeResponse

        if req.envelope_id:
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return

        payload = req.payload or {}
        event_id = payload.get("event_id") or ""
        if event_id:
            with _seen_lock:
                if event_id in _seen_events:
                    return
                if len(_seen_events) >= _SEEN_MAX:
                    _seen_events.clear()
                _seen_events.add(event_id)

        _handle_event(client.web_client, payload.get("event") or {})
    except Exception as e:  # noqa: BLE001
        log.warning("Slack socket: event handling failed: %s", e)


def _handle_event(web, event: dict) -> None:
    etype = event.get("type", "")
    subtype = event.get("subtype", "")
    if subtype in ("message_changed", "message_deleted"):
        return
    # Never react to bots (including Miles himself): loop prevention.
    if event.get("bot_id") or event.get("user") == BOT_USER_ID or not event.get("user"):
        return

    is_mention = etype == "app_mention"
    is_dm = etype == "message" and event.get("channel_type") == "im"
    if not (is_mention and not subtype) and not (is_dm and not subtype):
        return

    text = (event.get("text") or "").replace(f"<@{BOT_USER_ID}>", "").strip()
    if not text:
        text = "(the message was just a mention with no text; say hi and ask what they need)"

    channel = event.get("channel", "")
    user_name = _display_name(web, event.get("user", ""))
    channel_name = "a direct message" if is_dm else _channel_name(web, channel)

    reply = slack_reply(user_name, channel_name, text)
    if not reply:
        return

    kwargs: dict = {"channel": channel, "text": reply}
    if not is_dm:
        # Workspace thread rule: channel replies always go in the thread.
        kwargs["thread_ts"] = event.get("thread_ts") or event.get("ts")
    web.chat_postMessage(**kwargs)
    log.info("Slack socket: replied to %s in %s.", user_name, channel_name)


def _display_name(web, user_id: str) -> str:
    if not user_id:
        return "a team member"
    if user_id in _user_cache:
        return _user_cache[user_id]
    name = "a team member"
    try:
        info = web.users_info(user=user_id)
        u = info.get("user") or {}
        prof = u.get("profile") or {}
        name = prof.get("display_name") or prof.get("real_name") or u.get("name") or name
    except Exception as e:  # noqa: BLE001
        log.warning("Slack socket: users_info failed for %s: %s", user_id, e)
    _user_cache[user_id] = name
    return name


def _channel_name(web, channel_id: str) -> str:
    if not channel_id:
        return "a channel"
    if channel_id in _channel_cache:
        return _channel_cache[channel_id]
    name = "a channel"
    try:
        info = web.conversations_info(channel=channel_id)
        ch = info.get("channel") or {}
        if ch.get("name"):
            name = "#" + ch["name"]
    except Exception as e:  # noqa: BLE001
        log.warning("Slack socket: conversations_info failed for %s: %s", channel_id, e)
    _channel_cache[channel_id] = name
    return name


def slack_reply(user_name: str, channel_name: str, text: str) -> str:
    """One-shot AI reply for the internal MAVI Slack. No tools, no history."""
    import ai  # deferred: avoid import cycles at module load

    s = cfg.settings().get("ai", {})
    try:
        tone = (cfg.persona() or {}).get("tone", "")
    except Exception:  # noqa: BLE001
        tone = ""

    system = (
        "You are Miles, MAVI's AI executive assistant. You are replying INSIDE the "
        "internal MAVI Slack workspace to a team member, not to Kas. Be calm, brief, "
        "and helpful. Format with Slack mrkdwn (*bold*, bullets with •); short "
        "lines, phone readable, 1 to 6 lines unless a list is truly needed.\n\n"
        "HARD RULES, never break these:\n"
        "1. NEVER reveal client pricing, quotes, invoices, margins, or the MAVI tier "
        "sheet in Slack. Technical team members must not see pricing. If asked, say "
        "that stays with Kas and offer to flag it to her.\n"
        "2. NEVER share Kas's personal details (family, health, home, travel plans, "
        "contact info).\n"
        "3. If asked for anything confidential or client facing, politely say that "
        "stays with Kas and offer to flag it to her.\n"
        "4. No em dashes, ever. Use periods, colons, or commas instead.\n"
        "5. Sign nothing: no sign-offs, no name at the end.\n"
        "6. If you do not know something, say so plainly and offer the next step. "
        "Never invent project facts, numbers, or commitments.\n\n"
        + (f"Your tone: {tone.strip()}\n" if tone else "")
    )
    user_msg = (
        f"Slack message from {user_name} in {channel_name}:\n\n{text}\n\n"
        "Write Miles's reply (just the reply text, nothing else)."
    )
    try:
        client = ai._get_client()
        resp = client.messages.create(
            model=s.get("model", "claude-sonnet-4-6"),
            max_tokens=MAX_REPLY_TOKENS,
            temperature=0.6,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        reply = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        return reply or "On it. Give me one more line of detail and I will sort it."
    except Exception as e:  # noqa: BLE001
        log.warning("Slack socket: reply generation failed: %s", e)
        return "I hit a snag generating that reply. Try me again in a minute."
