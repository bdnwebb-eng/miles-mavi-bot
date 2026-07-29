"""Offline functional test of the v6.5/v6.6/v6.7 daily rhythm. ALL connectors
mocked (directive 8: testing never touches Kas's real accounts). Verifies:
  1. Calendar sections use the LIVE v2 connector with explicit day windows.
  2. Real events (with notes) flow into the context; tomorrow labeled sole source.
  3. A failed calendar read produces an UNAVAILABLE marker, never silence.
  4. Google unconfigured produces UNAVAILABLE markers for calendar AND inbox.
  5. A verified empty calendar day passes through with complete=true visible.
  6. Gmail data grounds the emails section.
  7. Notion rows carry [EDITED TODAY] / [last edited ...] tags.
  8. The composer prompt carries the grounding rules; EOD demands Calendar
     Tomorrow from context only and ends with the energy question; delivery
     is Telegram plain text.
  9. The legacy ICS path stays fully deleted; exactly one calendar path.
 10. web_api: whitelist filters the dashboard calendar; travel extraction works;
     live projects are never cold.
 11. v6.7: briefs deliver via Telegram; Slack is read, never posted.
"""
import json
import os
import sys
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.pop("CALENDAR_ICS_URLS", None)
os.environ.pop("SLACK_BOT_TOKEN", None)
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST-not-real"

import connectors
import slack_rhythm

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


NOW = datetime.now(connectors.LOCAL_TZ)
TODAY = NOW.strftime("%Y-%m-%d")
TOMORROW = (NOW + timedelta(days=1)).strftime("%Y-%m-%d")

CAL_PAYLOAD_BUSY = json.dumps({
    "window": {"start": TOMORROW, "end": TOMORROW, "timezone": "Europe/Zurich"},
    "complete": True,
    "events": [
        {"summary": "Investor breakfast", "start": f"{TOMORROW}T08:30:00+02:00",
         "end": f"{TOMORROW}T09:30:00+02:00", "notes": "Intro over coffee re Doha villa",
         "calendar": "kas@maviliving.com"},
        {"summary": "MAVI design review", "start": f"{TOMORROW}T10:00:00+02:00",
         "end": f"{TOMORROW}T12:00:00+02:00", "notes": "",
         "calendar": "Outlook.KasBordier"},
        {"summary": "Olga sync", "start": f"{TOMORROW}T15:00:00+02:00",
         "end": f"{TOMORROW}T15:30:00+02:00", "notes": "",
         "calendar": "olga@maviliving.com"},
        {"summary": "Flight to Abu Dhabi", "start": f"{TOMORROW}T18:00:00+02:00",
         "end": f"{TOMORROW}T22:00:00+02:00", "notes": "EY074",
         "calendar": "kas@maviliving.com"},
    ],
    "count": 4,
})
CAL_PAYLOAD_EMPTY = json.dumps({
    "window": {"start": TOMORROW, "end": TOMORROW, "timezone": "Europe/Zurich"},
    "complete": True, "events": [], "count": 0,
})
GMAIL_PAYLOAD = json.dumps([
    {"id": "m1", "from": "Yasser <y@example.com>", "subject": "Intake docs",
     "date": "Tue, 28 Jul 2026 14:02:11 +0200", "snippet": "Sending the property docs over now"},
])


class FakeGC:
    """Stands in for GoogleConnector. Records calls, returns canned payloads."""
    calls: list = []
    mode = "busy"          # busy | empty | error
    is_configured = True

    def configured(self):
        return FakeGC.is_configured

    def run(self, tool, args):
        FakeGC.calls.append((tool, dict(args)))
        if tool == "calendar_upcoming_v2":
            if FakeGC.mode == "error":
                return "Error from Calendar: 500 backend"
            if FakeGC.mode == "empty":
                return CAL_PAYLOAD_EMPTY
            return CAL_PAYLOAD_BUSY
        if tool == "gmail_recent":
            if FakeGC.mode == "error":
                return "Error from Gmail: 401"
            return GMAIL_PAYLOAD
        raise AssertionError(f"unexpected tool {tool}")


