#!/usr/bin/env python3
"""
Korea Job Search Agent (alert bot).

Polls job-board APIs / RSS feeds / careers pages, then for every job:
  * checks location (all of South Korea)
  * matches by title family OR by skills found in the description
  * computes a 0-100 technical match score + gaps
  * detects visa / Korean / English / experience requirements
  * ranks by FRESHNESS first, then score
  * sends only NEW jobs to Telegram and/or email

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
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
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

FAMILY_LABEL = {
    "ai_infra": "AI / MLOps Infrastructure",
    "automation_network": "Automation / Network",
    "devops_sre": "DevOps / SRE / Platform",
    "backend": "Backend",
}
FAMILY_ORDER = ["ai_infra", "automation_network", "devops_sre", "backend"]
DISPLAY = {
    "ci/cd": "CI/CD", "sre": "SRE", "gcp": "GCP", "aws": "AWS", "mlops": "MLOps",
    "llm": "LLM", "rag": "RAG", "rest api": "REST APIs", "fastapi": "FastAPI",
    "postgresql": "PostgreSQL", "mysql": "MySQL", "pytest": "PyTest",
    "gitlab ci": "GitLab CI", "argocd": "ArgoCD", "gitops": "GitOps", "ssh": "SSH",
    "netmiko": "Netmiko", "ai infrastructure": "AI Infrastructure",
}

# ------------------------------------------------------------- regexes
KOREAN_NOT_REQ = re.compile(
    r"(korean|한국어)\s+(language\s+)?(skills?\s+)?(is\s+|are\s+)?(not\s+(required|necessary|needed)|optional)|"
    r"\bno\s+korean|without\s+korean|english\s+cv\s+must|"
    r"english\s+(is\s+)?(our\s+)?(primary|working|official)\s+language|english[- ]speaking\s+(environment|team|workplace)",
    re.I,
)
KOREAN_MANDATORY = re.compile(
    r"(fluent|native|business[- ]?level|proficient|advanced)\s+(in\s+)?korean|"
    r"korean\s+(language\s+)?(fluen\w+|native|proficiency|required|speaker|is\s+required|is\s+a\s+must|mandatory|skills?\s+(required|needed))|"
    r"proficiency\s+in\s+korean|native[- ]level\s+korean|korean\s+proficiency\s+appropriate|"
    r"한국어\s*(능통|필수|가능자|구사)|국어\s*능통",
    re.I,
)
SOFT = r"(preferred|plus|advantage|bonus|nice\s+to\s+have|우대)"
KOREAN_SOFT = re.compile(rf"(korean|한국어).{{0,40}}{SOFT}|{SOFT}.{{0,40}}(korean|한국어)", re.I | re.S)
ENG_REQ = re.compile(
    r"(fluent|proficient|professional|business)\s+(in\s+)?english|"
    r"english\s+(communication|proficiency|fluency|skills?)(\s+(is|are))?\s*(required|essential|a\s+must)|"
    r"english\s+(is\s+)?(required|essential|a\s+must|mandatory)|"
    r"(excellent|strong|good)\s+[\w\s]{0,20}english|영어\s*(능통|필수)|english\s+cv\s+must",
    re.I,
)
ENG_PREF = re.compile(rf"english.{{0,25}}{SOFT}|{SOFT}.{{0,25}}(english|영어)|영어.{{0,10}}우대", re.I | re.S)

VISA_NO = re.compile(
    r"no\s+(visa\s+)?sponsorship|"
    r"not\s+(able|eligible|available|provide|offer)\w*.{0,30}(visa|sponsor)|"
    r"(cannot|can't|unable\s+to|does\s+not|doesn't|do\s+not|will\s+not|won't)\s+\w*\s*(provide|offer|sponsor)\w*.{0,30}(visa|sponsorship)|"
    r"(visa|sponsorship)\s+(is\s+)?(not|un)\w*\s*(available|provided|offered)|"
    r"without\s+(visa\s+)?sponsorship|korean\s+nationals?\s+only|"
    r"외국인\s*(지원\s*불가|불가)|비자\s*(지원\s*불가|스폰서\s*불가)",
    re.I | re.S,
)
VISA_YES = re.compile(
    r"\be-?7\b|visa\s+sponsor\w*|sponsor\w*\s+(a\s+|your\s+|work\s+)*visa|"
    r"work\s+visa\s+(support|sponsor\w*)|visa\s+support|"
    r"relocation\s+(support|assistance|package|allowance)|비자\s*(지원|스폰)|\bf-?2-?7\b",
    re.I,
)
VISA_LIKELY = re.compile(
    r"foreigners?\s+welcome|international\s+candidates?|international\s+team|global\s+team|"
    r"\d+\+?\s+nationalities|외국인|open\s+to\s+(international|foreign)|diverse\s+[\w\s]{0,20}nationalit",
    re.I,
)
EARLY_CLOSE = re.compile(r"close\s+early|조기\s*마감|until\s+(the\s+)?(position|role)\s+is\s+filled|rolling\s+basis", re.I)
DATE = r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})"
DEADLINE_KW = re.compile(rf"(deadline|apply\s+by|closing\s+date|closes?|마감)\D{{0,25}}{DATE}", re.I)
DEADLINE_RANGE = re.compile(rf"{DATE}\s*~\s*{DATE}")


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
        k = str(k).lower()
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


def parse_ts(v):
    """Return epoch seconds or None."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)):
            return v / 1000 if v > 1e11 else float(v)
        s = str(v).strip()
        if re.match(r"^[A-Za-z]{3},", s):
            return parsedate_to_datetime(s).timestamp()
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def hangul_ratio(text):
    sample = text[:2000]
    letters = re.findall(r"[A-Za-z\uac00-\ud7a3]", sample)
    if not letters:
        return 0.0
    return len(re.findall(r"[\uac00-\ud7a3]", sample)) / len(letters)


