"""Read-only live JSON API for the Miles command dashboard.

Runs a tiny stdlib http.server in a DAEMON THREAD alongside the python-telegram-bot
polling loop. No new pip dependencies, no entanglement with the PTB asyncio loop:
the connector layer uses synchronous httpx, which is perfectly happy in a thread.

Data routes:
    GET  /api/status?key=<DASHBOARD_API_KEY>
    (GET /api/dashboard is kept as a backward-compatible alias.)
    POST /api/note?key=<DASHBOARD_API_KEY>   {"section": "...", "text": "..."}
    (the ONE write route: Kas leaves a note under a dashboard section; it lands in
    sqlite on the volume, shows in the payload under "notes", and alerts Brandon.)

Everything else is READ ONLY and CLIENT-SAFE. The payload carries aggregates plus
project titles / stages only. It NEVER includes email bodies, message contents,
subjects, or any pricing. CORS is wide open (Access-Control-Allow-Origin: *) so
the Netlify dashboard can fetch it from the browser.

Auth: the key is validated against env DASHBOARD_API_KEY. On mismatch -> 401.
If DASHBOARD_API_KEY is unset the server still starts (so /health works) but the
data route returns 503 "not configured".

Health routes:
    GET /health -> 200 "ok"            (plain uptime ping)
    GET /api/health?key=<DASHBOARD_API_KEY> -> full Sentinel diagnostics JSON
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import httpx

import connectors
import database as db

log = logging.getLogger("hermes.web_api")

LOOP_SCHEDULE = {
    "cold_scan": "daily 07:35 Geneva",
    "morning_brief": "daily 07:40 Geneva, Telegram",
    "eod_brief_and_energy": "daily 18:30 Geneva, Telegram",
}


# data assembly (each section isolated)
def _principal_energy() -> list:
    """30 day energy history for the principal. Iterate allowed ids, use the first
    that has scores logged. Returns [{day, score}, ...] oldest first."""
    try:
        import config_loader as cfg
        ids = [int(x) for x in (cfg.settings().get("access", {}).get("allowed_ids") or [])]
    except Exception:  # noqa: BLE001
        ids = []
    for tid in ids:
        rows = db.energy_history(tid, 30)
        if rows:
            return [{"day": r[0], "score": r[1]} for r in rows]
    return []


def _projects() -> list:
    """Live project rows from Notion, flattened via the connector's own logic.

    Queries NOTION_PROJECTS_DB_ID directly (rather than the model-facing
    notion_query_database tool) so we also capture last_edited_time for the
    cold / days_stale calculation. Sorted NOW / live first.
    """
    dbid = os.environ.get("NOTION_PROJECTS_DB_ID")
    api_key = os.environ.get("NOTION_API_KEY")
    if not (dbid and api_key):
        return []
    nc = connectors.NotionConnector()
    now = datetime.now(connectors.timezone.utc)
    out = []
    with httpx.Client(timeout=25) as http:
        r = http.post(
            f"{nc._API}/databases/{dbid}/query",
            headers=nc._headers(),
            json={"page_size": 100},
        )
        r.raise_for_status()
        for pg in r.json().get("results", []):
            props = pg.get("properties", {}) or {}
            flat = {name: nc._prop_value(prop) for name, prop in props.items()}
            days_stale = None
            edited = pg.get("last_edited_time", "")
            try:
                ts = datetime.fromisoformat(edited.replace("Z", "+00:00"))
                days_stale = (now - ts).days
            except (ValueError, AttributeError):
                days_stale = None
            live = bool(flat.get("Live Now"))
            stage = flat.get("Stage") or ""
            # v6.7 (Kas): live projects and active engagements are NEVER cold.
            # Cold only means a non-live pipeline row nobody has touched.
            is_cold = bool(days_stale is not None and days_stale > 7
                           and not live and stage != "Active Engagement")
            out.append({
                "id": pg.get("id", ""),
                "client": flat.get("Client Name") or nc._title_of(pg),
                "code": flat.get("Project Code") or "",
                "stage": stage,
                "tier": flat.get("Lead Tier") or "",
                "urgency": flat.get("Urgency") or "",
                "location": flat.get("Property Location") or "",
                "property_type": flat.get("Property Type") or "",
                "next_action": flat.get("Next Action") or "",
                "live": live,
                "days_stale": days_stale,
                "cold": is_cold,
            })
    tier_rank = {"NOW": 0, "SOON": 1, "LATER": 2}

    def _key(p):
        return (
            0 if p["live"] else 1,
            tier_rank.get((p["tier"] or "").upper(), 3),
            -(p["days_stale"] or 0),
        )

    out.sort(key=_key)
    return out


# v8 (Kas): the project board shows ACTIVE work only. Known pipeline stages are
# Qualified Lead / Intro Call Booked / Proposal Sent / Review Deposit Received /
# Active Engagement / Handover / Post-Handover. Post-Handover is delivered work,
# so it leaves the board; the word check also catches any done, completed,
# archived, parked or on hold style stage added to Notion later. The filter is
# applied in build_payload so the KPI counts always agree with the board.
_INACTIVE_STAGE_EXACT = {"post-handover", "post handover"}
_INACTIVE_STAGE_WORDS = ("complete", "done", "archiv", "closed", "lost",
                         "park", "hold", "dorman", "cancel", "inactive")


def _stage_is_active(stage) -> bool:
    s = str(stage or "").strip().lower()
    if not s:
        return True
    if s in _INACTIVE_STAGE_EXACT:
        return False
    return not any(w in s for w in _INACTIVE_STAGE_WORDS)


_LIVE_STAGE_EXACT = {"active engagement", "review deposit received", "handover"}


def _stage_is_live(stage) -> bool:
    """v6.8: live = accepted and paying. Anything active but not live is a
    proposal in the pipeline (Qualified Lead / Intro Call Booked / Proposal
    Sent, plus any unknown stage) and is counted, never shown as a project."""
    return str(stage or "").strip().lower() in _LIVE_STAGE_EXACT


def _dash_calendars() -> list[str]:
    """v6.7 (Kas): the dashboard week projection shows ONLY the calendars she
    cares about: her kas@maviliving.com calendar and her Outlook.KasBordier
    import. Oliver, Olga, Kas & Ev, and Holidays in Switzerland are excluded
    from the DASHBOARD view only; Miles himself still reads every calendar."""
    raw = os.environ.get("DASHBOARD_CAL_WHITELIST", "kas@maviliving.com,Outlook.KasBordier")
    return [c.strip() for c in raw.split(",") if c.strip()]


def _calendar(days: int = 7) -> dict:
    """Upcoming events from the ONE calendar data path in the system:
    GoogleConnector.calendar_upcoming_v2 (live, every calendar, Sentinel probed).
    v6.6: the silent ICS fallback is gone. If Google is not connected the payload
    says complete=false with an explicit error, so the dashboard shows "not live"
    instead of a confidently empty calendar (the 2026-07-28 failure class).
    v6.7: events are filtered to the dashboard calendar whitelist (Kas's own
    calendars only) and carry the event notes.
    Returns {"events": [...], "meta": {window, complete, count, calendars}}."""
    gc = connectors.GoogleConnector()
    if not gc.configured():
        return {"events": [], "meta": {"window": None, "complete": False,
                                       "error": "google not connected"}}
    raw = gc.run("calendar_upcoming_v2", {"days": days})
    try:
        data = json.loads(raw)
    except ValueError:
        return {"events": [], "meta": {"window": None, "complete": False,
                                       "error": raw[:200]}}
    if isinstance(data, dict):
        items = data.get("events", []) or []
        meta = {"window": data.get("window"), "complete": data.get("complete"),
                "count": data.get("count")}
        if data.get("ideal_week_daily"):
            meta["ideal_week_daily"] = data["ideal_week_daily"]
        if data.get("note"):
            meta["note"] = data["note"]
    else:
        items = data if isinstance(data, list) else []
        meta = {"window": None, "complete": True, "count": len(items)}
    allow = _dash_calendars()
    events = [
        {"summary": e.get("summary", ""), "start": e.get("start", ""),
         "end": e.get("end", ""), "location": e.get("location", ""),
         "notes": e.get("notes", ""), "calendar": e.get("calendar", "")}
        for e in items if e.get("calendar", "") in allow
    ]
    meta["calendars"] = allow
    meta["count"] = len(events)
    return {"events": events, "meta": meta}


_TRAVEL_RE = None


def _travel(days: int = 60) -> list:
    """v6.7 (Kas): the travel strip shows real upcoming travel pulled LIVE from
    her calendars (all of them, travel is often on the Travel / imported
    calendars): events whose title or notes look like travel. The iCloud travel
    calendar joins this automatically once it is shared into her Google."""
    import re as _re
    global _TRAVEL_RE
    if _TRAVEL_RE is None:
        _TRAVEL_RE = _re.compile(
            r"flight|fly to|✈|airport|travel|trip\b|hotel|check.?in|depart|"
            r"arrival|airbnb|train to", _re.I)
    gc = connectors.GoogleConnector()
    if not gc.configured():
        return []
    raw = gc.run("calendar_upcoming_v2", {"days": days})
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    items = data.get("events", []) if isinstance(data, dict) else []
    out = []
    for e in items:
        hay = f"{e.get('summary','')} {e.get('notes','')} {e.get('calendar','')}"
        if _TRAVEL_RE.search(hay):
            out.append({"summary": e.get("summary", ""), "start": e.get("start", ""),
                        "end": e.get("end", ""), "location": e.get("location", ""),
                        "calendar": e.get("calendar", "")})
    return out[:10]


def _inbox():
    """Unread count ONLY. No subjects, no bodies. None if Google not connected.
    Returns a plain integer (or None)."""
    gc = connectors.GoogleConnector()
    if not gc.configured():
        return None
    with httpx.Client(timeout=20) as http:
        r = http.get(
            f"{gc._GMAIL}/messages",
            headers=gc._auth_headers(),
            params={"q": "is:unread", "maxResults": 1},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"gmail {r.status_code}: {r.text[:300]}")
        data = r.json()
        return int(data.get("resultSizeEstimate", 0) or 0)


_CHAT_LOCK = threading.Lock()


def _chat_key() -> str | None:
    """The dashboard chat key. Env DASHBOARD_CHAT_KEY wins; otherwise one is
    minted once, kept in the bot's own DB, and DMed to the operator so he can
    hand it to Kas. It is NEVER embedded in the dashboard page source."""
    envk = os.environ.get("DASHBOARD_CHAT_KEY")
    if envk:
        return envk
    try:
        k = db.get_sentinel_state("dashboard_chat_key")
        if not k:
            import secrets
            k = secrets.token_urlsafe(9)
            db.set_sentinel_state("dashboard_chat_key", k)
            try:
                import sentinel
                sentinel.send_ops_alert(
                    "Dashboard chat is live. Chat key: " + k + " . Give it to Kas; "
                    "she enters it once when she taps Miles's photo on the dashboard. "
                    "Set DASHBOARD_CHAT_KEY in Railway to replace it any time.")
            except Exception:  # noqa: BLE001
                pass
        return k
    except Exception:  # noqa: BLE001
        return None


def _chat_tid(who: str = "principal") -> int | None:
    """Whose conversation the dashboard chat joins. Default: the principal
    (Kas), the SAME thread, memory and ledger as her Telegram chat, so Miles
    is one person across both doors. 'operator' joins the operator's thread."""
    try:
        import sentinel
        ops = sentinel.ops_chat_id()
    except Exception:  # noqa: BLE001
        ops = None
    if who == "operator":
        return int(ops) if ops else None
    env = os.environ.get("DASHBOARD_CHAT_TID")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    try:
        import config_loader as cfg
        allowed = (cfg.settings().get("access", {}) or {}).get("allowed_ids") or []
        for a in allowed:
            if ops is None or int(a) != int(ops):
                return int(a)
        if allowed:
            return int(allowed[0])
    except Exception:  # noqa: BLE001
        pass
    return None