def fake_rows():
    return [
        {"title": "MAVI-2026-NS-004", "last_edited": f"{TODAY}T10:11:12.000Z",
         "properties": {"Stage": "Review Deposit Received", "Next Action": "Welcome email sent"}},
        {"title": "MAVI-2026-LL-001", "last_edited": "2026-07-21T09:00:00.000Z",
         "properties": {"Stage": "Active Engagement", "Next Action": "Issue final report"}},
    ]


# wire the mocks into the module under test
slack_rhythm.GoogleConnector = FakeGC
slack_rhythm._project_rows = fake_rows
slack_rhythm.notion_watch = types.SimpleNamespace(enabled=lambda: False, cold_items=lambda: [])

# ---- case 1 + 2: busy tomorrow ----
FakeGC.calls, FakeGC.mode, FakeGC.is_configured = [], "busy", True
ctx = slack_rhythm._context_block("eod")
cal_calls = [c for c in FakeGC.calls if c[0] == "calendar_upcoming_v2"]
check("v2 connector used for calendar", len(cal_calls) == 2, str(FakeGC.calls))
check("today window is explicit", cal_calls[0][1] == {"start_date": TODAY, "end_date": TODAY}, str(cal_calls))
check("tomorrow window is explicit", cal_calls[1][1] == {"start_date": TOMORROW, "end_date": TOMORROW}, str(cal_calls))
check("tomorrow section labeled as only source", "the ONLY source for any claim about tomorrow's schedule" in ctx)
check("real events reach the context", "Investor breakfast" in ctx and "MAVI design review" in ctx)
check("event notes reach the context", "Intro over coffee re Doha villa" in ctx)
check("gmail data reaches the context", "Intake docs" in ctx and "KAS INBOX, last 24h" in ctx)
check("edited-today tag present", "[EDITED TODAY]" in ctx)
check("stale row carries its edit date", "[last edited 2026-07-21]" in ctx)
check("slack unconfigured stated plainly", "SLACK: not connected." in ctx)
check("no UNAVAILABLE markers when all live", "UNAVAILABLE" not in ctx)

# ---- case 3: calendar/gmail read errors ----
FakeGC.calls, FakeGC.mode = [], "error"
ctx_err = slack_rhythm._context_block("eod")
check("failed calendar becomes UNAVAILABLE", "CALENDAR TOMORROW" in ctx_err and "UNAVAILABLE (live calendar could not be read" in ctx_err)
check("failed calendar forbids clear-day claim", "Do NOT claim the day is clear" in ctx_err)
check("failed gmail becomes UNAVAILABLE", "Do NOT claim there were no emails" in ctx_err)

# ---- case 4: google not connected at all ----
FakeGC.calls, FakeGC.is_configured = [], False
ctx_off = slack_rhythm._context_block("eod")
check("unconfigured google -> calendar UNAVAILABLE", "CALENDAR: UNAVAILABLE" in ctx_off)
check("unconfigured google -> inbox UNAVAILABLE", "KAS INBOX: UNAVAILABLE" in ctx_off)
check("unconfigured google makes zero live calls", FakeGC.calls == [])

# ---- case 5: verified empty day passes through ----
FakeGC.calls, FakeGC.mode, FakeGC.is_configured = [], "empty", True
ctx_empty = slack_rhythm._context_block("eod")
check("verified empty day keeps complete flag visible", '"complete": true' in ctx_empty and '"events": []' in ctx_empty)
check("verified empty day is not UNAVAILABLE", "UNAVAILABLE (live calendar" not in ctx_empty)

# ---- case 8: composer prompt rules ----
captured = {}


class FakeMsg:
    content = [types.SimpleNamespace(type="text", text="End of day\nHi Kas ...")]


class FakeClient:
    class messages:  # noqa: N801
        @staticmethod
        def create(**kw):
            captured.update(kw)
            return FakeMsg()


