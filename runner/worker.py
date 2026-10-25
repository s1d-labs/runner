"""Pool worker: ask the hub what to do next, run it, repeat.

    python -m runner.worker --family myfamily --family-dir family --job-index 0 --total-jobs 2 [--hub URL] [--max-hours 5.5]
    python -m runner.worker --family myfamily --family-dir ../myfamily --dry-run --hub-fake    # local test: no hub, no site

Each round the worker calls POST {hub}/api/v1/pool/assign with
    {"family": ..., "job_index": i, "total_jobs": n, "worker": "<runner name>/<pid>"}
and the GitHub Actions OIDC token (the sdk mints it). The hub answers either
    {"work": null, "reason": "wait" | "done" | ...}
or
    {"work": {"family": "...", "project": "...", "task": "crawl", "target": "example.gov.in", "env": {"X": "y"}, "args": [...],
              "max_minutes": 60}}
The hub never interprets task / target / env: the family wrote them into its projects' settings. The family's repo
(s1d-labs/<family>, checked out at --family-dir) says in scrape.json which command each task runs:
    {"family": "...", "setup": ["pip install -r requirements.txt"], "tasks": {"crawl": ["python", "crawl.py"]}}
The worker runs that command in the family's checkout with the assignment's env, the assignment's args appended, and
    SCRAPE_JOB_INDEX, SCRAPE_TOTAL_JOBS, SCRAPE_MAX_HOURS, SCRAPE_HUB_URL, SCRAPE_POOL_PROJECT, SCRAPE_POOL_TARGET
then asks again when it exits. On "wait" it sleeps 30-120 s (jittered); on "done" or any other reason it exits 0. It stops at
--max-hours. Nothing in here knows any family. Only the standard library and the sdk (scrape_hub_client) are used.
"""
import argparse
import json
import os
import random
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    from scrape_hub_client import HubRejected, PoolClient
except ImportError:  # running from a checkout without `pip install ./sdk`
    sys.path.insert(0, str(ROOT / "sdk"))
    from scrape_hub_client import HubRejected, PoolClient

MIN_LEFT_MINUTES = 10        # do not take new work with less time than this before the deadline
GRACE_MINUTES = 10           # a child that overruns its own --max-hours by this much is stopped
WAIT_MIN, WAIT_MAX = 30, 120  # back-off when the hub says "wait"

STOP = threading.Event()


class Ctx:
    """What a family function gets besides the assignment: who this worker is, its deadline, and how to run a command."""

    def __init__(self, args, log):
        self.args = args
        self.hub = args.hub.rstrip("/")
        self.job_index, self.total_jobs = args.job_index, args.total_jobs
        self.deadline = time.time() + args.max_hours * 3600
        self.dry_run = args.dry_run
        self.log = log
        self.child = None

    def minutes_left(self):
        return (self.deadline - time.time()) / 60

    def run(self, cmd, cwd, env_extra, minutes):
        """Run cmd in cwd with os.environ + env_extra, stream its output, stop it GRACE_MINUTES after `minutes`.
        Returns its exit code (0 in a dry run)."""
        shown = " ".join(f"{k}={shlex.quote(v)}" for k, v in env_extra.items())
        rel = os.path.relpath(cwd)
        self.log(f"{'would run' if self.dry_run else 'run'}: (cd {rel} && {shown} {shlex.join(cmd)})  [limit {minutes:.0f} min]")
        if self.dry_run:
            return 0
        env = dict(os.environ)
        env.pop("SCRAPE_PROJECT", None)   # the project follows from the assignment, never from a stray variable
        env.update(env_extra)
        env["PYTHONUNBUFFERED"] = "1"
        t0 = time.time()
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             bufsize=1, errors="replace", start_new_session=True)
        self.child = p
        killer = threading.Timer((minutes + GRACE_MINUTES) * 60, lambda: self._stop_child(p, "ran past its time limit"))
        killer.daemon = True
        killer.start()
        try:
            for line in p.stdout:
                print(f"{self.prefix()}  | {line.rstrip()}", flush=True)
            rc = p.wait()
        finally:
            killer.cancel()
            self.child = None
        self.log(f"exit {rc} after {(time.time() - t0) / 60:.1f} min")
        return rc

    def _stop_child(self, p, why):
        if p.poll() is not None:
            return
        self.log(f"stopping the child ({why})")
        try:
            os.killpg(p.pid, signal.SIGINT)   # lets the scraper close its hub run as failed instead of vanishing
            try:
                p.wait(60)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(30)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def prefix(self):
        return f"[worker {self.job_index}/{self.total_jobs}]"


