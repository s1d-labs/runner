#!/usr/bin/env python3
"""GePNIC's entries for the scrape-hub runner pool: one JSON line per hub project with the generic pool settings.

The hub never interprets these; it hands them to the pool worker as they are:
  task   "count" (the -plan project) or "crawl" (the tenders project): which GePNIC script the worker runs
  env    {"PORTAL": "<portal>"}: what portal.py needs
  target the host the work hits, for the hub's politeness caps; portals that share one server (the shipyard and
         defence-PSU portals, eprocure + ePublish) use that server's IP, so they share one budget
  probe  the hub's health check for the portal ("eprocure" / "gepnic:<portal>"); while it fails the hub holds the work
Apply with the family owner's rights (scrape.s1d.me), or by the hub owner. Usage: python pool_settings.py > pool.jsonl
"""
import json
from collections import Counter

import portal as P

reg = json.load(open("data/gepnic_portals.json"))["portals"]
shared = Counter(p.get("server_ip") for p in reg)
for p in reg:
    if p.get("ui") == "gepnic2":            # GePNIC 2.0 (Manipur): not supported by this crawler yet
        continue
    name = p["portal"]
    legacy = name == P.LEGACY_NAME
    tenders, plan = ("eprocure-tenders", "eprocure-plan") if legacy else (f"gepnic-{P.slug(name)}", f"gepnic-{P.slug(name)}-plan")
    target = p["server_ip"] if p.get("server_ip") and shared[p["server_ip"]] > 1 else name
    common = {"pool": True, "family": "gepnic", "source": "eprocure" if legacy else P.slug(name), "target": target,
              "env": {"PORTAL": name}, "probe": "eprocure" if legacy else f"gepnic:{name}"}
    print(json.dumps({"project": plan, "settings": {**common, "task": "count", "stage": "count"}}))
    print(json.dumps({"project": tenders, "settings": {**common, "task": "crawl", "stage": "crawl"}}))