def disp(skill):
    return DISPLAY.get(skill, skill.title())


# ---------------------------------------------------------------- fetchers
def fetch_greenhouse(token):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    for j in data.get("jobs", []):
        yield {
            "id": f"gh:{token}:{j['id']}",
            "title": j.get("title", ""),
            "company": j.get("company_name") or token,
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "text": strip_html(j.get("content", "")),
            "posted": parse_ts(j.get("first_published") or j.get("updated_at")),
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
            "posted": parse_ts(j.get("createdAt")),
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
            "posted": parse_ts(j.get("publishedAt")),
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
            "posted": parse_ts(j.get("releasedDate")),
        }


def _real_url(link):
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
    if entries:
        for e in entries:
            link_el = e.find("a:link", ns)
            link = _real_url(link_el.get("href", "") if link_el is not None else "")
            yield {
                "id": f"rss:{name}:{e.findtext('a:id', link, ns)}",
                "title": strip_html(e.findtext("a:title", "", ns)),
                "company": name,
                "location": "",
                "url": link,
                "text": strip_html(e.findtext("a:content", "", ns)),
                "posted": parse_ts(e.findtext("a:published", "", ns) or e.findtext("a:updated", "", ns)),
                "skip_location": True,
            }
    else:
        for it in root.iter("item"):
            link = it.findtext("link", "")
            yield {
                "id": f"rss:{name}:{it.findtext('guid', link)}",
                "title": strip_html(it.findtext("title", "")),
                "company": name,
                "location": "",
                "url": link,
                "text": strip_html(it.findtext("description", "")),
                "posted": parse_ts(it.findtext("pubDate", "")),
                "skip_location": True,
            }


def check_page(page, state):
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
            "posted": None,
            "page_change": True,
        }
    return None