# -- the family's commands ----------------------------------------------------------------------------------------------------

ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
ENV_RESERVED = ("PATH", "PYTHON", "LD_", "HOME", "ACTIONS_", "GITHUB_", "RUNNER_", "SCRAPE_")
TASK_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def family_env(work, ctx):
    """The family's env from the assignment, minus anything that could change how the runner itself works."""
    env = work.get("env") or {}
    if not isinstance(env, dict):
        return {}
    out = {}
    for k, v in env.items():
        if isinstance(k, str) and ENV_NAME.match(k) and not k.startswith(ENV_RESERVED) and isinstance(v, (str, int, float)):
            out[k] = str(v)
        else:
            ctx.log(f"ignoring env {k!r} from the hub")
    return out


def load_manifest(family_dir):
    m = json.loads((Path(family_dir) / "scrape.json").read_text())
    tasks = m.get("tasks") or {}
    if not isinstance(tasks, dict) or not all(TASK_NAME.match(k) and isinstance(v, list) and v and all(isinstance(x, str) for x in v)
                                              for k, v in tasks.items()):
        raise ValueError("scrape.json: tasks must map task names to commands (lists of strings)")
    return m


def run_task(work, ctx, manifest):
    """Run the family's command for this assignment's task; returns its exit code (2 when the assignment can't be run)."""
    task = work.get("task") or work.get("stage") or ""
    cmd = (manifest.get("tasks") or {}).get(task)
    if not cmd:
        ctx.log(f"no command for task {task!r} in scrape.json (has {sorted(manifest.get('tasks') or {})}); skipping")
        return 2
    extra = work.get("args") or []
    if not isinstance(extra, list) or not all(isinstance(a, str) for a in extra):
        ctx.log(f"args must be a list of strings, got {extra!r}; skipping")
        return 2
    minutes = _minutes(work, ctx)
    env = family_env(work, ctx)
    job_index = work.get("job_index", ctx.job_index)     # optional per-assignment slice; defaults to this worker's
    total_jobs = work.get("total_jobs", ctx.total_jobs)
    env.update({"SCRAPE_JOB_INDEX": str(int(job_index)), "SCRAPE_TOTAL_JOBS": str(int(total_jobs)), "SCRAPE_MAX_HOURS": f"{minutes / 60:.3f}",
                "SCRAPE_HUB_URL": ctx.hub, "SCRAPE_POOL_PROJECT": str(work.get("project") or ""), "SCRAPE_POOL_TARGET": str(work.get("target") or "")})
    ctx.log(f"{task} for {work.get('project') or '?'} on {work.get('target') or '?'}, slice {job_index} of {total_jobs}")
    argv = [sys.executable if cmd[0] == "python" else cmd[0], *cmd[1:], *extra]
    return ctx.run(argv, ctx.family_dir, env, minutes)


def _minutes(work, ctx):
    """The assignment's time budget, cut to what is left before this worker's deadline (2 minutes kept for wrap-up)."""
    try:
        asked = float(work.get("max_minutes") or 60)
    except (TypeError, ValueError):
        asked = 60.0
    return max(1.0, min(asked, ctx.minutes_left() - 2))


# -- hub --------------------------------------------------------------------------------------------------------------------

class FakePool:
    """--hub-fake: hands out one made-up assignment, then says "done"."""

    def __init__(self, family, task, env):
        self.family, self.task, self.env, self.given, self.reason = family, task, env, False, ""

    def assign(self, family, job_index=0, total_jobs=1, worker=""):
        if self.given:
            self.reason = "done"
            return None
        self.given = True
        return {"family": family, "project": f"{family}-test", "task": self.task, "target": "example.org", "env": dict(self.env),
                "args": [], "max_minutes": 60}


