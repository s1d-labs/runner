#!/usr/bin/env python3
"""Register both projects with scrape-hub and seed them.

  <tenders project>  one queue item per (organisation, date window) to scrape
  <plan project>     count tasks: count.py counts each window with one search and splits it until it fits a chunk

eprocure.gov.in (PORTAL unset): seeded from the per-organisation, per-year counts in data/org_year_counts.json, re-keyed by name
against today's dropdown (ids shift when organisations are added).

Any other portal (PORTAL=<portal>): seeded straight from today's organisation dropdown. Organisations that had no tenders in the June
2026 counts (data/gepnic_org_counts_june2026.json) are skipped; those that had more than one chunk get one count task per year, the rest
one count task over the whole period. Safe to re-run: existing queue items are left alone.
"""
import argparse
import json
import os

from scrape_hub_client import register_project, seed_queue

import orgs
import portal
import windows

TENDERS = portal.TENDERS
PLAN = portal.PLAN
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "org_year_counts.json")
JUNE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "gepnic_org_counts_june2026.json")


def remap_by_name(saved, live):
    """saved: {old_id: {name, years}}, live: {id: label}. Returns ({live_id: entry}, ids_changed, skipped)."""
    by_name = {}
    for oid, label in live.items():
        by_name.setdefault(orgs.norm(label), []).append(oid)
    out, moved, skipped = {}, 0, []
    for old, v in saved.items():
        ids = by_name.get(orgs.norm(v["name"]), [])
        if len(ids) != 1:
            skipped.append((old, v["name"], sum(v["years"].values()), len(ids)))
            continue
        moved += ids[0] != old
        out[ids[0]] = v
    return out, moved, skipped


def bootstrap_items(live, june, from_year, to_year, chunk_max):
    """Count tasks for a portal without saved per-year counts. live: {org_id: label}; june: {org_name: tenders} (may be empty).
    Returns (items, summary)."""
    by_name = {orgs.norm(k): v for k, v in june.items()}
    items, skipped, yearly = [], 0, 0
    for oid, label in sorted(live.items(), key=lambda kv: int(kv[0])):
        n = by_name.get(orgs.norm(label))
        if n == 0:
            skipped += 1
            continue
        if n is not None and n > chunk_max:
            yearly += 1
            items += [windows.count_item(oid, label, *windows.year_window(y), chunk_max) for y in range(from_year, to_year + 1)]
        else:
            items.append(windows.count_item(oid, label, f"01/01/{from_year}", f"31/12/{to_year}", chunk_max))
    return items, {"organisations": len(live), "skipped_no_tenders_in_june": skipped, "counted_by_year": yearly, "count_tasks": len(items),
                   "expected_from_june": sum(v for v in june.values() if v)}


def main_portal(a):
    june = json.load(open(JUNE))["portals"].get(portal.NAME, {})
    live = orgs.live_options()
    only = {x for x in a.only.split(",") if x}
    if only:
        live = {k: v for k, v in live.items() if k in only}
    items, summary = bootstrap_items(live, june, a.from_year, a.to_year, a.chunk_max)
    print(portal.NAME, summary)
    register_project(TENDERS, title=f"{portal.TITLE} tenders ({portal.NAME})", unit_label="window", record_label="tender",
                     expected_records=summary["expected_from_june"])
    register_project(PLAN, title=f"{portal.TITLE} count plan", unit_label="count task", record_label="count", expected_units=len(items))
    print("count queue:", seed_queue(PLAN, items, reset_failed=a.reset_failed))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-year", type=int, default=2011 if portal.LEGACY else 2005)
    ap.add_argument("--to-year", type=int, default=2026 if portal.LEGACY else 2027)
    ap.add_argument("--split-over", type=int, default=600, help="org-years with more tenders than this are counted by month first")
    ap.add_argument("--chunk-max", type=int, default=1200, help="a window with more tenders than this is halved until it fits")
    ap.add_argument("--only", default="", help="comma-separated live org ids (smoke tests)")
    ap.add_argument("--reset-failed", action="store_true")
    a = ap.parse_args()
    if not portal.LEGACY:
        return main_portal(a)

    saved = json.load(open(DATA))
    remapped, moved, skipped = remap_by_name(saved, orgs.live_options())
    print(f"{len(remapped)} organisations matched, {moved} ids changed since the counts, {len(skipped)} unmatched or ambiguous")
    for sk in skipped:
        print("  SKIPPED", sk)
    only = {x for x in a.only.split(",") if x}
    chosen = {k: v for k, v in remapped.items() if not only or k in only}
    crawl, counts, summary = windows.build_plan(chosen, a.from_year, a.to_year, a.split_over, a.chunk_max)
    print(summary)
    expected = summary["direct_tenders"] + summary["split_tenders"]
    register_project(TENDERS, title=f"eProcure tenders {a.from_year}-{a.to_year}", unit_label="window", record_label="tender",
                     expected_units=len(crawl), expected_records=expected)
    register_project(PLAN, title="eProcure monthly plan", unit_label="count task", record_label="count",
                     expected_units=len(counts))
    print("crawl queue:", seed_queue(TENDERS, crawl, reset_failed=a.reset_failed))
    print("count queue:", seed_queue(PLAN, counts, reset_failed=a.reset_failed))


if __name__ == "__main__":
    main()