_KIND_META = {
    "notion":     {"env": "NOTION_API_KEY",     "type": "Integration token"},
    "slack":      {"env": "SLACK_BOT_TOKEN",    "type": "Bot token"},
    "elevenlabs": {"env": "ELEVENLABS_API_KEY", "type": "API key"},
    "openai":     {"env": "OPENAI_API_KEY",     "type": "API key"},
    "composio":   {"env": "COMPOSIO_API_KEY",   "type": "API key"},
    "custom":     {"env": "",                    "type": "API key"},
}

_SENSE_LABELS = {
    "notion":   ("Notion", "Integration token"),
    "slack":    ("Slack", "Bot token"),
    "whatsapp": ("WhatsApp", "No access, by design"),
    "calendar": ("Google (Calendar, Gmail, Drive, Docs)", "Google sign-in by Kas"),
    "email":    ("Email inbox (IMAP)", "App password"),
    "gmail":    ("Gmail", "Google sign-in by Kas"),
    "drive":    ("Google Drive", "Google sign-in by Kas"),
    "digest":   ("Daily digest", "Runs with the operator"),
    "telegram": ("Telegram (Miles himself)", "Bot token"),
}


def _probe_secret(kind: str, secret: str) -> tuple[str, str]:
    """Live-test a pasted credential. Returns (status, detail): 'live' when the
    provider accepted it, 'bad' when it rejected it, 'stored' when this kind
    has no probe yet (saved for the builder to wire in)."""
    try:
        with httpx.Client(timeout=12) as hc:
            if kind == "notion":
                r = hc.get("https://api.notion.com/v1/users/me",
                           headers={"Authorization": f"Bearer {secret}",
                                    "Notion-Version": "2022-06-28"})
                return ("live", "Notion accepted the key") if r.status_code < 400 else ("bad", f"Notion said {r.status_code}")
            if kind == "slack":
                r = hc.post("https://slack.com/api/auth.test",
                            headers={"Authorization": f"Bearer {secret}"})
                ok = r.status_code < 400 and bool(r.json().get("ok"))
                return ("live", "Slack accepted the token") if ok else ("bad", "Slack rejected the token")
            if kind == "elevenlabs":
                r = hc.get("https://api.elevenlabs.io/v1/user", headers={"xi-api-key": secret})
                return ("live", "ElevenLabs accepted the key") if r.status_code < 400 else ("bad", f"ElevenLabs said {r.status_code}")
            if kind == "openai":
                r = hc.get("https://api.openai.com/v1/models",
                           headers={"Authorization": f"Bearer {secret}"})
                return ("live", "OpenAI accepted the key") if r.status_code < 400 else ("bad", f"OpenAI said {r.status_code}")
            if kind == "composio":
                r = hc.get("https://backend.composio.dev/api/v1/apps",
                           headers={"x-api-key": secret})
                return ("live", "Composio accepted the key") if r.status_code < 400 else ("bad", f"Composio said {r.status_code}")
    except Exception as e:  # noqa: BLE001
        return ("stored", f"saved; could not test it live ({str(e)[:80]})")
    return ("stored", "saved; the builder wires this kind in next")


