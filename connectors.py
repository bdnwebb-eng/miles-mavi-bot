"""Connector layer for Miles. Read only, env gated.

Each connector switches ON automatically when its credentials exist in the
environment (Railway variables). Wiring a new account is a config job:
set the env vars, redeploy, done. No code changes.

Currently shipped:
  email  : IMAP read only (works with Gmail app passwords, Infomaniak, any IMAP host)
           EMAIL_IMAP_HOST, EMAIL_ADDRESS, EMAIL_APP_PASSWORD  [optional EMAIL_IMAP_PORT]
  notion : Notion REST API read only (search + read page)
           NOTION_API_KEY

Adding a connector later (calendar, Slack, Instagram, Dispatch):
subclass Connector, implement configured() / tools() / run(), append to CONNECTORS.

Trust model: connectors are READ ONLY by design. Miles never sends, deletes,
or modifies anything through a connector. Drafts stay drafts until Kas taps.
"""
from __future__ import annotations

import email as email_lib
import imaplib
import json
import os
from email.header import decode_header

import httpx


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


# ───────────────────────── email (IMAP, read only) ─────────────────────────
class EmailConnector(Connector):
    name = "email"

    def needs(self) -> str:
        return "EMAIL_IMAP_HOST, EMAIL_ADDRESS, EMAIL_APP_PASSWORD"

    def configured(self) -> bool:
        return all(os.environ.get(k) for k in ("EMAIL_IMAP_HOST", "EMAIL_ADDRESS", "EMAIL_APP_PASSWORD"))

    def tools(self) -> list[dict]:
        return [
            {
                "name": "email_recent",
                "description": "List recent emails from the principal's inbox (read only). Returns uid, from, subject, date for each.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "How many, max 20. Default 10."},
                        "unread_only": {"type": "boolean", "description": "Only unread messages. Default false."},
                    },
                },
            },
            {
                "name": "email_read",
                "description": "Read one email in full (read only) by the uid returned from email_recent.",
                "input_schema": {
                    "type": "object",
                    "properties": {"uid": {"type": "string", "description": "The uid from email_recent."}},
                    "required": ["uid"],
                },
            },
        ]

    def _connect(self) -> imaplib.IMAP4_SSL:
        host = os.environ["EMAIL_IMAP_HOST"]
        port = int(os.environ.get("EMAIL_IMAP_PORT", "993"))
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(os.environ["EMAIL_ADDRESS"], os.environ["EMAIL_APP_PASSWORD"])
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
        conn = self._connect()
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
        ]

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


CONNECTORS: list[Connector] = [EmailConnector(), NotionConnector()]


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
            lines.append(f"🟢 {c.name}: wired and live (read only)")
        else:
            lines.append(f"⚪ {c.name}: not wired yet (needs {c.needs()})")
    return lines
