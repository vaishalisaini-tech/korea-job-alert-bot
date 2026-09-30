#!/usr/bin/env python3
"""
Debug helper: shows what each source returns.

  python3 check_sources.py                      # check everything in config.yaml
  python3 check_sources.py greenhouse coupang   # test one company token
  python3 check_sources.py lever palantir
"""
import sys

import yaml

import job_alert_bot as b

FETCHERS = {
    "greenhouse": b.fetch_greenhouse,
    "lever": b.fetch_lever,
    "ashby": b.fetch_ashby,
    "smartrecruiters": b.fetch_smartrecruiters,
}


def check(kind, name, f):
    try:
        jobs = list(FETCHERS[kind](name))
    except Exception as e:
        print(f"  ❌ {kind}/{name}: FAILED ({e})")
        return
    korea = [j for j in jobs if b.kw_match(j["location"], f["locations"])]
    matched = [j for j in korea if b.passes(j, f)]
    print(f"  ✅ {kind}/{name}: {len(jobs)} total | {len(korea)} in Korea | {len(matched)} match your filters")
    for j in korea:
        tag = "MATCH" if j in matched else "title filtered"
        print(f"       [{tag}] {j['title']}  ({j['location'].strip(' |,')})")
        print(f"                {j['url']}")


def main():
    cfg = yaml.safe_load(open(b.CONFIG_PATH, encoding="utf-8"))
    f = cfg["filters"]
    args = sys.argv[1:]
    if len(args) == 2 and args[0] in FETCHERS:
        check(args[0], args[1], f)
        return
    for kind in FETCHERS:
        for name in cfg.get("sources", {}).get(kind) or []:
            check(kind, name, f)


if __name__ == "__main__":
    main()
