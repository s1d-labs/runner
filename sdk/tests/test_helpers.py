"""SDK 0.10.0 helpers: stop_flag, timed_request / timed_get / count_request, LocalHub, tls.ca_bundle, and scrape-check's paused-family
WARN. No internet needed: the "site" is a local HTTP server."""
import io
import json
import os
import re
import signal
import ssl
import sys
import threading
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scrape_hub_client as shc  # noqa: E402
from scrape_hub_client import LocalHub, ca_bundle, check, count_request, stop_flag, timed_get, timed_post, timed_request  # noqa: E402
from scrape_hub_client import tls  # noqa: E402

from test_check import family, site  # noqa: E402,F401  (fixture)


def test_version_and_exports():
    assert shc.__version__ == "0.10.0"
    for name in ("HubClient", "PoolClient", "HubRejected", "register_project", "seed_queue", "list_queue", "replace_queue", "set_pool",
                 "set_probes", "LocalHub", "stop_flag", "timed_request", "timed_get", "timed_post", "count_request", "ca_bundle"):
        assert name in shc.__all__ and hasattr(shc, name), name


# -- stop_flag ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def restore_signals():
    old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    shc.helpers._stop.clear()
    yield
    for s, h in old.items():
        signal.signal(s, h)
    shc.helpers._stop.clear()


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_stop_flag_is_set_by_sigint_and_sigterm(restore_signals, sig):
    stop = stop_flag()
    assert isinstance(stop, threading.Event) and not stop.is_set()
    os.kill(os.getpid(), sig)              # the handler only sets the flag: no KeyboardInterrupt, the process lives on
    assert stop.wait(2)
    assert stop_flag() is stop and stop_flag(install=False) is stop


def test_stop_flag_without_install_leaves_the_handlers(restore_signals):
    before = signal.getsignal(signal.SIGINT)
    stop = stop_flag(install=False)
    assert signal.getsignal(signal.SIGINT) is before and not stop.is_set()


# -- request metrics ----------------------------------------------------------------------------------------------------------------
class Counter:
    def __init__(self):
        self.counts, self.errors, self.blocked = {}, 0, 0

    def metric(self, name, value=1):
        self.counts[name] = self.counts.get(name, 0) + value

    def error(self, n=1):
        self.errors += n

    def block(self, n=1):
        self.blocked += n


@pytest.fixture(scope="module")
def codes_site():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _answer(self):
            code = int(self.path.strip("/") or 200)
            body = json.dumps({"method": self.command, "path": self.path}).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_timed_get_counts_a_good_answer(codes_site):
    hub = Counter()
    r = timed_get(hub, requests.Session(), codes_site + "/200")
    assert r.status_code == 200 and hub.counts["n_http"] == 1 and hub.counts["t_http_s"] > 0
    assert set(hub.counts) == {"n_http", "t_http_s"} and hub.errors == hub.blocked == 0


@pytest.mark.parametrize("code,blocked", [(404, 0), (500, 0), (403, 1), (429, 1), (503, 1)])
def test_timed_get_counts_other_codes_and_refusals(codes_site, code, blocked):
    hub = Counter()
    r = timed_get(hub, requests.Session(), f"{codes_site}/{code}")
    assert r is not None and r.status_code == code             # every answer comes back: the caller decides
    assert hub.counts["n_http"] == 1 and hub.counts[f"http_{code}"] == 1 and hub.blocked == blocked and hub.errors == 0


def test_timed_request_on_a_dead_host_counts_http_error():
    hub = Counter()
    s = requests.Session()
    assert timed_get(hub, s, "http://127.0.0.1:9/", timeout=2) is None
    assert hub.counts == {"http_error": 1} and hub.errors == 1
    with pytest.raises(requests.RequestException):
        timed_get(hub, s, "http://127.0.0.1:9/", timeout=2, raise_errors=True)
    assert hub.counts == {"http_error": 2} and hub.errors == 2


