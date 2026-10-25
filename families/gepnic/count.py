#!/usr/bin/env python3
"""Count runner for the `eprocure-plan` project (GitHub Actions).

Claims count tasks (organisation + date window), counts the window with one portal search, stores the number as a record,
then either queues the window for scraping (it fits in a chunk) or halves it and queues the two halves as new count tasks.
The records in `eprocure-plan` are the tenders-per-window table.
"""
import argparse
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "captcha"))

from scrape_hub_client import HubClient, seed_queue  # noqa: E402

import counting  # noqa: E402
import orgs as orgs_mod  # noqa: E402
import scrape_organization as so  # noqa: E402
import windows  # noqa: E402
from plan import PLAN, TENDERS  # noqa: E402

_LIVE = {}
_live_lock = threading.Lock()


def label_matches(org_id, name):
    with _live_lock:
        if not _LIVE:
            _LIVE.update(orgs_mod.live_options())
    return orgs_mod.norm(_LIVE.get(org_id, "")) == orgs_mod.norm(name)


def sync_metrics(hub, seen, lock):
    with so._metrics_lock:
        snap = dict(so.METRICS)
    with lock:
        for k, v in snap.items():
            d = v - seen.get(k, 0)
            if d:
                hub.metric(k, d)
                seen[k] = v


def handle(hub, item, retries):
    """Count one window. Returns (status, error, count)."""
    p = item["payload"]
    org_id, name, frm, to, chunk_max = p["org_id"], p["org_name"], p["from_date"], p["to_date"], p.get("chunk_max", 1200)
    if not label_matches(org_id, name):
        hub.block()
        return "failed", f"portal id {org_id} now points elsewhere; reseed needed", 0
    try:
        n = counting.window_count(org_id, frm, to, retries)
    except Exception as e:
        hub.block()
        return "failed", f"count failed ({type(e).__name__}: {str(e)[:120]})", 0
    hub.record(item["key"], {"org_id": org_id, "org_name": name, "from_date": frm, "to_date": to, "count": n})
    if n == 0:
        return "done", "", 0
    halves = None if n <= chunk_max else windows.split_window(frm, to)
    if halves is None:       # fits in a chunk (or is already a single day): queue it for scraping
        seed_queue(TENDERS, [windows.crawl_item(org_id, name, frm, to, n)])
    else:
        seed_queue(PLAN, [windows.count_item(org_id, name, f, t, chunk_max) for f, t in halves])
    return "done", "", n


def should_idle(hub, index):
    """Workers beyond the dashboard's `threads` setting sit out, and everyone sits out while paused."""
    s = hub.settings
    return bool(s.get("paused")) or index >= int(s.get("threads", 4))


ALL_DONE = threading.Event()   # set when a worker finds the queue empty, so workers sitting out (above the threads setting) stop too


def worker(hub, args, deadline, counter, seen, lock, index=0):
    idle_since = None
    while time.time() < deadline and not (args.max_items and counter[0] >= args.max_items) and not ALL_DONE.is_set():
        if should_idle(hub, index):
            time.sleep(2)
            continue
        delay = hub.settings.get("delay_ms", 0)
        if delay:
            time.sleep(delay / 1000)
        items = hub.claim(1, steal=True, retry_failed=True)   # failed counts come back after a few minutes (never while the portal is down)
        if not items:
            # Other runners may still be counting windows that turn into new work, so wait while anything is in flight.
            if hub.queue.get("claimed", 0) == 0 and hub.queue.get("pending", 0) == 0 and hub.queue.get("retry", 0) == 0:
                ALL_DONE.set()
                return
            idle_since = idle_since or time.time()
            if time.time() - idle_since > 20 * 60:
                ALL_DONE.set()
                return
            time.sleep(10)
            continue
        idle_since = None
        item = items[0]
        status, error, n = handle(hub, item, args.retries)
        sync_metrics(hub, seen, lock)
        hub.complete([item["key"]], status, error=error, fetched=n)
        with lock:
            counter[0] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-index", type=int, default=0)
    ap.add_argument("--total-jobs", type=int, default=1)
    ap.add_argument("--workers", type=int, default=16, help="worker threads per runner; the dashboard's threads setting decides how many are active")
    ap.add_argument("--retries", type=int, default=10)
    ap.add_argument("--max-items", type=int, default=0, help="stop after this many count tasks (smoke tests)")
    ap.add_argument("--max-hours", type=float, default=5.5)
    args = ap.parse_args()

    deadline = time.time() + args.max_hours * 3600
    hub = HubClient(PLAN, args.job_index, args.total_jobs, note="counting windows")
    hub.settings["threads"] = 4   # until the hub's setting arrives
    counter, seen, lock = [0], {}, threading.Lock()
    try:
        ts = [threading.Thread(target=worker, args=(hub, args, deadline, counter, seen, lock, i), daemon=True) for i in range(args.workers)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        hub.close("finished", f"{counter[0]} windows counted")
    except BaseException as e:
        hub.close("failed", f"{type(e).__name__}: {e}"[:200])
        raise


if __name__ == "__main__":
    main()
