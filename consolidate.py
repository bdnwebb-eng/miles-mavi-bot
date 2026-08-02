"""consolidate.py: the nightly memory distiller (v7 transplant).

Runs at 03:30 Geneva via the PTB job queue. Takes the raw memories the extraction
pass collected during the day, applies the storage taxonomy, and maintains the
small curated tier that rides in every prompt. Every run is logged and DMed to
the operator as a diff. Raw rows are archived after distilling, never destroyed.

Taxonomy (what may become curated memory):
  - live-domain state (pipeline counts, calendar contents): REFUSE, live systems
    are the only truth for those.
  - forbidden class (credentials, third-party PII beyond names/roles, health or
    legal detail, anything behind the financial wall): REFUSE.
  - events and decisions Kas or Brandon stated: KEEP, they supersede older
    contradicting entries.
  - derived judgments: KEEP, marked derived.
  - standing directives ("never book me before 9am"): NOT memory. Flagged in the
    operator diff so a human moves them into the right knowledge file.
Trust boundary: only chat with the allowlisted principals feeds this pipeline;
tool results and connector text are never distilled into the bot's head.
"""
from __future__ import annotations

import json
import logging
import os

from anthropic import Anthropic

import config_loader as cfg
import database as db

log = logging.getLogger(__name__)

DISTILL_MODEL = "claude-haiku-4-5-20251001"
CURATED_CHAR_CAP = 6000  # ~1500 tokens, the v1.3 hard cap


def _client() -> Anthropic:
    return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def distill() -> dict:
    """One consolidation pass. Returns a summary dict for logging and the DM."""
    raw = db.unconsolidated_memories()
    curated = db.curated_all()
    summary = {"raw_seen": len(raw), "kept": 0, "superseded": 0, "directives": [], "dropped": 0}
    if not raw:
        db.log_consolidation(summary, "nothing new to distill")
        return summary

    cur_block = "\n".join(f"[{r['id']}] ({r['category']}) {r['content']}" for r in curated) or "(empty)"
    raw_block = "\n".join(f"- ({r['category']}) {r['content']}" for r in raw)[:9000]
    prompt = (
        "You maintain the curated long term memory of Miles, an executive assistant bot "
        "for Kas Bordier (MAVI). Below are the CURRENT curated memories (with ids) and "
        "NEW raw notes distilled from recent conversations with the principals.\n\n"
        "Rules, strict:\n"
        "1. REFUSE live-domain state (pipeline contents, calendar contents, inbox "
        "contents): live systems are the only truth for those.\n"
        "2. REFUSE credentials, health/legal detail, third-party personal data beyond "
        "names and roles, and anything about pricing amounts.\n"
        "3. KEEP events and decisions the principal stated (category event) and stable "
        "preferences/facts (category fact or preference). One tight sentence each.\n"
        "4. KEEP derived judgments only when clearly useful (category derived).\n"
        "5. A standing directive (a rule for how to behave, like never book before 9am) "
        "is NOT memory: put it in directives instead.\n"
        "6. If a new item contradicts or updates a current memory, list that current "
        "id in supersede_ids.\n"
        "7. Merge duplicates. Fewer, sharper memories beat many.\n\n"
        f"CURRENT CURATED:\n{cur_block}\n\nNEW RAW NOTES:\n{raw_block}\n\n"
        'Reply ONLY with JSON: {"keep": [{"category": "fact|preference|event|derived", '
        '"content": "one sentence"}], "supersede_ids": [int], "directives": ["..."]}. '
        'Empty lists are fine.'
    )
    resp = _client().messages.create(
        model=DISTILL_MODEL, max_tokens=1200, temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    start, end = text.find("{"), text.rfind("}")
    plan = json.loads(text[start:end + 1]) if start != -1 and end > start else {}

    sup = [int(x) for x in plan.get("supersede_ids", []) if str(x).isdigit()]
    if sup:
        summary["superseded"] = db.supersede_curated(sup)
    added = []
    for it in plan.get("keep", [])[:12]:
        if isinstance(it, dict) and it.get("content"):
            cat = str(it.get("category", "fact"))[:16]
            db.add_curated(cat, str(it["content"])[:400])
            added.append(f"({cat}) {str(it['content'])[:120]}")
    summary["kept"] = len(added)
    summary["directives"] = [str(x)[:200] for x in plan.get("directives", [])][:6]
    summary["dropped"] = db.enforce_curated_cap(CURATED_CHAR_CAP)
    db.archive_consolidated([r["id"] for r in raw])
    db.log_consolidation(summary, "; ".join(added)[:1500])
    return summary


async def nightly(context) -> None:
    """PTB job queue entry point, 03:30 Geneva daily."""
    import asyncio
    try:
        summary = await asyncio.to_thread(distill)
    except Exception as e:  # noqa: BLE001
        log.warning("[consolidate] failed: %s", e)
        try:
            import sentinel
            await asyncio.to_thread(sentinel.send_ops_alert, f"Nightly memory distiller FAILED: {e}")
        except Exception:  # noqa: BLE001
            pass
        return
    lines = [
        "Miles nightly memory diff:",
        f"raw notes seen {summary['raw_seen']}, kept {summary['kept']}, "
        f"superseded {summary['superseded']}, trimmed for cap {summary['dropped']}.",
    ]
    if summary.get("directives"):
        lines.append("STANDING DIRECTIVES flagged (move into a knowledge file):")
        lines.extend("- " + d for d in summary["directives"])
    if summary["raw_seen"] or summary.get("directives"):
        try:
            import sentinel
            import asyncio as _a
            await _a.to_thread(sentinel.send_ops_alert, "\n".join(lines))
        except Exception as e:  # noqa: BLE001
            log.warning("[consolidate] diff DM failed: %s", e)