# --------------------------------------------------------- analysis pieces
def match_skills(text, cfg):
    sk = cfg["skills"]
    aliases = sk.get("aliases", {})
    not_have = set(sk.get("not_have", []))
    weights = cfg["scoring"]["weights"]
    have, gaps, highest_hits, weighted, seen = [], [], 0, 0.0, set()
    for tier in ("highest", "high", "additional", "ai"):
        for s in sk.get(tier, []):
            if s in seen:
                continue
            if kw_match(text, [s] + aliases.get(s, [])):
                seen.add(s)
                if s in not_have:
                    gaps.append(s)
                    weighted += weights[tier] * 0.5
                else:
                    have.append(s)
                    weighted += weights[tier]
                    if tier == "highest":
                        highest_hits += 1
    return have, gaps, weighted, highest_hits


def matched_families(title, cfg):
    fams = cfg["titles"]["families"]
    return [f for f in FAMILY_ORDER if f in fams and kw_match(title, fams[f])]


def min_years(text):
    vals = []
    for m in re.finditer(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|~|to)\s*(\d{1,2})\s*)?(?:\+\s*)?(?:years?|yrs?)\b", text, re.I):
        vals.append(int(m.group(1)))
    for m in re.finditer(r"(\d{1,2})\s*년\s*(?:이상|~|-)", text):
        vals.append(int(m.group(1)))
    for m in re.finditer(r"경력\s*(\d{1,2})\s*년", text):
        vals.append(int(m.group(1)))
    vals = [v for v in vals if v <= 15]
    return max(vals) if vals else None


def korean_status(text):
    if KOREAN_NOT_REQ.search(text):
        return "Not required"
    m = KOREAN_MANDATORY.search(text)
    if m:
        window = text[max(0, m.start() - 80): m.end() + 80]
        return "Preferred" if re.search(SOFT, window, re.I) else "Required"
    if KOREAN_SOFT.search(text):
        return "Preferred"
    if hangul_ratio(text) > 0.3:
        return "Likely needed (JD in Korean)"
    return "Not mentioned"


def english_status(text):
    if ENG_REQ.search(text):
        return "Required"
    if ENG_PREF.search(text):
        return "Preferred"
    if len(text) > 200 and hangul_ratio(text) < 0.1:
        return "English JD (likely OK)"
    return "Not mentioned"


def visa_status(text):
    if VISA_NO.search(text):
        return "Not available"
    if VISA_YES.search(text):
        return "Confirmed"
    if VISA_LIKELY.search(text):
        return "Likely / needs verification"
    return "Not mentioned"


def find_deadline(text):
    m = DEADLINE_RANGE.search(text)
    if m:
        y, mo, d = int(m.group(4)), int(m.group(5)), int(m.group(6))
    else:
        m = DEADLINE_KW.search(text)
        if not m:
            return None, None
        y, mo, d = int(m.group(2)), int(m.group(3)), int(m.group(4))
    try:
        dt = datetime(y, mo, d, 23, 59, tzinfo=timezone.utc)
        return f"{y:04d}-{mo:02d}-{d:02d}", dt.timestamp()
    except ValueError:
        return None, None


