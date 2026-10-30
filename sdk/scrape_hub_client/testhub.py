"""A local stand-in for scrape-hub, for checking a scraper without the real hub (used by `scrape-check`).

It answers the same runner API as https://scrape.s1d.me with the same validation rules (project and key names, record ids, failure
kinds, upload paths, pool entries, health checks), keeps everything in memory, stores uploads in a local folder, and can report
"paused" for the first seconds of a run so a scraper's handling of pauses shows. Standard library only.

    hub = TestHub(pause_s=5, claimable=3)   # at most 3 items handed out
    hub.start()                              # hub.url -> http://127.0.0.1:<port>
    ...  run a scraper with SCRAPE_HUB_URL=hub.url, SCRAPE_HUB_TOKEN=anything
    hub.stop(); hub.state                    # projects, queue, runs, records, metrics, failures, uploads, events
"""
import hashlib
import json
import re
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
FAMILY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
PATH_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
ENV_RESERVED = ("PATH", "PYTHON", "LD_", "HOME", "ACTIONS_", "GITHUB_", "RUNNER_", "SCRAPE_")
PROBE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
METRIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class Reject(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


class TestHub:
    __test__ = False      # not a pytest test class

    def __init__(self, pause_s=0.0, claimable=None, threads=2, delay_ms=0, store_dir=None):
        self.pause_s, self.claimable, self.threads, self.delay_ms = pause_s, claimable, threads, delay_ms
        self.store = Path(store_dir or ".scrape-check/uploads")
        self.lock = threading.Lock()
        self.t0 = time.time()
        self.state = {"projects": {}, "queue": {}, "runs": {}, "records": {}, "metrics": {}, "failures": {}, "uploads": {},
                      "probes": {}, "events": [], "handed_out": 0}
        self._srv = None

    # ------------------------------------------------------------------ server
    def start(self):
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None, raw=None, headers=()):
                data = raw if raw is not None else json.dumps(obj if obj is not None else {}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json" if raw is None else "application/octet-stream")
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _do(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                try:
                    if self.path.startswith("/_store/"):
                        return hub._store(self, method, unquote(self.path[len("/_store/"):]), body)
                    if not self.headers.get("Authorization", "").startswith("Bearer "):
                        raise Reject(401, "no token (set SCRAPE_HUB_TOKEN when testing outside GitHub Actions)")
                    doc = json.loads(body) if body else {}
                    out = hub.handle(method, self.path, doc)
                    self._send(200, out)
                except Reject as e:
                    hub._event("rejected", {"method": method, "path": self.path.split("?")[0], "code": e.code, "detail": e.msg})
                    self._send(e.code, {"detail": e.msg})
                except (ValueError, KeyError, TypeError) as e:
                    hub._event("rejected", {"method": method, "path": self.path.split("?")[0], "code": 422, "detail": f"{type(e).__name__}: {e}"})
                    self._send(422, {"detail": f"bad request: {type(e).__name__}: {e}"})

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

            def do_PUT(self):
                self._do("PUT")

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._srv.server_port}"
        return self

    def stop(self):
        if self._srv:
            self._srv.shutdown()

    # ------------------------------------------------------------------ helpers
    def _event(self, kind, detail=None):
        with self.lock:
            self.state["events"].append({"t": round(time.time() - self.t0, 3), "kind": kind, **(detail or {})})

    def paused(self):
        return time.time() - self.t0 < self.pause_s

    def settings(self):
        p = self.paused()
        return {"threads": self.threads, "delay_ms": self.delay_ms, "paused": p, "auto_backoff": True, "max_claimed": 0,
                "throttled": p, "reason": "test hub: paused for the first seconds of the check" if p else ""}

    def _run(self, run_id):
        run = self.state["runs"].get(int(run_id))
        if not run:
            raise Reject(404, "run not found")
        run["last_seen"] = time.time()
        return run

    def _project(self, name):
        if not NAME_RE.match(name or ""):
            raise Reject(422, "project name must be 2-64 chars of a-z 0-9 - _")
        return self.state["projects"].setdefault(name, {"settings": {}})

    # ------------------------------------------------------------------ API
    def handle(self, method, path, doc):
        u = urlsplit(path)
        parts = [unquote(p) for p in u.path.strip("/").split("/")]
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if parts[:3] == ["api", "v1", "projects"] and len(parts) >= 4:
            return self._projects_api(method, parts[3], parts[4:], doc, q)
        if parts[:3] == ["api", "v1", "families"] and len(parts) == 5 and parts[4] == "probes" and method == "PUT":
            return self._probes(parts[3], doc)
        if parts[:3] == ["api", "v1", "runs"]:
            if len(parts) == 3 and method == "POST":
                return self._run_start(doc)
            if len(parts) >= 5 and method == "POST":
                return self._run_call(parts[3], "/".join(parts[4:]), doc)
        if parts[:4] == ["api", "v1", "pool", "assign"]:
            return {"work": None, "reason": "done"}
        raise Reject(404, f"the hub has no {method} {u.path}")

    def _projects_api(self, method, name, rest, doc, q):
        p = self._project(name)
        if not rest and method == "PUT":
            p.update({k: doc.get(k) for k in ("title", "unit_label", "record_label", "expected_units", "expected_records")})
            self._event("project", {"project": name})
            return {"ok": True}
        if rest == ["pool"] and method == "PUT":
            fam = doc.get("family", "")
            if not FAMILY_RE.match(fam):
                raise Reject(422, f"family {fam!r} is not a valid family name")
            if not isinstance(doc.get("task"), str) or not doc["task"]:
                raise Reject(422, "task is required")
            env = doc.get("env") or {}
            bad = [k for k in env if not ENV_RE.match(k) or k.startswith(ENV_RESERVED)]
            if bad:
                raise Reject(422, f"env names the runner would drop: {bad} (A-Z 0-9 _, not starting with {', '.join(ENV_RESERVED)})")
            if len(env) > 30 or any(len(str(v)) > 1000 for v in env.values()):
                raise Reject(422, "env too large")
            if doc.get("geo") not in (None, "") and not (isinstance(doc["geo"], str) and len(doc["geo"]) == 2 and doc["geo"].isupper()):
                raise Reject(422, f"geo must be a two-letter country code such as IN, got {doc['geo']!r}")
            old = p["settings"].get("family")
            if old and old != fam:
                raise Reject(403, "this project belongs to another family")
            p["settings"].update({k: v for k, v in doc.items() if v is not None})
            self._event("pool-entry", {"project": name, "family": fam, "task": doc["task"]})
            return {"ok": True}
        if rest == ["queue"] and method == "POST":
            added = reset = 0
            qd = self.state["queue"].setdefault(name, {})
            for it in doc.get("items", []):
                k = it.get("key")
                if not isinstance(k, str) or not 1 <= len(k) <= 200:
                    raise Reject(422, f"queue key {k!r}: 1-200 characters")
                if not isinstance(it.get("payload", {}), dict):
                    raise Reject(422, f"queue item {k}: payload must be an object")
                if k not in qd:
                    pid = it.get("partition_id")
                    qd[k] = {"key": k, "payload": it.get("payload", {}), "status": "pending", "attempts": 0, "fetched": 0, "error": "",
                             "partition_id": pid if pid is not None else zlib.crc32(k.encode()) % 1_000_000, "order": len(qd)}
                    added += 1
            if doc.get("reset_failed"):
                for it in qd.values():
                    if it["status"] == "failed":
                        it["status"], reset = "pending", reset + 1
            return {"added": added, "reset": reset}
        if rest == ["queue"] and method == "GET":
            st, after, limit = q.get("status", "pending"), q.get("after", ""), int(q.get("limit", 1000))
            items = sorted((i for i in self.state["queue"].get(name, {}).values() if i["status"] == st and i["key"] > after), key=lambda i: i["key"])[:limit]
            return {"items": [{k: i[k] for k in ("key", "partition_id", "status", "attempts", "payload")} for i in items],
                    "next": items[-1]["key"] if len(items) == limit else None}
        raise Reject(404, f"the hub has no {method} /api/v1/projects/{name}/{'/'.join(rest)}")

    def _probes(self, family, doc):
        if not FAMILY_RE.match(family):
            raise Reject(422, "bad family name")
        out = []
        for t in doc.get("targets", []):
            if not PROBE_RE.match(t.get("name", "")):
                raise Reject(422, f"health check name {t.get('name')!r}: letters, digits and . _ : / -")
            if not str(t.get("url", "")).startswith(("http://", "https://")):
                raise Reject(422, f"health check {t.get('name')}: url must start with http(s)://")
            out.append({"name": t["name"], "url": t["url"], "expect": t.get("expect", "")})
        self.state["probes"][family] = out
        self._event("probes", {"family": family, "targets": len(out)})
        return {"family": family, "targets": len(out)}

    def _run_start(self, doc):
        self._project(doc.get("project", ""))
        with self.lock:
            rid = len(self.state["runs"]) + 1
            self.state["runs"][rid] = {"id": rid, "project": doc["project"], "job_index": int(doc.get("job_index", 0)),
                                       "total_jobs": int(doc.get("total_jobs", 1)), "state": "running", "started": time.time(),
                                       "last_seen": time.time(), "records": 0, "note": doc.get("note", "")}
        self._event("run-start", {"run": rid, "project": doc["project"]})
        return {"run_id": rid, "settings": self.settings()}

    def _run_call(self, run_id, what, doc):
        run = self._run(run_id)
        proj = run["project"]
        if what == "heartbeat":
            if doc.get("note"):
                run["note"] = str(doc["note"])[:200]
            return {"ok": True, "settings": self.settings()}
        if what == "batches":
            recs = doc.get("records") or []
            store = self.state["records"].setdefault(proj, {})
            for r in recs:
                rid = r.get("record_id")
                if not isinstance(rid, str) or not rid:
                    raise Reject(422, f"record_id must be a non-empty string, got {rid!r}")
                json.dumps(r.get("data"))
                store[rid] = {"data": r.get("data"), "run": run["id"], "writes": store.get(rid, {}).get("writes", 0) + 1}
            met = self.state["metrics"].setdefault(proj, {})
            for k, v in (doc.get("metrics") or {}).items():
                if not METRIC_RE.match(k):
                    raise Reject(422, f"metric name {k!r}: letters, digits, _ and -")
                met[k] = met.get(k, 0) + float(v)
            for k in ("errors", "blocked"):
                met["_" + k] = met.get("_" + k, 0) + int(doc.get(k) or 0)
            fails = self.state["failures"].setdefault(proj, {})
            for f in doc.get("failures") or []:
                if not KIND_RE.match(f.get("kind", "")) or not f.get("key"):
                    raise Reject(422, f"failure kind {f.get('kind')!r} / key {f.get('key')!r}")
                fails[(f["kind"], f["key"])] = f.get("reason", "")
            for r in recs:
                for key in [k for k in fails if k[1] == r["record_id"]]:
                    fails.pop(key)
            run["records"] += len(recs)
            if recs or doc.get("metrics"):
                self._event("batch", {"records": len(recs), "n_http": (doc.get("metrics") or {}).get("n_http", 0)})
            return {"ok": True, "settings": self.settings()}
        if what == "claim":
            if doc.get("project") != proj:
                raise Reject(403, "a run can only claim from its own project")
            limit = max(1, min(int(doc.get("limit", 1)), 100))
            qd = self.state["queue"].get(proj, {})
            s = self.settings()
            items = []
            with self.lock:
                if not s["paused"]:
                    cands = sorted(qd.values(), key=lambda i: i["order"])
                    mine = [i for i in cands if i["status"] == "pending" and i["partition_id"] % max(1, run["total_jobs"]) == run["job_index"]]
                    pool = mine or ([i for i in cands if i["status"] == "pending"] if doc.get("steal") else [])
                    for i in pool:
                        if len(items) >= limit or (self.claimable is not None and self.state["handed_out"] >= self.claimable):
                            break
                        i["status"], i["attempts"], i["claimed_by"] = "claimed", i["attempts"] + 1, run["id"]
                        self.state["handed_out"] += 1
                        items.append({"key": i["key"], "payload": i["payload"], "attempts": i["attempts"]})
                left = sum(1 for i in qd.values() if i["status"] == "pending")
                if self.claimable is not None and self.state["handed_out"] >= self.claimable:
                    left = 0
                counts = {"pending": left, "claimed": sum(1 for i in qd.values() if i["status"] == "claimed"), "retry": 0}
            self._event("claim", {"got": len(items), "paused": s["paused"], "keys": [i["key"] for i in items]})
            return {"items": items, "settings": s, "queue": counts}
        if what == "complete":
            st = doc.get("status", "done")
            if st not in ("done", "failed"):
                raise Reject(422, f"status must be done or failed, got {st!r}")
            qd = self.state["queue"].get(proj, {})
            for k in doc.get("keys", []):
                it = qd.get(k)
                if not it or it.get("claimed_by") != run["id"] or it["status"] != "claimed":
                    raise Reject(409, f"item {k!r} is not claimed by this run")
                it["status"], it["error"] = st, str(doc.get("error", ""))[:500]
                if doc.get("fetched") is not None:
                    it["fetched"] = int(doc["fetched"])
            self._event("complete", {"keys": doc.get("keys", []), "status": st, "fetched": doc.get("fetched")})
            return {"ok": True, "settings": self.settings()}
        if what == "finish":
            run["state"] = doc.get("state", "finished")
            run["note"] = doc.get("note", "") or run["note"]
            self._event("run-finish", {"run": run["id"], "state": run["state"], "note": run["note"]})
            return {"ok": True}
        if what == "upload-urls":
            out = []
            files = doc.get("files") or []
            if not 1 <= len(files) <= 300:
                raise Reject(422, "1-300 files per call")
            for f in files:
                p = f.get("path", "")
                if not PATH_RE.match(p) or any(seg in (".", "..") for seg in p.split("/")):
                    raise Reject(422, f"bad path {p!r}: use letters, digits . _ - and / between folders")
                key = f"{proj}/{p}"
                out.append({"path": p, "key": key, "url": f"{self.url}/_store/{quote(key)}", "headers": {"Content-Type": f.get("content_type", "application/octet-stream")}})
            return {"urls": out, "expires_in": 900, "store": "test"}
        if what == "read-urls":
            return {"urls": {k: f"{self.url}/_store/{quote(k)}" for k in doc.get("keys", [])}, "expires_in": 900}
        raise Reject(404, f"the hub has no endpoint {what!r} (a family-app extra; the test hub doesn't know your family's app)")

    def _store(self, h, method, key, body):
        path = self.store / key
        if method == "PUT":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            md5 = hashlib.md5(body).hexdigest()
            with self.lock:
                self.state["uploads"][key] = {"bytes": len(body), "md5": md5}
            self._event("upload", {"key": key, "bytes": len(body)})
            return h._send(200, raw=b"", headers=[("ETag", f'"{md5}"')])
        if method == "GET" and path.is_file():
            return h._send(200, raw=path.read_bytes())
        return h._send(404, {"detail": "no such object"})
