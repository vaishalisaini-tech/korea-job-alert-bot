#!/usr/bin/env python3
"""
Korea DevOps job-alert bot.

Polls public job-board APIs / RSS feeds / career pages, filters by your
keywords + Korea locations, remembers what it already sent (seen.json),
and notifies you via Telegram and/or email only about NEW matches.

Usage:
    python job_alert_bot.py            # normal run
    python job_alert_bot.py --dry-run  # print matches, don't notify or save state
"""
import argparse
import hashlib
import html
import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

import requests
import yaml

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "seen.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal job alert bot)"}
TIMEOUT = 25
KEEP_DAYS = 180

KOREAN_REQ = re.compile(
    r"(korean\s+(language\s+)?(fluen|proficien|native|required|speaker)|"
    r"fluent\s+(in\s+)?korean|business[- ]level\s+korean|topik|한국어|국어)",
    re.I,
)
VISA_OK = re.compile(r"(visa\s+sponsor|sponsorship|relocation)", re.I)


# ----------------------------------------------------------------- helpers
def log(msg):
    print(msg, flush=True)


def strip_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def kw_match(text, keywords):
    """Word-boundary match for ASCII keywords, substring match for Korean."""
    text = (text or "").lower()
    for k in keywords:
        k = k.lower()
        if k.isascii():
            if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", text):
                return True
        elif k in text:
            return True
    return False


def get_json(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- fetchers
def fetch_greenhouse(token):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    for j in data.get("jobs", []):
        yield {
            "id": f"gh:{token}:{j['id']}",
            "title": j.get("title", ""),
            "company": token,
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "text": strip_html(j.get("content", "")),
        }


def fetch_lever(company):
    data = get_json(f"https://api.lever.co/v0/postings/{company}?mode=json")
    for j in data:
        cats = j.get("categories") or {}
        loc = " | ".join([cats.get("location") or ""] + (cats.get("allLocations") or []))
        yield {
            "id": f"lv:{company}:{j['id']}",
            "title": j.get("text", ""),
            "company": company,
            "location": loc,
            "url": j.get("hostedUrl", ""),
            "text": j.get("descriptionPlain", "") or "",
        }


def fetch_ashby(name):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{name}")
    for j in data.get("jobs", []):
        secondary = " | ".join(
            s.get("location", "") if isinstance(s, dict) else str(s)
            for s in (j.get("secondaryLocations") or [])
        )
        yield {
            "id": f"ab:{name}:{j['id']}",
            "title": j.get("title", ""),
            "company": name,
            "location": f"{j.get('location', '')} | {secondary}",
            "url": j.get("jobUrl", ""),
            "text": j.get("descriptionPlain", "") or "",
        }


def fetch_smartrecruiters(company):
    data = get_json(
        f"https://api.smartrecruiters.com/v1/companies/{company}/postings",
        params={"country": "kr", "limit": 100},
    )
    for j in data.get("content", []):
        loc = j.get("location") or {}
        yield {
            "id": f"sr:{company}:{j['id']}",
            "title": j.get("name", ""),
            "company": company,
            "location": f"{loc.get('city', '')}, {loc.get('country', '')}",
            "url": f"https://jobs.smartrecruiters.com/{company}/{j['id']}",
            "text": "",
        }


def _real_url(link):
    """Google Alerts wraps links in a google.com/url?url=... redirect."""
    try:
        q = parse_qs(urlparse(link).query)
        if "url" in q:
            return q["url"][0]
    except Exception:
        pass
    return link


def fetch_rss(feed):
    r = requests.get(feed["url"], headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    name = feed.get("name", "rss")
    entries = root.findall("a:entry", ns)
    if entries:  # Atom (Google Alerts)
        for e in entries:
            title = strip_html(e.findtext("a:title", "", ns))
            link_el = e.find("a:link", ns)
            link = _real_url(link_el.get("href", "") if link_el is not None else "")
            yield {
                "id": f"rss:{name}:{e.findtext('a:id', link, ns)}",
                "title": title,
                "company": name,
                "location": "",
                "url": link,
                "text": strip_html(e.findtext("a:content", "", ns)),
                "skip_location": True,
            }
    else:  # RSS 2.0
        for it in root.iter("item"):
            link = it.findtext("link", "")
            yield {
                "id": f"rss:{name}:{it.findtext('guid', link)}",
                "title": strip_html(it.findtext("title", "")),
                "company": name,
                "location": "",
                "url": link,
                "text": strip_html(it.findtext("description", "")),
                "skip_location": True,
            }


def check_page(page, state):
    """Watch a static careers page; return a pseudo-job if its text changed."""
    url = page["url"]
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    digest = hashlib.sha256(strip_html(r.text).encode()).hexdigest()
    old = state["pages"].get(url)
    state["pages"][url] = digest
    if old and old != digest:
        return {
            "id": f"page:{url}:{digest[:12]}",
            "title": f"Careers page changed: {page.get('name', url)}",
            "company": page.get("name", url),
            "location": "",
            "url": url,
            "text": "",
            "page_change": True,
        }
    return None


# ------------------------------------------------------------------ filter
def passes(job, f):
    if job.get("page_change"):
        return True
    if not job.get("skip_location") and not kw_match(job["location"], f["locations"]):
        return False
    if not kw_match(job["title"], f["title_include"]):
        return False
    if kw_match(job["title"], f.get("title_exclude", [])):
        return False
    if f.get("hide_if_korean_required") and KOREAN_REQ.search(job["text"]):
        return False
    return True


def flags(job):
    out = []
    if KOREAN_REQ.search(job["text"]):
        out.append("⚠️ Korean may be required")
    if VISA_OK.search(job["text"]):
        out.append("✅ mentions visa/relocation")
    return out


# ---------------------------------------------------------------- notifiers
def fmt_job_html(j):
    t = html.escape(j["title"])
    line = f'• <a href="{html.escape(j["url"], quote=True)}">{t}</a>'
    meta = " — ".join(x for x in [html.escape(j["company"]), html.escape(j["location"].strip(" |,"))] if x)
    if meta:
        line += f"\n   {meta}"
    fl = flags(j)
    if fl:
        line += "\n   " + " · ".join(fl)
    return line


def send_telegram(jobs):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return None
    chunks, cur = [], "🇰🇷 <b>New job matches</b>\n\n"
    for j in jobs:
        block = fmt_job_html(j) + "\n\n"
        if len(cur) + len(block) > 3800:
            chunks.append(cur)
            cur = ""
        cur += block
    chunks.append(cur)
    for c in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": c, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=TIMEOUT,
        )
        if not r.ok:
            log(f"Telegram error: {r.status_code} {r.text[:200]}")
            return False
        time.sleep(1)
    return True


def send_email(jobs):
    user, pw, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"), os.getenv("EMAIL_TO")
    if not (user and pw and to):
        return None
    body = "<h3>New job matches</h3>" + "<br><br>".join(
        fmt_job_html(j).replace("\n", "<br>") for j in jobs
    )
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = f"[Job Alert] {len(jobs)} new match(es) in Korea"
    msg["From"], msg["To"] = user, to
    try:
        with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), 465, timeout=TIMEOUT) as s:
            s.login(user, pw)
            s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())
        return True
    except Exception as e:
        log(f"Email error: {e}")
        return False


