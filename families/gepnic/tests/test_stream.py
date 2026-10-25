import os
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import crawl  # noqa: E402


class FakeHub:
    def __init__(self, stored=0):
        self.stored, self.settings, self.notes, self.failures = stored, {"threads": 8}, [], []

    def window_stored(self, org_id, frm, to):
        return self.stored

    def known(self, ids):
        return []

    def note(self, text):
        self.notes.append(text)

    def block(self): pass
    def error(self): pass
    def flush(self): pass
    def metric(self, *a, **k): pass
    def clear_failure(self, *a, **k): pass

    def failure(self, kind, key, reason, **k):
        self.failures.append((kind, key))


def args(**kw):
    return SimpleNamespace(rescrape=False, max_tenders=0, retries=1, documents=False, store=False, **kw)


def item(expected):
    return {"key": "k", "payload": {"org_id": "1", "org_name": "Org", "from_date": "01/01/2019", "to_date": "31/12/2019", "expected": expected}}


def rows(page, n=10):
    return [{"tender_id": f"T{page}_{i}", "view_link": f"link{page}_{i}"} for i in range(n)]


def test_complete_window_is_skipped_without_listing(monkeypatch):
    monkeypatch.setattr(crawl, "label_matches", lambda *a: True)
    monkeypatch.setattr(crawl, "list_pages", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not list")))
    status, err, fetched = crawl.crawl_org(FakeHub(stored=99), item(100), args(), time.time() + 60, crawl.Gate(4))
    assert status == "done" and fetched == 99 and "already complete" in err        # 99 of 100 is above the 98% line
    # 90 of 100 is not complete: it is listed
    monkeypatch.setattr(crawl, "list_pages", lambda *a, **k: iter([]))
    status, err, fetched = crawl.crawl_org(FakeHub(stored=90), item(100), args(), time.time() + 60, crawl.Gate(4))
    assert "already complete" not in err


def test_fetching_starts_before_the_listing_has_finished(monkeypatch):
    monkeypatch.setattr(crawl, "label_matches", lambda *a: True)
    events, lock = [], threading.Lock()

    def pages(*a, **k):
        for p in range(4):
            time.sleep(0.15)                                  # a results page takes a while to arrive
            with lock:
                events.append(("page", p))
            yield rows(p)

    def fake_fetch(pool, gate, view_link, tender_id, **k):
        with lock:
            events.append(("fetch", tender_id))
        raise RuntimeError("no portal in tests")

    monkeypatch.setattr(crawl, "list_pages", pages)
    monkeypatch.setattr(crawl, "fetch_pooled", fake_fetch)
    hub = FakeHub()
    status, err, fetched = crawl.crawl_org(hub, item(40), args(), time.time() + 60, crawl.Gate(8))
    first_fetch = next(i for i, e in enumerate(events) if e[0] == "fetch")
    last_page = max(i for i, e in enumerate(events) if e[0] == "page")
    assert first_fetch < last_page                            # tenders from the first pages were being fetched while later pages were still coming
    assert status == "failed" and "40 of 40 tender pages failed" in err
    assert len([e for e in events if e[0] == "fetch"]) == 40


def test_listing_error_still_processes_what_was_listed(monkeypatch):
    monkeypatch.setattr(crawl, "label_matches", lambda *a: True)
    seen = []

    def pages(*a, **k):
        yield rows(0, 25)
        raise RuntimeError("portal timed out")

    monkeypatch.setattr(crawl, "list_pages", pages)
    monkeypatch.setattr(crawl, "fetch_pooled", lambda pool, gate, view_link, tender_id, **k: seen.append(tender_id) or (_ for _ in ()).throw(RuntimeError("x")))
    status, err, fetched = crawl.crawl_org(FakeHub(), item(100), args(), time.time() + 60, crawl.Gate(8))
    assert status == "failed" and err.startswith("search failed") and len(seen) == 25


def test_list_pages_yields_page_by_page_and_honours_the_cap(monkeypatch):
    page = lambda i: f'<table id="tabList"></table>p{i}'
    nxt = {0: "n1", 1: "n2", 2: None}
    monkeypatch.setattr(crawl.so, "fetch_search_page", lambda *a, **k: page(0))
    monkeypatch.setattr(crawl.so, "parse_results_page", lambda h: (rows(int(h[-1])), nxt[int(h[-1])]))
    monkeypatch.setattr(crawl.so, "safe_request", lambda s, m, url: SimpleNamespace(text={"n1": page(1), "n2": page(2)}[url]))
    monkeypatch.setattr(crawl.time, "sleep", lambda s: None)
    got = list(crawl.list_pages("1", "/tmp", 0, 1, "a", "b"))
    assert [len(r) for r in got] == [10, 10, 10]
    capped = list(crawl.list_pages("1", "/tmp", 15, 1, "a", "b"))
    assert [len(r) for r in capped] == [10, 5]
    assert len(crawl.list_tenders("1", "/tmp", 0, 1, "a", "b")) == 30


class WaitHub:
    """A hub that has nothing to hand out now but still has windows waiting, then hands one out, then reports all done."""
    def __init__(self, script):
        self.script, self.settings, self.queue, self.notes = list(script), {"paused": False}, {}, []

    def claim(self, limit=1, steal=False, retry_failed=False):
        assert steal and retry_failed
        items, self.queue = self.script.pop(0)
        return items

    def note(self, t):
        self.notes.append(t)


def test_claim_next_waits_while_windows_remain_and_stops_when_all_done():
    sleeps = []
    hub = WaitHub([([], {"pending": 0, "claimed": 3, "retry": 0}),          # others are still working
                   ([], {"pending": 0, "claimed": 0, "retry": 5}),          # failed windows are waiting for their pause
                   ([{"key": "w"}], {"pending": 0, "claimed": 1, "retry": 4})])
    got = crawl.claim_next(hub, time.time() + 600, sleep=sleeps.append)
    assert got == [{"key": "w"}] and len(sleeps) == 2 and all(30 <= s <= 60 for s in sleeps)
    assert "waiting for the other runners" in hub.notes[0] and "retry failed" in hub.notes[1]
    done = WaitHub([([], {"pending": 0, "claimed": 0, "retry": 0})])
    assert crawl.claim_next(done, time.time() + 600, sleep=sleeps.append) == []        # everything is done: the run may end


def test_claim_next_gives_up_at_the_deadline_and_honours_a_pause():
    hub = WaitHub([([], {"pending": 0, "claimed": 0, "retry": 9})] * 3)
    assert crawl.claim_next(hub, time.time() - 1, sleep=lambda s: None) == []          # out of time: stop even though work remains
    paused = WaitHub([([], {"pending": 5}), ([{"key": "x"}], {"pending": 4})])
    paused.settings["paused"] = True
    calls = []
    def sleep(s):
        calls.append(s)
        paused.settings["paused"] = False
    assert crawl.claim_next(paused, time.time() + 600, sleep=sleep) == [{"key": "x"}] and calls == [5]


def test_a_results_page_without_a_table_is_an_error_not_the_end_of_the_list(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(crawl.so, "fetch_search_page", lambda *a, **k: '<table id="tabList">p0</table>')
    monkeypatch.setattr(crawl.so, "parse_results_page", lambda h: (rows(0), "next") if "tabList" in h else ([], None))
    monkeypatch.setattr(crawl.so, "safe_request", lambda s, m, url: SimpleNamespace(text="<html>session expired</html>"))
    monkeypatch.setattr(crawl.time, "sleep", lambda s: None)
    got = []
    try:
        for page in crawl.list_pages("1", "/tmp", 0, 1, "a", "b"):
            got.append(len(page))
        raise AssertionError("the listing ended silently")
    except RuntimeError as e:
        assert "without a results table after 10 tenders" in str(e) and got == [10]


def test_a_short_listing_is_retried_and_only_accepted_after_a_few_tries(monkeypatch):
    monkeypatch.setattr(crawl, "label_matches", lambda *a: True)
    monkeypatch.setattr(crawl, "list_pages", lambda *a, **k: iter([rows(0, 10)]))
    monkeypatch.setattr(crawl, "fetch_pooled", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no portal")))
    first = crawl.crawl_org(FakeHub(), {**item(100), "attempts": 1}, args(), time.time() + 60, crawl.Gate(4))
    assert first[0] == "failed"
    monkeypatch.setattr(crawl, "list_pages", lambda *a, **k: iter([rows(0, 10)]))
    monkeypatch.setattr(crawl, "fetch_pooled", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no portal")))
    # tender pages fail in this fake, which also fails the window; what matters is the short-listing branch below
    monkeypatch.setattr(crawl, "not_yet_stored", lambda hub, rows_, rescrape=False: ([], len(rows_)))
    again = crawl.crawl_org(FakeHub(), {**item(100), "attempts": 1}, args(), time.time() + 60, crawl.Gate(4))
    assert again[0] == "failed" and "retrying" in again[1]
    late = crawl.crawl_org(FakeHub(), {**item(100), "attempts": 4}, args(), time.time() + 60, crawl.Gate(4))
    assert late[0] == "done" and "listed 10 of 100" in late[1]
