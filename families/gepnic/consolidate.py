#!/usr/bin/env python3
"""Tidy the pending scrape queue before a big run: merge small adjacent windows, then balance them across the runner slices.

Planning leaves many tiny windows (5,097 of 7,678 hold 100 tenders or fewer) and each costs a fixed ~13 s of captchas and
listing. Merging an organisation's adjacent windows up to --cap tenders cuts that, and bucketing gives every runner the same
estimated workload, biggest windows first. Only pending items are touched, and each swap is atomic on the hub.

  python consolidate.py [--cap 1200] [--buckets 20] [--dry-run]
"""
import argparse

from scrape_hub_client import list_queue, replace_queue

import windows
from plan import TENDERS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=1200, help="largest merged window, in tenders")
    ap.add_argument("--buckets", type=int, default=20, help="runner slices to balance across (the workflow's total_jobs)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    pending = [i for i in list_queue(TENDERS, "pending") if i["payload"].get("kind") != "count" and i["payload"].get("from_date")]
    groups = windows.merge_adjacent(pending, a.cap)
    merged = [m for _, m in groups]
    totals = windows.bucketize(merged, a.buckets)
    tenders = sum(m["payload"]["expected"] for m in merged)
    print(f"{len(pending)} pending windows -> {len(merged)} after merging up to {a.cap} tenders ({tenders} tenders)")
    print(f"estimated hours per slice at 4 threads: min {min(totals) / 3600:.2f}, max {max(totals) / 3600:.2f}")
    if a.dry_run:
        print("dry run: nothing changed")
        return
    done, batch_remove, batch_add = 0, [], []
    for members, m in groups:
        batch_remove += [x["key"] for x in members]
        batch_add.append(m)
        if len(batch_remove) >= 400:
            replace_queue(TENDERS, batch_remove, batch_add)
            done += len(batch_add)
            batch_remove, batch_add = [], []
    if batch_add:
        replace_queue(TENDERS, batch_remove, batch_add)
        done += len(batch_add)
    print(f"replaced: {done} windows now queued")


if __name__ == "__main__":
    main()
