"""The local stand-in hub and scrape-check: a good family passes, broken ones fail with the right checks. No internet needed: the
"site" is a local HTTP server."""
import json
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrape_hub_client import HubClient, HubRejected, check  # noqa: E402
from scrape_hub_client.testhub import TestHub  # noqa: E402


@pytest.fixture(scope="module")
def site():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            n = self.path.rsplit("-", 1)[-1]
            body = f"<html><li id='a{n}'>Item A{n}</li><li id='b{n}'>Item B{n}</li></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


GOOD = '''
import os, re, sys, time, requests
from scrape_hub_client import HubClient
hub = HubClient(os.environ["SCRAPE_POOL_PROJECT"], job_index=int(os.environ["SCRAPE_JOB_INDEX"]), total_jobs=int(os.environ["SCRAPE_TOTAL_JOBS"]))
while True:
    if hub.settings.get("paused"):
        time.sleep(1); continue
    items = hub.claim(1, steal=True, retry_failed=True)
    if not items:
        break
    it = items[0]
    t = time.time(); r = requests.get(it["payload"]["url"], timeout=10)
    hub.metric("n_http"); hub.metric("t_http_s", time.time() - t)
    rows = re.findall(r"<li id='([^']+)'>([^<]+)</li>", r.text)
    for rid, title in rows:
        hub.record(rid, {"title": title})
    hub.upload_many([(f"pages/{it['key']}.html", r.content, "text/html")])
    hub.complete([it["key"]], "done", fetched=len(rows))
hub.close()
'''


def family(tmp_path, site, crawl=GOOD, name="demo"):
    d = tmp_path / name
    d.mkdir()
    (d / "scrape.json").write_text(json.dumps({"family": name, "setup": [], "tasks": {"crawl": ["python", "crawl.py"]}}))
    (d / "crawl.py").write_text(crawl)
    (d / "setup_family.py").write_text(textwrap.dedent(f'''
        from scrape_hub_client import register_project, seed_queue, set_pool, set_probes
        set_probes("{name}", [{{"name": "{name}:site", "url": "{site}/", "expect": "Item"}}])
        register_project("{name}-items")
        set_pool("{name}-items", "{name}", "crawl", target="127.0.0.1", env={{"SITE": "{site}"}}, probe="{name}:site")
        seed_queue("{name}-items", [{{"key": f"p-{{n}}", "payload": {{"url": "{site}/page-%d" % n, "expected": 2}}}} for n in range(1, 6)])
        '''))
    return d


def test_testhub_speaks_the_client_api(tmp_path, monkeypatch):
    hub = TestHub(store_dir=tmp_path / "up").start()
    monkeypatch.setenv("SCRAPE_HUB_TOKEN", "t")
    try:
        c = HubClient.__new__(HubClient)
        from scrape_hub_client import register_project, seed_queue, set_pool
        register_project("demo-x", hub_url=hub.url)
        set_pool("demo-x", "demo", "crawl", target="h", hub_url=hub.url)
        assert seed_queue("demo-x", [{"key": "a", "payload": {}}, {"key": "b", "payload": {}}], hub_url=hub.url)["added"] == 2
        c = HubClient("demo-x", hub_url=hub.url)
        items = c.claim(5)
        assert {i["key"] for i in items} == {"a", "b"}
        c.record("r1", {"x": 1}); c.metric("n_http")
        assert c.upload_many([("pages/a.txt", b"hello", "text/plain")])[0]["key"] == "demo-x/pages/a.txt"
        c.complete(["a", "b"], "done", fetched=1)
        c.close()
        assert hub.state["records"]["demo-x"]["r1"]["data"] == {"x": 1} and hub.state["metrics"]["demo-x"]["n_http"] == 1
        assert (tmp_path / "up/demo-x/pages/a.txt").read_bytes() == b"hello"
        with pytest.raises(HubRejected):
            set_pool("demo-y", "demo", "crawl", env={"GITHUB_TOKEN": "x"}, hub_url=hub.url)     # the runner would drop it
        with pytest.raises(Exception):
            c2 = HubClient("demo-x", hub_url=hub.url)
            c2.upload_many([("../escape", b"x", "text/plain")], attempts=1)
    finally:
        hub.stop()


def test_a_good_family_passes(tmp_path, site):
    d = family(tmp_path, site)
    assert check.main(["--dir", str(d), "--items", "3", "--pause", "2", "--timeout", "120"]) == 0
    rows = json.loads((d / ".scrape-check/report.json").read_text())
    assert not [r for r in rows if r["level"] in ("FAIL", "WARN")], rows
    assert (d / ".scrape-check/records.jsonl").read_text().count("\n") == 6


def test_ignoring_pause_and_metrics_fails(tmp_path, site):
    bad = GOOD.replace('    if hub.settings.get("paused"):\n        time.sleep(1); continue\n', "").replace(
        'hub.metric("n_http"); hub.metric("t_http_s", time.time() - t)', "pass")
    d = family(tmp_path, site, bad)
    assert check.main(["--dir", str(d), "--items", "2", "--pause", "3", "--no-rerun", "--timeout", "60"]) == 1
    failed = {r["check"] for r in json.loads((d / ".scrape-check/report.json").read_text()) if r["level"] == "FAIL"}
    assert "[run 1] waits while the hub says paused" in failed and "[run 1] claims the queue items" in failed


def test_unfinished_items_and_unstable_ids_fail(tmp_path, site):
    bad = GOOD.replace("hub.record(rid,", "import uuid; hub.record(uuid.uuid4().hex,").replace(
        'hub.complete([it["key"]], "done", fetched=len(rows))', "hub.flush()")
    d = family(tmp_path, site, bad)
    assert check.main(["--dir", str(d), "--items", "2", "--pause", "0", "--timeout", "60"]) == 1
    failed = {r["check"] for r in json.loads((d / ".scrape-check/report.json").read_text()) if r["level"] == "FAIL"}
    assert {"[run 1] completes every item it claims", "rerun keeps record ids"} <= failed


def test_bad_manifest_and_pool_entry_fail(tmp_path, site):
    d = family(tmp_path, site)
    (d / "scrape.json").write_text(json.dumps({"family": "demo", "tasks": {"crawl": ["python", "missing.py"]}}))
    assert check.main(["--dir", str(d), "--items", "1", "--timeout", "30"]) == 1
    failed = {r["check"] for r in json.loads((d / ".scrape-check/report.json").read_text()) if r["level"] == "FAIL"}
    assert "task crawl" in failed
