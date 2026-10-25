import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "captcha"))

import crawl  # noqa: E402


class FakeHub:
    def __init__(self, **s):
        self.settings = {"threads": 4, "delay_ms": 0, "paused": False, **s}


def run_workers(gate, n, hold=0.15):
    peak, cur, lock = [0], [0], threading.Lock()

    def work():
        with gate:
            with lock:
                cur[0] += 1
                peak[0] = max(peak[0], cur[0])
            time.sleep(hold)
            with lock:
                cur[0] -= 1
    ts = [threading.Thread(target=work) for _ in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return peak[0]


def test_gate_limits_concurrency():
    assert run_workers(crawl.Gate(3), 10) == 3


def test_gate_can_be_raised_while_running():
    g = crawl.Gate(1)
    assert run_workers(g, 6) == 1
    g.set_limit(5)
    assert run_workers(g, 10) == 5


def test_gate_limit_is_clamped():
    g = crawl.Gate(2)
    g.set_limit(999)
    assert g.limit == crawl.MAX_POOL
    g.set_limit(0)
    assert g.limit == 1


def test_obey_hub_resizes_the_gate_and_waits_out_a_pause():
    g, hub = crawl.Gate(1), FakeHub(threads=8)
    crawl.obey_hub(hub, g)
    assert g.limit == 8
    hub.settings["paused"] = True
    threading.Timer(0.5, lambda: hub.settings.update(paused=False)).start()
    t0 = time.time()
    crawl.obey_hub(hub, g)
    assert time.time() - t0 >= 0.4


def test_obey_hub_spaces_requests_by_delay():
    t0 = time.time()
    crawl.obey_hub(FakeHub(delay_ms=300), crawl.Gate(2))
    assert time.time() - t0 >= 0.29


def test_sessions_are_bounded_by_the_gate_not_the_thread_count(monkeypatch):
    """16 worker threads at a gate of 4 must create about 4 sessions (4 captchas), not 16."""
    created = []

    def fake_fetch(view_link, tender_id, **kw):          # mimics the scraper's per-thread session handling
        sess = crawl.so.thread_local.session
        if sess is None:
            sess = object()
            created.append(sess)                          # a new session == a captcha solved
        crawl.so.thread_local.session = sess
        time.sleep(0.02)
        return {"ok": tender_id}, "", "", ""

    monkeypatch.setattr(crawl.so, "fetch_tender_details", fake_fetch)
    pool, gate = crawl.SessionPool(), crawl.Gate(4)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(lambda i: crawl.fetch_pooled(pool, gate, "u", str(i)), range(80)))
    assert len(results) == 80
    assert len(created) <= 4, f"{len(created)} sessions created"


def test_a_session_that_raised_is_not_reused(monkeypatch):
    seen = []

    def fake_fetch(view_link, tender_id, **kw):
        sess = crawl.so.thread_local.session or object()
        crawl.so.thread_local.session = sess
        seen.append(sess)
        if tender_id == "boom":
            raise RuntimeError("expired")
        return {}, "", "", ""

    monkeypatch.setattr(crawl.so, "fetch_tender_details", fake_fetch)
    pool, gate = crawl.SessionPool(), crawl.Gate(1)
    crawl.fetch_pooled(pool, gate, "u", "a")
    good = seen[0]
    try:
        crawl.fetch_pooled(pool, gate, "u", "boom")
    except RuntimeError:
        pass
    assert pool.free == []                                # the broken session was dropped
    crawl.fetch_pooled(pool, gate, "u", "c")
    assert len(pool.free) == 1 and pool.free[0] is not good      # "c" got a fresh session, not the broken one


def test_fetch_pooled_runs_the_after_callback_on_the_live_session_inside_the_gate(monkeypatch):
    sessions, order = [], []

    def fake_fetch(view_link, tender_id, **kw):
        crawl.so.thread_local.session = crawl.so.thread_local.session or object()
        order.append("fetch")
        return {"d": 1}, "p0", "p1", "p2"

    monkeypatch.setattr(crawl.so, "fetch_tender_details", fake_fetch)
    pool, gate = crawl.SessionPool(), crawl.Gate(1)

    def after(sess, out):
        order.append("after")
        sessions.append((sess, gate.active))
        return ["downloaded"]

    out, extra = crawl.fetch_pooled(pool, gate, "u", "t1", after=after)
    assert out[3] == "p2" and extra == ["downloaded"] and order == ["fetch", "after"]
    assert sessions[0][0] is not None and sessions[0][1] == 1                      # live session, still holding the gate
    assert len(pool.free) == 1                                                      # and the session went back to the pool afterwards


class KnownHub:
    def __init__(self, known=None, boom=False):
        self.known_ids, self.boom = known or [], boom

    def known(self, ids):
        if self.boom:
            raise RuntimeError("hub down")
        return [i for i in ids if i in self.known_ids]


def test_tenders_already_stored_are_skipped_and_the_rest_fetched():
    ts = [{"tender_id": f"T{i}"} for i in range(5)]
    todo, skipped = crawl.not_yet_stored(KnownHub(["T1", "T3"]), ts)
    assert [t["tender_id"] for t in todo] == ["T0", "T2", "T4"] and skipped == 2


def test_rescrape_fetches_everything_and_a_hub_error_never_loses_work():
    ts = [{"tender_id": f"T{i}"} for i in range(3)]
    assert crawl.not_yet_stored(KnownHub(["T0"]), ts, rescrape=True) == (ts, 0)
    assert crawl.not_yet_stored(KnownHub(boom=True), ts) == (ts, 0)               # cannot ask: fetch all, safe
    assert crawl.not_yet_stored(KnownHub(), []) == ([], 0)


def test_timed_adds_seconds_to_the_phase_metric():
    before = crawl.so.METRICS.get("t_unit_test_s", 0)
    with crawl.timed("unit_test"):
        time.sleep(0.05)
    assert crawl.so.METRICS["t_unit_test_s"] - before >= 0.049


def test_waiting_for_a_gate_slot_is_measured():
    g = crawl.Gate(1)
    before = crawl.so.METRICS.get("t_gate_wait_s", 0)
    started = threading.Event()

    def hold():
        with g:
            started.set()
            time.sleep(0.2)
    t = threading.Thread(target=hold); t.start(); started.wait()
    with g:
        pass
    t.join()
    assert crawl.so.METRICS["t_gate_wait_s"] - before >= 0.15


def test_sync_metrics_reports_cpu_wall_and_cores_once(monkeypatch):
    class H:
        def __init__(self):
            self.m = {}

        def metric(self, k, v=1):
            self.m[k] = self.m.get(k, 0) + v
    crawl._last_cpu[:] = [time.process_time(), time.perf_counter(), False]
    sum(i * i for i in range(300000))
    time.sleep(0.05)
    h = H()
    crawl.sync_metrics(h)
    assert h.m["cpu_s"] > 0 and h.m["wall_s"] >= 0.05 and h.m["cores"] >= 1
    h2 = H()
    crawl.sync_metrics(h2)
    assert "cores" not in h2.m                                                       # sent only the first time


def test_safe_request_counts_requests_and_portal_wait(monkeypatch):
    class R:
        status_code = 200

    class S:
        def get(self, url, **kw):
            time.sleep(0.03)
            return R()
    n0, t0 = crawl.so.METRICS.get("n_http", 0), crawl.so.METRICS.get("t_http_s", 0)
    crawl.so.safe_request(S(), "GET", "https://x")
    assert crawl.so.METRICS["n_http"] - n0 == 1 and crawl.so.METRICS["t_http_s"] - t0 >= 0.029