def _sleep(seconds, ctx):
    STOP.wait(max(0.0, min(seconds, ctx.deadline - time.time())))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m runner.worker", description="scrape-hub pool worker")
    ap.add_argument("--family", required=True, help="scraper family (its code is the repo s1d-labs/<family>)")
    ap.add_argument("--family-dir", default="family", help="checkout of the family's repo (holds scrape.json)")
    ap.add_argument("--job-index", type=int, default=0, help="this worker's slot, 0-based")
    ap.add_argument("--total-jobs", type=int, default=1, help="how many workers run in this pool run")
    ap.add_argument("--hub", default=os.environ.get("SCRAPE_HUB_URL", "https://scrape.s1d.me"))
    ap.add_argument("--max-hours", type=float, default=5.5, help="stop taking work after this long")
    ap.add_argument("--worker", default="", help="name sent to the hub (default <runner name>/<pid>)")
    ap.add_argument("--dry-run", action="store_true", help="print what would run instead of running it (stops after one assignment)")
    ap.add_argument("--hub-fake", action="store_true", help="do not call the hub: fake one assignment (use with --dry-run)")
    ap.add_argument("--fake-task", default="", help="task of the fake assignment (default: the first in scrape.json)")
    ap.add_argument("--fake-env", action="append", default=[], metavar="NAME=VALUE", help="env of the fake assignment")
    args = ap.parse_args(argv)

    def log(msg):
        print(f"{time.strftime('%H:%M:%S')} {ctx.prefix()} {msg}", flush=True)

    ctx = Ctx(args, log)
    ctx.family_dir = Path(args.family_dir).resolve()
    try:
        manifest = load_manifest(ctx.family_dir)
    except (OSError, ValueError) as e:
        log(f"can't read the family's scrape.json in {ctx.family_dir}: {e}")
        return 2
    if manifest.get("family") and manifest["family"] != args.family:
        log(f"{ctx.family_dir}/scrape.json is for family {manifest['family']!r}, not {args.family!r}")
        return 2
    if args.hub_fake and not args.dry_run:
        log("--hub-fake only makes sense with --dry-run (the fake assignment is not real work)")
        return 2
    worker = args.worker or f"{os.environ.get('RUNNER_NAME') or socket.gethostname()}/{os.getpid()}"

    def on_signal(signum, _frame):
        log(f"got signal {signum}: finishing")
        STOP.set()
        if ctx.child is not None:
            threading.Thread(target=ctx._stop_child, args=(ctx.child, "worker is stopping"), daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    fake_env = dict(e.split("=", 1) for e in args.fake_env if "=" in e)
    pool = FakePool(args.family, args.fake_task or next(iter(manifest["tasks"]), "run"), fake_env) if args.hub_fake else PoolClient(ctx.hub)
    log(f"family {args.family}, hub {'(fake)' if args.hub_fake else ctx.hub}, worker {worker}, until {args.max_hours} h")
    errors = failures = done = 0
    while not STOP.is_set():
        if ctx.minutes_left() < (0 if args.dry_run else MIN_LEFT_MINUTES):
            log(f"deadline reached after {done} assignments")
            return 0
        try:
            work = pool.assign(args.family, job_index=args.job_index, total_jobs=args.total_jobs, worker=worker)
            errors = 0
        except HubRejected as e:
            log(f"the hub refused this worker: {e}. Is this repository registered on the hub's Runners page?")
            return 3
        except Exception as e:  # network trouble or a hub 5xx after the sdk's own retries: keep trying until the deadline
            errors += 1
            wait = min(300, 15 * 2 ** min(errors, 5)) * random.uniform(0.5, 1.5)
            log(f"hub error ({type(e).__name__}: {str(e)[:200]}); retrying in {wait:.0f} s")
            _sleep(wait, ctx)
            continue
        if work is None:
            if pool.reason == "wait":
                wait = random.uniform(WAIT_MIN, WAIT_MAX)
                log(f"no work yet (wait); asking again in {wait:.0f} s")
                _sleep(wait, ctx)
                continue
            log(f"no more work ({pool.reason or 'no reason given'}); {done} assignments done")
            return 0
        family = work.get("family") or args.family
        if family != args.family:
            log(f"the hub sent work for family {family!r}, but this worker runs {args.family!r}; skipping it")
            rc = 2
        else:
            rc = run_task(work, ctx, manifest)
        done += 1
        if rc == 0:
            failures = 0
        elif not STOP.is_set():
            failures += 1
            wait = min(300, 30 * failures) * random.uniform(0.75, 1.25)
            log(f"assignment ended with exit {rc}; next one in {wait:.0f} s")
            _sleep(wait, ctx)
        if args.dry_run and not args.hub_fake:
            log("dry run: stopping after one assignment")
            return 0
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