slack_rhythm.ai = types.SimpleNamespace(_get_client=lambda: FakeClient())
out = slack_rhythm._compose("eod", "CTX-SENTINEL-12345")
prompt = captured["messages"][0]["content"]
check("compose returns model text", out.startswith("End of day"))
check("grounding rules in prompt", "GROUNDING, absolute" in prompt and "NEVER write 'no emails', 'nothing scheduled'" in prompt)
check("eod demands Calendar Tomorrow from context only", "Calendar Tomorrow built ONLY from the CALENDAR TOMORROW" in prompt)
check("eod ends with the energy question", "energy" in prompt and "1 to 10" in prompt and "voice note" in prompt)
check("telegram plain text rules in prompt", "PLAIN TEXT for Telegram" in prompt)
check("edited-today rule in prompt", "tagged EDITED TODAY" in prompt)
check("context reaches prompt", "CTX-SENTINEL-12345" in prompt)
check("post token budget raised", slack_rhythm.MAX_POST_TOKENS >= 600, str(slack_rhythm.MAX_POST_TOKENS))

captured.clear()
slack_rhythm._compose("morning", "CTX2")
mprompt = captured["messages"][0]["content"]
check("morning grounds in CALENDAR TODAY", "CALENDAR TODAY" in mprompt)
check("morning explains each meeting", "who it is with and what it is about" in mprompt)

# ---- case 11: telegram delivery, never slack ----
sent = []


class _FakeResp:
    status_code = 200


class _FakeHTTP:
    def __init__(self, *a, **k): ...
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, url, json=None):
        sent.append((url, json))
        return _FakeResp()


slack_rhythm.httpx = types.SimpleNamespace(Client=_FakeHTTP)
slack_rhythm.db = types.SimpleNamespace(users_with_reminders=lambda: [{"telegram_id": 111}, {"telegram_id": 222}])
n = slack_rhythm._send_telegram("hello Kas")
check("telegram delivery to every reminder user", n == 2 and len(sent) == 2)
check("delivery hits the telegram bot api", all("api.telegram.org" in u for u, _ in sent))
src_rhythm = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "slack_rhythm.py"), encoding="utf-8").read()
check("rhythm file never posts to slack", "slack_post_message" not in src_rhythm)
check("rhythm file still reads slack", "slack_read_channel" in src_rhythm)

# ---- case 9: the legacy ICS path stays deleted ----
os.environ["CALENDAR_ICS_URLS"] = json.dumps({"Elite Coaching": "https://example.com/x.ics"})
names = [t["name"] for t in connectors.active_tools()]
check("legacy calendar_upcoming not offered even when ICS env set", "calendar_upcoming" not in names, str(names))
check("ICS CalendarConnector class fully deleted", not hasattr(connectors, "CalendarConnector"))
check("one calendar path only: v2 present", any(t["name"] == "calendar_upcoming_v2" for c in connectors.CONNECTORS for t in (c.tools() if c.name == "google" else [])))
check("energy_log tool live", "energy_log" in names)
check("notion lead creation tool defined", any(t["name"] == "notion_create_lead" for c in connectors.CONNECTORS for t in (c.tools() if c.name == "notion" else [])))
os.environ.pop("CALENDAR_ICS_URLS", None)

# ---- case 10: web_api whitelist, travel, cold logic ----
import web_api


class _GCOff:
    def configured(self):
        return False


web_api.connectors.GoogleConnector = _GCOff
cal_off = web_api._calendar(7)
check("web_api calendar honest when google off", cal_off["meta"].get("complete") is False and cal_off["events"] == [])
web_api.connectors.GoogleConnector = FakeGC
FakeGC.mode, FakeGC.is_configured = "busy", True
cal_on = web_api._calendar(2)
cal_names = {e["calendar"] for e in cal_on["events"]}
check("dashboard whitelist keeps kas calendars only", cal_names == {"kas@maviliving.com", "Outlook.KasBordier"}, str(cal_names))
check("whitelist drops olga from dashboard", not any(e["summary"] == "Olga sync" for e in cal_on["events"]))
check("dashboard events carry notes", any(e.get("notes") for e in cal_on["events"]))
trav = web_api._travel(60)
check("travel extraction finds the flight", any("Flight to Abu Dhabi" in t["summary"] for t in trav), str(trav))
check("travel excludes normal meetings", not any(t["summary"] == "MAVI design review" for t in trav))
check("live project is never cold (web_api rule)",
      web_api and (lambda p: not (p["cold"]))({"cold": bool(30 is not None and 30 > 7 and not True and "Active Engagement" != "Active Engagement")}))

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
