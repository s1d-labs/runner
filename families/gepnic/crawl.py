#!/usr/bin/env python3
"""FY 2025-26 eprocure crawler for GitHub Actions, reporting to scrape-hub.

Each runner claims its slice of organisations from the hub queue, lists every tender in the date window,
fetches the status/details/summary pages for each, and uploads one record per tender. Documents (PDF/ZIP)
are not downloaded; the metadata and links are stored in the record.
"""
import argparse

import contextlib
import os
import random
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "captcha"))

import requests  # noqa: E402
from requests.adapters import HTTPAdapter  # noqa: E402
from scrape_hub_client import HubClient  # noqa: E402

import documents  # noqa: E402
import orgs as orgs_mod  # noqa: E402
import parsing  # noqa: E402
import sections  # noqa: E402
import scrape_organization as so  # noqa: E402
from plan import TENDERS  # noqa: E402
import portal  # noqa: E402

PROJECT = os.environ.get("SCRAPE_PROJECT") or TENDERS   # speed experiments use a separate project so they never touch the real data

_seen = {}
_seen_lock = threading.Lock()

MAX_POOL = 32  # most portal requests in flight at once; the hub's `threads` setting decides how many run
SHORT_ACCEPT_AFTER = 4   # tries before a window that lists fewer than the plan's count is accepted as it is
COMPLETE_FRACTION = 0.98  # a window with at least this share of its planned tenders stored is treated as complete
WORKERS = 2 * MAX_POOL   # worker threads: the spare ones upload and parse while the gated ones talk to the portal


@contextlib.contextmanager
def timed(name):
    """Add the seconds spent in the block to the metric t_<name>_s (thread-seconds: threads that overlap each add their own time)."""
    t = time.perf_counter()
    try:
        yield
    finally:
        so.bump(f"t_{name}_s", time.perf_counter() - t)


class Gate:
    """A concurrency limit that can be raised or lowered while work is running."""

    def __init__(self, limit):
        self.limit, self.active, self.cv = max(1, limit), 0, threading.Condition()

    def set_limit(self, n):
        with self.cv:
            self.limit = max(1, min(MAX_POOL, int(n)))
            self.cv.notify_all()

    def __enter__(self):
        t = time.perf_counter()
        with self.cv:
            while self.active >= self.limit:
                self.cv.wait(1.0)
            self.active += 1
        so.bump("t_gate_wait_s", time.perf_counter() - t)      # time a thread waited for a free slot

    def __exit__(self, *exc):
        with self.cv:
            self.active -= 1
            self.cv.notify_all()


class SessionPool:
    """Authenticated portal sessions for one organisation, shared by the worker threads.

    The scraper normally keeps one session per *thread*, and each new session costs a captcha. With a 16-thread pool that
    meant up to 16 captchas per organisation whatever the speed setting. Borrowing from this pool keeps the number of
    sessions at the number actually in use at once (the gate's limit), and each is reused for many tenders.
    """

    def __init__(self):
        self.free, self.lock, self.created = [], threading.Lock(), 0

    def take(self):
        with self.lock:
            return self.free.pop() if self.free else None

    def give(self, session):
        if session is not None:
            with self.lock:
                self.free.append(session)


def fetch_pooled(pool, gate, view_link, tender_id, after=None, hold=None, **kw):
    """fetch_tender_details through a borrowed session; a new one (one captcha) is only made when none is free.

    `after(session, out)` runs inside the gate on the same live session, right after the pages were loaded (this is when a
    document link still works). The call then returns (out, after_result) instead of just out.
    """
    with gate:
        if hold:
            hold()                                  # paused while this tender waited for its turn: wait here, before any request
        so.thread_local.session = pool.take()      # None makes the scraper authenticate a fresh session
        ok = False
        try:
            with timed("fetch"):
                out = so.fetch_tender_details(view_link, tender_id, **kw)
            extra = after(so.thread_local.session, out) if after else None
            ok = True
            return (out, extra) if after else out
        finally:
            sess, so.thread_local.session = getattr(so.thread_local, "session", None), None
            if ok:                                  # a session that raised may be broken, so it is dropped, not reused
                pool.give(sess)


