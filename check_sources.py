#!/usr/bin/env python3
"""
Debug helper: shows what each source returns and how the bot scores it.

  python3 check_sources.py                      # check everything in config.yaml
  python3 check_sources.py greenhouse coupang   # test one company token
  python3 check_sources.py lever palantir
"""
import sys
import time

import yaml

import job_alert_bot as b

FETCHERS = {
    "greenhouse": b.fetch_greenhouse,
    "lever": b.fetch_lever,
    "ashby": b.fetch_ashby,
    "smartrecruiters": b.fetch_smartrecruiters,
}


def check(kind, name, cfg):
    try:
        jobs = list(FETCHERS[kind](name))
    except Exception as e:
        print(f"  ❌ {kind}/{name}: FAILED ({e})")
        return
    now = time.time()
    korea = [j for j in jobs if b.kw_match(j["location"], cfg["location"]["include"])]
    results = [(j, *b.evaluate(j, cfg, now, False, {"jobs": {}, "pages": {}})) for j in korea]
    matched = [r for r in results if r[1]]
    print(f"  ✅ {kind}/{name}: {len(jobs)} total | {len(korea)} in Korea | {len(matched)} relevant")
    for j, ev, reason in results:
        if ev:
            print(f"       [{ev['score']:>3}/100 {ev['priority_label']}] {j['title']}")
            print(f"            Visa: {ev['visa']} | Korean: {ev['korean']} | English: {ev['english']} | Exp: {ev['min_req']}")
            print(f"            {j['url']}")
        else:
            print(f"       [skipped: {reason}] {j['title']}")


def main():
    cfg = yaml.safe_load(open(b.CONFIG_PATH, encoding="utf-8"))
    args = sys.argv[1:]
    if len(args) == 2 and args[0] in FETCHERS:
        check(args[0], args[1], cfg)
        return
    for kind in FETCHERS:
        for name in cfg.get("sources", {}).get(kind) or []:
            check(kind, name, cfg)


if __name__ == "__main__":
    main()
