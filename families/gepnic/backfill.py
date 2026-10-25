#!/usr/bin/env python3
"""Fill in details.sections (the tender's own document lists, covers, payment instruments, corrigenda) on tenders already stored.

Each queue item is one (year, organisation). The runner asks the hub which of its tenders still lack the sections, downloads their
saved pages straight from object storage through short-lived links from the hub, parses the details page, and sends the result back.
The portal is not touched. Run by .github/workflows/backfill.yml.
"""
import argparse
import gzip
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from scrape_hub_client import HubClient

import documents
import sections

PROJECT = "eprocure-sections"
TARGET = "eprocure-tenders"
CHUNK = 100
TODO_LIMIT = 3000   # the hub returns at most this many records per question; a bigger org-year goes back to the queue so other processes share it


def fetch_and_parse(record_id, key, url):
    """(record_id, sections_dict) or (record_id, None) when the pages could not be read."""
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            raw = documents.bundle_member(r.content, "pages.json.gz") if key.endswith(".tar") else r.content   # a tender bundle (tar) or old pages file
            bundle = json.loads(gzip.decompress(raw))
            return record_id, sections.parse_sections(bundle.get("details", ""))
        except Exception:
            time.sleep(1 + attempt)
    return record_id, None


def process_item(hub, item, target=TARGET, threads=8, fetch=fetch_and_parse):
    """Returns (status, error, patched)."""
    p = item["payload"]
    todo = hub.backfill_todo(target, p["org_id"], p["year"], limit=TODO_LIMIT)
    patched = failed = 0
    for i in range(0, len(todo), CHUNK):
        chunk = todo[i:i + CHUNK]
        urls = hub.read_urls(target, [r["pages_key"] for r in chunk])
        with ThreadPoolExecutor(max_workers=threads) as ex:
            results = list(ex.map(lambda r: fetch(r["record_id"], r["pages_key"], urls[r["pages_key"]]), chunk))
        good = [(rid, sec) for rid, sec in results if sec is not None]
        failed += len(results) - len(good)
        if good:
            hub.patch_records(target, good)
            patched += len(good)
    if failed:
        return "failed", f"{failed} of {len(todo)} pages could not be read", patched
    if len(todo) >= TODO_LIMIT:
        return "pending", f"{patched} done, more left for another runner", patched
    return "done", "", patched


def claim_next(hub, deadline, sleep=time.sleep):
    while time.time() < deadline:
        items = hub.claim(1, steal=True, retry_failed=True)
        if items:
            return items
        q = hub.queue or {}
        if q.get("pending", 0) + q.get("claimed", 0) + q.get("retry", 0) == 0:
            return []
        sleep(random.uniform(20, 40))
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-index", type=int, default=0)
    ap.add_argument("--total-jobs", type=int, default=1)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--max-hours", type=float, default=5.5)
    args = ap.parse_args()
    deadline = time.time() + args.max_hours * 3600
    hub = HubClient(PROJECT, args.job_index, args.total_jobs, note="parsing document lists")
    done = 0
    try:
        while True:
            items = claim_next(hub, deadline)
            if not items:
                break
            item = items[0]
            t = time.perf_counter()
            status, error, patched = process_item(hub, item, threads=args.threads)
            hub.metric("wall_item_s", time.perf_counter() - t)
            hub.complete([item["key"]], status, error=error, fetched=patched)
            done += 1
        hub.close("finished", f"{done} org-years")
    except BaseException as e:
        hub.close("failed", f"{type(e).__name__}: {e}"[:200])
        raise


if __name__ == "__main__":
    sys.exit(main())
