"""Connector layer for Miles. Read only, env gated.

Each connector switches ON automatically when its credentials exist in the
environment (Railway variables). Wiring a new account is a config job:
set the env vars, redeploy, done. No code changes.

Currently shipped:
  email    : IMAP read only, MULTI ACCOUNT.
             Either the single account vars EMAIL_IMAP_HOST, EMAIL_ADDRESS,
             EMAIL_APP_PASSWORD [EMAIL_IMAP_PORT], or EMAIL_ACCOUNTS, a JSON
             array: [{"label": "main", "host": "imap.gmail.com",
             "address": "kas@maviliving.com", "app_password": "..."}, ...]
             Per the Jul 10 meeting: kas@maviliving.com is the only sending
             identity; every other account is historical context, read only.
  calendar : ICS feeds, read only. CALENDAR_ICS_URLS, a JSON object:
             {"KE": "https://...basic.ics", "Travel": "https://...basic.ics"}
             Covers the shared travel calendar and subscription calendars.
  notion   : Notion REST API (search + read + scoped project writes)
             NOTION_API_KEY  [optional NOTION_PROJECTS_DB_ID for the cold scan]

Adding a connector later (Slack, WhatsApp, Instagram): subclass Connector,
implement configured() / tools() / run(), append to CONNECTORS.

Trust model: email and calendar are READ ONLY. Notion allows one scoped write:
updating a property on a project page (health, next action, dates) per the SOW;
every write is announced to the principal in the reply. Nothing is ever sent,
deleted, or published through a connector. Drafts stay drafts until Kas taps.
"""
from __future__ import annotations

import email as email_lib
import imaplib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from zoneinfo import ZoneInfo

import httpx

LOCAL_TZ = ZoneInfo("Europe/Zurich")


class Connector:
    name = "base"

    def configured(self) -> bool:
        raise NotImplementedError

    def tools(self) -> list[dict]:
        raise NotImplementedError

    def run(self, tool_name: str, args: dict) -> str:
        raise NotImplementedError

    def needs(self) -> str:
        return ""


# ───────────────────── email (IMAP, read only, multi account) ─────────────────────
class EmailConnector(Connector):
    name = "email"

    def needs(self) -> str:
        return "EMAIL_ACCOUNTS (JSON) or EMAIL_IMAP_HOST + EMAIL_ADDRESS + EMAIL_APP_PASSWORD"

    @staticmethod
    def _accounts() -> list[dict]:
        accounts: list[dict] = []
        raw = os.environ.get("EMAIL_ACCOUNTS", "")
        if raw:
            try:
                for a in json.loads(raw):
                    if a.get("address") and a.get("app_password"):
                        a.setdefault("host", "imap.gmail.com")
                        a.setdefault("port", 993)
                        a.setdefault("label", a["address"].split("@")[0])
                        accounts.append(a)
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        if all(os.environ.get(k) for k in ("EMAIL_IMAP_HOST", "EMAIL_ADDRESS", "EMAIL_APP_PASSWORD")):
            addr = os.environ["EMAIL_ADDRESS"]
            if not any(a["address"] == addr for a in accounts):
                accounts.insert(0, {
                    "label": "main",
                    "host": os.environ["EMAIL_IMAP_HOST"],
                    "port": int(os.environ.get("EMAIL_IMAP_PORT", "993")),
                    "address": addr,
                    "app_password": os.environ["EMAIL_APP_PASSWORD"],
                })
        return accounts

    def configured(self) -> bool:
        return bool(self._accounts())

    def tools(self) -> list[dict]:
        labels = [a["label"] for a in self._accounts()]
        acct_desc = f" Available accounts: {', '.join(labels)}. Default is the first (the principal's main inbox)." if labels else ""
        return [
            {
                "name": "email_recent",
                "description": "List recent emails from an inbox (read only). Returns uid, from, subject, date." + acct_desc,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "How many, max 20. Default 10."},
                        "unread_only": {"type": "boolean", "description": "Only unread messages. Default false."},
                        "account": {"type": "string", "description": "Account label. Default: main inbox."},
                    },
                },
            },
            {
                "name": "email_read",
                "description": "Read one email in full (read only) by the uid returned from email_recent. Use the same account label." + acct_desc,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "uid": {"type": "string", "description": "The uid from email_recent."},
                        "account": {"type": "string", "description": "Account label. Default: main inbox."},
                    },
                    "required": ["uid"],
                },
            },
        ]

    def _connect(self, account_label: str | None) -> imaplib.IMAP4_SSL:
        accounts = self._accounts()
        acct = accounts[0]
        if account_label:
            for a in accounts:
                if a["label"].lower() == str(account_label).lower() or a["address"].lower() == str(account_label).lower():
                    acct = a
                    break
        conn = imaplib.IMAP4_SSL(acct["host"], int(acct.get("port", 993)))
        conn.login(acct["address"], acct["app_password"])
        return conn

    @staticmethod
    def _hdr(raw: str | None) -> str:
        if not raw:
            return ""
        parts = decode_header(raw)
        out = ""
        for text, enc in parts:
            out += text.decode(enc or "utf-8", "replace") if isinstance(text, bytes) else text
        return out

    def run(self, tool_name: str, args: dict) -> str:
        conn = self._connect(args.get("account"))
        try:
            conn.select("INBOX", readonly=True)
            if tool_name == "email_recent":
                crit = "UNSEEN" if args.get("unread_only") else "ALL"
                _, data = conn.uid("search", None, crit)
                uids = data[0].split()
                limit = min(int(args.get("limit", 10) or 10), 20)
                out = []
                for uid in reversed(uids[-limit:]):
                    _, msg_data = conn.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                    msg = email_lib.message_from_bytes(msg_data[0][1])
                    out.append(
                        {
                            "uid": uid.decode(),
                            "from": self._hdr(msg.get("From")),
                            "subject": self._hdr(msg.get("Subject")),
                            "date": msg.get("Date", ""),
                        }
                    )
                return json.dumps(out, ensure_ascii=False)
            if tool_name == "email_read":
                uid = str(args.get("uid", "")).encode()
                _, msg_data = conn.uid("fetch", uid, "(BODY.PEEK[])")
                if not msg_data or msg_data[0] is None:
                    return "Error: no message with that uid."
                msg = email_lib.message_from_bytes(msg_data[0][1])
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                            body = part.get_payload(decode=True).decode(
                                part.get_content_charset() or "utf-8", "replace"
                            )
                            break
                else:
                    body = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
                return json.dumps(
                    {
                        "from": self._hdr(msg.get("From")),
                        "subject": self._hdr(msg.get("Subject")),
                        "date": msg.get("Date", ""),
                        "body": body[:4000],
                    },
                    ensure_ascii=False,
                )
            return f"Error: unknown email tool {tool_name}."
        finally:
            try:
                conn.logout()
            except Exception:
                pass


