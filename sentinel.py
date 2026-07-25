"""sentinel.py — Miles self-monitoring ("Sentinel").

End-to-end, read-only health probes of every subsystem so the OPERATOR (Brandon)
is alerted privately BEFORE the client (Kas) ever notices a break. Every failure
class Miles has ever hit is mapped to a named check here:

    gmail 403 accessNotConfigured    -> gmail_api      (names the disabled API + enable URL)
    calendar 403 accessNotConfigured -> calendar_api   (same, plus a forward window probe)
    calendar visibility / cap        -> calendar_api forward probe (short window shows up)
    truncated / bad deploy           -> module_integrity + db_integrity
    JSON slice / half written file   -> config + module_integrity
    missing clock / wrong date       -> clock
    tool loop claiming success       -> module_integrity asserts the real tool names ship

Design rules:
  * Every check is defensive: its own try/except, it NEVER throws.
  * Every check is fast: short (~8s) per request timeouts. Whole run under ~20s.
  * Every check is READ ONLY: it never sends, creates, or deletes anything real.
  * Nothing here ever logs or returns a secret. Tokens and keys are redacted.

Public surface:
    run_diagnostics(deep=False) -> dict   full structured health report
    boot_selftest()             -> dict   one shot boot probe + RED alert + state seed
    run_watchdog(reason)        -> dict   scheduled compare and alert pass
    send_ops_alert(text)        -> bool   plain Bot API sendMessage to the operator
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("hermes.sentinel")

ZURICH = ZoneInfo("Europe/Zurich")
TIMEOUT = 8.0
BRANDON_ID = 8533640297
_DEBOUNCE_SECONDS = 3 * 3600  # do not re alert the same RED more than once per 3h
_STATE_KEY = "watchdog"

GREEN, AMBER, RED = "green", "amber", "red"
CRITICAL, IMPORTANT = "critical", "important"

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
_CAL = "https://www.googleapis.com/calendar/v3"
_DRIVE = "https://www.googleapis.com/drive/v3"
_ANTHROPIC = "https://api.anthropic.com/v1/messages"

_URL_RE = re.compile(r"https?://[^\s\"'<>)\\]+")

# The tools that MUST ship for Miles to function. A truncated deploy or a tool loop
# that silently drops a function is caught by asserting these are live.
_GOOGLE_TOOLS = {
    "calendar_upcoming_v2", "calendar_create_event",
    "gmail_recent", "gmail_draft", "docs_create", "sheets_create",
}
_NOTION_TOOLS = {"notion_query_database"}


def _now() -> datetime:
    return datetime.now(ZURICH)


def _redact(text: str) -> str:
    """Scrub telegram bot tokens out of any detail text. Google console enable URLs
    carry only a project number (no secret) and are preserved on purpose."""
    if not text:
        return ""
    out = re.sub(r"bot\d+:[\w-]+", "bot<redacted>", str(text))
    return out[:500]


def _enable_url(body: str) -> str:
    """Pull the Google enable this API console URL out of an accessNotConfigured
    error body. Returns '' if none found. These URLs are safe to surface."""
    if not body:
        return ""
    for m in _URL_RE.finditer(body):
        u = m.group(0).rstrip(".,);")
        if "console." in u or "developers.google" in u or "cloud.google" in u:
            return u
    return ""


def _is_access_not_configured(body: str) -> bool:
    low = (body or "").lower()
    return "accessnotconfigured" in low or "has not been used in project" in low


def _check(cid: str, label: str, status: str, detail: str = "",
           fix: str = "", category: str = CRITICAL) -> dict:
    return {
        "id": cid,
        "label": label,
        "status": status,
        "detail": _redact(detail),
        "fix": fix,
        "category": category,
    }


# google shared access token (read only)
def _google_access_token():
    """Exchange the stored refresh token for an access token. Returns
    (access_token, error). error == 'no_token' when Google is not connected."""
    try:
        import database as db
        rt = db.get_google_token()
    except Exception as e:  # noqa: BLE001
        return None, f"db error: {e}"
    if not rt:
        return None, "no_token"
    cid = os.environ.get("GOOGLE_CLIENT_ID", "")
    cs = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not (cid and cs):
        return None, "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set"
    try:
        with httpx.Client(timeout=TIMEOUT) as http:
            r = http.post(_TOKEN_URL, data={
                "client_id": cid, "client_secret": cs,
                "refresh_token": rt, "grant_type": "refresh_token",
            })
        data = r.json()
        if r.status_code >= 400 or not data.get("access_token"):
            err = data.get("error_description") or data.get("error") or f"HTTP {r.status_code}"
            return None, str(err)
        return data["access_token"], None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


# critical checks
def _c_anthropic_key(deep: bool = False) -> dict:
    label = "Anthropic API"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _check("anthropic_key", label, RED, "ANTHROPIC_API_KEY missing.",
                      "Set ANTHROPIC_API_KEY on Railway.", CRITICAL)
    if not deep:
        return _check("anthropic_key", label, GREEN, "ANTHROPIC_API_KEY present.",
                      "", CRITICAL)
    # Deep run only: a real 1 token ping so an invalid/expired key surfaces as RED.
    try:
        import config_loader as cfg
        model = cfg.settings().get("ai", {}).get("model") or "claude-3-5-haiku-latest"
    except Exception:  # noqa: BLE001
        model = "claude-3-5-haiku-latest"
    try:
        with httpx.Client(timeout=TIMEOUT) as http:
            r = http.post(_ANTHROPIC, headers={
                "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }, json={"model": model, "max_tokens": 1,
                     "messages": [{"role": "user", "content": "ping"}]})
        if r.status_code == 200:
            return _check("anthropic_key", label, GREEN, "1 token ping ok.", "", CRITICAL)
        if r.status_code in (401, 403):
            return _check("anthropic_key", label, RED,
                          f"ping rejected HTTP {r.status_code}: {r.text[:160]}",
                          "ANTHROPIC_API_KEY invalid or revoked. Rotate it on Railway.", CRITICAL)
        # Non auth error (rate limit, transient). Key is valid; do not scream RED.
        return _check("anthropic_key", label, AMBER,
                      f"ping HTTP {r.status_code} (key present).",
                      "Transient Anthropic error, watch for repeats.", CRITICAL)
    except Exception as e:  # noqa: BLE001
        return _check("anthropic_key", label, AMBER,
                      f"ping failed but key present: {str(e)[:120]}",
                      "Network to Anthropic failed, watch for repeats.", CRITICAL)


def _c_telegram() -> dict:
    label = "Telegram bot"
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return _check("telegram", label, RED, "TELEGRAM_BOT_TOKEN missing.",
                      "Set TELEGRAM_BOT_TOKEN on Railway.", CRITICAL)
    with httpx.Client(timeout=TIMEOUT) as http:
        r = http.get(f"https://api.telegram.org/bot{token}/getMe")
    if r.status_code == 200 and r.json().get("ok"):
        uname = r.json().get("result", {}).get("username", "")
        return _check("telegram", label, GREEN, f"getMe ok (@{uname}).", "", CRITICAL)
    return _check("telegram", label, RED, f"getMe HTTP {r.status_code}.",
                  "Check TELEGRAM_BOT_TOKEN on Railway.", CRITICAL)


def _c_db_integrity() -> dict:
    """Open the sqlite file and assert the tables Miles depends on exist and the
    token accessor is callable. Catches the truncated file class."""
    label = "Database"
    import database as db
    callable_fn = callable(getattr(db, "get_google_token", None))
    path = os.environ.get("HERMES_DB_PATH") or db.DB_PATH
    conn = sqlite3.connect(path, timeout=TIMEOUT)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        conn.close()
    names = {r[0] for r in rows}
    missing = [t for t in ("google_auth", "memories") if t not in names]
    if not missing and callable_fn:
        return _check("db_integrity", label, GREEN,
                      "sqlite opens, google_auth + memories tables present, "
                      "get_google_token callable.", "", CRITICAL)
    problems = []
    if missing:
        problems.append("missing tables: " + ", ".join(missing))
    if not callable_fn:
        problems.append("get_google_token not callable")
    return _check("db_integrity", label, RED, "; ".join(problems),
                  "Deploy or DB looks truncated. Re run selfcheck and redeploy a full "
                  "database.py, confirm HERMES_DB_PATH volume mounted.", CRITICAL)


def _c_module_integrity() -> dict:
    """Import every core module and assert the real tool names ship. Catches a
    truncated deploy or a silently dropped function the moment it boots."""
    label = "Module integrity"
    import importlib
    problems = []
    for mod in ("connectors", "database", "web_api", "ai", "handlers", "bot"):
        try:
            importlib.import_module(mod)
        except Exception as e:  # noqa: BLE001
            problems.append(f"import {mod}: {str(e)[:120]}")
    try:
        import connectors
        names = {t.get("name") for t in connectors.active_tools()}
        if connectors.GoogleConnector().configured():
            miss = sorted(_GOOGLE_TOOLS - names)
            if miss:
                problems.append("google tools missing: " + ", ".join(miss))
        if connectors.NotionConnector().configured():
            miss = sorted(_NOTION_TOOLS - names)
            if miss:
                problems.append("notion tools missing: " + ", ".join(miss))
    except Exception as e:  # noqa: BLE001
        problems.append(f"tool audit: {str(e)[:120]}")
    if problems:
        return _check("module_integrity", label, RED, "; ".join(problems),
                      "A module failed to import or a critical tool is missing. Re run "
                      "selfcheck before deploy; the deploy may be truncated.", CRITICAL)
    return _check("module_integrity", label, GREEN,
                  "All core modules import; critical tools live.", "", CRITICAL)


def _c_google_token(token, err) -> dict:
    label = "Google token"
    if err == "no_token":
        return _check("google_token", label, AMBER, "No Google refresh token stored yet.",
                      "Kas has not connected Google. Run /connectgoogle when ready.", CRITICAL)
    if err:
        return _check("google_token", label, RED, f"token refresh failed: {err}",
                      "Google refresh rejected or expired. Kas must run /connectgoogle again.",
                      CRITICAL)
    if token:
        return _check("google_token", label, GREEN,
                      "Refresh token exchanged for an access token.", "", CRITICAL)
    return _check("google_token", label, RED, "No access token obtained.",
                  "Kas must run /connectgoogle.", CRITICAL)


def _c_gmail(token) -> dict:
    label = "Gmail API"
    if not token:
        return _check("gmail_api", label, AMBER, "No Google access token (see Google token).",
                      "Fix Google token first.", CRITICAL)
    with httpx.Client(timeout=TIMEOUT) as http:
        r = http.get(f"{_GMAIL}/profile", headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 200:
        addr = r.json().get("emailAddress", "")
        return _check("gmail_api", label, GREEN, f"users.getProfile ok ({addr}).", "", CRITICAL)
    body = r.text[:400]
    if r.status_code == 403 and _is_access_not_configured(body):
        url = _enable_url(body) or "https://console.cloud.google.com/apis/library/gmail.googleapis.com?project=miles-502613"
        return _check("gmail_api", label, RED,
                      "Gmail API disabled on the project (accessNotConfigured).",
                      f"Enable Gmail API: {url}", CRITICAL)
    return _check("gmail_api", label, RED, f"{r.status_code} {body}",
                  "Inspect the Gmail API error.", CRITICAL)


def _c_calendar(token) -> dict:
    label = "Calendar API"
    if not token:
        return _check("calendar_api", label, AMBER, "No Google access token (see Google token).",
                      "Fix Google token first.", CRITICAL)
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=TIMEOUT) as http:
        r = http.get(f"{_CAL}/users/me/calendarList", headers=headers,
                     params={"maxResults": 50})
    if r.status_code != 200:
        body = r.text[:400]
        if r.status_code == 403 and _is_access_not_configured(body):
            url = _enable_url(body) or "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com?project=miles-502613"
            return _check("calendar_api", label, RED,
                          "Calendar API disabled on the project (accessNotConfigured).",
                          f"Enable Calendar API: {url}", CRITICAL)
        return _check("calendar_api", label, RED, f"calendarList {r.status_code} {body}",
                      "Inspect the Calendar API error.", CRITICAL)
    cals = r.json().get("items", []) or []
    if len(cals) < 1:
        return _check("calendar_api", label, AMBER, "calendarList ok but 0 calendars visible.",
                      "Confirm the connected Google account can see calendars.", CRITICAL)
    fwd = _calendar_forward_probe()
    return _check("calendar_api", label, GREEN, f"{len(cals)} calendars. {fwd}", "", CRITICAL)


def _calendar_forward_probe() -> str:
    """Read events across calendars for the next 45 days via the tested connector
    path. Returns a compact string: window + count + furthest event date. A
    visibility capped or 40 event cap regression shows up as a short window / low count."""
    try:
        import connectors
        gc = connectors.GoogleConnector()
        if not gc.configured():
            return "forward probe skipped (connector not configured)."
        raw = gc.run("calendar_upcoming_v2", {"days": 45})
        data = json.loads(raw)
        if not isinstance(data, dict):
            return "forward probe: unexpected shape."
        events = data.get("events", []) or []
        cnt = data.get("count")
        if isinstance(cnt, dict):
            cnt = cnt.get("appointments", len(events))
        elif cnt is None:
            cnt = len(events)
        furthest = ""
        for e in events:
            s = str(e.get("start") or "")[:10]
            if s > furthest:
                furthest = s
        window = data.get("window", {}) or {}
        win = f"{window.get('start', '?')}..{window.get('end', '?')}"
        return f"45d window {win}: {cnt} events, furthest {furthest or 'none'}."
    except Exception as e:  # noqa: BLE001
        return f"forward probe failed: {str(e)[:120]}"


def _c_notion() -> dict:
    label = "Notion"
    import connectors
    nc = connectors.NotionConnector()
    if not nc.configured():
        return _check("notion", label, AMBER, "NOTION_API_KEY not set.",
                      "Set NOTION_API_KEY.", CRITICAL)
    dbid = os.environ.get("NOTION_PROJECTS_DB_ID")
    if not dbid:
        return _check("notion", label, RED, "NOTION_PROJECTS_DB_ID not set.",
                      "Set NOTION_PROJECTS_DB_ID.", CRITICAL)
    with httpx.Client(timeout=TIMEOUT) as http:
        r = http.post(f"{nc._API}/databases/{dbid}/query",
                      headers=nc._headers(), json={"page_size": 1})
    if r.status_code == 200:
        n = len(r.json().get("results", []) or [])
        if n >= 1:
            return _check("notion", label, GREEN, "Projects DB reachable, rows present.",
                          "", CRITICAL)
        return _check("notion", label, AMBER, "Projects DB reachable but 0 rows.",
                      "Confirm the projects database has entries.", CRITICAL)
    body = r.text[:300]
    if r.status_code == 401:
        return _check("notion", label, RED, f"401 {body}",
                      "Notion token invalid, re issue it.", CRITICAL)
    if r.status_code == 404 or "object_not_found" in body.lower():
        return _check("notion", label, RED, f"{r.status_code} {body}",
                      "Share the projects DB with the Miles integration.", CRITICAL)
    return _check("notion", label, RED, f"{r.status_code} {body}",
                  "Inspect the Notion API error.", CRITICAL)


def _c_slack() -> dict:
    label = "Slack"
    import connectors
    sc = connectors.SlackConnector()
    if not sc.configured():
        return _check("slack", label, AMBER, "SLACK_BOT_TOKEN not set.",
                      "Set SLACK_BOT_TOKEN.", CRITICAL)
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    with httpx.Client(timeout=TIMEOUT) as http:
        r = http.post("https://slack.com/api/auth.test",
                      headers={"Authorization": f"Bearer {token}"})
    data = r.json() if r.status_code == 200 else {}
    socket = ""
    try:
        import slack_socket
        connected = bool(getattr(slack_socket, "_socket_client", None)) and slack_socket.enabled()
        socket = f" Socket: {'connected' if connected else 'not connected'}."
    except Exception:  # noqa: BLE001
        socket = ""
    if data.get("ok"):
        team = data.get("team", "")
        return _check("slack", label, GREEN, f"auth.test ok (team {team}).{socket}",
                      "", CRITICAL)
    return _check("slack", label, RED,
                  f"auth.test failed: {data.get('error', r.status_code)}.{socket}",
                  "Check SLACK_BOT_TOKEN.", CRITICAL)


# important checks
def _c_drive(token) -> dict:
    label = "Drive API"
    if not token:
        return _check("drive_api", label, AMBER, "No Google access token (see Google token).",
                      "Fix Google token first.", IMPORTANT)
    with httpx.Client(timeout=TIMEOUT) as http:
        r = http.get(f"{_DRIVE}/about", headers={"Authorization": f"Bearer {token}"},
                     params={"fields": "user"})
    if r.status_code == 200:
        return _check("drive_api", label, GREEN, "Drive about.get ok.", "", IMPORTANT)
    body = r.text[:400]
    if r.status_code == 403 and _is_access_not_configured(body):
        url = _enable_url(body) or "https://console.cloud.google.com/apis/library/drive.googleapis.com?project=miles-502613"
        return _check("drive_api", label, AMBER, "Drive API disabled (accessNotConfigured).",
                      f"Enable Drive API: {url}", IMPORTANT)
    return _check("drive_api", label, AMBER, f"{r.status_code} {body}",
                  "Inspect the Drive API error.", IMPORTANT)


def _c_dashboard() -> dict:
    """Ping the local http server so we know the web thread that serves /health and
    /api/status is actually alive inside this process."""
    label = "Dashboard API"
    port = int(os.environ.get("PORT", "8080") or "8080")
    try:
        with httpx.Client(timeout=4.0) as http:
            r = http.get(f"http://127.0.0.1:{port}/health")
        if r.status_code == 200:
            return _check("dashboard_api", label, GREEN,
                          f"local /health 200 on :{port} (web thread alive).", "", IMPORTANT)
        return _check("dashboard_api", label, AMBER,
                      f"local /health HTTP {r.status_code} on :{port}.",
                      "Web thread degraded. Check web_api.start logs.", IMPORTANT)
    except Exception as e:  # noqa: BLE001
        return _check("dashboard_api", label, AMBER,
                      f"local /health unreachable on :{port}: {str(e)[:120]}",
                      "Web thread not serving. Check web_api.start logs.", IMPORTANT)


def _c_clock() -> dict:
    label = "System clock"
    now = datetime.now(ZURICH)
    year_ok = 2025 <= now.year <= 2027
    tz_ok = now.tzinfo is not None
    if year_ok and tz_ok:
        return _check("clock", label, GREEN, f"now {now.isoformat()} (Europe/Zurich).",
                      "", IMPORTANT)
    return _check("clock", label, RED, f"suspect clock: {now.isoformat()} year {now.year}.",
                  "System clock is wrong. Redeploy / check the host time source.", IMPORTANT)


def _c_elevenlabs() -> dict:
    label = "ElevenLabs"
    if os.environ.get("ELEVENLABS_API_KEY"):
        return _check("elevenlabs", label, GREEN, "ELEVENLABS_API_KEY present.",
                      "", IMPORTANT)
    return _check("elevenlabs", label, AMBER, "ELEVENLABS_API_KEY missing.",
                  "Voice replies will be silent. Set ELEVENLABS_API_KEY if voice is needed.",
                  IMPORTANT)


def _c_elevenlabs_stt() -> dict:
    """Live key validation: /v1/user with the key must answer 200. A scoped key
    that cannot transcribe (missing speech_to_text permission) shows up here as
    amber instead of silently killing voice note input."""
    label = "ElevenLabs STT"
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return _check("elevenlabs_stt", label, AMBER, "ELEVENLABS_API_KEY missing.",
                      "Voice note transcription is off. Set ELEVENLABS_API_KEY.", IMPORTANT)
    try:
        r = httpx.get("https://api.elevenlabs.io/v1/user",
                      headers={"xi-api-key": key}, timeout=15)
    except Exception as e:  # noqa: BLE001
        return _check("elevenlabs_stt", label, AMBER, f"probe failed: {str(e)[:120]}",
                      "Network or ElevenLabs outage; voice notes may not transcribe.", IMPORTANT)
    if r.status_code == 200:
        return _check("elevenlabs_stt", label, GREEN,
                      "key valid (user probe 200); voice note input live.", "", IMPORTANT)
    return _check("elevenlabs_stt", label, AMBER,
                  f"key rejected by user probe ({r.status_code}).",
                  "Key exists but is invalid or lacks permissions. Issue a full permission "
                  "ElevenLabs key (speech_to_text and user_read) and update "
                  "ELEVENLABS_API_KEY on Railway.", IMPORTANT)


def _c_config() -> dict:
    label = "Config files"
    import config_loader as cfg
    persona = cfg.persona()
    knowledge = cfg.knowledge_text()
    settings = cfg.settings()
    examples = cfg.examples()  # must not raise
    _ = examples
    if persona and settings and knowledge:
        return _check("config", label, GREEN,
                      "persona, knowledge, examples and settings all load and are non empty.",
                      "", IMPORTANT)
    empties = []
    if not persona:
        empties.append("persona")
    if not knowledge:
        empties.append("knowledge_text")
    if not settings:
        empties.append("settings")
    return _check("config", label, AMBER, f"empty config: {', '.join(empties)}.",
                  "A config YAML is empty or half written. Re run selfcheck.", IMPORTANT)


# orchestration
def _safe(cid: str, label: str, category: str, fn) -> dict:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return _check(cid, label, RED, f"check crashed: {str(e)[:180]}",
                      "Inspect sentinel logs.", category)


def _safe_call(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def run_diagnostics(deep: bool = False) -> dict:
    """Run every check. Never throws. Returns the structured report.

    deep=True adds a live 1 token Anthropic ping (used by /api/health). The light
    run is used at boot and by the 30 minute watchdog and stays under ~20s."""
    checks: list = []
    checks.append(_safe("anthropic_key", "Anthropic API", CRITICAL, lambda: _c_anthropic_key(deep)))
    checks.append(_safe("telegram", "Telegram bot", CRITICAL, _c_telegram))
    checks.append(_safe("db_integrity", "Database", CRITICAL, _c_db_integrity))
    checks.append(_safe("module_integrity", "Module integrity", CRITICAL, _c_module_integrity))

    token, err = _safe_call(_google_access_token, (None, "probe crashed"))
    checks.append(_safe("google_token", "Google token", CRITICAL, lambda: _c_google_token(token, err)))
    checks.append(_safe("gmail_api", "Gmail API", CRITICAL, lambda: _c_gmail(token)))
    checks.append(_safe("calendar_api", "Calendar API", CRITICAL, lambda: _c_calendar(token)))
    checks.append(_safe("notion", "Notion", CRITICAL, _c_notion))
    checks.append(_safe("slack", "Slack", CRITICAL, _c_slack))

    checks.append(_safe("drive_api", "Drive API", IMPORTANT, lambda: _c_drive(token)))
    checks.append(_safe("dashboard_api", "Dashboard API", IMPORTANT, _c_dashboard))
    checks.append(_safe("clock", "System clock", IMPORTANT, _c_clock))
    checks.append(_safe("elevenlabs", "ElevenLabs", IMPORTANT, _c_elevenlabs))
    checks.append(_safe("elevenlabs_stt", "ElevenLabs STT", IMPORTANT, _c_elevenlabs_stt))
    checks.append(_safe("config", "Config files", IMPORTANT, _c_config))

    counts = {GREEN: 0, AMBER: 0, RED: 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    if counts[RED]:
        overall = RED
    elif counts[AMBER]:
        overall = AMBER
    else:
        overall = GREEN
    return {
        "generated_at": _now().isoformat(),
        "overall": overall,
        "deep": bool(deep),
        "checks": checks,
        "summary_counts": {"green": counts[GREEN], "amber": counts[AMBER],
                           "red": counts[RED], "total": len(checks)},
    }


# alerting (plain Bot API)
def ops_chat_id():
    """Operator chat id. SENTINEL_ALERT_CHAT env overrides; else Brandon's id if it
    is in allowed_ids; else the first allowed id. NEVER Kas."""
    env = os.environ.get("SENTINEL_ALERT_CHAT") or os.environ.get("SENTINEL_OPS_CHAT")
    if env:
        try:
            return int(env)
        except ValueError:
            return None
    try:
        import config_loader as cfg
        ids = [int(x) for x in (cfg.settings().get("access", {}).get("allowed_ids") or [])]
    except Exception:  # noqa: BLE001
        ids = []
    if BRANDON_ID in ids:
        return BRANDON_ID
    if ids:
        return ids[0]
    return BRANDON_ID


def send_ops_alert(text: str) -> bool:
    """Send a Telegram message to the operator via the raw Bot API. Callable from
    the watchdog job and from boot (no PTB context needed). Never raises. Never
    messages Kas."""
    try:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat = ops_chat_id()
        if not (token and chat):
            log.warning("[sentinel] cannot alert: token or operator chat missing.")
            return False
        with httpx.Client(timeout=TIMEOUT) as http:
            r = http.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text, "disable_web_page_preview": True})
        ok = r.status_code == 200
        if not ok:
            log.warning("[sentinel] ops alert HTTP %s", r.status_code)
        return ok
    except Exception as e:  # noqa: BLE001
        log.warning("[sentinel] ops alert failed: %s", e)
        return False


def format_alert(report: dict, focus=None) -> str:
    """Crisp, actionable operator alert. No em dashes or hyphen punctuation."""
    checks = report.get("checks", [])
    all_reds = [c for c in checks if c["status"] == RED]
    focus = focus if focus is not None else all_reds
    counts = report.get("summary_counts", {})
    green = counts.get("green", sum(1 for c in checks if c["status"] == GREEN))
    red = counts.get("red", len(all_reds))
    lines = [f"Miles Sentinel: {report.get('overall', RED).upper()}."]
    for c in focus:
        lines.append(f"{c['label']}: {c['detail']}")
        if c.get("fix"):
            lines.append(f"Fix: {c['fix']}")
    lines.append(f"{green} systems green, {red} red. Kas is not affected yet.")
    return "\n".join(lines)


def format_recovery(reds_cleared: list) -> str:
    if len(reds_cleared) == 1:
        c = reds_cleared[0]
        return f"Miles Sentinel: RECOVERED. {c['label']} is green again."
    labels = ", ".join(c["label"] for c in reds_cleared)
    return f"Miles Sentinel: RECOVERED. Back to all green ({labels})."


# state (survives restarts, no alert spam)
def _load_state() -> dict:
    try:
        import database as db
        raw = db.get_sentinel_state(_STATE_KEY)
        if raw:
            return json.loads(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("[sentinel] state load failed: %s", e)
    return {"checks": {}, "alerts": {}, "overall": None}


def _save_state(state: dict) -> None:
    try:
        import database as db
        db.set_sentinel_state(_STATE_KEY, json.dumps(state))
    except Exception as e:  # noqa: BLE001
        log.warning("[sentinel] state save failed: %s", e)


# boot self-test + watchdog
def boot_selftest() -> dict:
    """Run once at boot. Log one line per check. If overall RED, alert Brandon and
    seed state (with alert timestamps) so the watchdog does not immediately re alert."""
    report = run_diagnostics(deep=False)
    for c in report["checks"]:
        line = f"[sentinel] {c['id']} {c['status'].upper()} ({c['label']})"
        if c["detail"]:
            line += f": {c['detail'][:160]}"
        (log.warning if c["status"] == RED else log.info)(line)
    log.info("[sentinel] boot self-test overall: %s (%s)",
             report["overall"].upper(), report["summary_counts"])

    alerts: dict = {}
    reds = [c for c in report["checks"] if c["status"] == RED]
    if report["overall"] == RED:
        send_ops_alert("Miles boot: RED\n" + format_alert(report, reds))
        now_iso = _now().isoformat()
        for c in reds:
            alerts[c["id"]] = now_iso
    _save_state({
        "checks": {c["id"]: c["status"] for c in report["checks"]},
        "alerts": alerts,
        "overall": report["overall"],
    })
    return report


def run_watchdog(reason: str = "scheduled") -> dict:
    """Scheduled pass. Compare to last state, alert Brandon on new/persisting RED
    (debounced 3h) and on RED->GREEN recovery. Never alerts Kas. Never throws."""
    report = run_diagnostics(deep=False)
    prev = _load_state()
    prev_checks = prev.get("checks", {})
    prev_overall = prev.get("overall")
    alerts = dict(prev.get("alerts", {}))
    now = _now()

    to_alert: list = []
    recovered: list = []

    for c in report["checks"]:
        cid, st = c["id"], c["status"]
        pst = prev_checks.get(cid)
        if st == RED:
            newly = pst != RED
            should = False
            if newly:
                should = True  # a brand new failure always alerts
            else:
                last_iso = alerts.get(cid)
                if not last_iso:
                    should = True  # persisting RED we have not recorded yet
                else:
                    try:
                        last = datetime.fromisoformat(last_iso)
                        if (now - last).total_seconds() >= _DEBOUNCE_SECONDS:
                            should = True  # persisting RED, debounce window elapsed
                    except Exception:  # noqa: BLE001
                        should = True
            if should:
                to_alert.append(c)
                alerts[cid] = now.isoformat()
        else:
            if pst == RED:
                recovered.append(c)
            alerts.pop(cid, None)  # cleared: reset debounce so a re fail alerts again

    if to_alert:
        send_ops_alert(format_alert(report, to_alert))
    # Recovery note: a single consolidated all green message when everything returns
    # to green after we alerted, otherwise a partial recovery naming the cleared checks.
    if recovered and report["overall"] == GREEN and prev_overall == RED:
        send_ops_alert(format_recovery(recovered))
    elif recovered:
        send_ops_alert(format_recovery(recovered))

    _save_state({
        "checks": {c["id"]: c["status"] for c in report["checks"]},
        "alerts": alerts,
        "overall": report["overall"],
    })
    log.info("[sentinel] watchdog (%s): overall %s, %d new alert(s), %d recovered.",
             reason, report["overall"].upper(), len(to_alert), len(recovered))
    return report


def cached_overall() -> str:
    """Last known overall from the watchdog/boot state, for the cheap /health ping.
    Never runs a probe. Returns 'unknown' if nothing recorded yet."""
    try:
        st = _load_state()
        return st.get("overall") or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(run_diagnostics(deep=False), ensure_ascii=False, indent=2))
