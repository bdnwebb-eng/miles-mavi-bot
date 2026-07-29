# Miles: system architecture and invariants (v6.6, 2026-07-28)

One brain, many mouths. Everything below exists to keep that literally true.

## The map

    Kas ── Telegram (@MilesMaviBot, voice in/out) ─┐
    Kas ── Slack #agenda (07:40 agenda, 18:30 EOD) ─┤
    Team ─ Slack mentions + DMs (Socket Mode) ──────┼─► ONE worker process on Railway
    Kas ── Dashboard PWA (Netlify, live JSON feed) ─┤    (bot.py: PTB polling + JobQueue
    Kas ── WhatsApp daily digest (Cowork task) ─────┘     + web_api daemon thread)
                                                              │
                                            ONE connector layer (connectors.py)
                                                              │
                    ┌──────────────┬──────────────┬───────────┴──┬─────────────┐
                 Google OAuth    Notion REST    Slack Web API   ElevenLabs   Anthropic
                 (Gmail draft-only, (board read +  (post + read,   (TTS + STT)  (claude-sonnet)
                  Calendar r/w,      1-prop write)   internal only)
                  Drive/Docs/Sheets/Slides)

State lives in ONE place: sqlite at /data/hermes.db on the Railway volume
(worker-volume). Google refresh token, memories, energy scores, prefs, the
verified action ledger, sentinel state. Survives restarts AND redeploys.

## The invariants (break one of these and you recreate an old incident)

1. ONE DATA PATH PER SOURCE. Calendar and email = GoogleConnector only.
   The 2026-07-28 EOD incident happened because a second, stale calendar path
   (ICS feeds) still fed the scheduled posts. That class is deleted; selfcheck
   refuses to ship a file that reintroduces it (FORBIDDEN_SYMBOLS).
2. EVERY SURFACE READS LIVE OR SAYS SO. Interactive replies, Slack posts and
   the dashboard all pull at answer time. A source that cannot be read yields
   an explicit UNAVAILABLE marker / complete:false, never silence: the model
   and the dashboard must say "could not verify", never invent an empty state.
3. NO CLAIM WITHOUT PROOF. Truth gate v6.4: every successful write lands in the
   action ledger with a proof link; every prompt carries the ledger; a
   deterministic backstop corrects done-claims with an empty ledger; the link
   gate strips links that did not come from this turn's tool results.
4. THREE COPIES, ZERO DRIFT. Deployed bytes == GitHub main == the Windows
   folder `AI Consultancy/Kasia Bordier/Miles/telegram-bot/`. The Railway
   service is CONNECTED to the repo (a push to main redeploys), so a stale repo
   is a loaded gun. Any deploy ends with the repo commit and a blob-sha check.
5. FAILURES ALERT THE OPERATOR. Sentinel (15 checks, 30 min watchdog + boot
   self test) alerts Brandon, never Kas. v6.6 adds: a Slack rhythm post that
   fails to publish alerts Brandon immediately (_alert_operator).
6. KAS NEVER SEES THE MACHINERY. No build mechanics, no blame, no pricing in
   Slack, financial wall (Kas/India/Ollie only) enforced in code.
7. DRAFT MODE IS ABSOLUTE. Gmail is draft only in code. Calendar writes are
   readback verified with proof links and idempotency guarded. Drive deletes
   are trash only.

## The one deploy pipeline (never any other way)

    1. Stage a CLEAN copy in /tmp (never deploy from the mount).
    2. ast.parse every changed .py; python3 selfcheck.py must print SELFCHECK PASS
       (AST + py_compile + required symbols + FORBIDDEN symbols + YAML + imports).
    3. RAILWAY_TOKEN=<from .env> railway up --service worker --detach  (from /tmp copy)
    4. Poll the deployment to SUCCESS; GET /health; GET /api/health?key= must be
       15/15 green.
    5. Commit the EXACT deployed bytes to GitHub main (Composio
       GITHUB_COMMIT_MULTIPLE_FILES, one atomic commit) and verify blob shas
       against local git hash-object. The repo push may itself trigger a Railway
       rebuild of the same bytes; that is fine and expected.
    6. Sync the same files back to the Windows folder.

## Monitoring coverage matrix

| Failure class | Guard |
|---|---|
| Truncated/corrupt file ships | selfcheck AST + py_compile + symbol counts |
| Stale data path resurrected | selfcheck FORBIDDEN_SYMBOLS |
| Google token dead / API disabled | Sentinel google_token + gmail_api + calendar_api (RED alert within 30 min) |
| Calendar visibility shrinks | Sentinel calendar forward probe (45 day, all calendars, count reported) |
| Notion/Slack/Telegram/Anthropic/ElevenLabs outage | Sentinel per-service checks |
| Worker hang or crash | Railway healthcheck /health + restart ALWAYS (10 retries) + Sentinel watchdog |
| Slack rhythm post fails to publish | v6.6 _alert_operator -> Telegram to Brandon |
| Model invents an action | Truth gate: ledger + link gate + deterministic backstop |
| Model invents an empty day / empty inbox | v6.5 grounding: live sections or explicit UNAVAILABLE, prompt forbids unverified empty-state claims |
| Repo/deploy drift | Pipeline step 5 (sha verify); repo is the auto-deploy source, so it is never allowed to lag |
| Dashboard shows stale as live | complete flags + "Snapshot, not live" states; hardcoded KPIs banned (podcasts tile hides when no live source) |

## Known accepted tradeoffs (conscious, not accidents)

- The dashboard embeds a READ ONLY status key in public HTML. The payload is
  client safe by construction (titles/aggregates, no bodies, no pricing). If
  Kas ever wants it locked, add Netlify Identity or a PIN gate; do not bake
  more data into the payload instead.
- The WhatsApp digest rides Brandon's Chrome session (a Cowork scheduled task,
  not worker code). If the session unlinks, the digest flags RELINK NEEDED.
- The Google refresh token dies only if Kas changes her Google password or
  revokes access. Sentinel goes RED within 30 min; recovery is one
  /connectgoogle.

## Deleted, do not resurrect

- ICS CalendarConnector + CALENDAR_ICS_URLS (v6.6): one stale feed masquerading
  as "the calendar". calendar_upcoming_v2 reads every calendar live.
- handlers_v41new.py + config/knowledge_v41new.yaml: an abandoned experiment
  that shipped inside deploy images for weeks. Archived out of the folder.
- The repo's stray telegram-bot/ subfolder of ancient copies (deleted in the
  v6.6 commit).

## Where things are

- Railway: project miles-mavi 8c709cbf / env production e0c04fcf / service
  worker 31b97fbe / volume worker-volume at /data. Worker URL
  https://worker-production-1a3d.up.railway.app (/health, /api/health, /api/status).
- Repo: github.com/bdnwebb-eng/miles-mavi-bot (private, connected to Railway).
- Dashboard: https://miles-mavi-dashboard.netlify.app (Netlify site 5b8c416e,
  source dashboard/index.html in the Miles folder, PWA installable).
- Canonical folder: AI Consultancy/Kasia Bordier/Miles/telegram-bot/ (+ .env
  with RAILWAY_TOKEN; never committed).
- Tests: telegram-bot/test_rhythm_fix.py (offline, mocked) and
  Miles/test-harness-2026-07-25/ (14k suite, rerunnable).