def _connections() -> list:
    """Everything connected, for the dashboard Settings page. Built-in senses
    first (operator-managed env and OAuth), then keys added from the dashboard.
    Secrets never leave the server; only names and statuses do."""
    out = []
    for k, v in (_senses() or {}).items():
        label, typ = _SENSE_LABELS.get(k, (k.capitalize(), "Connection"))
        out.append({"name": label, "kind": k, "type": typ, "status": v,
                    "managed": "operator"})
    if os.environ.get("TELEGRAM_BOT_TOKEN") and not any(c["kind"] == "telegram" for c in out):
        out.append({"name": "Telegram (Miles himself)", "kind": "telegram",
                    "type": "Bot token", "status": "live", "managed": "operator"})
    try:
        for row in db.vault_all():
            out.append({
                "name": row["name"], "kind": row["kind"],
                "type": _KIND_META.get(row["kind"], _KIND_META["custom"])["type"],
                "status": row["status"], "detail": row["detail"] or "",
                "added": (row["added_utc"] or "")[:10], "managed": "dashboard",
            })
    except Exception:  # noqa: BLE001
        pass
    return out


def _senses() -> dict:
    """Connector -> status string. 'live' / 'pending' / 'operator'. v6.6: calendar
    truth comes only from the Google token (the ICS 'partial' state is gone with
    the ICS path). v8 honesty: WhatsApp is NOT wired into this worker. The daily
    digest runs through the operator's own session outside the bot, so it must
    never report live; the dashboard renders 'operator' as Not connected."""
    has_google_token = bool(db.get_google_token())
    email_imap = connectors.EmailConnector().configured()
    return {
        "notion": "live" if connectors.NotionConnector().configured() else "pending",
        "slack": "live" if connectors.SlackConnector().configured() else "pending",
        "whatsapp": "off",
        "calendar": "live" if has_google_token else "pending",
        "email": "live" if (has_google_token or email_imap) else "pending",
        "google": "live" if has_google_token else "pending",
        "instagram": "pending",
    }


