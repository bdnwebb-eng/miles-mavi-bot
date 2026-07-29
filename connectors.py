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
                        "page_size": {"type": "integer", "description": "Rows to return, max 30. Default 25."},
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
                size = min(int(args.get("page_size", 25) or 25), 30)
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
                return json.dumps(rows, ensure_ascii=False)
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
                return json.dumps(out[:200], ensure_ascii=False)

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
                return json.dumps(out, ensure_ascii=False)

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
    has approved). Drive, Docs, Sheets and Slides are FULL create + edit: Miles
    can create folders, move/rename files, create and edit documents, spreadsheets
    and presentations inside Kas's Google. He always announces what he created or
    changed and shares the link. Drive deletes are trash only (recoverable), never
    permanent. The refresh token lives in the sqlite google_auth table on the
    Railway volume, so it survives redeploys."""

    name = "google"
    _TOKEN_URL = "https://oauth2.googleapis.com/token"
    _AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    _GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
    _CAL = "https://www.googleapis.com/calendar/v3"
    _DRIVE = "https://www.googleapis.com/drive/v3"
    _DOCS = "https://docs.googleapis.com/v1/documents"
    _SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
    _SLIDES = "https://slides.googleapis.com/v1/presentations"
    # Desktop OAuth client: Google has retired the OOB (urn:ietf:wg:oauth:2.0:oob)
    # redirect for newly created clients, so we use the loopback redirect and have Kas
    # copy the "code" param out of the localhost URL after she approves. No local
    # server is needed: the browser just fails to load localhost and the code sits in
    # the address bar.
    REDIRECT_URI = "http://localhost"
    SCOPES = [
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/presentations",
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
        # Single-tenant: one shared Google identity for the whole bot. No per-user
        # scan — every allowed user transparently shares the same token.
        import database as db
        return db.get_google_token()

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
            # Single shared Google identity: store under the sentinel row so every
            # allowed user (Kas, Brandon) uses this same connection. tid is ignored.
            db.set_google_token(data["refresh_token"], " ".join(cls.SCOPES))
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

    @staticmethod
    def _docs_plain_text(doc: dict) -> str:
        """Flatten a Google Docs document resource body to plain text."""
        out: list[str] = []
        for el in ((doc.get("body") or {}).get("content") or []):
            para = el.get("paragraph")
            if not para:
                continue
            line = ""
            for pe in para.get("elements", []) or []:
                tr = pe.get("textRun") or {}
                line += tr.get("content", "")
            out.append(line)
        return "".join(out)

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
                "description": (
                    "List events from Kas's live Google Calendar (read), merged across ALL of her "
                    "calendars, not just primary. Times are Europe/Zurich. Default window is the next "
                    "30 days; you may request up to 800 days, roughly two years ahead, so Kas can see "
                    "far future events (a summit next year is always reachable). Use "
                    "start_date and end_date for a specific week or month (they override days). Use q "
                    "to find a person or meeting by name. The payload always states the exact window "
                    "covered and a complete flag; if complete is false, tell Kas exactly what you "
                    "could not see. Returns summary/start/end/location/attendee count/calendar name."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "description": "Window ahead in days, up to 800 (roughly two years ahead). Default 30."},
                        "start_date": {"type": "string", "description": "Optional window start, YYYY-MM-DD, Europe/Zurich. Overrides days."},
                        "end_date": {"type": "string", "description": "Optional window end, YYYY-MM-DD inclusive, Europe/Zurich. Overrides days."},
                        "q": {"type": "string", "description": "Optional free text search, e.g. a person or meeting name. Passed to Google's event search."},
                        "calendar_id": {"type": "string", "description": "Optional single calendar id to limit to. Default reads every calendar Kas can see."},
                    },
                },
            },
            {
                "name": "calendar_create_event",
                "description": (
                    "Create an event on Kas's Google Calendar. Use ONLY when Kas has approved placing it. "
                    "The tool verifies the write by reading the event straight back from Google and only "
                    "then reports success, returning the event id, the htmlLink and the calendar it "
                    "landed on (default primary, Kas's main kas@maviliving calendar). ALWAYS give Kas "
                    "the htmlLink as proof and name the calendar it went on. If this tool returns an "
                    "error the event was NOT created: never tell Kas it is booked. Default timezone "
                    "is Europe/Zurich; pass RFC3339 datetimes like 2026-07-20T14:00:00. For events "
                    "happening in another city, pass that city's timezone (e.g. America/Chicago for "
                    "Dallas) with the local wall time; Google shows it correctly in Kas's calendar "
                    "automatically. When confirming to Kas, state BOTH the event's local time and "
                    "the Geneva time."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "Event title."},
                        "start_iso": {"type": "string", "description": "Start, RFC3339 e.g. 2026-07-20T14:00:00."},
                        "end_iso": {"type": "string", "description": "End, RFC3339 e.g. 2026-07-20T15:00:00."},
                        "description": {"type": "string", "description": "Optional event notes."},
                        "timezone": {"type": "string", "description": "IANA timezone the start_iso and end_iso wall times are in, e.g. Europe/Zurich or America/Chicago. Default Europe/Zurich. Use the venue city's timezone for events happening elsewhere."},
                        "calendar_id": {"type": "string", "description": "Calendar id. Default 'primary'."},
                    },
                    "required": ["summary", "start_iso", "end_iso"],
                },
            },
            {
                "name": "calendar_delete_event",
                "description": (
                    "Delete ONE event from Kas's Google Calendar by event id. CONFIRM FIRST: only "
                    "delete when Kas has explicitly approved removing that exact event, and always "
                    "tell her exactly what you removed and from which calendar. Use this to clean up "
                    "your own booking mistakes once Kas confirms."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "The event id from calendar_upcoming_v2 or calendar_create_event."},
                        "calendar_id": {"type": "string", "description": "Calendar id the event lives on. Default 'primary'."},
                    },
                    "required": ["event_id"],
                },
            },
            {
                "name": "drive_search",
                "description": "Search Kas's Google Drive by file name (excludes trashed files). Returns name/id/mimeType/modifiedTime/webViewLink.",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Text to match in file names."}},
                    "required": ["query"],
                },
            },
            {
                "name": "drive_create_folder",
                "description": "Create a folder in Kas's Google Drive. Miles is acting inside Kas's Google: announce the folder you made and give her the link.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Folder name."},
                        "parent_id": {"type": "string", "description": "Optional parent folder id. Default: My Drive root."},
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "drive_move",
                "description": "Move a file or folder in Kas's Drive to a new parent folder. Announce what you moved and where.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string", "description": "The file or folder id to move."},
                        "new_parent_id": {"type": "string", "description": "The destination folder id."},
                    },
                    "required": ["file_id", "new_parent_id"],
                },
            },
            {
                "name": "drive_rename",
                "description": "Rename a file or folder in Kas's Drive. Announce the new name.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string", "description": "The file or folder id."},
                        "new_name": {"type": "string", "description": "The new name."},
                    },
                    "required": ["file_id", "new_name"],
                },
            },
            {
                "name": "drive_trash",
                "description": "Move a file or folder in Kas's Drive to the trash. This is a SOFT delete: it is recoverable from Trash for 30 days and is NEVER a permanent delete. Always tell Kas you moved it to trash and that she can restore it.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string", "description": "The file or folder id to move to trash."},
                    },
                    "required": ["file_id"],
                },
            },
            {
                "name": "docs_create",
                "description": "Create a new Google Doc in Kas's Drive, optionally with starting body text. Miles is acting inside Kas's Google: announce the doc you created and share its link.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Document title."},
                        "body_text": {"type": "string", "description": "Optional starting body text."},
                    },
                    "required": ["title"],
                },
            },
            {
                "name": "docs_read",
                "description": "Read the full plain text of one Google Doc by id.",
                "input_schema": {
                    "type": "object",
                    "properties": {"document_id": {"type": "string", "description": "The Google Doc id."}},
                    "required": ["document_id"],
                },
            },
            {
                "name": "docs_append",
                "description": "Append text to the end of an existing Google Doc in Kas's Drive. Announce what you added and share the link.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string", "description": "The Google Doc id."},
                        "text": {"type": "string", "description": "Text to append at the end of the doc."},
                    },
                    "required": ["document_id", "text"],
                },
            },
            {
                "name": "docs_replace",
                "description": "Find and replace all occurrences of text in a Google Doc in Kas's Drive. Announce what you changed and share the link.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string", "description": "The Google Doc id."},
                        "find": {"type": "string", "description": "Exact text to find."},
                        "replace": {"type": "string", "description": "Text to replace it with."},
                    },
                    "required": ["document_id", "find", "replace"],
                },
            },
            {
                "name": "sheets_create",
                "description": "Create a new Google Sheet in Kas's Drive. Miles is acting inside Kas's Google: announce the sheet you created and share its link.",
                "input_schema": {
                    "type": "object",
                    "properties": {"title": {"type": "string", "description": "Spreadsheet title."}},
                    "required": ["title"],
                },
            },
            {
                "name": "sheets_read",
                "description": "Read a cell range from a Google Sheet. Returns rows of values.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "spreadsheet_id": {"type": "string", "description": "The spreadsheet id."},
                        "range": {"type": "string", "description": "A1 range, e.g. 'A1:Z100' or 'Sheet1!A1:D20'. Default 'A1:Z100'."},
                    },
                    "required": ["spreadsheet_id"],
                },
            },
            {
                "name": "sheets_write",
                "description": "Write a 2D array of values to a range in a Google Sheet (overwrites that range). Announce what you wrote and share the link.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "spreadsheet_id": {"type": "string", "description": "The spreadsheet id."},
                        "range": {"type": "string", "description": "A1 range to write to, e.g. 'A1' or 'Sheet1!A1'."},
                        "values_json": {"type": "string", "description": "JSON string of rows, a 2D array e.g. [[\"Name\",\"Amount\"],[\"Rug\",1200]]."},
                    },
                    "required": ["spreadsheet_id", "range", "values_json"],
                },
            },
            {
                "name": "sheets_append",
                "description": "Append rows of values to a Google Sheet after the last row of a range. Announce what you added and share the link.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "spreadsheet_id": {"type": "string", "description": "The spreadsheet id."},
                        "range": {"type": "string", "description": "A1 range that anchors the table, e.g. 'A1' or 'Sheet1!A1'."},
                        "values_json": {"type": "string", "description": "JSON string of rows to append, a 2D array."},
                    },
                    "required": ["spreadsheet_id", "range", "values_json"],
                },
            },
            {
                "name": "slides_create",
                "description": "Create a new Google Slides presentation in Kas's Drive. Miles is acting inside Kas's Google: announce the deck you created and share its link.",
                "input_schema": {
                    "type": "object",
                    "properties": {"title": {"type": "string", "description": "Presentation title."}},
                    "required": ["title"],
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
                # Window: explicit start_date / end_date (Europe/Zurich) override days.
                now = datetime.now(LOCAL_TZ)
                sd = str(args.get("start_date", "") or "").strip()
                ed = str(args.get("end_date", "") or "").strip()
                try:
                    days = int(args.get("days", 30) or 30)
                except (TypeError, ValueError):
                    days = 30
                days = max(1, min(days, 800))
                start_dt = now
                if sd:
                    try:
                        start_dt = datetime.strptime(sd, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
                    except ValueError:
                        return "Error: start_date must be YYYY-MM-DD."
                if ed:
                    try:
                        end_dt = datetime.strptime(ed, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ) + timedelta(days=1)
                    except ValueError:
                        return "Error: end_date must be YYYY-MM-DD."
                else:
                    end_dt = start_dt + timedelta(days=days)
                if end_dt <= start_dt:
                    return "Error: the window is empty (end_date is before start_date)."
                time_min = start_dt.isoformat()
                time_max = end_dt.isoformat()
                search_q = str(args.get("q", "") or "").strip()
                # Kas's real schedule lives across many named calendars, not just
                # primary. List every calendar the account can see, then read each.
                cl = http.get(f"{self._CAL}/users/me/calendarList", headers=h,
                              params={"maxResults": 250})
                if cl.status_code >= 400:
                    # Narrow token scope: fall back to primary so the read never
                    # comes back empty. Re-consent unlocks every calendar.
                    cals = [{"id": "primary", "summary": "primary"}]
                else:
                    cals = cl.json().get("items", []) or []
                only = str(args.get("calendar_id", "") or "").strip()
                if only and only != "primary":
                    cals = [c for c in cals if c.get("id") == only]
                merged = []
                truncated_cals: list[str] = []
                failed_cals: list[str] = []
                for cal in cals:
                    cid = cal.get("id")
                    if not cid:
                        continue
                    cal_name = cal.get("summaryOverride") or cal.get("summary") or cid
                    try:
                        page_token = ""
                        pages = 0
                        while pages < 2:  # up to 2 pages (500 events) per calendar
                            params = {
                                "timeMin": time_min,
                                "timeMax": time_max,
                                "singleEvents": "true",
                                "orderBy": "startTime",
                                "maxResults": 250,
                            }
                            if search_q:
                                params["q"] = search_q
                            if page_token:
                                params["pageToken"] = page_token
                            er = http.get(
                                f"{self._CAL}/calendars/{quote(cid, safe='@')}/events",
                                headers=h, params=params)
                            if er.status_code >= 400:
                                failed_cals.append(cal_name)
                                page_token = ""
                                break
                            ej = er.json()
                            for ev in ej.get("items", []):
                                start = ev.get("start", {}) or {}
                                end = ev.get("end", {}) or {}
                                merged.append({
                                    "id": ev.get("id"),
                                    "summary": ev.get("summary", "(no title)"),
                                    "start": start.get("dateTime") or start.get("date"),
                                    "end": end.get("dateTime") or end.get("date"),
                                    "location": ev.get("location", ""),
                                    "attendees": len(ev.get("attendees", []) or []),
                                    "calendar": cal_name,
                                })
                            page_token = ej.get("nextPageToken", "") or ""
                            pages += 1
                            if not page_token:
                                break
                        if page_token:
                            truncated_cals.append(cal_name)
                    except Exception:  # noqa: BLE001
                        # One bad calendar must not kill the whole read.
                        failed_cals.append(cal_name)
                        continue
                # Dedupe on summary + start (the same event often appears on
                # several calendars), then sort chronologically.
                seen = set()
                deduped = []
                for ev in merged:
                    key = (ev.get("summary"), ev.get("start"))
                    if key in seen:
                        continue
                    seen.add(key)
                    deduped.append(ev)
                deduped.sort(key=lambda e: (e.get("start") or ""))
                window = {
                    "start": start_dt.strftime("%Y-%m-%d"),
                    "end": (end_dt - timedelta(seconds=1)).strftime("%Y-%m-%d"),
                    "timezone": "Europe/Zurich",
                }
                payload: dict = {"window": window,
                                 "complete": not (truncated_cals or failed_cals)}
                notes = []
                if truncated_cals:
                    notes.append(
                        "Pagination stopped after 2 pages on: "
                        + ", ".join(sorted(set(truncated_cals)))
                        + ". Later events on those calendars are missing from this pull.")
                if failed_cals:
                    notes.append(
                        "Could not read: " + ", ".join(sorted(set(failed_cals)))
                        + ". Their events are missing from this pull.")
                if notes:
                    notes.append("Tell Kas plainly which part of the window you could not see.")
                    payload["note"] = " ".join(notes)
                if search_q:
                    payload["search"] = search_q

                # Never let routine blocks crowd out real appointments: when the
                # merged list is large, keep every non rhythm event in full and
                # compress the recurring Ideal Week blocks into one line per day.
                def _is_rhythm(e: dict) -> bool:
                    return "ideal week" in str(e.get("calendar", "")).lower()

                if len(deduped) > 120:
                    real = [e for e in deduped if not _is_rhythm(e)]
                    rhythm = [e for e in deduped if _is_rhythm(e)]
                    per_day: dict = {}
                    for e in rhythm:
                        day = str(e.get("start") or "")[:10]
                        d = per_day.setdefault(day, {"blocks": 0, "first": "", "last": ""})
                        d["blocks"] += 1
                        st = str(e.get("start") or "")
                        en = str(e.get("end") or "")
                        t0 = st[11:16] if len(st) >= 16 else "00:00"
                        t1 = en[11:16] if len(en) >= 16 else t0
                        if not d["first"] or t0 < d["first"]:
                            d["first"] = t0
                        if not d["last"] or t1 > d["last"]:
                            d["last"] = t1
                    payload["events"] = real
                    payload["ideal_week_daily"] = [
                        {"day": day,
                         "summary": (f"Ideal Week rhythm blocks: {v['first']} to "
                                     f"{v['last']} ({v['blocks']} blocks)")}
                        for day, v in sorted(per_day.items())
                    ]
                    payload["count"] = {"appointments": len(real),
                                        "ideal_week_blocks": len(rhythm)}
                    payload["digest"] = (
                        "Recurring Ideal Week rhythm blocks are compressed to one line per "
                        "day; every other event in the window is listed in full.")
                else:
                    payload["events"] = deduped
                    payload["count"] = len(deduped)
                # Full JSON, never string sliced: a byte cap truncates valid JSON
                # mid event and breaks the consumer's parse.
                return json.dumps(payload, ensure_ascii=False)

            if tool_name == "calendar_create_event":
                raw_cal = str(args.get("calendar_id", "primary") or "primary").strip() or "primary"
                cal_id = quote(raw_cal, safe="@")
                summary = str(args.get("summary", "")).strip()
                start_iso = str(args.get("start_iso", "")).strip()
                end_iso = str(args.get("end_iso", "")).strip()
                if not (summary and start_iso and end_iso):
                    return "Error: need summary, start_iso and end_iso (RFC3339, e.g. 2026-07-20T14:00:00)."
                tz_name = str(args.get("timezone", "") or "").strip() or "Europe/Zurich"
                try:
                    ZoneInfo(tz_name)
                except Exception:  # noqa: BLE001
                    return (f"Error: '{tz_name}' is not a valid IANA timezone, so the event was "
                            "NOT created. Pass one like Europe/Zurich or America/Chicago.")
                # Idempotency guard (2026-07-25, from the 10k test harness): the model
                # sometimes re-issues an identical create after an approval turn, which
                # lands duplicate events on the principal's real calendar. Read the
                # minute around the requested start and refuse to create an event whose
                # normalized title and start instant match an existing one. Fail open:
                # if this probe errors, the create proceeds (the guard must never block
                # a legitimate booking).
                def _norm_title(s: str) -> str:
                    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())
                try:
                    g_start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                    if g_start.tzinfo is None:
                        g_start = g_start.replace(tzinfo=ZoneInfo(tz_name))
                    probe = http.get(
                        f"{self._CAL}/calendars/{cal_id}/events", headers=h, params={
                            "timeMin": (g_start - timedelta(minutes=1)).isoformat(),
                            "timeMax": (g_start + timedelta(minutes=1)).isoformat(),
                            "singleEvents": "true", "maxResults": 50,
                        })
                    if probe.status_code < 400:
                        for ex in probe.json().get("items", []) or []:
                            if _norm_title(ex.get("summary", "")) != _norm_title(summary):
                                continue
                            exs = (ex.get("start") or {}).get("dateTime") or ""
                            try:
                                ex_dt = datetime.fromisoformat(exs.replace("Z", "+00:00"))
                                if ex_dt.tzinfo is None:
                                    ex_tz = (ex.get("start") or {}).get("timeZone") or tz_name
                                    ex_dt = ex_dt.replace(tzinfo=ZoneInfo(ex_tz))
                            except ValueError:
                                continue
                            if ex_dt == g_start:
                                return json.dumps({
                                    "created": False,
                                    "duplicate_of": {
                                        "id": ex.get("id", ""),
                                        "summary": ex.get("summary", ""),
                                        "start": exs,
                                        "htmlLink": ex.get("htmlLink", ""),
                                    },
                                    "note": ("NOT created: an identical event (same title, "
                                             "same start) already exists on this calendar. "
                                             "Tell the principal it is already booked and "
                                             "share the EXISTING link above. Do not create "
                                             "it again."),
                                }, ensure_ascii=False)
                except Exception:  # noqa: BLE001
                    pass  # fail open: a broken guard must never block a real booking
                body_payload: dict = {
                    "summary": summary,
                    "start": {"dateTime": start_iso, "timeZone": tz_name},
                    "end": {"dateTime": end_iso, "timeZone": tz_name},
                }
                desc = str(args.get("description", "") or "").strip()
                if desc:
                    body_payload["description"] = desc
                r = http.post(f"{self._CAL}/calendars/{cal_id}/events", headers=h, json=body_payload)
                if r.status_code >= 400:
                    return f"Error: the event was NOT created. Google said: {r.text[:300]}"
                ev = r.json()
                event_id = str(ev.get("id", "") or "")
                if not event_id:
                    return ("Error: the event was NOT created (Google returned no event id). "
                            "Do not tell Kas it is booked.")
                # Verified write: read the event straight back before claiming success.
                vr = http.get(f"{self._CAL}/calendars/{cal_id}/events/{quote(event_id, safe='')}",
                              headers=h)
                if vr.status_code >= 400:
                    return (f"Error: Google returned event id {event_id} but the verification "
                            f"readback failed ({vr.status_code}: {vr.text[:200]}). Treat this as "
                            "NOT booked and tell Kas plainly.")
                vj = vr.json()
                cal_label = raw_cal
                cm = http.get(f"{self._CAL}/calendars/{cal_id}", headers=h)
                if cm.status_code < 400:
                    cal_label = str(cm.json().get("summary") or raw_cal)
                if raw_cal == "primary" and cal_label != "primary":
                    cal_label = f"primary ({cal_label})"
                vstart = vj.get("start") or {}
                vend = vj.get("end") or {}
                return json.dumps({
                    "verified": True,
                    "id": event_id,
                    "htmlLink": vj.get("htmlLink", ""),
                    "calendar": cal_label,
                    "summary": vj.get("summary", summary),
                    "start": vstart.get("dateTime") or vstart.get("date"),
                    "end": vend.get("dateTime") or vend.get("date"),
                    "note": ("Verified by readback from Google. Tell Kas exactly what you "
                             "booked, name the calendar it landed on, and give her the "
                             "htmlLink as proof."),
                }, ensure_ascii=False)

            if tool_name == "calendar_delete_event":
                event_id = str(args.get("event_id", "")).strip()
                if not event_id:
                    return "Error: need event_id."
                raw_cal = str(args.get("calendar_id", "primary") or "primary").strip() or "primary"
                cal_id = quote(raw_cal, safe="@")
                r = http.delete(f"{self._CAL}/calendars/{cal_id}/events/{quote(event_id, safe='')}",
                                headers=h)
                if r.status_code in (200, 204):
                    return (f"Deleted event {event_id} from calendar '{raw_cal}'. "
                            "Tell Kas exactly what you removed.")
                if r.status_code in (404, 410):
                    return (f"Error: no event {event_id} on calendar '{raw_cal}'. "
                            "It may already be gone or live on a different calendar.")
                return f"Error: delete failed ({r.status_code}): {r.text[:300]}"

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
                return json.dumps(r.json().get("files", []), ensure_ascii=False)

            if tool_name == "drive_create_folder":
                name = str(args.get("name", "")).strip()
                if not name:
                    return "Error: need a folder name."
                body_payload = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
                parent = str(args.get("parent_id", "") or "").strip()
                if parent:
                    body_payload["parents"] = [parent]
                r = http.post(f"{self._DRIVE}/files", headers=h,
                              params={"fields": "id,name,webViewLink"}, json=body_payload)
                if r.status_code >= 400:
                    return f"Error creating folder: {r.text[:300]}"
                f = r.json()
                return (f"Created folder '{f.get('name', name)}' (id {f.get('id','')}) in Kas's Drive: "
                        f"{f.get('webViewLink','')}. Tell Kas you made it and share the link.")

            if tool_name == "drive_move":
                fid = str(args.get("file_id", "")).strip()
                new_parent = str(args.get("new_parent_id", "")).strip()
                if not (fid and new_parent):
                    return "Error: need file_id and new_parent_id."
                # look up current parents so we can remove them
                g = http.get(f"{self._DRIVE}/files/{fid}", headers=h, params={"fields": "parents,name"})
                if g.status_code >= 400:
                    return f"Error reading file: {g.text[:300]}"
                cur = g.json()
                old_parents = ",".join(cur.get("parents", []) or [])
                r = http.patch(f"{self._DRIVE}/files/{fid}", headers=h, params={
                    "addParents": new_parent,
                    "removeParents": old_parents,
                    "fields": "id,name,parents,webViewLink",
                })
                if r.status_code >= 400:
                    return f"Error moving file: {r.text[:300]}"
                f = r.json()
                return (f"Moved '{f.get('name','')}' into folder {new_parent}. {f.get('webViewLink','')}. "
                        "Tell Kas what you moved and where.")

            if tool_name == "drive_rename":
                fid = str(args.get("file_id", "")).strip()
                new_name = str(args.get("new_name", "")).strip()
                if not (fid and new_name):
                    return "Error: need file_id and new_name."
                r = http.patch(f"{self._DRIVE}/files/{fid}", headers=h,
                               params={"fields": "id,name,webViewLink"}, json={"name": new_name})
                if r.status_code >= 400:
                    return f"Error renaming file: {r.text[:300]}"
                f = r.json()
                return (f"Renamed to '{f.get('name', new_name)}'. {f.get('webViewLink','')}. "
                        "Tell Kas the new name.")

            if tool_name == "drive_trash":
                fid = str(args.get("file_id", "")).strip()
                if not fid:
                    return "Error: need file_id."
                r = http.patch(f"{self._DRIVE}/files/{fid}", headers=h,
                               params={"fields": "id,name"}, json={"trashed": True})
                if r.status_code >= 400:
                    return f"Error trashing file: {r.text[:300]}"
                f = r.json()
                return (f"Moved '{f.get('name','')}' to Kas's Drive Trash. This is recoverable from "
                        "Trash for 30 days, not a permanent delete. Tell Kas you trashed it and that "
                        "she can restore it.")

            if tool_name == "docs_create":
                title = str(args.get("title", "")).strip()
                if not title:
                    return "Error: need a document title."
                r = http.post(self._DOCS, headers=h, json={"title": title})
                if r.status_code >= 400:
                    return f"Error creating doc: {r.text[:300]}"
                doc_id = r.json().get("documentId", "")
                body_text = str(args.get("body_text", "") or "")
                if body_text and doc_id:
                    ur = http.post(f"{self._DOCS}/{doc_id}:batchUpdate", headers=h, json={
                        "requests": [{"insertText": {"location": {"index": 1}, "text": body_text}}]
                    })
                    if ur.status_code >= 400:
                        return (f"Created doc '{title}' (id {doc_id}) but couldn't add the body text "
                                f"({ur.text[:150]}). Link: https://docs.google.com/document/d/{doc_id}/edit")
                link = f"https://docs.google.com/document/d/{doc_id}/edit"
                return (f"Created Google Doc '{title}' (id {doc_id}) in Kas's Drive: {link}. "
                        "Tell Kas you made it and share the link.")

            if tool_name == "docs_read":
                doc_id = str(args.get("document_id", "")).strip()
                if not doc_id:
                    return "Error: need a document_id."
                r = http.get(f"{self._DOCS}/{doc_id}", headers=h)
                if r.status_code >= 400:
                    return f"Error reading doc: {r.text[:300]}"
                return self._docs_plain_text(r.json())[:6000] or "(doc has no readable text)"

            if tool_name == "docs_append":
                doc_id = str(args.get("document_id", "")).strip()
                text = str(args.get("text", ""))
                if not (doc_id and text):
                    return "Error: need document_id and text."
                r = http.post(f"{self._DOCS}/{doc_id}:batchUpdate", headers=h, json={
                    "requests": [{"insertText": {"endOfSegmentLocation": {}, "text": text}}]
                })
                if r.status_code >= 400:
                    return f"Error appending to doc: {r.text[:300]}"
                link = f"https://docs.google.com/document/d/{doc_id}/edit"
                return f"Appended your text to the doc: {link}. Tell Kas what you added and share the link."

            if tool_name == "docs_replace":
                doc_id = str(args.get("document_id", "")).strip()
                find = str(args.get("find", ""))
                replace = str(args.get("replace", ""))
                if not (doc_id and find):
                    return "Error: need document_id and the text to find."
                r = http.post(f"{self._DOCS}/{doc_id}:batchUpdate", headers=h, json={
                    "requests": [{"replaceAllText": {
                        "containsText": {"text": find, "matchCase": True},
                        "replaceText": replace,
                    }}]
                })
                if r.status_code >= 400:
                    return f"Error editing doc: {r.text[:300]}"
                occ = 0
                for rep in (r.json().get("replies") or []):
                    occ += ((rep.get("replaceAllText") or {}).get("occurrencesChanged") or 0)
                link = f"https://docs.google.com/document/d/{doc_id}/edit"
                return (f"Replaced {occ} occurrence(s) of that text in the doc: {link}. "
                        "Tell Kas what you changed and share the link.")

            if tool_name == "sheets_create":
                title = str(args.get("title", "")).strip()
                if not title:
                    return "Error: need a spreadsheet title."
                r = http.post(self._SHEETS, headers=h, json={"properties": {"title": title}})
                if r.status_code >= 400:
                    return f"Error creating sheet: {r.text[:300]}"
                j = r.json()
                sid = j.get("spreadsheetId", "")
                link = j.get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{sid}/edit"
                return (f"Created Google Sheet '{title}' (id {sid}) in Kas's Drive: {link}. "
                        "Tell Kas you made it and share the link.")

            if tool_name == "sheets_read":
                sid = str(args.get("spreadsheet_id", "")).strip()
                rng = str(args.get("range", "") or "A1:Z100").strip() or "A1:Z100"
                if not sid:
                    return "Error: need a spreadsheet_id."
                r = http.get(f"{self._SHEETS}/{sid}/values/{quote(rng, safe='!:')}", headers=h)
                if r.status_code >= 400:
                    return f"Error reading sheet: {r.text[:300]}"
                values = r.json().get("values", []) or []
                if len(values) > 100:
                    return json.dumps({"rows": values[:100],
                                       "note": f"Showing the first 100 of {len(values)} rows."},
                                      ensure_ascii=False)
                return json.dumps(values, ensure_ascii=False)

            if tool_name in ("sheets_write", "sheets_append"):
                sid = str(args.get("spreadsheet_id", "")).strip()
                rng = str(args.get("range", "")).strip()
                if not (sid and rng):
                    return "Error: need spreadsheet_id and range."
                try:
                    values = json.loads(str(args.get("values_json", "")))
                    if not isinstance(values, list):
                        raise ValueError
                    if values and not isinstance(values[0], list):
                        values = [values]
                except (json.JSONDecodeError, ValueError):
                    return "Error: values_json must be a JSON 2D array, e.g. [[\"A\",\"B\"],[1,2]]."
                enc_rng = quote(rng, safe="!:")
                if tool_name == "sheets_write":
                    r = http.put(f"{self._SHEETS}/{sid}/values/{enc_rng}", headers=h,
                                 params={"valueInputOption": "USER_ENTERED"}, json={"values": values})
                    if r.status_code >= 400:
                        return f"Error writing to sheet: {r.text[:300]}"
                    updated = (r.json() or {}).get("updatedCells", "?")
                    verb = f"Wrote {updated} cell(s) to"
                else:
                    r = http.post(f"{self._SHEETS}/{sid}/values/{enc_rng}:append", headers=h,
                                  params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
                                  json={"values": values})
                    if r.status_code >= 400:
                        return f"Error appending to sheet: {r.text[:300]}"
                    updated = ((r.json() or {}).get("updates") or {}).get("updatedCells", "?")
                    verb = f"Appended {updated} cell(s) to"
                link = f"https://docs.google.com/spreadsheets/d/{sid}/edit"
                return f"{verb} range {rng} of the sheet: {link}. Tell Kas what you changed and share the link."

            if tool_name == "slides_create":
                title = str(args.get("title", "")).strip()
                if not title:
                    return "Error: need a presentation title."
                r = http.post(self._SLIDES, headers=h, json={"title": title})
                if r.status_code >= 400:
                    return f"Error creating presentation: {r.text[:300]}"
                pid = r.json().get("presentationId", "")
                link = f"https://docs.google.com/presentation/d/{pid}/edit"
                return (f"Created Google Slides deck '{title}' (id {pid}) in Kas's Drive: {link}. "
                        "Tell Kas you made it and share the link.")

            return f"Error: unknown google tool {tool_name}."


# v6.5/v6.6 (2026-07-28 incident): the legacy ICS CalendarConnector is GONE. In
# production CALENDAR_ICS_URLS held a single stale "Elite Coaching" feed, so the
# model had a wrong "calendar_upcoming" tool sitting next to the live
# calendar_upcoming_v2 read, and the Slack EOD post built on it claimed "nothing
# scheduled" on a back to back day. There is now exactly ONE calendar data path
# in the whole system: GoogleConnector.calendar_upcoming_v2 (live, all calendars,
# Sentinel probed). Do not add a second one.
CONNECTORS: list[Connector] = [EmailConnector(), NotionConnector(), SlackConnector(), GoogleConnector()]


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


# v6.2: calendar depth + search, verified writes with proof links, truncation audit (2026-07-24)
# v6.3: create-event idempotency guard (duplicate title+start refused) + gate covers
#       www.google.com/calendar links (2026-07-25, 10k test harness findings)
def status_lines() -> list[str]:
    lines = []
    for c in CONNECTORS:
        if c.name == "google":
            if c.configured():
                lines.append("\U0001f7e2 google: wired and live (Gmail draft-only, Calendar read+write, Drive/Docs/Sheets/Slides full create+edit, Drive delete=trash-only)")
            elif GoogleConnector._has_app():
                lines.append("\U0001f7e1 google: app ready, run /connectgoogle to connect Kas's account")
            else:
                lines.append(f"⚪ google: not wired yet (needs {c.needs()})")
            continue
        if c.configured():
            extra = ""
            if c.name == "email":
                extra = f" ({len(EmailConnector._accounts())} account(s))"
            lines.append(f"\U0001f7e2 {c.name}: wired and live (read only){extra}")
        else:
            lines.append(f"⚪ {c.name}: not wired yet (needs {c.needs()})")
    return lines
