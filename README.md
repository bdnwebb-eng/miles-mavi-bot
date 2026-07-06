# AI Clone — Telegram Bot Template

An always-on Telegram bot that talks in one specific person's voice, walks users
through that person's program, keeps them accountable with reminders, and serves
their resources. **100% config-driven** — you clone a new person by editing the
files in `config/`, never the code.

## The 6 files you edit (in config/)

| File | What it holds |
|------|---------------|
| `persona.yaml` | **The clone.** Voice, backstory, framework, tone, guardrails. Most important file. |
| `knowledge.yaml` | Real facts, frameworks, numbers, and verbatim quotes injected into every answer. |
| `examples.yaml` | 3-5 few-shot Q/A pairs that lock in the voice. |
| `program.yaml` | The lesson-by-lesson curriculum. |
| `resources.yaml` | Real links the bot hands out. |
| `settings.yaml` | Model, bot name, timezone, reminders, access gating. |

A fully filled-in real example lives in `../examples/felipe-config/`. Copy its
structure, swap in your person.

## The code (rarely touched)

```
bot.py            entry point: handlers + reminder scheduler + polling
handlers.py       commands, menus, program, resources, accountability, coaching router
ai.py             builds the persona system prompt and calls Claude
database.py       SQLite: users, progress, goals, check-ins, chat memory
config_loader.py  loads the YAML configs
```

## Run it (test on your computer)

```bash
pip install -r requirements.txt
cp .env.example .env      # then paste your two keys into .env
python bot.py             # you should see the bot start
```

Two secrets go in `.env`: `TELEGRAM_BOT_TOKEN` (from @BotFather) and
`ANTHROPIC_API_KEY` (from console.anthropic.com).

Full non-technical setup + always-on hosting: see the skill's
`references/05-go-live.md`.