def age_text(age_h):
    if age_h < 1:
        return "less than 1 hour ago"
    if age_h < 24:
        n = int(age_h)
        return f"{n} hour{'s' if n != 1 else ''} ago"
    n = int(age_h // 24)
    return f"{n} day{'s' if n != 1 else ''} ago"


def freshness(age_h):
    if age_h is None:
        return "🆕", "posting date unavailable"
    if age_h < 24:
        return "🔥", f"NEW — {age_text(age_h)}"
    d = age_h / 24
    emoji = "🟢" if d <= 2 else "🟡" if d <= 5 else "🟠"
    return emoji, age_text(age_h)


def priority_of(age_h, score, sc, fr):
    if age_h is not None and age_h < 24 and score >= sc["strong"]:
        return "immediate", "🔥 APPLY IMMEDIATELY"
    if age_h is not None and age_h <= fr["today_hours"] and score >= sc["good"]:
        return "today", "🟢 APPLY TODAY"
    if age_h is not None and age_h <= fr["review_hours"] and score >= sc["reasonable"]:
        return "review", "🟡 REVIEW"
    if age_h is None and score >= sc["strong"]:
        return "review", "🟡 REVIEW"
    return "backup", "⚪ BACKUP"


def why_text(ev):
    parts = []
    top = [disp(s) for s in ev["have"][:4]]
    if top:
        parts.append(f"Your {', '.join(top)} experience lines up with this {ev['category']} role.")
    else:
        parts.append(f"This {ev['category']} role is relevant to your background.")
    hv = set(ev["have"]) | set(ev["gaps"])
    extras = []
    if hv & {"ai infrastructure", "llm", "rag", "agentic", "automated remediation", "anomaly detection", "model serving", "mlops"}:
        extras.append("Your AI-SRE and MCP infrastructure projects are directly relevant.")
    if hv & {"network automation", "cisco", "huawei", "nokia", "juniper", "netmiko", "network infrastructure"}:
        extras.append("Your network-automation work at Airtel (Cisco/Huawei/Nokia/Juniper) is a strong fit.")
    m = ev["min_req"]
    if m is None:
        extras.append("No strict experience bar was found, so check the posting.")
    else:
        extras.append(f"The {m}+ year requirement fits your {ev['mine']} years of experience.")
    return " ".join(parts + extras[:2])


# -------------------------------------------------------------- evaluation
def evaluate(job, cfg, now, first_run, state):
    """Return (ev, None) if the job is relevant, else (None, reason)."""
    title, text = job["title"], job["text"]
    full = f"{title}. {text}"
    sc, ex = cfg["scoring"], cfg["experience"]

    # location
    if not job.get("skip_location") and not kw_match(job["location"], cfg["location"]["include"]):
        return None, "outside Korea"

    # hard title excludes
    if kw_match(title, cfg["titles"]["exclude"]) or kw_match(title, ex["junior_title_words"]):
        return None, "excluded title (intern/junior/unrelated)"

    have, gaps, weighted, highest_hits = match_skills(full, cfg)
    skill_count = len(have) + len(gaps)
    has_desc = len(text) > 200
    base = min(100.0, 100.0 * weighted / sc["cap"])

    fams = matched_families(title, cfg)
    category = None
    via = "title"

    if fams:
        category = FAMILY_LABEL[fams[0]]
        if fams == ["backend"]:
            bk = cfg["skills"]["backend"]
            aliases = cfg["skills"].get("aliases", {})
            cnt = sum(1 for s in bk if kw_match(full, [s] + aliases.get(s, [])))
            core = kw_match(full, ["python"] + aliases.get("python", [])) or kw_match(full, ["kubernetes"] + aliases.get("kubernetes", []))
            if has_desc and (cnt < sc["backend_min_skills"] or not core):
                return None, "backend title but stack does not match"
        if has_desc and skill_count < sc["min_skill_matches"]:
            return None, "title matches but skills do not"
    else:
        via = "skills"
        if not kw_match(title, cfg["titles"]["generic_words"]):
            return None, "not an engineering title"
        if not has_desc or base < sc["skill_only_min"] or highest_hits < sc["skill_only_min_highest"]:
            return None, "skills too weak"
        hv = set(have) | set(gaps)
        if hv & {"network automation", "cisco", "juniper", "huawei", "nokia", "netmiko"} and "network" in full.lower():
            category = FAMILY_LABEL["automation_network"]
        elif hv & {"ai infrastructure", "mlops", "llm", "model serving"}:
            category = FAMILY_LABEL["ai_infra"]
        elif hv & {"kubernetes", "terraform", "sre", "infrastructure automation", "platform engineering"}:
            category = FAMILY_LABEL["devops_sre"]
        else:
            category = FAMILY_LABEL["backend"]

    # experience
    min_req = min_years(full)
    lo, hi = ex["min_accept_years"], ex["max_accept_years"]
    if min_req is not None and min_req < lo:
        return None, f"experience too low ({min_req} yr)"
    if min_req is not None and min_req > hi:
        return None, f"experience too high ({min_req}+ yrs)"

    # language / visa
    korean = korean_status(full)
    english = english_status(full)
    visa = visa_status(full)
    if korean == "Required" and cfg["language"]["exclude_if_korean_mandatory"]:
        return None, "Korean fluency mandatory"
    if visa == "Not available" and cfg["visa"]["exclude_if_unavailable"]:
        return None, "visa/foreigners not accepted"

    # score
    score = base
    if via == "title":
        score += 5
    if min_req is not None and 3 <= min_req <= 6:
        score += 5
    if kw_match(title, ex["leadership_title_words"]):
        score += ex["leadership_penalty"]
    if english in ("Required", "Preferred", "English JD (likely OK)") and korean in ("Not required", "Not mentioned", "Preferred"):
        score += 5
    if visa == "Confirmed":
        score += 6
    elif visa == "Likely / needs verification":
        score += 3
    elif visa == "Not available":
        score -= 25
    if korean == "Required":
        score -= 20
    elif korean.startswith("Likely"):
        score -= 8
    if kw_match(job["company"], cfg.get("target_companies", [])):
        score += 3

    # freshness
    posted = job.get("posted")
    if posted:
        age_h = max(0.0, (now - posted) / 3600)
    elif first_run:
        age_h = None
    else:
        age_h = 0.0  # first detected by the bot just now
    fr = cfg["freshness"]
    if age_h is None:
        if not fr.get("include_unknown_date_on_first_run", False):
            return None, "stale: no posting date on first run"
    elif age_h > fr["max_age_days"] * 24:
        return None, f"stale: posted {int(age_h // 24)} days ago"
    if age_h is not None:
        score += 5 if age_h < 24 else 3 if age_h <= fr["today_hours"] else 0
    score = int(max(0, min(100, round(score))))

    pri_key, pri_label = priority_of(age_h, score, sc, cfg["freshness"])

    dl_str, dl_ts = find_deadline(full)
    badges = []
    if dl_ts and 0 <= dl_ts - now <= 72 * 3600:
        badges.append("🚨 DEADLINE SOON")
    if EARLY_CLOSE.search(full):
        badges.append("⚠️ MAY CLOSE EARLY")

    ev = {
        "job": job, "kind": "job", "category": category, "score": score, "age_h": age_h,
        "posted": posted, "priority": pri_key, "priority_label": pri_label,
        "have": have, "gaps": gaps, "min_req": min_req, "mine": ex["mine"],
        "korean": korean, "english": english, "visa": visa,
        "deadline": dl_str, "badges": badges,
    }
    return ev, None


# ---------------------------------------------------------------- messages
def sort_key(ev):
    return (ev["age_h"] if ev["age_h"] is not None else 1e9, -ev["score"])


def fmt_job_html(ev):
    j = ev["job"]
    e = html.escape
    if ev["kind"] == "page":
        return f'🟡 <b>{e(j["title"])}</b>\n<a href="{e(j["url"], quote=True)}">Open careers page</a>'
    emoji, ftxt = freshness(ev["age_h"] if ev["posted"] or ev["age_h"] is None else None)
    if ev["age_h"] == 0.0 and not ev["posted"]:
        emoji, ftxt = "🆕", "First detected just now"
    posted = datetime.fromtimestamp(ev["posted"], timezone.utc).strftime("%Y-%m-%d") if ev["posted"] else "n/a"
    exp = f"{ev['min_req']}+ yrs" if ev["min_req"] else "not stated"
    lines = [
        f"{emoji} <b>{e(ftxt)}</b>  |  {e(ev['priority_label'])}",
    ]
    if ev["badges"]:
        lines.append(" ".join(ev["badges"]))
    lines += [
        f"<b>{e(j['title'])}</b> — {e(j['company'])}",
        f"Category: {e(ev['category'])}",
        f"Location: {e(j['location'].strip(' |,') or 'South Korea')}",
        f"Experience: {e(exp)}",
        f"Posted: {posted}  |  Deadline: {e(ev['deadline'] or 'not stated')}",
        f"Technical Match: <b>{ev['score']}/100</b>",
        f"Visa: {e(ev['visa'])}  |  Korean: {e(ev['korean'])}  |  English: {e(ev['english'])}",
    ]
    if ev["have"]:
        lines.append("✓ " + "  ✓ ".join(e(disp(s)) for s in ev["have"][:8]))
    if ev["gaps"]:
        lines.append("Gaps: " + ", ".join(e(disp(s)) for s in ev["gaps"][:6]))
    lines.append(f"Why: {e(why_text(ev))}")
    lines.append(f'<a href="{e(j["url"], quote=True)}">Apply / view posting</a>')
    return "\n".join(lines)


def send_telegram(evs):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return None
    chunks, cur = [], f"🇰🇷 <b>{len(evs)} new job match(es)</b> (freshest first)\n\n"
    for ev in evs:
        block = fmt_job_html(ev) + "\n\n━━━━━━━━━━\n\n"
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


def send_email(evs):
    user, pw, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"), os.getenv("EMAIL_TO")
    if not (user and pw and to):
        return None
    body = "<h3>New job matches (freshest first)</h3>" + "<hr>".join(
        "<p>" + fmt_job_html(ev).replace("\n", "<br>") + "</p>" for ev in evs
    )
    msg = MIMEText(body, "html", "utf-8")
    top = evs[0]["priority_label"] if evs else ""
    msg["Subject"] = f"[Job Alert] {len(evs)} new in Korea — {top}"
    msg["From"], msg["To"] = user, to
    try:
        with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), 465, timeout=TIMEOUT) as s:
            s.login(user, pw)
            s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())
        return True
    except Exception as ex:
        log(f"Email error: {ex}")
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
    now = time.time()

    evs, stale_ids = {}, []
    for job in collect(cfg, state):
        if job.get("page_change"):
            evs[job["id"]] = {"job": job, "kind": "page", "age_h": 0.0, "score": 0,
                              "priority": "review", "priority_label": "🟡 REVIEW", "posted": None}
            continue
        ev, reason = evaluate(job, cfg, now, first_run, state)
        if ev:
            evs[job["id"]] = ev
        elif reason and reason.startswith("stale"):
            stale_ids.append(job["id"])
    log(f"Matched {len(evs)} relevant job(s) within the freshness window; {len(stale_ids)} older ones skipped.")

    allowed = set(cfg.get("notify", {}).get("send_priorities", ["immediate", "today", "review", "backup"]))
    new = [ev for jid, ev in evs.items() if jid not in state["jobs"] and ev["priority"] in allowed]
    new.sort(key=sort_key)
    if first_run:
        cap = cfg.get("max_first_run_alerts", 30)
        log(f"First run: {len(new)} existing matches; sending the freshest {cap} at most.")
        new = new[:cap]

    if args.dry_run:
        for ev in new:
            print(re.sub(r"<[^>]+>", "", html.unescape(fmt_job_html(ev))), "\n" + "-" * 50)
        log(f"Dry run: {len(new)} alert(s) shown; nothing sent, state not saved.")
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
        stamp = int(now)
        for jid in list(evs) + stale_ids:
            state["jobs"].setdefault(jid, stamp)
        cutoff = stamp - KEEP_DAYS * 86400
        state["jobs"] = {k: v for k, v in state["jobs"].items() if v >= cutoff}
        STATE_PATH.write_text(json.dumps(state, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
