"""Proactive cold-flag scan of the principal's Notion project database.

SOW item 2: Miles "proactively messages Kas when anything goes cold."
Env gated: does nothing until BOTH are set on Railway:
  NOTION_API_KEY          (same key as the connector)
  NOTION_PROJECTS_DB_ID   (the database Miles watches)
Optional tuning:
  NOTION_ACTIVITY_PROP    date property to judge staleness by (default: use
                          Notion's own last_edited_time on each row)
  COLD_DAYS               staleness threshold in days (default 7)
  NOTION_TITLE_PROP       title property name (auto-detected if omitted)

Read only: the scan never writes. It reports; Kas (or Miles, on her word)
updates the page via the notion_update_property tool.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import httpx

_API = "https://api.notion.com/v1"


def enabled() -> bool:
    return bool(os.environ.get("NOTION_API_KEY") and os.environ.get("NOTION_PROJECTS_DB_ID"))


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def _row_title(page: dict) -> str:
    want = os.environ.get("NOTION_TITLE_PROP")
    for pname, prop in (page.get("properties") or {}).items():
        if prop.get("type") == "title" and (want is None or pname == want):
            return "".join(t.get("plain_text", "") for t in prop.get("title", [])) or "(untitled)"
    return "(untitled)"


def _row_activity(page: dict) -> datetime | None:
    """Timestamp of last activity: the configured date prop, else last_edited_time."""
    prop_name = os.environ.get("NOTION_ACTIVITY_PROP")
    if prop_name:
        prop = (page.get("properties") or {}).get(prop_name)
        if prop and prop.get("type") == "date" and prop.get("date"):
            try:
                raw = prop["date"].get("start", "")
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                return None
        return None
    raw = page.get("last_edited_time", "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def cold_items(limit: int = 5) -> list[dict]:
    """Rows whose last activity is older than COLD_DAYS. Empty list if healthy or unconfigured."""
    if not enabled():
        return []
    cold_days = int(os.environ.get("COLD_DAYS", "7") or 7)
    dbid = os.environ["NOTION_PROJECTS_DB_ID"]
    out: list[dict] = []
    with httpx.Client(timeout=25) as http:
        r = http.post(f"{_API}/databases/{dbid}/query", headers=_headers(), json={"page_size": 100})
        r.raise_for_status()
        now = datetime.now(timezone.utc)
        for page in r.json().get("results", []):
            ts = _row_activity(page)
            if ts is None:
                continue
            days = (now - ts).days
            if days >= cold_days:
                out.append({"id": page["id"], "title": _row_title(page), "days_quiet": days})
    out.sort(key=lambda x: -x["days_quiet"])
    return out[:limit]


def cold_report() -> str | None:
    """Phone-length message for the principal, or None when there's nothing to flag."""
    try:
        items = cold_items()
    except Exception:  # noqa: BLE001  (a Notion hiccup must never crash the bot)
        return None
    if not items:
        return None
    lines = ["🧊 Cold check, from your Notion:"]
    for it in items:
        lines.append(f"• {it['title']}: quiet {it['days_quiet']} days")
    lines.append("")
    lines.append("Say the word and I'll suggest a revival move for any of them.")
    return "\n".join(lines)
