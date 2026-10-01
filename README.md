# Korea Job Search Agent (alert bot)

Checks job boards every 30 minutes (free, GitHub Actions) and sends Telegram/email
alerts for NEW jobs, **freshest first**, with a score and requirement summary.

## What it checks for every job
- **Location:** all of South Korea (Seoul, Busan, Pangyo, Daejeon, etc.).
- **Relevance (not just title):** title family (DevOps/SRE, Automation/Network, Backend, AI/MLOps)
  OR strong skill match in the description. Backend titles only count if the stack
  matches (Python/Kubernetes + 3 backend skills).
- **Score 0-100:** weighted skills (Python, Kubernetes, Terraform, CI/CD...), plus bonuses
  (English-friendly, visa support, experience fit, fresh, target company) and penalties
  (Korean required, lead/manager titles). Skills you lack (AWS, Helm...) show as **Gaps**.
- **Freshness:** only jobs posted in the last **10 days** are alerted: 🔥 <24h first, then 🟢 up to 2 days, 🟡 up to 5 days, 🟠 up to 10 days. Also 🚨 DEADLINE SOON (<72h), ⚠️ MAY CLOSE EARLY.
- **Visa:** Confirmed / Likely / Not mentioned / Not available.
- **Korean:** Required / Preferred / Not required / Likely needed (JD in Korean) / Not mentioned.
- **English:** Required / Preferred / English JD / Not mentioned.
- **Priority:** 🔥 APPLY IMMEDIATELY, 🟢 APPLY TODAY, 🟡 REVIEW, ⚪ BACKUP.
- **Experience:** only jobs asking **2 to 6 years** are kept (7+/10+ are removed). Change in `config.yaml`.
- **Auto-excluded:** interns/juniors, unrelated stacks, "no visa sponsorship",
  Korean fluency explicitly mandatory (switch off in `config.yaml`).

## Files
- `job_alert_bot.py` main bot
- `config.yaml` all filters, skills, weights, companies, sources (edit this)
- `check_sources.py` test a company: `python3 check_sources.py greenhouse coupang`
- `.github/workflows/job-alerts.yml` schedule (create it on GitHub if the folder is missing)

## Setup
1. Telegram: @BotFather -> `/newbot` -> token; message the bot; open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` for your chat id.
2. Add GitHub secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   (optional email: `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO`).
3. Actions tab -> Job alerts -> Run workflow.

## Covering Wanted / Saramin / JobKorea / Jumpit / LinkedIn
They block bots, so use Google Alerts RSS (queries listed in `config.yaml`) and paste the
feed URLs under `sources.rss`. Alerts from RSS have little text, so they are scored mostly by title.

## Local test
```
pip install -r requirements.txt
python3 job_alert_bot.py --dry-run
python3 check_sources.py
```
