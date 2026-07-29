# Wiring Miles into real accounts

Connectors are env gated: set the variables on Railway, redeploy, and the
matching tools switch on automatically. `/connectors` in Telegram shows live
status. No code changes needed.

RULE (v6.6): there is exactly ONE data path per source. Google OAuth is THE
calendar and email path. Notion REST is THE board path. Slack Web API is THE
Slack path. Never add a second path to the same data (the retired ICS calendar
feed is the cautionary tale: it served one stale feed next to the live read and
produced a false "nothing scheduled" EOD on 2026-07-28).

## Google (Gmail + Calendar + Drive + Docs + Sheets + Slides) - PRIMARY
| Variable | Value |
|---|---|
| GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET | Desktop OAuth client on Cloud project miles-502613 (INTERNAL consent, maviliving.com org) |
| (token) | refresh token stored in the google_auth table on the Railway volume via /connectgoogle; survives redeploys |

Gives Miles 19 tools: gmail_recent/read/draft (DRAFT ONLY, never sends),
calendar_upcoming_v2 (live, merged across ALL calendars, up to 800 days) +
calendar_create_event (readback verified, idempotency guarded) +
calendar_delete_event (confirm first), drive_search/create_folder/move/rename/
trash (trash only), docs/sheets/slides create + edit. The scheduled Slack posts
and the dashboard feed use these same tools; nothing in the system reads
calendar or email any other way.

## Notion
| Variable | Value |
|---|---|
| NOTION_API_KEY | secret from Kas's own "Miles" internal integration |
| NOTION_PROJECTS_DB_ID | 9eb26e7d492c4f489344e19725eadde3 (projects board) |

Gives Miles: notion_search, notion_read_page, notion_query_database, and one
scoped write: notion_update_property (one property per call, always announced).
Cold scan: daily 07:35 Geneva via notion_watch.py. Optional NOTION_ACTIVITY_PROP
and COLD_DAYS (default 7).

## Slack (internal team only, never client facing)
| Variable | Value |
|---|---|
| SLACK_BOT_TOKEN | app "Miles" A0BGP2Q9JCS in maviliving.slack.com, bot U0BGSQ58ETW |
| SLACK_APP_TOKEN | xapp token (connections:write) for Socket Mode mentions + DMs |
| SLACK_AGENDA_CHANNEL | C0B6ZD7R9DY (the private #agenda channel, India's rhythm channel) |

Jobs: morning agenda 07:40 + EOD summary 18:30 Geneva (slack_rhythm.py, LIVE
data only, explicit UNAVAILABLE markers, ops alert to Brandon if a post fails).
Mentions + DMs answered via slack_socket.py. Pricing is hard blocked in Slack.

## Email over IMAP (OPTIONAL side accounts, not wired)
Only for operations@ / projects@ maviliving if ever wanted; kas@ is fully
covered by Google OAuth. EMAIL_ACCOUNTS JSON (or the single account vars
EMAIL_IMAP_HOST / EMAIL_ADDRESS / EMAIL_APP_PASSWORD). Read only.

## WhatsApp
Kas's linked device session "Miles" lives in Brandon's Chrome. Daily digest =
Cowork scheduled task miles-whatsapp-digest-daily (06:45 Geneva, read only,
Cruz chats + Polish reno group excluded). Not a bot connector.

## Memory
Long term memory lives in the memories table of hermes.db at
HERMES_DB_PATH=/data/hermes.db (Railway volume worker-volume, survives
restarts and redeploys). Managed with /memories, /remember, /forget.

## Retired
- ICS calendar feeds (CALENDAR_ICS_URLS): class deleted in v6.6, env var dead.
  Do not resurrect; calendar_upcoming_v2 covers every calendar live.

## Adding a connector later (Instagram, ...)
Subclass Connector in connectors.py (configured / tools / run), append to
CONNECTORS, add its Sentinel check, and give it exactly one data path. Reads
stay read only; drafts stay drafts until Kas approves.