# Warm cache for the default dashboard window. Building the payload does live
# Notion + multi calendar + Gmail calls and takes ~6s, which loses the race
# against a browser fetch timeout (the dashboard always fell back to snapshot).
# A background thread keeps the days=7 payload warm so the endpoint answers in
# milliseconds. Other day windows (verification) still build live on demand.
_CACHE_LOCK = threading.Lock()
_STATUS_CACHE = {"payload": None, "built_at": 0.0}
_CACHE_TTL = 240          # a fresh build is served for up to 4 minutes
_REFRESH_EVERY = 120      # background refresher cadence in seconds


def get_cached_status(days: int = 7) -> dict:
    """Return the warm days=7 payload instantly when possible; build live for
    any other window or a cold/stale cache."""
    if days != 7:
        return build_payload(days)
    now = time.time()
    with _CACHE_LOCK:
        cached = _STATUS_CACHE["payload"]
        age = now - _STATUS_CACHE["built_at"]
    if cached is not None and age < _CACHE_TTL:
        return cached
    payload = build_payload(7)
    with _CACHE_LOCK:
        _STATUS_CACHE["payload"] = payload
        _STATUS_CACHE["built_at"] = time.time()
    return payload


def _refresh_cache_loop() -> None:
    """Rebuild the warm days=7 payload on a cadence so page loads are instant."""
    while True:
        try:
            payload = build_payload(7)
            with _CACHE_LOCK:
                _STATUS_CACHE["payload"] = payload
                _STATUS_CACHE["built_at"] = time.time()
        except Exception as e:  # noqa: BLE001
            log.warning("status cache refresh failed: %s", e)
        time.sleep(_REFRESH_EVERY)


