"""runner.worker (runner mode): a task added to the family after the job's checkout is found by fetching the family again."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from runner import worker  # noqa: E402


def git(*a, cwd):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True, capture_output=True)


def setup_family(tmp):
    origin = tmp / "origin"
    origin.mkdir()
    (origin / "scrape.json").write_text('{"family": "fam", "setup": ["echo set-up >> setup.log"], "tasks": {"a": ["python", "a.py"]}}')
    git("init", "-q", "-b", "main", cwd=origin)
    git("add", ".", cwd=origin)
    git("commit", "-q", "-m", "one", cwd=origin)
    job = tmp / "job"
    git("clone", "-q", "--depth", "1", f"file://{origin}", str(job), cwd=tmp)
    return origin, job


def test_missing_task_fetches_once_and_runs_setup(tmp_path, monkeypatch):
    origin, job = setup_family(tmp_path)
    logs = []
    ctx = SimpleNamespace(family_dir=job)
    m = worker.load_manifest(job)
    monkeypatch.setattr(worker, "_last_refetch", [0.0])
    # nothing new upstream: same manifest, no setup
    assert worker.refetch_family(ctx, m, "b", logs.append) is m
    assert not (job / "setup.log").exists()
    # the family adds task b
    (origin / "scrape.json").write_text('{"family": "fam", "setup": ["echo set-up >> setup.log"], "tasks": {"a": ["python", "a.py"], "b": ["python", "b.py"]}}')
    git("commit", "-q", "-am", "two", cwd=origin)
    # within REFETCH_EVERY_S of the last fetch: no fetch
    assert "b" not in worker.refetch_family(ctx, m, "b", logs.append)["tasks"]
    monkeypatch.setattr(worker, "_last_refetch", [0.0])
    m2 = worker.refetch_family(ctx, m, "b", logs.append)
    assert "b" in m2["tasks"]
    assert (job / "setup.log").read_text().strip() == "set-up"
    assert any("moved" in x for x in logs)


def test_bad_manifest_after_fetch_keeps_old(tmp_path, monkeypatch):
    origin, job = setup_family(tmp_path)
    ctx = SimpleNamespace(family_dir=job)
    m = worker.load_manifest(job)
    (origin / "scrape.json").write_text('{"tasks": {"b": "not a list"}}')
    git("commit", "-q", "-am", "bad", cwd=origin)
    monkeypatch.setattr(worker, "_last_refetch", [0.0])
    assert worker.refetch_family(ctx, m, "b", lambda x: None) is m
    assert worker.load_manifest(job) == m


def test_no_git_checkout_is_left_alone(tmp_path, monkeypatch):
    (tmp_path / "scrape.json").write_text('{"tasks": {"a": ["python", "a.py"]}}')
    m = worker.load_manifest(tmp_path)
    monkeypatch.setattr(worker, "_last_refetch", [0.0])
    assert worker.refetch_family(SimpleNamespace(family_dir=tmp_path), m, "b", lambda x: None) is m
