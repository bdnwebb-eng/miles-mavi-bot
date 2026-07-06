"""Loads and lightly validates the YAML config files for Hermes."""
from __future__ import annotations

import os
from functools import lru_cache

import yaml

CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")


def _load(name: str) -> dict:
    path = os.path.join(CONFIG_DIR, name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing config file: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


@lru_cache(maxsize=1)
def settings() -> dict:
    return _load("settings.yaml")


@lru_cache(maxsize=1)
def persona() -> dict:
    p = _load("persona.yaml")
    # Resolve {niche} placeholders inside guardrails so the prompt is clean.
    niche = p.get("niche", "")
    for key in ("guardrails", "intro"):
        if isinstance(p.get(key), str):
            p[key] = p[key].replace("{niche}", niche)
    return p


@lru_cache(maxsize=1)
def program() -> dict:
    return _load("program.yaml")


@lru_cache(maxsize=1)
def resources() -> dict:
    return _load("resources.yaml")


@lru_cache(maxsize=1)
def knowledge() -> dict:
    """Felipe's construction + business knowledge base (optional file)."""
    try:
        return _load("knowledge.yaml")
    except FileNotFoundError:
        return {}


def knowledge_text() -> str:
    """Flatten the knowledge base into a compact reference block for the AI prompt."""
    k = knowledge()
    if not k:
        return ""
    parts: list[str] = []
    labels = {
        "business_coaching": "BUSINESS COACHING",
        "building_insights": "BUILDING / CONSTRUCTION",
    }
    for section, label in labels.items():
        items = k.get(section) or []
        if items:
            parts.append(f"## {label}")
            for it in items:
                parts.append(f"- {it.get('topic','')}: {it.get('insight','').strip()}")
    facts = k.get("quick_facts") or {}
    if facts:
        parts.append("## QUICK FACTS")
        for key, val in facts.items():
            parts.append(f"- {key}: {str(val).strip()}")
    coaching = k.get("coaching_program") or {}
    if coaching:
        parts.append("## COACHING PROGRAM (Versa Method)")
        for key, val in coaching.items():
            parts.append(f"- {key}: {str(val).strip()}")
    quotes = k.get("verbatim_quotes") or []
    if quotes:
        parts.append("## VERBATIM QUOTES (echo his phrasing, don't overuse)")
        for q in quotes:
            parts.append(f'- "{q}"')
    return "\n".join(parts)


def examples() -> list[dict]:
    """Few-shot Q/A pairs that anchor Felipe's voice (optional file)."""
    try:
        data = _load("examples.yaml")
    except FileNotFoundError:
        return []
    return data.get("examples", []) if isinstance(data, dict) else []


def flat_lessons() -> list[dict]:
    """Return lessons in order with module context attached."""
    out: list[dict] = []
    for m in program().get("modules", []):
        for lesson in m.get("lessons", []):
            out.append(
                {
                    "module_id": m["id"],
                    "module_title": m["title"],
                    "lesson_id": lesson["id"],
                    "title": lesson["title"],
                    "body": lesson.get("body", ""),
                    "coach_note": lesson.get("coach_note", ""),
                }
            )
    return out


def lesson_by_id(lesson_id: str) -> dict | None:
    for lesson in flat_lessons():
        if lesson["lesson_id"] == lesson_id:
            return lesson
    return None