def test_timed_post_passes_arguments_and_works_with_the_requests_module(codes_site):
    hub = Counter()
    r = timed_post(hub, requests, codes_site + "/201", json={"a": 1})
    assert r.json()["method"] == "POST" and hub.counts["http_201"] == 1
    r = timed_request(None, requests.Session(), "GET", codes_site + "/200")     # no hub: no metrics, still the response
    assert r.status_code == 200


def test_timed_request_sets_a_default_timeout():
    seen = {}

    class S:
        def request(self, method, url, **kw):
            seen.update(kw)
            return type("R", (), {"status_code": 200})()

    timed_get(Counter(), S(), "http://x/")
    assert seen["timeout"] == 30
    timed_get(Counter(), S(), "http://x/", timeout=(5, 60))
    assert seen["timeout"] == (5, 60)


def test_count_request_for_other_clients():
    hub = Counter()
    count_request(hub, 200, 0.5)
    count_request(hub, 429, 0.1)
    count_request(hub, None)
    count_request(None, 200, 1.0)
    assert hub.counts == {"n_http": 2, "t_http_s": 0.6, "http_429": 1, "http_error": 1} and hub.blocked == 1 and hub.errors == 1


def test_timed_get_with_a_real_hub_client_reports_to_the_hub(tmp_path, monkeypatch, codes_site):
    from scrape_hub_client.testhub import TestHub
    hub = TestHub(store_dir=tmp_path / "up").start()
    monkeypatch.setenv("SCRAPE_HUB_TOKEN", "t")
    monkeypatch.setattr(shc.HubClient, "_egress_ip", staticmethod(lambda: ""))
    try:
        shc.register_project("demo-m", hub_url=hub.url)
        c = shc.HubClient("demo-m", hub_url=hub.url)
        timed_get(c, requests.Session(), codes_site + "/200")
        timed_get(c, requests.Session(), codes_site + "/503")
        c.close()
        m = hub.state["metrics"]["demo-m"]
        assert m["n_http"] == 2 and m["http_503"] == 1 and m["t_http_s"] > 0
        assert not [e for e in hub.state["events"] if e["kind"] == "rejected"]
    finally:
        hub.stop()


# -- LocalHub -----------------------------------------------------------------------------------------------------------------------
def test_local_hub_matches_the_family_copies(tmp_path, capsys):
    hub = LocalHub()
    assert hub.settings == {"threads": 1, "delay_ms": 0, "paused": False} and LocalHub.settings == hub.settings
    hub.metric("n_http"); hub.metric("t_http_s", 0.25); hub.error(); hub.block(2)
    assert hub.counts == {"n_http": 1, "t_http_s": 0.25, "errors": 1, "blocked": 2}
    hub.record("r1", {"t": "ü"})
    assert json.loads(capsys.readouterr().out) == {"record_id": "r1", "data": {"t": "ü"}}

    hub = LocalHub("proj", threads=4, delay_ms=500, save_dir=str(tmp_path), items=[{"key": "a", "payload": {}}, {"key": "b", "payload": {}}])
    assert hub.settings["threads"] == 4 and hub.settings["delay_ms"] == 500
    (up,) = hub.upload_many([("pages/a.html", b"<p>", "text/html")])
    assert (tmp_path / "pages/a.html").read_bytes() == b"<p>" and up["key"] == "proj/pages/a.html" and up["bytes"] == 3 and len(up["sha256"]) == 64
    assert [i["key"] for i in hub.claim(1)] == ["a"] and hub.queue["pending"] == 1
    hub.complete(["a"], "done", fetched=1); hub.complete(["b"], "failed", error="x")
    assert hub.done == ["a"] and hub.failed == ["b"]
    assert [i["key"] for i in hub.claim(5)] == ["b"] and hub.claim(1) == []
    hub.failure("page", "k", "gone", url="u"); hub.clear_failure("page", "k"); hub.flush(); hub.note("n"); hub.close()
    err = capsys.readouterr().err
    assert "failure page k: gone" in err and "finished" in err


def test_local_hub_works_as_a_metrics_target(codes_site):
    hub = LocalHub()
    timed_get(hub, requests.Session(), codes_site + "/429")
    assert hub.counts == {"n_http": 1, "t_http_s": hub.counts["t_http_s"], "http_429": 1, "blocked": 1}