# -------------------------------------------------------------------- main
def load_state():
    if STATE_PATH.exists():
        try:
            s = json.loads(STATE_PATH.read_text())
            s.setdefault("jobs", {})
            s.setdefault("pages", {})
            return s, False
        except Exception:
            pass
    return {"jobs": {}, "pages": {}}, True


def collect(cfg, state):
    src = cfg.get("sources", {})
    fetchers = [
        ("greenhouse", fetch_greenhouse),
        ("lever", fetch_lever),
        ("ashby", fetch_ashby),
        ("smartrecruiters", fetch_smartrecruiters),
    ]
    for key, fn in fetchers:
        for name in src.get(key) or []:
            try:
                yield from fn(name)
            except Exception as e:
                log(f"[warn] {key}/{name}: {e}")
    for feed in src.get("rss") or []:
        try:
            yield from fetch_rss(feed)
        except Exception as e:
            log(f"[warn] rss/{feed.get('name')}: {e}")
    for page in src.get("pages") or []:
        try:
            j = check_page(page, state)
            if j:
                yield j
        except Exception as e:
            log(f"[warn] page/{page.get('name')}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    state, first_run = load_state()

    matches = {}
    for job in collect(cfg, state):
        if passes(job, cfg["filters"]):
            matches[job["id"]] = job
    log(f"Matched {len(matches)} job(s) after filtering.")

    new = [j for jid, j in matches.items() if jid not in state["jobs"]]
    if first_run:
        cap = cfg.get("max_first_run_alerts", 30)
        log(f"First run: {len(new)} existing matches; sending at most {cap}.")
        new = new[:cap]

    if args.dry_run:
        for j in new:
            print(re.sub(r"<[^>]+>", "", fmt_job_html(j)), "\n")
        log("Dry run: nothing sent, state not saved.")
        return 0

    ok = True
    if new:
        results = [send_telegram(new), send_email(new)]
        configured = [r for r in results if r is not None]
        if not configured:
            log("No notifier configured (set Telegram or SMTP secrets). State not saved.")
            return 1
        ok = all(configured)
        log(f"Sent {len(new)} alert(s)." if ok else "Some notifications failed; will retry next run.")

    if ok:
        now = int(time.time())
        for jid in matches:
            state["jobs"].setdefault(jid, now)
        cutoff = now - KEEP_DAYS * 86400
        state["jobs"] = {k: v for k, v in state["jobs"].items() if v >= cutoff}
        STATE_PATH.write_text(json.dumps(state, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
