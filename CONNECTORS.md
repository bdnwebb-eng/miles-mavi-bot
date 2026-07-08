# Wiring Miles into real accounts

Connectors are READ ONLY and env gated: set the variables on Railway, redeploy,
and the matching tools switch on automatically. `/connectors` in Telegram shows
live status. No code changes needed.

## Email (any IMAP host, incl. Gmail app passwords)
| Variable | Example |
|---|---|
| EMAIL_IMAP_HOST | imap.gmail.com |
| EMAIL_ADDRESS | kas@maviliving.com |
| EMAIL_APP_PASSWORD | (16 char Gmail app password from the 1Password vault) |
| EMAIL_IMAP_PORT | 993 (optional, default) |

Gives Miles: email_recent (list inbox) and email_read (read one message).

## Notion
| Variable | Value |
|---|---|
| NOTION_API_KEY | secret from a Notion internal integration shared with Kas's PM pages |

Gives Miles: notion_search and notion_read_page.

## Memory
Long term memory lives in the memories table of hermes.db. Set
HERMES_DB_PATH=/data/hermes.db with a Railway volume mounted at /data so it
survives restarts and redeploys. Miles distills new durable facts automatically
every few exchanges; manage by hand with /memories, /remember, /forget.

## Adding more connectors later (calendar, Slack, Instagram, Dispatch)
Subclass Connector in connectors.py (configured / tools / run), append to
CONNECTORS. Keep them read only; drafts stay drafts until Kas approves.