# ───────────────────── calendar (ICS feeds, read only) ─────────────────────
class CalendarConnector(Connector):
    name = "calendar"

    def needs(self) -> str:
        return 'CALENDAR_ICS_URLS (JSON object of {"label": "ics url"})'

    @staticmethod
    def _feeds() -> dict[str, str]:
        raw = os.environ.get("CALENDAR_ICS_URLS", "")
        if not raw:
            return {}
        try:
            feeds = json.loads(raw)
            return {str(k): str(v) for k, v in feeds.items()} if isinstance(feeds, dict) else {}
        except json.JSONDecodeError:
            return {}

    def configured(self) -> bool:
        return bool(self._feeds())

    def tools(self) -> list[dict]:
        labels = ", ".join(self._feeds().keys())
        return [
            {
                "name": "calendar_upcoming",
                "description": (
                    "List upcoming events from the principal's calendars (read only). "
                    f"Feeds: {labels}. Times are Europe/Zurich. Use for travel screening, "
                    "the morning brief, and scheduling checks. Note recurring events may "
                    "only show their next literal date."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "description": "Window in days ahead, max 60. Default 7."},
                        "calendar": {"type": "string", "description": "One feed label to limit to. Default all."},
                    },
                },
            }
        ]

    @staticmethod
    def _parse_dt(value: str, params: str) -> datetime | None:
        value = value.strip()
        try:
            if re.fullmatch(r"\d{8}", value):
                dt = datetime.strptime(value, "%Y%m%d").replace(tzinfo=LOCAL_TZ)
                return dt
            if value.endswith("Z"):
                return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
            m = re.search(r"TZID=([^;:]+)", params or "")
            tz = LOCAL_TZ
            if m:
                try:
                    tz = ZoneInfo(m.group(1))
                except Exception:
                    tz = LOCAL_TZ
            return dt.replace(tzinfo=tz)
        except ValueError:
            return None

    def _parse_ics(self, text: str) -> list[dict]:
        # unfold continuation lines
        text = re.sub(r"\r?\n[ \t]", "", text)
        events = []
        for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
            ev: dict = {}
            for line in block.splitlines():
                if ":" not in line:
                    continue
                key, val = line.split(":", 1)
                name, _, params = key.partition(";")
                name = name.upper()
                if name == "SUMMARY":
                    ev["summary"] = val.strip()
                elif name == "LOCATION" and val.strip():
                    ev["location"] = val.strip()[:120]
                elif name == "DTSTART":
                    ev["start"] = self._parse_dt(val, params)
                    ev["all_day"] = bool(re.fullmatch(r"\d{8}", val.strip()))
                elif name == "DTEND":
                    ev["end"] = self._parse_dt(val, params)
                elif name == "RRULE":
                    ev["recurring"] = True
            if ev.get("start"):
                events.append(ev)
        return events

    def run(self, tool_name: str, args: dict) -> str:
        if tool_name != "calendar_upcoming":
            return f"Error: unknown calendar tool {tool_name}."
        days = min(int(args.get("days", 7) or 7), 60)
        only = args.get("calendar")
        now = datetime.now(LOCAL_TZ)
        horizon = now + timedelta(days=days)
        out = []
        with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (Miles EA)"}) as http:
            for label, url in self._feeds().items():
                if only and label.lower() != str(only).lower():
                    continue
                try:
                    r = http.get(url)
                    r.raise_for_status()
                    for ev in self._parse_ics(r.text):
                        start = ev["start"]
                        if not (now - timedelta(days=1) <= start <= horizon):
                            continue
                        item = {
                            "calendar": label,
                            "summary": ev.get("summary", "(no title)"),
                            "start": start.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M" if not ev.get("all_day") else "%Y-%m-%d (all day)"),
                        }
                        if ev.get("end"):
                            item["end"] = ev["end"].astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
                        if ev.get("location"):
                            item["location"] = ev["location"]
                        if ev.get("recurring"):
                            item["recurring"] = True
                        out.append(item)
                except Exception as e:  # noqa: BLE001
                    out.append({"calendar": label, "error": str(e)[:160]})
        out.sort(key=lambda x: x.get("start", ""))
        return json.dumps(out[:60], ensure_ascii=False)
