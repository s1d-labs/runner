"""scrape-check: verify that a scraper family works with scrape-hub, locally, before it runs on the pool.

    scrape-check                       # in a family repo: setup + one task on 3 real queue items, then a rerun
    scrape-check --items 5 --task crawl --project myfamily-books
    scrape-check --project gepnic-wbtenders --task crawl --env PORTAL=wbtenders.gov.in \\
                 --item '{"key": "...", "payload": {...}}'        # families without setup_family.py

It starts a local stand-in for the hub (testhub.TestHub), runs `setup_family.py` against it (if the repo has one), then runs the
task's command from scrape.json exactly as the pool runner does (same env vars, cwd, argv), on the first N queue items of one
project, with the hub reporting "paused" for the first seconds. Then it reruns the same items. Real pages are fetched (N of them,
twice), so the check also shows the scraper works against the live site. Results: one line per check (PASS / WARN / FAIL),
exit code 1 if anything failed; details in .scrape-check/ (report.json, records.jsonl, uploads/, task logs).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .testhub import FAMILY_RE, TestHub

TASK_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


class Report:
    def __init__(self):
        self.rows = []

    def add(self, level, check, detail=""):
        self.rows.append({"level": level, "check": check, "detail": detail})
        colour = {"PASS": "32", "WARN": "33", "FAIL": "31"}.get(level) if sys.stdout.isatty() else None
        mark = f"\033[{colour}m{level}\033[0m" if colour else level
        print(f"  {mark}  {check}" + (f": {detail}" if detail else ""), flush=True)

    def ok(self, cond, check, good="", bad="", warn=False):
        self.add("PASS" if cond else ("WARN" if warn else "FAIL"), check, good if cond else bad)
        return cond

    @property
    def failed(self):
        return any(r["level"] == "FAIL" for r in self.rows)


def load_manifest(d, rep):
    f = d / "scrape.json"
    try:
        m = json.loads(f.read_text())
    except (OSError, ValueError) as e:
        rep.add("FAIL", "scrape.json", f"can't read it ({e})")
        return None
    fam = m.get("family", "")
    rep.ok(bool(FAMILY_RE.match(fam)), "scrape.json family name", fam, f"{fam!r}: a-z 0-9 _ -, at most 41 characters")
    tasks = m.get("tasks") or {}
    good = isinstance(tasks, dict) and tasks and all(TASK_RE.match(k) and isinstance(v, list) and v and all(isinstance(x, str) for x in v)
                                                    for k, v in tasks.items())
    rep.ok(good, "scrape.json tasks", ", ".join(tasks) if good else "", "tasks must map names (a-z 0-9 _ -) to commands (lists of strings)")
    for t, cmd in (tasks.items() if good else []):
        script = next((x for x in cmd[1:] if x.endswith(".py")), None)
        if script and not (d / script).is_file():
            rep.add("FAIL", f"task {t}", f"{script} is not in the repo")
    setup = m.get("setup", [])
    rep.ok(isinstance(setup, list) and all(isinstance(x, str) for x in setup), "scrape.json setup", f"{len(setup)} step(s)", "setup must be a list of shell commands")
    if d.name != fam and fam:
        rep.add("INFO", "repo name", f"the folder is {d.name!r}; on GitHub the repo has to be s1d-labs/{fam}")
    return m if good else None


def base_env(hub, family):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SCRAPE_", "ACTIONS_ID_TOKEN"))}
    env.update({"SCRAPE_HUB_URL": hub.url, "SCRAPE_HUB_TOKEN": "scrape-check", "SCRAPE_HUB_REPO": f"s1d-labs/{family}",
                "PYTHONUNBUFFERED": "1"})
    here = str(Path(__file__).resolve().parent.parent)          # the scraper gets the same client as this check
    env["PYTHONPATH"] = here + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def run_setup(d, hub, family, rep, out):
    f = d / "setup_family.py"
    if not f.is_file():
        rep.add("INFO", "setup_family.py", "not in this repo: give --project, --task and --item")
        return True
    t = time.time()
    p = subprocess.run([sys.executable, "setup_family.py"], cwd=d, env=base_env(hub, family), capture_output=True, text=True, timeout=300)
    (out / "setup.log").write_text(p.stdout + p.stderr)
    if not rep.ok(p.returncode == 0, "setup_family.py runs", f"{time.time() - t:.1f} s",
                  f"exit {p.returncode}: {(p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout).strip() else ''}"):
        return False
    st = hub.state
    entries = {n: p["settings"] for n, p in st["projects"].items() if p["settings"].get("pool")}
    rep.ok(bool(entries), "pool entries", f"{len(entries)} project(s): {', '.join(list(entries)[:5])}", "setup put no project in the pool (set_pool)")
    probes = {t["name"] for t in st["probes"].get(family, [])}
    rep.ok(bool(probes), "health checks", f"{len(probes)} registered", "none registered (set_probes): work can't be held when the site is down", warn=True)
    return True


def check_entries(m, hub, family, rep):
    tasks = m["tasks"]
    probes = {t["name"] for t in hub.state["probes"].get(family, [])}
    for name, s in hub.state["projects"].items():
        s = s["settings"]
        if not s.get("pool"):
            continue
        problems = []
        if s.get("family") != family:
            problems.append(f"family is {s.get('family')!r}, scrape.json says {family!r}")
        if s.get("task") not in tasks:
            problems.append(f"task {s.get('task')!r} is not in scrape.json")
        if not s.get("target"):
            problems.append("no target host (the per-site worker cap needs it)")
        if s.get("probe") and probes and s["probe"] not in probes:
            problems.append(f"probe {s['probe']!r} is not one of the registered health checks")
        if not name.startswith(family):
            problems.append("name doesn't start with the family name (allowed, but it helps)")
        q = hub.state["queue"].get(name, {})
        if not q:
            problems.append("empty queue")
        level = "FAIL" if any(not p.startswith(("name doesn't", "empty queue")) for p in problems) else ("WARN" if problems else "PASS")
        rep.add(level, f"project {name}", "; ".join(problems) or f"task {s['task']}, target {s['target']}, {len(q)} queue items")


def run_task(d, m, hub, family, project, task, env_extra, items_n, pause_s, timeout, out, label):
    hub.t0, hub.pause_s, hub.claimable, hub.state["handed_out"] = time.time(), pause_s, items_n, 0
    hub.state["events"].clear()
    hub.state["metrics"].pop(project, None)          # counters per run
    cmd = list(m["tasks"][task])
    argv = [sys.executable if cmd[0] == "python" else cmd[0], *cmd[1:]]
    s = hub.state["projects"][project]["settings"]
    env = base_env(hub, family)
    env.update({k: str(v) for k, v in (s.get("env") or {}).items()})
    env.update(env_extra)
    env.update({"SCRAPE_POOL_PROJECT": project, "SCRAPE_POOL_TARGET": str(s.get("target") or ""), "SCRAPE_JOB_INDEX": "0",
                "SCRAPE_TOTAL_JOBS": "1", "SCRAPE_MAX_HOURS": f"{max(timeout, 600) / 3600:.3f}"})
    t = time.time()
    with open(out / f"task-{label}.log", "w") as log:
        try:
            p = subprocess.run(argv, cwd=d, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
            code = p.returncode
        except subprocess.TimeoutExpired:
            code = None
    return code, time.time() - t, list(hub.state["events"])


def tail(path, n=6):
    try:
        lines = Path(path).read_text(errors="replace").strip().splitlines()
        return " | ".join(l.strip()[:160] for l in lines[-n:])
    except OSError:
        return ""


def judge_run(rep, hub, project, code, secs, events, items_n, pause_s, out, label):
    runs = [e for e in events if e["kind"] == "run-start"]
    claims = [e for e in events if e["kind"] == "claim"]
    got = [k for e in claims for k in e["keys"]]
    completes = [e for e in events if e["kind"] == "complete"]
    done_keys = {k for e in completes if e["status"] == "done" for k in e["keys"]}
    failed_keys = {k for e in completes if e["status"] == "failed" for k in e["keys"]}
    finishes = [e for e in events if e["kind"] == "run-finish"]
    rejected = [e for e in events if e["kind"] == "rejected"]
    batches = [e for e in events if e["kind"] == "batch"]
    log = out / f"task-{label}.log"

    if not rep.ok(code is not None, f"[{label}] exits on its own", f"{secs:.0f} s", f"still running after {secs:.0f} s (stopped); log: {tail(log)}"):
        return set()
    rep.ok(code == 0, f"[{label}] exit code", "0", f"{code}; log: {tail(log)}")
    if not rep.ok(bool(runs), f"[{label}] starts a run on the hub (HubClient)", f"{len(runs)} run(s)", f"no run started; log: {tail(log)}"):
        return set()
    if pause_s:
        during = [e for e in claims if e["paused"]]
        exit_t = events[-1]["t"] if events else 0
        rep.ok(exit_t > pause_s or code is None, f"[{label}] waits while the hub says paused",
               f"{len(during)} claim(s) during the {pause_s:.0f} s pause, then carried on",
               f"exited after {exit_t:.1f} s, inside the {pause_s:.0f} s pause: an empty claim while paused means 'wait', not 'done' "
               f"(check hub.settings['paused'] and sleep)")
        if during and len(during) > pause_s * 4:
            rep.add("WARN", f"[{label}] polling while paused", f"{len(during)} claims in {pause_s:.0f} s; a few seconds' sleep between checks is gentler on the hub")
    rep.ok(len(got) == items_n, f"[{label}] claims the queue items", f"{len(got)} of {items_n}",
           f"{len(got)} of {items_n}: it stopped before the queue was empty (or never called hub.claim)")
    open_ = [k for k in got if k not in done_keys | failed_keys]
    rep.ok(not open_, f"[{label}] completes every item it claims", f"{len(done_keys)} done, {len(failed_keys)} failed",
           f"{len(open_)} left claimed: {open_[:3]} (call hub.complete(keys, 'done' or 'failed'))")
    errors = "; ".join({hub.state["queue"][project][k]["error"] for k in failed_keys if k in hub.state["queue"].get(project, {})})[:300]
    rep.ok(bool(done_keys) or not got, f"[{label}] at least one item done", "",
           f"all {len(failed_keys)} failed; errors: {errors}" if failed_keys else "no item was marked done")
    recs = {rid: r for rid, r in hub.state["records"].get(project, {}).items() if r["run"] in {e["run"] for e in runs}}
    rep.ok(bool(recs), f"[{label}] stores records", f"{len(recs)} record(s)", "no records stored (hub.record(id, data))", warn=not done_keys)
    fetched = sum((e.get("fetched") or 0) for e in completes if e["status"] == "done")
    if done_keys and fetched == 0:
        rep.add("WARN", f"[{label}] fetched counts", "complete(..., fetched=n) wasn't given; the dashboard's progress and ETAs use it")
    met = hub.state["metrics"].get(project, {})
    rep.ok(met.get("n_http", 0) > 0, f"[{label}] reports requests (n_http)", f"{met.get('n_http', 0):.0f}",
           "no n_http counted: the hub's speed control and the overview run on it (hub.metric('n_http') per request)")
    rep.ok(met.get("t_http_s", 0) > 0, f"[{label}] reports request time (t_http_s)", f"{met.get('t_http_s', 0):.1f} s in all",
           "no t_http_s counted (hub.metric('t_http_s', seconds) per request)")
    if met.get("n_http"):
        rep.add("INFO", f"[{label}] speed", f"{met['n_http'] / max(secs, 1):.1f} requests/s, {met.get('t_http_s', 0) / met['n_http']:.2f} s per request, "
                f"{secs / max(len(got), 1):.1f} s per item")
    rep.ok(bool(finishes) and finishes[-1]["state"] == "finished", f"[{label}] closes its run",
           f"state {finishes[-1]['state']}" if finishes else "",
           (f"closed as {finishes[-1]['state']!r}: {finishes[-1].get('note', '')}" if finishes else "hub.close() wasn't called"),
           warn=not finishes)
    rep.ok(not rejected, f"[{label}] hub accepted every call", f"{len(batches)} batch(es)",
           "; ".join(f"{e['method']} {e['path']} -> {e['code']} {e['detail'][:120]}" for e in rejected[:3]))
    ups = [e for e in events if e["kind"] == "upload"]
    if ups:
        rep.add("PASS", f"[{label}] uploads files", f"{len(ups)} file(s), {sum(e['bytes'] for e in ups) / 1e3:.0f} KB (checksums verified by the client)")
    return set(recs)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="scrape-check", description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", default=".", help="the family repo (default: here)")
    ap.add_argument("--project", help="project to test (default: the first one setup puts in the pool)")
    ap.add_argument("--task", help="task to run (default: the project's pool task)")
    ap.add_argument("--items", type=int, default=3, help="queue items to run (default 3)")
    ap.add_argument("--item", action="append", default=[], help='a queue item as JSON {"key": ..., "payload": {...}} (repeatable)')
    ap.add_argument("--env", action="append", default=[], metavar="NAME=VALUE", help="extra env for the task (repeatable)")
    ap.add_argument("--pause", type=float, default=5, help="seconds the hub reports 'paused' at the start (default 5; 0 = off)")
    ap.add_argument("--no-rerun", action="store_true", help="skip the second run (stable record ids)")
    ap.add_argument("--timeout", type=int, default=600, help="seconds one task run may take (default 600)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON at the end")
    a = ap.parse_args(argv)
    d = Path(a.dir).resolve()
    out = d / ".scrape-check"
    out.mkdir(exist_ok=True)
    rep = Report()
    print(f"scrape-check: {d}")
    m = load_manifest(d, rep)
    if not m:
        return finish(rep, out, a)
    family = m["family"]
    hub = TestHub(store_dir=out / "uploads").start()
    try:
        if not run_setup(d, hub, family, rep, out):
            return finish(rep, out, a)
        if a.item or (a.project and a.project not in hub.state["projects"]):
            if not a.project or not a.task:
                rep.add("FAIL", "manual items", "--item needs --project and --task")
                return finish(rep, out, a)
            hub.handle("PUT", f"/api/v1/projects/{a.project}/pool", {"pool": True, "family": family, "task": a.task, "target": "manual",
                                                                    "env": dict(e.split("=", 1) for e in a.env if "=" in e)})
            hub.handle("POST", f"/api/v1/projects/{a.project}/queue", {"items": [json.loads(x) for x in a.item]})
        check_entries(m, hub, family, rep)
        pooled = [n for n, p in hub.state["projects"].items() if p["settings"].get("pool")]
        project = a.project or (pooled[0] if pooled else None)
        if not project or project not in hub.state["projects"]:
            rep.add("FAIL", "project to test", f"{project!r} has no pool entry")
            return finish(rep, out, a)
        task = a.task or hub.state["projects"][project]["settings"].get("task")
        if task not in m["tasks"]:
            rep.add("FAIL", "task to run", f"{task!r} is not in scrape.json")
            return finish(rep, out, a)
        queue = hub.state["queue"].get(project, {})
        n = min(a.items, len(queue))
        if not rep.ok(n > 0, "queue items to run", f"{n} of {len(queue)} in {project}", f"{project} has no queue items"):
            return finish(rep, out, a)
        extra = dict(e.split("=", 1) for e in a.env if "=" in e)
        print(f"\nrunning task {task!r} on {n} item(s) of {project} (fetches real pages) ...")
        code, secs, events = run_task(d, m, hub, family, project, task, extra, n, a.pause, a.timeout, out, "run 1")
        first = judge_run(rep, hub, project, code, secs, events, n, a.pause, out, "run 1")
        if not a.no_rerun and first is not None and code is not None:
            handed = [k for e in events if e["kind"] == "claim" for k in e["keys"]]
            for k in handed:
                it = queue[k]
                it["status"], it["claimed_by"] = "pending", None
            print(f"\nrerunning the same {len(handed)} item(s) ...")
            code2, secs2, events2 = run_task(d, m, hub, family, project, task, extra, len(handed), 0, a.timeout, out, "run 2")
            second = judge_run(rep, hub, project, code2, secs2, events2, len(handed), 0, out, "run 2")
            if first and second is not None:
                same = first == second
                rep.ok(same or second <= first, "rerun keeps record ids", f"{len(second)} ids, same as run 1",
                       f"run 2 stored {len(second - first)} id(s) run 1 didn't (ids like {sorted(second - first)[:2]}): "
                       "ids that change between runs make duplicates; the site's own id works best", warn=bool(second & first))
        with open(out / "records.jsonl", "w") as f:
            for rid, r in hub.state["records"].get(project, {}).items():
                f.write(json.dumps({"record_id": rid, "data": r["data"]}, ensure_ascii=False) + "\n")
        sample = next(iter(hub.state["records"].get(project, {}).items()), None)
        if sample:
            rep.add("INFO", "a record", f"{sample[0]}: {json.dumps(sample[1]['data'], ensure_ascii=False)[:200]}")
    finally:
        hub.stop()
    return finish(rep, out, a)


def finish(rep, out, a):
    (out / "report.json").write_text(json.dumps(rep.rows, indent=1, ensure_ascii=False))
    fails = sum(r["level"] == "FAIL" for r in rep.rows)
    warns = sum(r["level"] == "WARN" for r in rep.rows)
    print(f"\n{'FAILED' if fails else 'OK'}: {fails} failed, {warns} warning(s). Details: {out}/ (report.json, records.jsonl, task logs)")
    if a.json:
        print(json.dumps(rep.rows, ensure_ascii=False))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
