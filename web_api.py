"""Read-only live JSON API for the Miles command dashboard.

Runs a tiny stdlib http.server in a DAEMON THREAD alongside the python-telegram-bot
polling loop. No new pip dependencies, no entanglement with the PTB asyncio loop:
the connector layer uses synchronous httpx, which is perfectly happy in a thread.

Single data route:
    GET /api/status?key=<DASHBOARD_API_KEY>
    (GET /api/dashboard is kept as a backward-compatible alias.)

Everything is READ ONLY and CLIENT-SAFE. The payload carries aggregates plus
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
    "whatsapp_digest": "daily 06:45",
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


def _senses() -> dict:
    """Connector -> status string. 'live' / 'pending'. v6.6: calendar truth comes
    only from the Google token (the ICS 'partial' state is gone with the ICS path)."""
    has_google_token = bool(db.get_google_token())
    email_imap = connectors.EmailConnector().configured()
    return {
        "notion": "live" if connectors.NotionConnector().configured() else "pending",
        "slack": "live" if connectors.SlackConnector().configured() else "pending",
        "whatsapp": "live",
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
    travel = _safe("travel", _travel, [])
    cal = _safe("calendar", lambda: _calendar(days), {"events": [], "meta": {}})
    if not isinstance(cal, dict):
        cal = {"events": cal or [], "meta": {}}
    calendar = cal.get("events", [])
    calendar_meta = cal.get("meta", {})
    inbox_unread = _safe("inbox_unread", _inbox, None)
    energy_days = _safe("energy", _principal_energy, [])
    senses = _safe("senses", _senses, {})

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
    }

    return {
        "generated_at": now_zurich.isoformat(),
        "kpis": kpis,
        "projects": projects,
        "calendar": calendar,
        "calendar_meta": calendar_meta,
        "travel": travel,
        "inbox_unread": inbox_unread,
        "energy": energy,
        "senses": senses,
        "loops": dict(LOOP_SCHEDULE),
        "errors": errors,
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
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
    print(f"[web_api] Dashboard API listening on 0.0.0.0:{port}", flush=True)
    return port


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = start()
    import time
    print(f"serving on {p}; ctrl-c to stop")
    while True:
        time.sleep(3600)
