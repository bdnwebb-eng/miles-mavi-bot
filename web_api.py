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

Health route:
    GET /health -> 200 "ok"
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import httpx

import connectors
import database as db

log = logging.getLogger("hermes.web_api")

LOOP_SCHEDULE = {
    "slack_agenda": "daily 07:40 #agenda",
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
            out.append({
                "client": flat.get("Client Name") or nc._title_of(pg),
                "code": flat.get("Project Code") or "",
                "stage": flat.get("Stage") or "",
                "tier": flat.get("Lead Tier") or "",
                "urgency": flat.get("Urgency") or "",
                "location": flat.get("Property Location") or "",
                "property_type": flat.get("Property Type") or "",
                "next_action": flat.get("Next Action") or "",
                "live": live,
                "days_stale": days_stale,
                "cold": bool(days_stale is not None and days_stale > 7),
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


def _calendar(days: int = 7) -> dict:
    """Upcoming events. Google (live) preferred, ICS feeds as fallback.
    Titles and times only. Returns {"events": [...], "meta": {window, complete, count}}."""
    gc = connectors.GoogleConnector()
    if gc.configured():
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
        events = [
            {"summary": e.get("summary", ""), "start": e.get("start", ""),
             "end": e.get("end", ""), "location": e.get("location", ""),
             "calendar": e.get("calendar", "")}
            for e in items
        ]
        return {"events": events, "meta": meta}
    cc = connectors.CalendarConnector()
    if cc.configured():
        raw = cc.run("calendar_upcoming", {"days": days})
        items = json.loads(raw) if raw.strip().startswith("[") else []
        events = [
            {"summary": e.get("summary", ""), "start": e.get("start", ""),
             "end": e.get("end", ""), "location": e.get("location", "")}
            for e in items if "error" not in e
        ]
        return {"events": events,
                "meta": {"window": f"next {days} days (ICS fallback)",
                         "complete": True, "count": len(events)}}
    return {"events": [], "meta": {"window": None, "complete": True, "count": 0}}


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
    """Connector -> status string. 'live' / 'partial' / 'pending'."""
    has_google_token = bool(db.get_google_token())
    ics_configured = connectors.CalendarConnector().configured()
    email_imap = connectors.EmailConnector().configured()
    if has_google_token:
        cal = "live"
    elif ics_configured:
        cal = "partial"
    else:
        cal = "pending"
    return {
        "notion": "live" if connectors.NotionConnector().configured() else "pending",
        "slack": "live" if connectors.SlackConnector().configured() else "pending",
        "whatsapp": "live",
        "calendar": cal,
        "email": "live" if (has_google_token or email_imap) else "pending",
        "google": "live" if has_google_token else "pending",
        "instagram": "pending",
    }


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

    kpis = {
        "projects_total": len(projects),
        "live_now": live_now,
        "now_tier": now_tier,
        "cold_count": cold_count,
        "podcasts_booked": 1,
        "senses_live": senses_live,
    }

    return {
        "generated_at": now_zurich.isoformat(),
        "kpis": kpis,
        "projects": projects,
        "calendar": calendar,
        "calendar_meta": calendar_meta,
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
            self._send(200, b"ok", "text/plain")
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
                payload = build_payload(days)
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
    log.info("Dashboard API on :%s", port)
    print(f"[web_api] Dashboard API listening on 0.0.0.0:{port}", flush=True)
    return port


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = start()
    import time
    print(f"serving on {p}; ctrl-c to stop")
    while True:
        time.sleep(3600)
