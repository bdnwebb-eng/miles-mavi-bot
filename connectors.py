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

  slack    : Slack Web API. SLACK_BOT_TOKEN. Read channels + post messages,
             INTERNAL TEAM ONLY (Jul 10 meeting: Slack is the approved AI
             notification channel; nothing client facing ever goes there).

Adding a connector later (WhatsApp, Instagram): subclass Connector,
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



# ───────────────────── slack (Web API, internal team only) ─────────────────────
class SlackConnector(Connector):
    name = "slack"
    _API = "https://slack.com/api"

    def needs(self) -> str:
        return "SLACK_BOT_TOKEN"

    def configured(self) -> bool:
        return bool(os.environ.get("SLACK_BOT_TOKEN"))

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"}

    def tools(self) -> list[dict]:
        return [
            {
                "name": "slack_channels",
                "description": "List the Slack channels in the team workspace (read only). Returns id, name, is_member.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "slack_read_channel",
                "description": (
                    "Read the recent messages of one Slack channel (read only). Accepts a "
                    "channel id or #name. User ids are resolved to display names."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "Channel id (C…) or #name."},
                        "limit": {"type": "integer", "description": "Messages to return, max 30. Default 15."},
                    },
                    "required": ["channel"],
                },
            },
            {
                "name": "slack_post_message",
                "description": (
                    "Post a message to a Slack channel. INTERNAL TEAM ONLY: Slack is the approved "
                    "AI notification channel (Jul 10 meeting). Never post client facing content, "
                    "client pricing, or anything meant for someone outside the MAVI team. Always "
                    "tell the principal exactly what you posted and where."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "Channel id (C…) or #name."},
                        "text": {"type": "string", "description": "The message text (Slack mrkdwn)."},
                    },
                    "required": ["channel", "text"],
                },
            },
        ]

    def _call(self, http: httpx.Client, method: str, *, get: bool = False, **params) -> dict:
        if get:
            r = http.get(f"{self._API}/{method}", params=params, headers=self._auth())
        else:
            r = http.post(f"{self._API}/{method}", json=params, headers=self._auth())
        r.raise_for_status()
        return r.json()

    def _channels(self, http: httpx.Client) -> list[dict]:
        out: list[dict] = []
        cursor = ""
        while True:
            data = self._call(
                http, "conversations.list", get=True,
                types="public_channel,private_channel", limit=200,
                exclude_archived="true", cursor=cursor,
            )
            if not data.get("ok"):
                raise RuntimeError(f"Slack error: {data.get('error')}")
            out.extend(data.get("channels", []))
            cursor = (data.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                break
        return out

    def _resolve_channel(self, http: httpx.Client, ref: str) -> str:
        ref = (ref or "").strip()
        if re.fullmatch(r"[CGD][A-Z0-9]+", ref):
            return ref
        name = ref.lstrip("#").lower()
        for ch in self._channels(http):
            if (ch.get("name") or "").lower() == name:
                return ch["id"]
        raise RuntimeError(f"no Slack channel named #{name}")

    def _user_map(self, http: httpx.Client) -> dict[str, str]:
        users: dict[str, str] = {}
        cursor = ""
        while True:
            data = self._call(http, "users.list", get=True, limit=200, cursor=cursor)
            if not data.get("ok"):
                return users
            for u in data.get("members", []):
                users[u["id"]] = (
                    (u.get("profile") or {}).get("display_name")
                    or u.get("real_name")
                    or u.get("name", u["id"])
                )
            cursor = (data.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                break
        return users

    def run(self, tool_name: str, args: dict) -> str:
        with httpx.Client(timeout=20) as http:
            if tool_name == "slack_channels":
                out = [
                    {"id": ch["id"], "name": ch.get("name", ""), "is_member": bool(ch.get("is_member"))}
                    for ch in self._channels(http)
                ]
                return json.dumps(out, ensure_ascii=False)[:8000]

            if tool_name == "slack_read_channel":
                cid = self._resolve_channel(http, str(args.get("channel", "")))
                limit = min(int(args.get("limit", 15) or 15), 30)
                data = self._call(http, "conversations.history", get=True, channel=cid, limit=limit)
                if not data.get("ok") and data.get("error") == "not_in_channel":
                    # Auto join public channels, then retry once.
                    joined = self._call(http, "conversations.join", channel=cid)
                    if joined.get("ok"):
                        data = self._call(http, "conversations.history", get=True, channel=cid, limit=limit)
                if not data.get("ok"):
                    return f"Error from Slack: {data.get('error')}"
                names = self._user_map(http)
                out = []
                for m in data.get("messages", []):
                    try:
                        when = datetime.fromtimestamp(float(m.get("ts", "0")), LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
                    except (ValueError, OSError, OverflowError):
                        when = m.get("ts", "")
                    text = re.sub(
                        r"<@([A-Z0-9]+)>",
                        lambda x: "@" + names.get(x.group(1), x.group(1)),
                        m.get("text", ""),
                    )
                    out.append(
                        {
                            "when": when,
                            "from": names.get(m.get("user", ""), m.get("username") or m.get("bot_id") or "bot"),
                            "text": text[:600],
                        }
                    )
                return json.dumps(out, ensure_ascii=False)[:8000]

            if tool_name == "slack_post_message":
                cid = self._resolve_channel(http, str(args.get("channel", "")))
                text = str(args.get("text", "")).strip()
                if not text:
                    return "Error: nothing to post."
                data = self._call(http, "chat.postMessage", channel=cid, text=text)
                if not data.get("ok") and data.get("error") == "not_in_channel":
                    joined = self._call(http, "conversations.join", channel=cid)
                    if joined.get("ok"):
                        data = self._call(http, "chat.postMessage", channel=cid, text=text)
                if not data.get("ok"):
                    return f"Error from Slack: {data.get('error')}"
                return (
                    f"Posted to <#{cid}>. Remember: internal team only, and tell the "
                    "principal exactly what was posted and where."
                )

            return f"Error: unknown slack tool {tool_name}."


# ───────────────── google (Gmail + Calendar + Drive, OAuth 2.0) ─────────────────
class GoogleConnector(Connector):
    """Live Google via OAuth 2.0. Miles mints and stores his own refresh token
    (see /connectgoogle): no secret ever passes through a human. Gmail is DRAFT
    ONLY (Miles drafts, Kas sends). Calendar is read + write (writes only when Kas
    has approved). Drive is read only (the audit). The refresh token lives in the
    sqlite google_auth table on the Railway volume, so it survives redeploys."""

    name = "google"
    _TOKEN_URL = "https://oauth2.googleapis.com/token"
    _AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    _GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
    _CAL = "https://www.googleapis.com/calendar/v3"
    _DRIVE = "https://www.googleapis.com/drive/v3"
    # Desktop OAuth client: Google has retired the OOB (urn:ietf:wg:oauth:2.0:oob)
    # redirect for newly created clients, so we use the loopback redirect and have Kas
    # copy the "code" param out of the localhost URL after she approves. No local
    # server is needed: the browser just fails to load localhost and the code sits in
    # the address bar.
    REDIRECT_URI = "http://localhost"
    SCOPES = [
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/drive.readonly",
    ]

    def __init__(self) -> None:
        self._cached_token: str | None = None
        self._cached_expiry: float = 0.0

    def needs(self) -> str:
        return "GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET, then /connectgoogle to authorize"

    @staticmethod
    def _owner_ids() -> list[int]:
        try:
            import config_loader as cfg
            return [int(x) for x in (cfg.settings().get("access", {}).get("allowed_ids") or [])]
        except Exception:  # noqa: BLE001
            return []

    @classmethod
    def _stored_refresh_token(cls) -> str | None:
        import database as db
        for tid in cls._owner_ids():
            rt = db.get_google_refresh_token(tid)
            if rt:
                return rt
        return None

    @staticmethod
    def _has_app() -> bool:
        return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))

    def configured(self) -> bool:
        return bool(self._has_app() and self._stored_refresh_token())

    # ---- OAuth: auth url + code exchange (used by /connectgoogle in handlers) ----
    @classmethod
    def auth_url(cls) -> str:
        from urllib.parse import urlencode
        params = {
            "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
            "redirect_uri": cls.REDIRECT_URI,
            "response_type": "code",
            "access_type": "offline",
            "prompt": "consent",
            "scope": " ".join(cls.SCOPES),
        }
        return f"{cls._AUTH_URL}?{urlencode(params)}"

    @classmethod
    def exchange_code(cls, tid: int, raw: str) -> tuple[bool, str]:
        """Exchange an authorization code for a refresh token and store it. Accepts a
        bare code OR a pasted http://localhost/?code=... URL. Never logs the code."""
        from urllib.parse import urlparse, parse_qs, unquote
        code = (raw or "").strip()
        if "code=" in code:
            try:
                code = (parse_qs(urlparse(code).query).get("code") or [""])[0]
            except Exception:  # noqa: BLE001
                pass
        code = unquote(code).strip()
        if not code:
            return False, "I didn't catch a code there. Paste just the code, or run /connectgoogle again."
        import database as db
        try:
            with httpx.Client(timeout=30) as http:
                r = http.post(cls._TOKEN_URL, data={
                    "code": code,
                    "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
                    "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
                    "redirect_uri": cls.REDIRECT_URI,
                    "grant_type": "authorization_code",
                })
            data = r.json()
            if r.status_code >= 400 or not data.get("refresh_token"):
                err = data.get("error_description") or data.get("error") or "no refresh token returned"
                return False, (f"That didn't take ({err}). The code may have expired or already "
                               "been used. Run /connectgoogle and try once more.")
            db.set_google_auth(tid, data["refresh_token"], " ".join(cls.SCOPES))
            return True, "connected"
        except Exception as e:  # noqa: BLE001
            return False, f"Hit a snag exchanging the code. Run /connectgoogle and try again. ({str(e)[:120]})"

    # ---- access token (refresh_token -> access_token, cached in memory) ----
    def _access_token(self) -> str:
        import time as _time
        now = _time.time()
        if self._cached_token and self._cached_expiry > now + 60:
            return self._cached_token
        rt = self._stored_refresh_token()
        if not rt:
            raise RuntimeError("Google is not connected. Run /connectgoogle.")
        with httpx.Client(timeout=30) as http:
            r = http.post(self._TOKEN_URL, data={
                "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
                "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
                "refresh_token": rt,
                "grant_type": "refresh_token",
            })
        data = r.json()
        if r.status_code >= 400 or not data.get("access_token"):
            raise RuntimeError("Google auth expired or was revoked. Run /connectgoogle to reconnect.")
        self._cached_token = data["access_token"]
        self._cached_expiry = now + int(data.get("expires_in", 3600))
        return self._cached_token

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token()}"}

    # ---- gmail body helpers ----
    @staticmethod
    def _b64url_decode(data: str) -> str:
        import base64
        if not data:
            return ""
        pad = "=" * (-len(data) % 4)
        try:
            return base64.urlsafe_b64decode(data + pad).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""

    @classmethod
    def _find_part(cls, payload: dict, want: str) -> str | None:
        if not payload:
            return None
        if payload.get("mimeType") == want and (payload.get("body") or {}).get("data"):
            return payload["body"]["data"]
        for part in payload.get("parts", []) or []:
            found = cls._find_part(part, want)
            if found:
                return found
        return None

    @classmethod
    def _message_body(cls, payload: dict) -> str:
        data = cls._find_part(payload, "text/plain")
        if data:
            return cls._b64url_decode(data)
        data = cls._find_part(payload, "text/html")
        if data:
            return re.sub(r"<[^>]+>", " ", cls._b64url_decode(data))
        return ""

    def tools(self) -> list[dict]:
        return [
            {
                "name": "gmail_recent",
                "description": "List recent Gmail messages from Kas's inbox (read only). Returns id, from, subject, date, snippet. Use 'query' for Gmail search syntax (e.g. from:someone, newer_than:2d).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "How many, max 20. Default 10."},
                        "unread_only": {"type": "boolean", "description": "Only unread. Default false."},
                        "query": {"type": "string", "description": "Optional Gmail search query."},
                    },
                },
            },
            {
                "name": "gmail_read",
                "description": "Read one Gmail message in full (read only) by the id from gmail_recent. Returns from/to/subject/date/thread_id/body (plain text).",
                "input_schema": {
                    "type": "object",
                    "properties": {"id": {"type": "string", "description": "Message id from gmail_recent."}},
                    "required": ["id"],
                },
            },
            {
                "name": "gmail_draft",
                "description": "Create a DRAFT email in Kas's Gmail for her to review and send. This NEVER sends: it only saves a draft. Pass thread_id to draft a reply in an existing thread.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient email address."},
                        "subject": {"type": "string", "description": "Subject line."},
                        "body": {"type": "string", "description": "Plain text body, in Kas's voice / UHNW register."},
                        "thread_id": {"type": "string", "description": "Optional Gmail thread id to reply within."},
                    },
                    "required": ["to", "body"],
                },
            },
            {
                "name": "calendar_upcoming_v2",
                "description": "List upcoming events from Kas's live Google Calendar (read). Times are Europe/Zurich. Returns summary/start/end/location/attendee count. This is the live API (complements the ICS calendar feeds).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "description": "Window ahead in days, max 60. Default 7."},
                        "calendar_id": {"type": "string", "description": "Calendar id. Default 'primary'."},
                    },
                },
            },
            {
                "name": "calendar_create_event",
                "description": "Create an event on Kas's Google Calendar. Use ONLY when Kas has approved placing it. Always tell her exactly what you booked. Times are Europe/Zurich; pass RFC3339 datetimes like 2026-07-20T14:00:00.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "Event title."},
                        "start_iso": {"type": "string", "description": "Start, RFC3339 e.g. 2026-07-20T14:00:00."},
                        "end_iso": {"type": "string", "description": "End, RFC3339 e.g. 2026-07-20T15:00:00."},
                        "description": {"type": "string", "description": "Optional event notes."},
                        "calendar_id": {"type": "string", "description": "Calendar id. Default 'primary'."},
                    },
                    "required": ["summary", "start_iso", "end_iso"],
                },
            },
            {
                "name": "drive_search",
                "description": "Search Kas's Google Drive by file name (read only). Returns name/id/mimeType/modifiedTime/webViewLink. For the Drive audit.",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Text to match in file names."}},
                    "required": ["query"],
                },
            },
        ]

    def run(self, tool_name: str, args: dict) -> str:
        from urllib.parse import quote
        with httpx.Client(timeout=30) as http:
            h = self._auth_headers()

            if tool_name == "gmail_recent":
                limit = min(int(args.get("limit", 10) or 10), 20)
                params: dict = {"maxResults": limit}
                q = str(args.get("query", "") or "").strip()
                if args.get("unread_only"):
                    q = (q + " is:unread").strip()
                if q:
                    params["q"] = q
                r = http.get(f"{self._GMAIL}/messages", headers=h, params=params)
                if r.status_code >= 400:
                    return f"Error from Gmail: {r.text[:300]}"
                out = []
                for m in (r.json().get("messages") or []):
                    mid = m["id"]
                    mr = http.get(
                        f"{self._GMAIL}/messages/{mid}", headers=h,
                        params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
                    )
                    if mr.status_code >= 400:
                        continue
                    mj = mr.json()
                    hdrs = {x["name"].lower(): x["value"] for x in (mj.get("payload", {}).get("headers") or [])}
                    out.append({
                        "id": mid,
                        "from": hdrs.get("from", ""),
                        "subject": hdrs.get("subject", ""),
                        "date": hdrs.get("date", ""),
                        "snippet": (mj.get("snippet") or "")[:200],
                    })
                return json.dumps(out, ensure_ascii=False)

            if tool_name == "gmail_read":
                mid = str(args.get("id", "")).strip()
                if not mid:
                    return "Error: need a message id from gmail_recent."
                r = http.get(f"{self._GMAIL}/messages/{mid}", headers=h, params={"format": "full"})
                if r.status_code >= 400:
                    return f"Error from Gmail: {r.text[:300]}"
                mj = r.json()
                payload = mj.get("payload", {}) or {}
                hdrs = {x["name"].lower(): x["value"] for x in (payload.get("headers") or [])}
                return json.dumps({
                    "id": mid,
                    "thread_id": mj.get("threadId"),
                    "from": hdrs.get("from", ""),
                    "to": hdrs.get("to", ""),
                    "subject": hdrs.get("subject", ""),
                    "date": hdrs.get("date", ""),
                    "body": self._message_body(payload)[:4000],
                }, ensure_ascii=False)

            if tool_name == "gmail_draft":
                import base64
                to = str(args.get("to", "")).strip()
                subject = str(args.get("subject", "")).strip()
                body = str(args.get("body", "")).strip()
                thread_id = str(args.get("thread_id", "") or "").strip()
                if not to or not body:
                    return "Error: need at least 'to' and 'body' to draft."
                lines = [f"To: {to}", "MIME-Version: 1.0", 'Content-Type: text/plain; charset="UTF-8"']
                if subject:
                    lines.insert(1, f"Subject: {subject}")
                mime = ("\r\n".join(lines) + "\r\n\r\n" + body).encode("utf-8")
                raw = base64.urlsafe_b64encode(mime).decode("ascii")
                msg: dict = {"raw": raw}
                if thread_id:
                    msg["threadId"] = thread_id
                r = http.post(f"{self._GMAIL}/drafts", headers=h, json={"message": msg})
                if r.status_code >= 400:
                    return f"Error creating draft: {r.text[:300]}"
                did = r.json().get("id", "")
                return (f"Draft saved to Kas's Gmail Drafts (draft id {did}). It is NOT sent. "
                        "Tell Kas it's ready for her to review and send.")

            if tool_name == "calendar_upcoming_v2":
                days = min(int(args.get("days", 7) or 7), 60)
                cal_id = quote(str(args.get("calendar_id", "primary") or "primary"), safe="@")
                now = datetime.now(LOCAL_TZ)
                r = http.get(f"{self._CAL}/calendars/{cal_id}/events", headers=h, params={
                    "timeMin": now.isoformat(),
                    "timeMax": (now + timedelta(days=days)).isoformat(),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": 50,
                })
                if r.status_code >= 400:
                    return f"Error from Calendar: {r.text[:300]}"
                out = []
                for ev in r.json().get("items", []):
                    start = ev.get("start", {}) or {}
                    end = ev.get("end", {}) or {}
                    out.append({
                        "id": ev.get("id"),
                        "summary": ev.get("summary", "(no title)"),
                        "start": start.get("dateTime") or start.get("date"),
                        "end": end.get("dateTime") or end.get("date"),
                        "location": ev.get("location", ""),
                        "attendees": len(ev.get("attendees", []) or []),
                    })
                return json.dumps(out, ensure_ascii=False)[:8000]

            if tool_name == "calendar_create_event":
                cal_id = quote(str(args.get("calendar_id", "primary") or "primary"), safe="@")
                summary = str(args.get("summary", "")).strip()
                start_iso = str(args.get("start_iso", "")).strip()
                end_iso = str(args.get("end_iso", "")).strip()
                if not (summary and start_iso and end_iso):
                    return "Error: need summary, start_iso and end_iso (RFC3339, e.g. 2026-07-20T14:00:00)."
                body_payload: dict = {
                    "summary": summary,
                    "start": {"dateTime": start_iso, "timeZone": "Europe/Zurich"},
                    "end": {"dateTime": end_iso, "timeZone": "Europe/Zurich"},
                }
                desc = str(args.get("description", "") or "").strip()
                if desc:
                    body_payload["description"] = desc
                r = http.post(f"{self._CAL}/calendars/{cal_id}/events", headers=h, json=body_payload)
                if r.status_code >= 400:
                    return f"Error creating event: {r.text[:300]}"
                ev = r.json()
                return (f"Booked '{summary}', {start_iso} to {end_iso} (Europe/Zurich). "
                        f"Event id {ev.get('id','')}. Tell Kas exactly what you placed on her calendar.")

            if tool_name == "drive_search":
                q = str(args.get("query", "")).strip()
                if not q:
                    return "Error: need a search query."
                safe_q = q.replace("\\", "\\\\").replace("'", "\\'")
                r = http.get(f"{self._DRIVE}/files", headers=h, params={
                    "q": f"name contains '{safe_q}' and trashed = false",
                    "fields": "files(id,name,mimeType,modifiedTime,webViewLink)",
                    "pageSize": 20,
                    "orderBy": "modifiedTime desc",
                })
                if r.status_code >= 400:
                    return f"Error from Drive: {r.text[:300]}"
                return json.dumps(r.json().get("files", []), ensure_ascii=False)[:6000]

            return f"Error: unknown google tool {tool_name}."


CONNECTORS: list[Connector] = [EmailConnector(), CalendarConnector(), NotionConnector(), SlackConnector(), GoogleConnector()]


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
        if c.name == "google":
            if c.configured():
                lines.append("🟢 google: wired and live (Gmail draft-only, Calendar read+write, Drive read)")
            elif GoogleConnector._has_app():
                lines.append("🟡 google: app ready, run /connectgoogle to connect Kas's account")
            else:
                lines.append(f"⚪ google: not wired yet (needs {c.needs()})")
            continue
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
