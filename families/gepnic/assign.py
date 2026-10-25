#!/usr/bin/env python3
"""Which portals a runner process works on, when one workflow covers many GePNIC portals (gepnic.yml).

Every portal gets at least one process; the rest go to the big portals in proportion to their tenders (June 2026 counts), never more
than --per-server processes on one server (portals that share a server, like the six shipyard/defence-PSU portals, share that cap).
A process prints its own portal first, then every other portal biggest first, so when its own portal runs out of work it moves on to
help the others instead of sitting idle.

  python assign.py --slot 7 --slots 80            # portals for process 7 of 80, one per line
  python assign.py --slots 80 --show              # the whole allocation
  python assign.py --list                         # every portal this workflow covers
  python assign.py --hub --stage crawl --slots 80   # JSON plan for a workflow run, weighted by the work each portal has left on the hub
"""
import argparse
import json
import os

import portal

HERE = os.path.dirname(os.path.abspath(__file__))
SKIP = {portal.LEGACY_NAME}          # eprocure.gov.in has its own workflow (crawl.yml)


def portals(only=""):
    reg = [p for p in json.load(open(portal.REGISTRY))["portals"] if p.get("ui") != "gepnic2" and p["portal"] not in SKIP]
    if only and only != "all":
        want = {x.strip() for x in only.split(",") if x.strip()}
        reg = [p for p in reg if p["portal"] in want]
    return sorted(reg, key=lambda p: -p["tenders_june2026"])


def allocate(ps, slots, per_server=8):
    """{portal: processes}. Every portal one (if there are enough slots), the rest by tenders, at most per_server per server."""
    n = {p["portal"]: 0 for p in ps}
    server = {p["portal"]: p["server_ip"] for p in ps}
    used = lambda ip: sum(v for k, v in n.items() if server[k] == ip)
    for p in ps[:slots]:
        n[p["portal"]] = 1
    left = slots - sum(n.values())
    total = sum(p["tenders_june2026"] for p in ps) or 1
    while left > 0:
        # the portal furthest below its fair share that still has room on its server
        cand = [p for p in ps if used(p["server_ip"]) < per_server]
        if not cand:
            break
        best = max(cand, key=lambda p: p["tenders_june2026"] / total * slots - n[p["portal"]])
        n[best["portal"]] += 1
        left -= 1
    return n


def order(alloc, ps):
    """Slot number -> primary portal, biggest portals' processes spread across the slots so every runner gets a mix."""
    flat = []
    rounds = max(alloc.values()) if alloc else 0
    for r in range(rounds):
        flat += [p["portal"] for p in ps if alloc[p["portal"]] > r]
    return flat


def hub_remaining(ps, stage):
    """{portal: work left}: tenders still expected (crawl) or count tasks still open (count), from each portal's hub projects."""
    import importlib
    from scrape_hub_client import list_queue
    out = {}
    for p in ps:
        os.environ["PORTAL"] = p["portal"]
        importlib.reload(portal)
        project = portal.PLAN if stage == "count" else portal.TENDERS
        try:
            items = list_queue(project, "pending") + list_queue(project, "claimed") + \
                [i for i in list_queue(project, "failed") if i.get("attempts", 0) < 40]
        except Exception:      # project not created yet (not seeded): nothing to do there
            items = []
        out[p["portal"]] = len(items) if stage == "count" else sum(int(i["payload"].get("expected") or 0) or 1 for i in items)
    return out


def hub_plan(ps, stage, slots, per_server):
    """[[portal, index among that portal's processes, processes on that portal], ...] for slots 0..n-1, only portals with work left."""
    left = hub_remaining(ps, stage)
    work = [dict(p, tenders_june2026=left[p["portal"]]) for p in ps if left[p["portal"]] > 0]
    work.sort(key=lambda p: -p["tenders_june2026"])
    alloc = allocate(work, slots, per_server)
    seen, out = {}, []
    for name in order(alloc, work):
        out.append([name, seen.get(name, 0), alloc[name]])
        seen[name] = seen.get(name, 0) + 1
    return {"stage": stage, "slots": out, "left": {k: v for k, v in left.items() if v}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=0)
    ap.add_argument("--slots", type=int, default=80)
    ap.add_argument("--per-server", type=int, default=8)
    ap.add_argument("--only", default="all")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--hub", action="store_true", help="weight by the work left on the hub and print the workflow's JSON plan")
    ap.add_argument("--stage", choices=["count", "crawl"], default="crawl")
    a = ap.parse_args()
    ps = portals(a.only)
    if a.hub:
        print(json.dumps(hub_plan(ps, a.stage, a.slots, a.per_server), separators=(",", ":")))
        return
    if a.list:
        print("\n".join(p["portal"] for p in ps))
        return
    alloc = allocate(ps, a.slots, a.per_server)
    slots = order(alloc, ps)
    if a.show:
        for p in ps:
            print(f"{alloc[p['portal']]:3}  {p['portal']:34} {p['tenders_june2026']:>10,}  {p['server_ip']}")
        print(f"{len(slots)} processes for {len(ps)} portals")
        return
    if a.slot >= len(slots):
        return
    mine = slots[a.slot]
    print("\n".join([mine] + [p["portal"] for p in ps if p["portal"] != mine]))


if __name__ == "__main__":
    main()