# ───────────────────────── notion (REST, read only) ─────────────────────────
class NotionConnector(Connector):
    name = "notion"
    _API = "https://api.notion.com/v1"

    def needs(self) -> str:
        return "NOTION_API_KEY"

    def configured(self) -> bool:
        return bool(os.environ.get("NOTION_API_KEY"))

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }

    def tools(self) -> list[dict]:
        return [
            {
                "name": "notion_search",
                "description": "Search the principal's Notion workspace (read only). Returns page titles and ids.",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Search text."}},
                    "required": ["query"],
                },
            },
            {
                "name": "notion_read_page",
                "description": "Read the text content of one Notion page by id (read only).",
                "input_schema": {
                    "type": "object",
                    "properties": {"page_id": {"type": "string", "description": "Page id from notion_search."}},
                    "required": ["page_id"],
                },
            },
            {
                "name": "notion_query_database",
                "description": "List rows of a Notion database with their properties (read only). Use for the principal's project pipeline: titles, status/health fields, dates.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "database_id": {"type": "string", "description": "Database id (from notion_search or configuration)."},
                        "page_size": {"type": "integer", "description": "Rows to return, max 50. Default 25."},
                    },
                    "required": ["database_id"],
                },
            },
            {
                "name": "notion_update_property",
                "description": "Update ONE property on a Notion page (e.g. project health, next action, a date). The only write Miles is allowed. Always tell the principal exactly what you changed.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "string", "description": "The page (database row) to update."},
                        "property": {"type": "string", "description": "Exact property name, e.g. 'Health' or 'Next action'."},
                        "prop_type": {
                            "type": "string",
                            "enum": ["select", "status", "rich_text", "date", "checkbox", "number"],
                            "description": "The property's type in the database schema.",
                        },
                        "value": {"type": "string", "description": "New value. For date use YYYY-MM-DD; for checkbox use true/false."},
                    },
                    "required": ["page_id", "property", "prop_type", "value"],
                },
            },
        ]

    @staticmethod
    def _prop_value(prop: dict):
        """Flatten one Notion property to a plain value for the model."""
        t = prop.get("type", "")
        v = prop.get(t)
        if v is None:
            return None
        if t == "title" or t == "rich_text":
            return "".join(x.get("plain_text", "") for x in v)
        if t in ("select", "status"):
            return (v or {}).get("name")
        if t == "multi_select":
            return [x.get("name") for x in v]
        if t == "date":
            return (v or {}).get("start")
        if t in ("checkbox", "number", "url", "email", "phone_number"):
            return v
        if t == "people":
            return [x.get("name") or x.get("id") for x in v]
        if t == "last_edited_time" or t == "created_time":
            return v
        return str(v)[:120]

    @staticmethod
    def _build_prop_payload(ptype: str, val: str):
        if ptype == "select":
            return {"select": {"name": val}}
        if ptype == "status":
            return {"status": {"name": val}}
        if ptype == "rich_text":
            return {"rich_text": [{"type": "text", "text": {"content": val[:1900]}}]}
        if ptype == "date":
            return {"date": {"start": val}}
        if ptype == "checkbox":
            return {"checkbox": val.strip().lower() in ("true", "1", "yes", "on")}
        if ptype == "number":
            try:
                return {"number": float(val)}
            except ValueError:
                return None
        return None

    @staticmethod
    def _title_of(result: dict) -> str:
        props = result.get("properties", {}) or {}
        for prop in props.values():
            if prop.get("type") == "title":
                return "".join(t.get("plain_text", "") for t in prop.get("title", []))
        for t in result.get("title", []) or []:
            return t.get("plain_text", "")
        return "(untitled)"

    def run(self, tool_name: str, args: dict) -> str:
        with httpx.Client(timeout=20) as http:
            if tool_name == "notion_search":
                r = http.post(
                    f"{self._API}/search",
                    headers=self._headers(),
                    json={"query": args.get("query", ""), "page_size": 10},
                )
                r.raise_for_status()
                out = [
                    {"id": item["id"], "type": item.get("object"), "title": self._title_of(item)}
                    for item in r.json().get("results", [])
                ]
                return json.dumps(out, ensure_ascii=False)
            if tool_name == "notion_query_database":
                dbid = args.get("database_id", "")
                size = min(int(args.get("page_size", 25) or 25), 50)
                r = http.post(
                    f"{self._API}/databases/{dbid}/query",
                    headers=self._headers(),
                    json={"page_size": size},
                )
                r.raise_for_status()
                rows = []
                for pg in r.json().get("results", []):
                    props_out = {}
                    for pname, prop in (pg.get("properties") or {}).items():
                        props_out[pname] = self._prop_value(prop)
                    rows.append({"id": pg["id"], "properties": props_out})
                return json.dumps(rows, ensure_ascii=False)[:8000]
            if tool_name == "notion_update_property":
                pid = args.get("page_id", "")
                pname = args.get("property", "")
                ptype = args.get("prop_type", "")
                val = str(args.get("value", ""))
                payload = self._build_prop_payload(ptype, val)
                if payload is None:
                    return f"Error: unsupported prop_type {ptype}."
                r = http.patch(
                    f"{self._API}/pages/{pid}",
                    headers=self._headers(),
                    json={"properties": {pname: payload}},
                )
                if r.status_code >= 400:
                    return f"Error from Notion: {r.text[:400]}"
                return f"Updated '{pname}' to '{val}'. Tell the principal this was changed."
            if tool_name == "notion_read_page":
                pid = args.get("page_id", "")
                r = http.get(f"{self._API}/blocks/{pid}/children?page_size=100", headers=self._headers())
                r.raise_for_status()
                lines = []
                for b in r.json().get("results", []):
                    btype = b.get("type", "")
                    rich = (b.get(btype) or {}).get("rich_text", [])
                    text = "".join(t.get("plain_text", "") for t in rich)
                    if text:
                        lines.append(text)
                return "\n".join(lines)[:6000] or "(page has no readable text blocks)"
            return f"Error: unknown notion tool {tool_name}."


CONNECTORS: list[Connector] = [EmailConnector(), CalendarConnector(), NotionConnector()]


def active() -> list[Connector]:
    return [c for c in CONNECTORS if c.configured()]


def active_tools() -> list[dict]:
    tools: list[dict] = []
    for c in active():
        tools.extend(c.tools())
    return tools


def dispatch(tool_name: str, args: dict) -> str:
    for c in active():
        if any(t["name"] == tool_name for t in c.tools()):
            try:
                return c.run(tool_name, args or {})
            except Exception as e:  # noqa: BLE001
                return f"Error using {c.name}: {e}"
    return f"Error: tool {tool_name} is not wired."


def status_lines() -> list[str]:
    lines = []
    for c in CONNECTORS:
        if c.configured():
            extra = ""
            if c.name == "email":
                extra = f" ({len(EmailConnector._accounts())} account(s))"
            if c.name == "calendar":
                extra = f" ({len(CalendarConnector._feeds())} feed(s))"
            lines.append(f"🟢 {c.name}: wired and live (read only){extra}")
        else:
            lines.append(f"⚪ {c.name}: not wired yet (needs {c.needs()})")
    return lines
