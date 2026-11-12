"""Small helpers that most task scripts need (SDK 0.10.0). All are importable from `scrape_hub_client`.

    from scrape_hub_client import HubClient, LocalHub, stop_flag, timed_get

    stop = stop_flag()                           # set on SIGINT / SIGTERM (the runner's "time is up")
    r = timed_get(hub, session, url, timeout=30)   # counts n_http, t_http_s, http_<code>, http_error for the hub
    while not stop.is_set(): ...
    hub.close("finished" if not stop.is_set() else "failed", "" if not stop.is_set() else "stopped by signal")

`LocalHub` is a stand-in for `HubClient` for a task's `--local` mode (one page, no hub).
"""
import hashlib
import json
import os
import signal
import sys
import threading
import time

BLOCK_CODES = (403, 429, 503)

_stop = threading.Event()


def _on_signal(signum, frame):
    _stop.set()


def stop_flag(install=True):
    """A threading.Event that is set when the process gets SIGINT or SIGTERM. The pool runner sends SIGINT when the time budget is
    over (then SIGTERM, then SIGKILL): check `stop.is_set()` in the work loop, close the run and exit.

    The handlers replace the default ones (so Ctrl-C no longer raises KeyboardInterrupt); they only set the flag. Every call
    returns the same Event. Install them from the main thread (Python allows signal handlers only there); `install=False`
    returns the Event without touching the handlers, for module level code that is also imported by tests."""
    if install:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, _on_signal)
    return _stop


def count_request(hub, status=None, seconds=0.0, block_codes=BLOCK_CODES):
    """Report one request to the hub the standard way, for code that does not use `timed_request` (httpx, Playwright, ...).

    status=None: the request got no answer (connection error, timeout): `http_error` + `hub.error()`.
    Otherwise: `n_http` + `t_http_s` (seconds); a status other than 200 also counts `http_<status>`, and 403 / 429 / 503 count
    `hub.block()`. hub=None does nothing (code that also runs without a hub)."""
    if hub is None:
        return
    if status is None:
        hub.metric("http_error")
        hub.error()
        return
    hub.metric("n_http")
    hub.metric("t_http_s", seconds)
    if status != 200:
        hub.metric(f"http_{status}")
        if status in block_codes:
            hub.block()


def timed_request(hub, session, method, url, raise_errors=False, block_codes=BLOCK_CODES, **kw):
    """session.request(method, url, **kw) with the request metrics the hub reads (see `count_request`).

    Returns the response for every status code (check `r.status_code` yourself). On a connection error or timeout it counts
    `http_error` and returns None, or re-raises the exception with raise_errors=True. `timeout` defaults to 30 seconds.
    `session` may be the `requests` module itself. hub=None: no metrics."""
    import requests

    kw.setdefault("timeout", 30)
    t = time.time()
    try:
        r = session.request(method, url, **kw)
    except requests.RequestException:
        count_request(hub, None)
        if raise_errors:
            raise
        return None
    count_request(hub, r.status_code, time.time() - t, block_codes)
    return r


def timed_get(hub, session, url, **kw):
    """timed_request(hub, session, "GET", url, **kw)."""
    return timed_request(hub, session, "GET", url, **kw)


def timed_post(hub, session, url, **kw):
    """timed_request(hub, session, "POST", url, **kw)."""
    return timed_request(hub, session, "POST", url, **kw)


class LocalHub:
    """Stand-in for HubClient so a task's `--local` mode runs without the hub: counters in `.counts` (print them to stderr at the
    end), records to stdout as JSON lines, failures to stderr, uploads to `save_dir` (or only listed on stderr).

        hub = LocalHub()                        # settings {"threads": 1, "delay_ms": 0, "paused": False}
        page = fetch(hub, requests.Session(), url)
        print(f"counters {hub.counts}", file=sys.stderr)

    `items` (a list of queue items {"key", "payload"}) are handed out by `claim()`, so a task's main loop can run on them too.
    For the real API with the same validation, use `scrape-check` (scrape_hub_client.testhub)."""

    settings = {"threads": 1, "delay_ms": 0, "paused": False}

    def __init__(self, project="local", threads=1, delay_ms=0, save_dir=None, items=None, out=None):
        self.project, self.save_dir = project, save_dir
        self.settings = {"threads": threads, "delay_ms": delay_ms, "paused": False}
        self.counts, self.records, self.done, self.failed = {}, 0, [], []
        self.items = list(items or [])
        self.queue = {"pending": len(self.items), "claimed": 0, "retry": 0}
        self.run_id = 0
        self._out = out or sys.stdout
        self._lock = threading.Lock()

    def metric(self, name, value=1):
        with self._lock:
            self.counts[name] = self.counts.get(name, 0) + value

    def error(self, n=1):
        self.metric("errors", n)

    def block(self, n=1):
        self.metric("blocked", n)

    def record(self, record_id, data):
        with self._lock:
            self.records += 1
        print(json.dumps({"record_id": str(record_id), "data": data}, ensure_ascii=False, default=str), file=self._out, flush=True)

    def failure(self, kind, key, reason="", **detail):
        print(f"failure {kind} {str(key)[:200]}: {reason}" + (f" {detail}" if detail else ""), file=sys.stderr)

    def clear_failure(self, kind, key):
        pass

    def upload_many(self, files):
        out = []
        for path, data, ctype in files:
            if self.save_dir:
                p = os.path.join(self.save_dir, path)
                os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
                with open(p, "wb") as f:
                    f.write(data)
            print(f"(local) upload {path} ({len(data)} bytes, {ctype})" + (f" -> {self.save_dir}" if self.save_dir else ""), file=sys.stderr)
            self.metric("uploaded_files")
            self.metric("uploaded_bytes", len(data))
            out.append({"path": path, "key": f"{self.project}/{path}", "bytes": len(data), "md5": hashlib.md5(data).hexdigest(),
                        "sha256": hashlib.sha256(data).hexdigest()})
        return out

    def claim(self, limit=1, steal=False, retry_failed=False):
        with self._lock:
            out, self.items = self.items[:limit], self.items[limit:]
            self.queue = {"pending": len(self.items), "claimed": len(out), "retry": 0}
        return out

    def complete(self, keys, status="done", error="", fetched=None):
        (self.done if status == "done" else self.failed).extend(keys)
        if status != "done":
            print(f"failed {list(keys)}: {error}", file=sys.stderr)

    def flush(self, note=None):
        pass

    def note(self, text):
        print(f"note: {text}", file=sys.stderr)

    def close(self, state="finished", note=""):
        print(f"(local) {state}{': ' + note if note else ''}; {self.records} records, counters {self.counts}", file=sys.stderr)
