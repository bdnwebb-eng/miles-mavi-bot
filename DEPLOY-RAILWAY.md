# Deploy Miles to Railway (10 minutes)

1. **GitHub:** create a PRIVATE repo (suggest `miles-mavi-bot`). Upload everything in this
   `telegram-bot/` folder EXCEPT `.env`. Rename `gitignore.txt` to `.gitignore` first.
2. **Railway:** railway.app → New Project → Deploy from GitHub repo → pick `miles-mavi-bot`.
3. **Variables** (Railway → your service → Variables):
   - `TELEGRAM_BOT_TOKEN` = value from `.env`
   - `ANTHROPIC_API_KEY` = your key
   - `ELEVENLABS_API_KEY` = optional, for voice notes
4. **Start command:** Railway reads the `Procfile` (`worker: python bot.py`). If it asks,
   set Start Command to `python bot.py`.
5. Deploy. Open t.me/MilesMaviBot → /start → it should answer with the laptop closed.
6. Lock to Kas: add her numeric Telegram ID to `config/settings.yaml` → `access.allowed_ids`,
   push, Railway redeploys.

Polling based: no domain, webhook, or port config needed.
