"""Offline functional test of the v6.5 slack_rhythm fix. ALL connectors mocked
(directive 8: testing never touches Kas's real accounts). Verifies:
  1. Calendar tomorrow section uses the LIVE v2 connector with explicit day windows.
  2. Real events flow into the context labeled as the only source for tomorrow.
  3. A failed calendar read produces an UNAVAILABLE marker, never silence.
  4. Google unconfigured produces UNAVAILABLE markers for calendar AND inbox.
  5. A verified empty calendar day passes through with complete=true visible.
  6. Gmail data is now in the context (grounds the 'Kas Emails' section).
  7. Notion rows carry [EDITED TODAY] / [last edited ...] tags.
  8. The composer prompt carries the grounding rules and the eod brief demands
     a Calendar Tomorrow section built only from the context.
  9. The legacy ICS calendar tool is no longer offered to the interactive model.
"""
import json
import os
import sys
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.pop("CALENDAR_ICS_URLS", None)

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
         "end": f"{TOMORROW}T09:30:00+02:00", "calendar": "kas@maviliving.com"},
        {"summary": "MAVI design review", "start": f"{TOMORROW}T10:00:00+02:00",
         "end": f"{TOMORROW}T12:00:00+02:00", "calendar": "MAVI Projects"},
    ],
    "count": 2,
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
         "properties": {"Stage": "Deposit received", "Next Action": "Send welcome email"}},
        {"title": "MAVI-2026-LL-001", "last_edited": "2026-07-21T09:00:00.000Z",
         "properties": {"Stage": "Wellbeing report", "Next Action": "Issue final report"}},
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
check("gmail data reaches the context", "Intake docs" in ctx and "KAS INBOX, last 24h" in ctx)
check("edited-today tag present", "[EDITED TODAY]" in ctx)
check("stale row carries its edit date", "[last edited 2026-07-21]" in ctx)
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
    content = [types.SimpleNamespace(type="text", text="*End of day*\nHi Kas ...")]


class FakeClient:
    class messages:  # noqa: N801
        @staticmethod
        def create(**kw):
            captured.update(kw)
            return FakeMsg()


slack_rhythm.ai = types.SimpleNamespace(_get_client=lambda: FakeClient())
out = slack_rhythm._compose("eod", "CTX-SENTINEL-12345")
prompt = captured["messages"][0]["content"]
check("compose returns model text", out.startswith("*End of day*"))
check("grounding rules in prompt", "GROUNDING, absolute" in prompt and "NEVER write 'no emails', 'nothing scheduled'" in prompt)
check("eod brief demands Calendar Tomorrow from context only", "*Calendar Tomorrow:*" in prompt and "built ONLY from the CALENDAR TOMORROW context section" in prompt)
check("edited-today rule in prompt", "tagged EDITED TODAY" in prompt)
check("context reaches prompt", "CTX-SENTINEL-12345" in prompt)
check("post token budget raised", slack_rhythm.MAX_POST_TOKENS >= 600, str(slack_rhythm.MAX_POST_TOKENS))

morn = {}
captured.clear()
slack_rhythm._compose("morning", "CTX2")
check("morning grounds in CALENDAR TODAY", "CALENDAR TODAY" in captured["messages"][0]["content"])

# ---- case 9: the legacy ICS path is fully deleted (v6.6) ----
os.environ["CALENDAR_ICS_URLS"] = json.dumps({"Elite Coaching": "https://example.com/x.ics"})
names = [t["name"] for t in connectors.active_tools()]
check("legacy calendar_upcoming not offered even when ICS env set", "calendar_upcoming" not in names, str(names))
check("registry keeps 4 connectors", len(connectors.CONNECTORS) == 4)
check("ICS CalendarConnector class fully deleted", not hasattr(connectors, "CalendarConnector"))
check("one calendar path only: v2 present", any(t["name"] == "calendar_upcoming_v2" for c in connectors.CONNECTORS for t in (c.tools() if c.name == "google" else [])))
os.environ.pop("CALENDAR_ICS_URLS", None)

# ---- case 10: web_api has no silent calendar fallback (v6.6) ----
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
check("web_api calendar serves live v2 events", any(e.get("summary") == "Investor breakfast" for e in cal_on["events"]))

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