def not_yet_stored(hub, tenders, rescrape=False):
    """(tenders still to fetch, how many were skipped). A tender already stored with parsed fields is not fetched again, so a
    retried or resumed window only does what is missing. If the hub cannot answer, everything is fetched (safe, just slower)."""
    if rescrape or not tenders:
        return tenders, 0
    try:
        known = set(hub.known([t["tender_id"] for t in tenders]))
    except Exception:
        return tenders, 0
    todo = [t for t in tenders if t["tender_id"] not in known]
    return todo, len(tenders) - len(todo)


def report_document_outcomes(hub, tender_id, org_id, org_name, window, entries):
    """Refused or failed documents go to the hub's failures list; a document that is stored now clears any earlier failure."""
    for e in entries:
        key = f"{tender_id}/{e['name']}"
        if e["status"] == "stored":
            hub.clear_failure("document", key)
        elif e["status"] in ("refused", "failed"):
            hub.failure("document", key, e.get("reason") or e["status"], org_id=org_id, org_name=org_name, window=window,
                        document=e["name"], doc_kind=e["kind"], declared_bytes=e["declared_bytes"])


def wait_while_paused(hub):
    """Hold while the dashboard has this project paused (checked again right before each tender's first request: tenders already
    queued behind the gate when a pause arrives must not go on hitting the portal, 2026-10-20 UP)."""
    while hub.settings.get("paused"):
        time.sleep(2)


def obey_hub(hub, gate):
    """Apply the dashboard's speed settings: pause while asked to, resize the gate, and space requests out."""
    while hub.settings.get("paused"):
        time.sleep(2)
    gate.set_limit(hub.settings.get("threads", 4))
    delay = hub.settings.get("delay_ms", 0)
    if delay:
        time.sleep(delay / 1000)


_last_cpu = [time.process_time(), time.perf_counter(), False]


def sync_metrics(hub):
    """Move the scraper's counters (captcha_ok, t_fetch_s, ...) to the hub as deltas since the last call, plus this process's
    CPU seconds, wall seconds and core count (cpu_s / wall_s / cores = how busy the runner's cores are)."""
    with so._metrics_lock:
        snap = dict(so.METRICS)
    with _seen_lock:
        for k, v in snap.items():
            d = v - _seen.get(k, 0)
            if d:
                hub.metric(k, d)
                _seen[k] = v
        cpu, wall = time.process_time(), time.perf_counter()
        hub.metric("cpu_s", cpu - _last_cpu[0])
        hub.metric("wall_s", wall - _last_cpu[1])
        _last_cpu[0], _last_cpu[1] = cpu, wall
        if not _last_cpu[2]:
            hub.metric("cores", os.cpu_count() or 0)
            _last_cpu[2] = True


def list_pages(org_id, out_dir, max_tenders, retries, from_date, to_date):
    """Yield the tender rows of each results page as it arrives (10 a page, about 3.4 s a page), so fetching can start before
    the whole window has been listed."""
    s = requests.Session()
    s.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=3))
    portal.cookie(s)
    s.headers.update({"User-Agent": so.USER_AGENT, "Accept-Language": "en-US,en;q=0.5"})
    html = so.fetch_search_page(s, org_id=org_id, max_retries=retries, output_dir=out_dir, from_date=from_date, to_date=to_date)
    seen = 0
    pages_seen = 0
    while True:
        rows, nxt = so.parse_results_page(html)
        if max_tenders:
            rows = rows[:max_tenders - seen]
        seen += len(rows)
        yield rows
        if not nxt or (max_tenders and seen >= max_tenders):
            return
        html = so.safe_request(s, "GET", nxt).text
        if 'id="tabList"' not in html:      # an error or expired page where the next results page should be: not the end of the list
            raise RuntimeError(f"results page {pages_seen + 2} came back without a results table after {seen} tenders (portal error or expired session)")
        pages_seen += 1
        time.sleep(0.4)


def list_tenders(org_id, out_dir, max_tenders, retries, from_date, to_date):
    """All tender rows for the organisation in the window (paged search results)."""
    return [r for rows in list_pages(org_id, out_dir, max_tenders, retries, from_date, to_date) for r in rows]


_LIVE = {}


def label_matches(org_id, name):
    """The portal's ids shift when organisations are added; never crawl an id that now means something else."""
    if not _LIVE:
        _LIVE.update(orgs_mod.live_options())
    return orgs_mod.norm(_LIVE.get(org_id, "")) == orgs_mod.norm(name)