# -- tls.ca_bundle ------------------------------------------------------------------------------------------------------------------
PEM = re.compile(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)


def test_bundled_certificates_are_one_per_file_and_distinct():
    files = tls.bundled_pems()
    assert len(files) >= 13
    blocks = [PEM.findall(f.read_bytes()) for f in files]
    assert all(len(b) == 1 for b in blocks), [f.name for f, b in zip(files, blocks) if len(b) != 1]
    assert len({b[0] for b in blocks}) == len(files)


def test_ca_bundle_is_certifi_plus_the_intermediates_and_loads_in_ssl():
    import certifi
    path = ca_bundle()
    data = Path(path).read_bytes()
    assert data.startswith(Path(certifi.where()).read_bytes().rstrip(b"\n"))
    for f in tls.bundled_pems():
        assert PEM.findall(f.read_bytes())[0] in data
    assert ca_bundle() == path                                   # written once per process
    ctx = ssl.create_default_context(cafile=path)                # a valid CA file: verification stays on
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert len(ctx.get_ca_certs()) >= len(PEM.findall(Path(certifi.where()).read_bytes()))


def test_ca_bundle_takes_extra_pems_and_dedupes():
    one = tls.bundled_pems()[0]
    text = PEM.findall(one.read_bytes())[0]
    base = Path(ca_bundle()).read_bytes()
    assert Path(ca_bundle([one, text, text.decode()])).read_bytes() == base   # already bundled: nothing added
    with pytest.raises(ValueError):
        ca_bundle([b"not a certificate"])


def test_bundled_certificates_are_ca_certificates_that_chain_to_certifi():
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.x509.oid import ExtensionOID
    import certifi
    roots = set()
    for b in PEM.findall(Path(certifi.where()).read_bytes()):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")     # one old root has a negative serial number
                roots.add(x509.load_pem_x509_certificate(b).subject)
        except ValueError:
            pass
    certs = [x509.load_pem_x509_certificate(f.read_bytes()) for f in tls.bundled_pems()]
    subjects = {c.subject for c in certs}
    for c in certs:
        assert c.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value.ca, c.subject
        assert c.subject not in roots or c.issuer != c.subject, c.subject          # never a copy of a root that certifi has
        assert c.issuer in roots or c.issuer in subjects, c.issuer                  # issued by a certifi root, or by another bundled CA


def test_package_data_lists_the_certificates():
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    assert 'scrape_hub_client = ["certs/*.pem"]' in text


# -- scrape-check: a paused family --------------------------------------------------------------------------------------------------
def test_a_paused_family_is_a_warning_not_a_failure(tmp_path, site):  # noqa: F811
    d = family(tmp_path, site)
    f = d / "setup_family.py"
    f.write_text(f.read_text().replace('probe="demo:site")', 'probe="demo:site", pool=False)'))
    assert check.main(["--dir", str(d), "--items", "1", "--timeout", "60"]) == 0
    rows = json.loads((d / ".scrape-check/report.json").read_text())
    assert not [r for r in rows if r["level"] == "FAIL"], rows
    (w,) = [r for r in rows if r["check"] == "pool entries"]
    assert w["level"] == "WARN" and "pool=False" in w["detail"]
    # --project still runs the paused project's task
    assert check.main(["--dir", str(d), "--project", "demo-items", "--items", "1", "--pause", "0", "--no-rerun", "--timeout", "60"]) == 0
    assert (d / ".scrape-check/records.jsonl").read_text().count("\n") == 2


def test_no_pool_entry_at_all_still_fails(tmp_path, site):  # noqa: F811
    d = family(tmp_path, site)
    f = d / "setup_family.py"
    f.write_text("\n".join(l for l in f.read_text().splitlines() if "set_pool(" not in l))
    assert check.main(["--dir", str(d), "--items", "1", "--timeout", "30"]) == 1
    rows = json.loads((d / ".scrape-check/report.json").read_text())
    assert [r for r in rows if r["check"] == "pool entries"][0]["level"] == "FAIL"