def build_payload(days: int = 7) -> dict:
    """Assemble the whole dashboard payload. Every section is isolated so one
    failing source degrades to null instead of 500-ing the endpoint."""
    now_zurich = datetime.now(connectors.LOCAL_TZ)
    errors = {}

    def _safe(name, fn, default):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            errors[name] = str(e)[:200]
            log.warning("dashboard section %s failed: %s", name, e)
            return default

    projects = _safe("projects", _projects, [])
    # Active work only; KPI counts below run on the same filtered list.
    projects = [p for p in projects if _stage_is_active(p.get("stage"))]
    # v6.8 (Brandon, 2026-07-30): the board shows LIVE projects only. Accepted
    # and paying work: Active Engagement / Review Deposit Received / Handover.
    # Everything else is a proposal until accepted, so proposal stage rows
    # collapse into the funnel numbers plus a proposals_pipeline count instead
    # of posing as projects. KPIs are computed AFTER this filter so the numbers
    # always agree with the board.
    _stg = lambda p: str(p.get("stage") or "").strip().lower()
    funnel = {
        "qualified": sum(1 for p in projects if _stg(p) == "qualified lead"),
        "intro_booked": sum(1 for p in projects if _stg(p) == "intro call booked"),
        "proposal_out": sum(1 for p in projects if _stg(p) == "proposal sent"),
    }
    pipeline = [p for p in projects if not _stage_is_live(p.get("stage"))]
    proposals_pipeline = len(pipeline)
    projects = [p for p in projects if _stage_is_live(p.get("stage"))]
    travel = _safe("travel", _travel, [])
    cal = _safe("calendar", lambda: _calendar(days), {"events": [], "meta": {}})
    if not isinstance(cal, dict):
        cal = {"events": cal or [], "meta": {}}
    calendar = cal.get("events", [])
    calendar_meta = cal.get("meta", {})
    inbox_unread = _safe("inbox_unread", _inbox, None)
    energy_days = _safe("energy", _principal_energy, [])
    senses = _safe("senses", _senses, {})
    connections = _safe("connections", _connections, [])
    notes = _safe("notes", lambda: [
        {"id": r["id"], "ts_utc": r["ts_utc"], "section": r["section"],
         "text": r["text"]}
        for r in db.recent_notes(50)
    ], [])

    _scores = [d["score"] for d in (energy_days or [])
               if isinstance(d.get("score"), (int, float))]
    energy = {
        "days": energy_days or [],
        "average": round(sum(_scores) / len(_scores), 1) if _scores else None,
    }

    senses_live = sum(1 for v in (senses or {}).values() if v == "live")
    now_tier = sum(1 for p in projects if (p.get("tier") or "").upper() == "NOW")
    live_now = sum(1 for p in projects if p.get("live"))
    cold_count = sum(1 for p in projects if p.get("cold"))

    # v6.6: podcasts_booked removed. It was a hardcoded 1 with no live source,
    # exactly the "confident number nobody is maintaining" class the dashboard
    # honesty pass exists to kill. The dashboard hides the tile when absent;
    # re-add only when a real tracker (Notion) feeds it.
    kpis = {
        "projects_total": len(projects),
        "live_now": live_now,
        "now_tier": now_tier,
        "cold_count": cold_count,
        "senses_live": senses_live,
        "proposals_pipeline": proposals_pipeline,
    }

    return {
        "generated_at": now_zurich.isoformat(),
        "kpis": kpis,
        "projects": projects,
        "funnel": funnel,
        "pipeline": pipeline,
        "calendar": calendar,
        "calendar_meta": calendar_meta,
        "travel": travel,
        "inbox_unread": inbox_unread,
        "energy": energy,
        "senses": senses,
        "connections": connections,
        "notes": notes,
        "loops": dict(LOOP_SCHEDULE),
        "errors": errors,
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:  # noqa: BLE001
            pass

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"

        if route == "/health":
            # Tiny, key-free uptime ping. Returns the LAST known overall from the
            # Sentinel watchdog state (never runs a live probe here, so it stays fast
            # and cheap for uptime pingers). Full health is /api/health.
            try:
                import sentinel
                overall = sentinel.cached_overall()
            except Exception:  # noqa: BLE001
                overall = "unknown"
            self._send(200, json.dumps({"status": "ok", "overall": overall}).encode())
            return

        if route == "/api/health":
            # Full Sentinel diagnostics as JSON, key-gated like /api/status. Lets the
            # builder eyeball the true live health of every subsystem by curling one URL.
            expected = os.environ.get("DASHBOARD_API_KEY")
            if not expected:
                self._send(503, json.dumps({"error": "not configured"}).encode(), "application/json")
                return
            qs = parse_qs(parsed.query)
            key = (qs.get("key") or [""])[0]
            if key != expected:
                self._send(401, json.dumps({"error": "unauthorized"}).encode(), "application/json")
                return
            try:
                import sentinel
                report = sentinel.run_diagnostics(deep=True)
                body = json.dumps(report, ensure_ascii=False).encode("utf-8")
            except Exception as e:  # noqa: BLE001
                log.exception("sentinel diagnostics failed")
                self._send(500, json.dumps({"error": "internal", "detail": str(e)[:200]}).encode())
                return
            self._send(200, body)
            return

        if route == "/api/chat/history":
            qs = parse_qs(parsed.query)
            ck = _chat_key()
            if not ck or (qs.get("key") or [""])[0] != ck:
                self._send(401, json.dumps({"error": "unauthorized"}).encode())
                return
            tid = _chat_tid((qs.get("as") or ["principal"])[0])
            if not tid:
                self._send(503, json.dumps({"error": "no principal configured"}).encode())
                return
            try:
                limit = min(int((qs.get("limit") or ["40"])[0]), 80)
            except ValueError:
                limit = 40
            try:
                rows = [{"role": r["role"], "content": r["content"]}
                        for r in db.recent_messages(tid, limit)]
            except Exception:  # noqa: BLE001
                rows = []
            self._send(200, json.dumps({"messages": rows}, ensure_ascii=False).encode("utf-8"))
            return

        if route in ("/api/status", "/api/dashboard"):
            expected = os.environ.get("DASHBOARD_API_KEY")
            if not expected:
                self._send(503, json.dumps({"error": "not configured"}).encode(), "application/json")
                return
            qs = parse_qs(parsed.query)
            key = (qs.get("key") or [""])[0]
            if key != expected:
                self._send(401, json.dumps({"error": "unauthorized"}).encode(), "application/json")
                return
            try:
                days = int((qs.get("days") or ["7"])[0])
            except (TypeError, ValueError):
                days = 7
            days = max(1, min(days, 60))
            try:
                payload = get_cached_status(days)
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            except Exception as e:  # noqa: BLE001
                log.exception("dashboard payload build failed")
                self._send(500, json.dumps({"error": "internal", "detail": str(e)[:200]}).encode())
                return
            self._send(200, body)
            return

        self._send(404, json.dumps({"error": "not found"}).encode())

    def _post_connection(self, parsed) -> None:
        """POST /api/connections?key= {name, kind, secret}. Additive only: a
        credential the operator set in Railway can never be replaced from the
        dashboard. The key is live-tested before it is stored; the operator is
        DMed on every add so a poisoned key never lands silently."""
        expected = os.environ.get("DASHBOARD_API_KEY")
        if not expected:
            self._send(503, json.dumps({"error": "not configured"}).encode())
            return
        qs = parse_qs(parsed.query)
        if (qs.get("key") or [""])[0] != expected:
            self._send(401, json.dumps({"error": "unauthorized"}).encode())
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > 8192:
            self._send(400, json.dumps({"error": "bad body size"}).encode())
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            self._send(400, json.dumps({"error": "invalid json"}).encode())
            return
        if not isinstance(data, dict):
            self._send(400, json.dumps({"error": "invalid body"}).encode())
            return
        name = str(data.get("name") or "").strip()[:80]
        kind = str(data.get("kind") or "custom").strip().lower()[:40]
        secret = str(data.get("secret") or "").strip()
        if not (name and secret) or len(secret) > 4000:
            self._send(400, json.dumps({"error": "need a name and the key"}).encode())
            return
        meta = _KIND_META.get(kind, _KIND_META["custom"])
        env_slot = meta.get("env") or ""
        if env_slot and os.environ.get(env_slot):
            self._send(409, json.dumps({
                "error": "already connected",
                "detail": "This one is managed by the operator. Ask Brandon to change it."}).encode())
            return
        status, detail = _probe_secret(kind, secret)
        if status == "bad":
            self._send(400, json.dumps({"error": "key rejected", "detail": detail}).encode())
            return
        try:
            db.vault_set(name=name, kind=kind, secret=secret, status=status,
                         detail=detail, added_by="dashboard")
        except Exception as e:  # noqa: BLE001
            log.exception("vault save failed")
            self._send(500, json.dumps({"error": "internal", "detail": str(e)[:200]}).encode())
            return
        with _CACHE_LOCK:
            _STATUS_CACHE["payload"] = None
            _STATUS_CACHE["built_at"] = 0.0
        try:
            import sentinel
            sentinel.send_ops_alert(
                f"New connection added from the dashboard: {name} ({kind}) -> {status}. "
                "If this was not Kas or you, remove the row from the connections "
                "table. Env credentials cannot be overridden by this.")
        except Exception:  # noqa: BLE001
            pass
        self._send(200, json.dumps({"ok": True, "status": status, "detail": detail}).encode())

    _ALLOWED_STAGES = ("Qualified Lead", "Intro Call Booked", "Proposal Sent",
                       "Review Deposit Received", "Active Engagement", "Handover",
                       "Post-Handover")

    def _post_stage(self, parsed) -> None:
        """POST /api/stage?key= {page_id, stage}. The kanban write: moves a card's
        Stage select in Notion itself (the one source of truth), then verifies by
        readback. The dashboard never keeps its own copy of the stage."""
        expected = os.environ.get("DASHBOARD_API_KEY")
        if not expected:
            self._send(503, json.dumps({"error": "not configured"}).encode())
            return
        qs = parse_qs(parsed.query)
        if (qs.get("key") or [""])[0] != expected:
            self._send(401, json.dumps({"error": "unauthorized"}).encode())
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > 2048:
            self._send(400, json.dumps({"error": "bad body size"}).encode())
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            self._send(400, json.dumps({"error": "invalid json"}).encode())
            return
        page_id = str((data or {}).get("page_id") or "").strip()
        stage = str((data or {}).get("stage") or "").strip()
        if not page_id or stage not in self._ALLOWED_STAGES:
            self._send(400, json.dumps({"error": "need page_id and a real stage"}).encode())
            return
        api_key = connectors.cred("NOTION_API_KEY")
        if not api_key:
            self._send(503, json.dumps({"error": "Notion is not connected"}).encode())
            return
        headers = {"Authorization": f"Bearer {api_key}",
                   "Notion-Version": "2022-06-28",
                   "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=15) as hc:
                r = hc.patch(f"https://api.notion.com/v1/pages/{page_id}",
                             headers=headers,
                             json={"properties": {"Stage": {"select": {"name": stage}}}})
                if r.status_code >= 400:
                    self._send(502, json.dumps(
                        {"error": "Notion refused the move",
                         "detail": r.text[:200]}).encode())
                    return
                got = (((r.json().get("properties") or {}).get("Stage") or {})
                       .get("select") or {}).get("name")
        except Exception as e:  # noqa: BLE001
            self._send(502, json.dumps({"error": "internal", "detail": str(e)[:150]}).encode())
            return
        if got != stage:
            self._send(502, json.dumps(
                {"error": "verification failed",
                 "detail": f"Notion reports stage '{got}'"}).encode())
            return
        with _CACHE_LOCK:
            _STATUS_CACHE["payload"] = None
            _STATUS_CACHE["built_at"] = 0.0
        self._send(200, json.dumps({"ok": True, "stage": stage}).encode())

    def _post_chat(self, parsed) -> None:
        """POST /api/chat?key=<chat key> {text, as?}. Runs the SAME brain as
        Telegram: ai.coach_reply with the principal's thread, short-term
        history, curated memory, verified ledger and every tool gate. The chat
        key is separate from the dashboard read key and never appears in the
        page source; Kas types it once."""
        ck = _chat_key()
        if not ck:
            self._send(503, json.dumps({"error": "chat not configured"}).encode())
            return
        qs = parse_qs(parsed.query)
        if (qs.get("key") or [""])[0] != ck:
            self._send(401, json.dumps({"error": "unauthorized"}).encode())
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > 16384:
            self._send(400, json.dumps({"error": "bad body size"}).encode())
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            self._send(400, json.dumps({"error": "invalid json"}).encode())
            return
        text = str((data or {}).get("text") or "").strip()
        if not text or len(text) > 4000:
            self._send(400, json.dumps({"error": "need text under 4000 characters"}).encode())
            return
        tid = _chat_tid(str((data or {}).get("as") or "principal"))
        if not tid:
            self._send(503, json.dumps({"error": "no principal configured"}).encode())
            return
        try:
            import ai
            with _CHAT_LOCK:
                reply = ai.coach_reply(tid, text)
        except Exception as e:  # noqa: BLE001
            log.exception("dashboard chat failed")
            self._send(500, json.dumps(
                {"error": "Miles hit an error and did NOT finish that",
                 "detail": str(e)[:200]}).encode())
            return
        self._send(200, json.dumps({"reply": reply}, ensure_ascii=False).encode("utf-8"))

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        if route == "/api/chat":
            self._post_chat(parsed)
            return
        if route == "/api/stage":
            self._post_stage(parsed)
            return
        if route == "/api/connections":
            self._post_connection(parsed)
            return
        if route != "/api/note":
            self._send(404, json.dumps({"error": "not found"}).encode())
            return
        expected = os.environ.get("DASHBOARD_API_KEY")
        if not expected:
            self._send(503, json.dumps({"error": "not configured"}).encode())
            return
        qs = parse_qs(parsed.query)
        key = (qs.get("key") or [""])[0]
        if key != expected:
            self._send(401, json.dumps({"error": "unauthorized"}).encode())
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > 8192:
            self._send(400, json.dumps({"error": "bad body size"}).encode())
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            self._send(400, json.dumps({"error": "invalid json"}).encode())
            return
        if not isinstance(data, dict):
            self._send(400, json.dumps({"error": "invalid body"}).encode())
            return
        section = str(data.get("section") or "").strip()[:120]
        text = str(data.get("text") or "").strip()
        if not text:
            self._send(400, json.dumps({"error": "empty note"}).encode())
            return
        if len(text) > 2000:
            self._send(400, json.dumps({"error": "note too long"}).encode())
            return
        try:
            note_id = db.add_note(section or "General", text)
        except Exception as e:  # noqa: BLE001
            log.exception("note save failed")
            self._send(500, json.dumps({"error": "internal", "detail": str(e)[:200]}).encode())
            return
        # Drop the warm cache so the notes log reflects the new note on the
        # next dashboard load (the refresher also rebuilds within 2 minutes).
        with _CACHE_LOCK:
            _STATUS_CACHE["payload"] = None
            _STATUS_CACHE["built_at"] = 0.0
        # Operator alert to Brandon through the Sentinel raw Bot API helper
        # (reused, not duplicated). Best effort: the note is saved either way.
        try:
            import sentinel
            preview = text if len(text) <= 700 else text[:700] + "..."
            sentinel.send_ops_alert(
                f"Kas left a dashboard note on {section or 'General'}: {preview}")
        except Exception as e:  # noqa: BLE001
            log.warning("note ops alert failed: %s", e)
        self._send(200, json.dumps({"ok": True, "id": note_id}).encode())
        return

    def log_message(self, fmt, *args):
        log.debug("web_api %s", fmt % args)


def start() -> int:
    """Bind the API server (on the calling thread, so bind errors surface loudly)
    then serve_forever in a daemon thread. Returns the port it bound to."""
    port = int(os.environ.get("PORT", "8080") or "8080")
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, name="web_api", daemon=True)
    t.start()
    # Keep the dashboard payload warm so page loads answer instantly.
    threading.Thread(target=_refresh_cache_loop, name="status_cache", daemon=True).start()
    log.info("Dashboard API on :%s (warm cache refresher armed)", port)
    # Mint the dashboard chat key on boot (no-op when it already exists) so the
    # operator gets the DM immediately after deploy, not on first use.
    threading.Thread(target=_chat_key, name="chat_key_mint", daemon=True).start()
    print(f"[web_api] Dashboard API listening on 0.0.0.0:{port}", flush=True)
    return port


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = start()
    import time
    print(f"serving on {p}; ctrl-c to stop")
    while True:
        time.sleep(3600)