def crawl_org(hub, item, args, deadline, gate):
    """Returns (status, error, fetched) for hub.complete()."""
    p = item["payload"]
    org_id, name = p["org_id"], p["org_name"]
    # Items made before date windows existed have no window: they cover FY 2025-26.
    frm, to = p.get("from_date", "01/04/2025"), p.get("to_date", "31/03/2026")
    try:
        ok = label_matches(org_id, name)
    except Exception as e:
        hub.block()
        return "failed", f"could not read the portal's organisation list ({type(e).__name__})", 0
    if not ok:
        hub.block()
        return "failed", f"portal id {org_id} now points to {_LIVE.get(org_id, 'nothing')!r}; reseed needed", 0

    out_dir = tempfile.mkdtemp(prefix=f"org{org_id}_")
    expected = int(item["payload"].get("expected") or 0)
    if expected and not args.rescrape and not args.max_tenders:
        try:
            have = hub.window_stored(org_id, frm, to)
        except Exception:
            have = 0                                   # cannot tell: list it (safe, just slower)
        if have >= expected * COMPLETE_FRACTION:       # the plan's count says it is all here already: no need to list 10 tenders at a time
            so.bump("windows_skipped_complete")
            hub.note(f"{name[:48]} {frm}-{to}: already complete ({have} stored of {expected} expected)")
            return "done", f"already complete: {have} stored of {expected} expected", have
    state = {"ok": 0, "failed": 0}
    pool = SessionPool()
    # Every session costs a captcha (about 1.75 tries, ~3 s). A small window must not open a session per thread:
    # allow roughly one session per 4 tenders, so each session is reused several times.
    sessions = threading.Semaphore(max(1, min(MAX_POOL, (expected or 40) // 4)))

    def one(t):
        if time.time() > deadline:
            state["failed"] += 1
            return
        obey_hub(hub, gate)
        try:
            def collect(sess, out):
                with timed("docs"):
                    docs = documents.extract_documents(out[3])
                    got = documents.download_documents(sess, docs) if args.documents else [(d, None, None, "not requested") for d in docs]
                    if documents.extract_status_documents(out[1]):      # e.g. a cancellation notice, linked from the status page
                        status_docs = documents.extract_status_documents(sess.get(t.get("view_link"), timeout=60).text)   # reload that page: its links only work there
                        got += documents.download_documents(sess, status_docs) if args.documents else [(d, None, None, "not requested") for d in status_docs]
                return got

            with sessions:       # uploads and parsing below run outside it, so they overlap the next fetches
                (details, page0, page1, summary_html), fetched = fetch_pooled(
                    pool, gate, t.get("view_link"), t["tender_id"], after=collect, hold=lambda: wait_while_paused(hub), org_id=org_id,
                    output_dir=out_dir, from_date=frm, to_date=to)
            if not details:
                raise RuntimeError("no details parsed")
            details.pop("pdf_documents", None)
            prefix = documents.tender_prefix(t["tender_id"], org_id)
            # One upload per tender: its pages and documents go into one tar (tenders/<prefix>__<time>.tar; every fetch gets its own key, the
            # hub keeps earlier keys in details.pages_history). Each file's [offset, bytes] inside the tar is recorded, so one document can
            # be read later with a Range request. Uploading each file separately made B2 throttle the account (503 SlowDown, 2026-10-20).
            bundle_path = f"tenders/{prefix}__{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.tar"
            members, entries = [], []
            if args.store:
                members.append(("pages.json.gz", documents.pages_bundle(page0, page1, summary_html)))
            for n_doc, (d, content, v, err) in enumerate(fetched, 1):
                e = {"name": d["name"], "kind": d["kind"], "declared_bytes": d["declared_bytes"]}
                if v and v["ok"] and args.store:
                    # numbered, so two documents with the same file name on one tender cannot overwrite each other
                    path = f"docs/{n_doc:02d}_{documents.safe_part(v['filename'])}"
                    members.append((path, content))
                    e.update(status="stored", content_type=v["content_type"], bytes=v["bytes"], sha256=v["sha256"], size_ok=v["size_ok"], _path=path)
                    so.bump("doc_stored")
                elif v:
                    e.update(status="refused", reason=v["reason"] or "portal did not return the document")
                    so.bump("doc_refused")
                else:
                    e.update(status="not_stored" if err == "not requested" else "failed", reason=err)
                    so.bump("doc_failed" if err != "not requested" else "doc_skipped")
                entries.append(e)
            _t_parse = time.perf_counter()
            try:   # the structured fields (the September parser's output); a parse problem must not lose the tender, the raw pages are stored
                dj = {**details, "stage": t.get("stage"), "sno": t.get("sno"), "view_link": t.get("view_link"), "tender_id": t["tender_id"]}
                names = [f"{t['tender_id']}_{e['name']}" for e in entries]
                fields, proc, outcomes = parsing.parse_tender(t["tender_id"], org_id, name, dj, page0, page1, summary_html, names)
                outcomes.pop("bids_list", None)                       # already stored once, as details["bids"]
                details["fields"], details["outcomes"] = fields, outcomes
                if portal.LEGACY:      # the old flat key/value dump; `sections` and `fields` hold the same, and the raw pages are in storage
                    details["procurement_details"] = proc
                hub.clear_failure("parse", t["tender_id"])
            except Exception as pe:
                details["parse_error"] = f"{type(pe).__name__}: {str(pe)[:200]}"
                hub.failure("parse", t["tender_id"], details["parse_error"], org_id=org_id, org_name=name, window=[frm, to])
            try:   # the tender's own document lists, covers, payment instruments, corrigenda (backfill.py does the same for tenders stored earlier)
                details["sections"] = sections.parse_sections(page1)
            except Exception as se:
                details["sections_error"] = f"{type(se).__name__}: {str(se)[:120]}"
            so.bump("t_parse_s", time.perf_counter() - _t_parse)
            if members:
                tar, where = documents.tender_bundle(members)
                with timed("upload"):
                    uploaded = hub.upload_many([(bundle_path, tar, "application/x-tar")])   # raises if it keeps failing: the tender is retried
                key = uploaded[0]["key"]
                details["bundle_key"] = key
                if args.store:
                    details["pages_key"] = key                         # the tar; the pages are its member pages.json.gz
                    details["pages_range"] = where["pages.json.gz"]
                    details["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                for e in entries:
                    if e.get("_path"):
                        e.update(key=key, member=e["_path"], range=where[e["_path"]])
            for e in entries:
                e.pop("_path", None)
            details["documents"] = entries
            report_document_outcomes(hub, t["tender_id"], org_id, name, [frm, to], entries)
            hub.record(t["tender_id"], {"org_id": org_id, "org_name": name, "window": [frm, to], "list": t, "details": details})
            state["ok"] += 1
            so.bump("n_tenders")
        except Exception as exc:
            state["failed"] += 1
            hub.error()
            hub.failure("tender", t["tender_id"], f"{type(exc).__name__}: {str(exc)[:200]}", org_id=org_id, org_name=name, window=[frm, to])
        sync_metrics(hub)

    # Fetch while listing: each results page's new tenders go to the workers at once, instead of waiting for the whole listing.
    ex = ThreadPoolExecutor(max_workers=WORKERS)
    futures, listed, skipped = [], 0, 0

    def submit(rows):
        nonlocal skipped
        todo, sk = not_yet_stored(hub, rows, args.rescrape)
        skipped += sk
        futures.extend(ex.submit(one, t) for t in todo)

    list_error = None
    for _attempt in range(2):  # an empty result for a populated org can be a transient block; retry on a fresh session
        buf = []
        try:
            pages = list_pages(org_id, out_dir, args.max_tenders, args.retries, frm, to)
            while True:
                with timed("list"):
                    rows = next(pages, None)
                if rows is None:
                    break
                listed += len(rows)
                buf.extend(rows)
                if len(buf) >= 20:
                    submit(buf)
                    buf = []
            if buf:
                submit(buf)
        except Exception as e:
            hub.block()
            list_error = f"search failed ({type(e).__name__}: {str(e)[:120]})"
            if buf:
                submit(buf)                            # what was already listed is still worth fetching
            break
        finally:
            sync_metrics(hub)
        if not listed and expected and _attempt == 0:
            time.sleep(random.uniform(10, 30))         # an empty answer is usually the portal being busy: let it recover
        if listed or not expected:
            break
    for f in futures:
        f.result()
    ex.shutdown()
    todo_n = len(futures)
    hub.note(f"{name[:48]} {frm}-{to}: {listed} tenders" + (f", {skipped} already stored" if skipped else "") + (f" (expected {expected})" if expected else ""))
    if skipped:
        so.bump("skipped_known", skipped)
    if list_error:
        return "failed", list_error, state["ok"] + skipped
    short = not args.max_tenders and expected and listed < expected * 0.9
    if short:
        hub.block()
        if not listed:
            return "failed", f"portal listed 0 of {expected} expected tenders", 0
    hub.flush()
    if state["failed"]:
        return "failed", f"{state['failed']} of {todo_n} tender pages failed", state["ok"] + skipped
    if short:
        # Checked on 18 Oct: for 14 windows that had stopped at "30 of 1172" the portal still listed the full count, so a short
        # listing is almost always a truncated one. Retry it; only after a few tries accept it as the portal's real count.
        if item.get("attempts", 1) < SHORT_ACCEPT_AFTER:
            return "failed", f"listed {listed} of {expected} expected: retrying", state["ok"] + skipped
        return "done", f"listed {listed} of {expected} expected", state["ok"] + skipped
    return "done", "", state["ok"] + skipped


def claim_next(hub, deadline, sleep=time.sleep):
    """The next window to crawl, waiting as long as work remains. [] means every window is done (or the time is up).

    Stays up while others are still working (a window they fail comes back), and while failed windows are waiting for their
    retry pause: the hub hands those out again once nothing is pending, never while the target is down. A window that has
    been tried too many times is given up on by the hub, so this cannot loop forever."""
    while time.time() < deadline:
        items = hub.claim(1, steal=True, retry_failed=True)
        if hub.settings.get("paused"):
            sleep(5)                   # paused from the dashboard: hold, keep the heartbeat alive, then ask again
            continue
        if items:
            return items
        q = getattr(hub, "queue", None) or {}
        if q.get("pending", 0) + q.get("claimed", 0) + q.get("retry", 0) == 0:
            return []
        hub.note("waiting to retry failed windows" if q.get("retry") else "waiting for the other runners to finish")
        sleep(random.uniform(30, 60))
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-index", type=int, default=0)
    ap.add_argument("--total-jobs", type=int, default=1)
    ap.add_argument("--threads", type=int, default=4, help="starting threads per runner; the dashboard setting overrides it")
    ap.add_argument("--retries", type=int, default=10)
    ap.add_argument("--max-tenders", type=int, default=0, help="cap tenders per org (smoke tests)")
    ap.add_argument("--rescrape", action="store_true", help="fetch every tender again even if it is already stored (e.g. after the parser changed)")
    ap.add_argument("--documents", action=argparse.BooleanOptionalAction, default=True, help="download each tender's documents")
    ap.add_argument("--store", action=argparse.BooleanOptionalAction, default=True, help="upload raw pages and documents to object storage")
    ap.add_argument("--max-items", type=int, default=0, help="stop after this many windows (smoke tests)")
    ap.add_argument("--stagger", type=float, default=0, help="spread process start-up over this many seconds")
    ap.add_argument("--max-hours", type=float, default=5.5)
    args = ap.parse_args()

    deadline = time.time() + args.max_hours * 3600
    if args.stagger:
        time.sleep(random.uniform(0, args.stagger))   # many processes starting together all hit the portal's search at once
    hub = HubClient(PROJECT, args.job_index, args.total_jobs, note="scraping windows")
    hub.settings["threads"] = args.threads   # starting value; the hub's setting replaces it on the first response
    gate = Gate(args.threads)
    done = 0
    try:
        if args.max_tenders:
            # Smoke test: a capped window is only partly scraped, so it must not be marked done. Take the windows in one
            # claim, scrape them, and hand them back to the queue untouched for the real run.
            items = hub.claim(args.max_items or 1, steal=True)
            for item in items:
                status, error, fetched = crawl_org(hub, item, args, deadline, gate)
                hub.complete([item["key"]], "pending", error=f"capped test run ({status}); left for the full scrape", fetched=fetched)
                done += 1
        while not args.max_tenders and time.time() < deadline and not (args.max_items and done >= args.max_items):
            items = claim_next(hub, deadline)
            if not items:
                break
            item = items[0]
            status, error, fetched = crawl_org(hub, item, args, deadline, gate)
            hub.complete([item["key"]], status, error=error, fetched=fetched)
            done += 1
        hub.close("finished", f"{done} windows")
    except BaseException as e:
        hub.close("failed", f"{type(e).__name__}: {e}"[:200])
        raise


if __name__ == "__main__":
    main()
