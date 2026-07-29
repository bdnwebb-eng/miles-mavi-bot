#!/usr/bin/env python3
"""selfcheck.py — pre-deploy safety gate for the Miles bot.

Codifies the deploy safety protocol as a script so no future deploy ever ships a
truncated or broken file. RUN IT IN THE STAGING DIRECTORY BEFORE `railway up`:

    python3 selfcheck.py

It must exit 0 (SELFCHECK PASS) before a deploy proceeds. Any non zero exit means
a file is truncated, a critical symbol was lost, a YAML is broken, or a module will
not import. That is exactly the class of failure (torn writes, sliced files) that
has shipped a broken Miles before. Do not deploy on a FAIL.

Five passes over the telegram-bot folder:
  1. AST parse every .py                (catches truncation / syntax breakage).
  2. py_compile every .py               (subprocess safe: compiles without executing,
                                         so it also vets the telegram dependent files
                                         handlers.py / bot.py that cannot be imported
                                         in a bare environment).
  3. Grep for critical symbols          (catches a file that parsed but lost a
                                         required function/class/route/tool name).
  4. Validate every config/*.yaml with PyYAML.
  5. Import the import safe core        (connectors, database, sentinel) so import
                                         time breakage surfaces here, not at boot.

Exits non-zero with a clear message on the first failing pass. Prints
"SELFCHECK PASS" only when every pass is clean.
"""
from __future__ import annotations

