# Korea DevOps Job-Alert Bot

Checks job boards every 30 minutes (free, via GitHub Actions) and sends you
Telegram/email alerts for NEW jobs matching your filters. Jobs that mention
Korean-language requirements get a ⚠️ flag; ones mentioning visa/relocation get ✅.

## Setup (about 15 minutes)

### 1. Create a Telegram bot (fastest alerts)
1. In Telegram, message **@BotFather** -> `/newbot` -> copy the **bot token**.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}` = your **chat id**.

### 2. (Optional) Email via Gmail
Enable 2-Step Verification, then create an **App Password**
(Google Account -> Security -> App passwords). Use it as `SMTP_PASS`.

### 3. Put the project on GitHub
Create a new repo, upload all files (including the `.github` folder).
Public repo = unlimited free Actions minutes. Private repo = 2,000 free
min/month, so change the cron to hourly (`0 * * * *`).

### 4. Add secrets
Repo -> Settings -> Secrets and variables -> Actions -> New repository secret:

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from BotFather |
| `TELEGRAM_CHAT_ID` | your chat id |
| `SMTP_USER` | your Gmail address (optional) |
| `SMTP_PASS` | Gmail app password (optional) |
| `EMAIL_TO` | where to receive alerts (optional) |

### 5. Run it
Actions tab -> "Job alerts" -> **Run workflow**. Check the logs, and you should
get your first alerts. After that it runs automatically.

## Covering Wanted, Saramin, LinkedIn, JobKorea
These sites block bots, so the bot does not scrape them. Use the Google Alerts
RSS trick (see `config.yaml`): create alerts such as
`site:wanted.co.kr DevOps`, `site:saramin.co.kr 데브옵스`,
`site:linkedin.com/jobs DevOps Seoul`, set delivery to **RSS feed**, and paste
the feed URLs under `sources.rss`. Also keep each site's own built-in alerts on.

## Test locally
```
pip install -r requirements.txt
python job_alert_bot.py --dry-run
```

## Notes
- Company tokens in `config.yaml` must be valid; bad ones only print a warning.
  Add more companies by finding their Greenhouse/Lever/Ashby board name.
- If GitHub pauses the schedule after long inactivity, re-enable it in the Actions tab.
- To be less strict, add more words to `title_include`; to be stricter, set
  `hide_if_korean_required: true`.