import ast
import glob
import os
import py_compile
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _fail(msg: str) -> "None":
    print(f"SELFCHECK FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


# Critical symbols that MUST survive in each file. file -> list of (needle, min_count).
REQUIRED_SYMBOLS = {
    "database.py": [("def get_google_token", 1),
                    ("def set_sentinel_state", 1),
                    ("def get_sentinel_state", 1),
                    ("def log_action", 1),
                    ("def recent_actions", 1)],
    "connectors.py": [
        ("class GoogleConnector", 1),
        ("def active_tools", 1),
        ("def run", 1),
        ("calendar_upcoming_v2", 1),
        ("calendar_create_event", 1),
        ("gmail_recent", 1),
        ("gmail_draft", 1),
        ("notion_query_database", 1),
        ("notion_create_lead", 1),
        ("energy_log", 1),
        ("docs_create", 1),
        ("sheets_create", 1),
        ('"timezone": {"type": "string"', 1),
    ],
    "web_api.py": [("ThreadingHTTPServer", 1), ("def start", 1),
                   ("Dashboard API", 1), ("/api/health", 1)],
    "ai.py": [("CURRENT DATE AND TIME", 1), ("def _enforce_link_integrity", 1),
              ("HARD ANTI FABRICATION RULES", 1),
              ("def _enforce_action_integrity", 1),
              ("def _log_write_action", 1),
              ("VERIFIED ACTION LEDGER", 3)],
    "bot.py": [("def main", 1), ("boot_selftest", 1), ("sentinel_watchdog", 1)],
    "sentinel.py": [("def run_diagnostics", 1), ("def run_watchdog", 1),
                    ("def boot_selftest", 1), ("def send_ops_alert", 1),
                    ("def _c_elevenlabs_stt", 1)],
    "handlers.py": [("def sentinel_cmd", 1), ("def voice_note_handler", 1),
                    ("filters.VOICE", 1)],
    # v6.6: the scheduled briefs MUST stay on the live Google path (the
    # 2026-07-28 EOD said "nothing scheduled" on a back to back day because this
    # file quietly still used the retired ICS connector). v6.7: delivery is
    # Telegram only; Slack is read, never posted to, from this file.
    "slack_rhythm.py": [("calendar_upcoming_v2", 1), ("GoogleConnector", 1),
                        ("UNAVAILABLE", 3), ("gmail_recent", 1),
                        ("def _alert_operator", 1), ("def _send_telegram", 1),
                        ("def _slack_section", 1)],
}

# Symbols that must NOT appear anywhere in a file: a reappearing forbidden symbol
# means a stale copy of the file (pre v6.6/v6.7) is about to ship. file -> [needle].
FORBIDDEN_SYMBOLS = {
    "slack_rhythm.py": ["CalendarConnector", "calendar_upcoming\"", "slack_post_message"],
    "web_api.py": ["CalendarConnector", "calendar_upcoming\""],
    "connectors.py": ["class CalendarConnector"],
}


def pass_ast() -> None:
    files = sorted(glob.glob(os.path.join(HERE, "*.py")))
    if not files:
        _fail("no .py files found to check.")
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                src = f.read()
            ast.parse(src, filename=path)
        except SyntaxError as e:
            _fail(f"syntax/truncation error in {os.path.basename(path)}: line {e.lineno}: {e.msg}")
        except Exception as e:  # noqa: BLE001
            _fail(f"could not read/parse {os.path.basename(path)}: {e}")
    print(f"[selfcheck] AST parse OK ({len(files)} files).")


def pass_compile() -> None:
    files = sorted(glob.glob(os.path.join(HERE, "*.py")))
    for path in files:
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as e:
            _fail(f"py_compile failed for {os.path.basename(path)}: {e}")
        except Exception as e:  # noqa: BLE001
            _fail(f"py_compile error for {os.path.basename(path)}: {e}")
    print(f"[selfcheck] py_compile OK ({len(files)} files).")


def pass_symbols() -> None:
    for fname, needles in REQUIRED_SYMBOLS.items():
        path = os.path.join(HERE, fname)
        if not os.path.exists(path):
            _fail(f"required file missing: {fname}")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        for needle, want in needles:
            got = src.count(needle)
            if got < want:
                _fail(f"{fname}: expected >= {want} of '{needle}', found {got} "
                      f"(file may be truncated or a symbol was lost).")
    for fname, needles in FORBIDDEN_SYMBOLS.items():
        path = os.path.join(HERE, fname)
        if not os.path.exists(path):
            _fail(f"required file missing: {fname}")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        for needle in needles:
            if needle in src:
                _fail(f"{fname}: forbidden symbol '{needle}' present. A stale "
                      "pre v6.6 copy of this file is about to ship. Refuse.")
    print(f"[selfcheck] critical symbols OK ({len(REQUIRED_SYMBOLS)} files, "
          f"{len(FORBIDDEN_SYMBOLS)} forbidden-symbol files).")


def pass_yaml() -> None:
    try:
        import yaml
    except Exception as e:  # noqa: BLE001
        _fail(f"PyYAML not importable: {e}")
    cfg_files = sorted(glob.glob(os.path.join(HERE, "config", "*.yaml")))
    if not cfg_files:
        _fail("no config/*.yaml files found.")
    for path in cfg_files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                yaml.safe_load(f)
        except Exception as e:  # noqa: BLE001
            _fail(f"invalid YAML in config/{os.path.basename(path)}: {e}")
    print(f"[selfcheck] YAML valid ({len(cfg_files)} files).")


def pass_settings() -> None:
    """The action chain budget must stay at 3000: an 800 token budget truncates
    multi tool chains and is exactly what pushed the model into fabricating."""
    import yaml
    path = os.path.join(HERE, "config", "settings.yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    mt = (data.get("ai") or {}).get("max_tokens")
    if mt != 3000:
        _fail(f"settings.yaml ai.max_tokens must be 3000, found {mt}.")
    print("[selfcheck] settings OK (ai.max_tokens 3000).")


def pass_imports() -> None:
    # Only the import safe core. handlers.py / bot.py pull in python-telegram-bot,
    # which may be absent in a bare staging shell; py_compile already vetted them.
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    for mod in ("connectors", "database", "sentinel", "web_api", "config_loader"):
        try:
            __import__(mod)
        except Exception as e:  # noqa: BLE001
            _fail(f"import {mod} failed: {e}")
    print("[selfcheck] imports OK (connectors, database, sentinel, web_api, config_loader).")


def main() -> None:
    print(f"[selfcheck] target: {HERE}")
    pass_ast()
    pass_compile()
    pass_symbols()
    pass_yaml()
    pass_settings()
    pass_imports()
    print("SELFCHECK PASS")


if __name__ == "__main__":
    main()
