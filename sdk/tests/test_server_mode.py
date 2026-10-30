"""runner.worker --server: a family checkout + venv per family, re-set-up when the family's commit changes."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from runner import worker  # noqa: E402


def git(*a, cwd):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True, capture_output=True)


def make_family(tmp, name="fam"):
    origin = tmp / "origin" / name
    origin.mkdir(parents=True)
    (origin / "scrape.json").write_text('{"family": "fam", "setup": ["echo set-up >> setup.log"], "tasks": {"crawl": ["python", "crawl.py"]}}')
    (origin / "crawl.py").write_text("import scrape_hub_client, os; print('ok', os.environ['SCRAPE_POOL_PROJECT'])\n")
    git("init", "-q", "-b", "main", cwd=origin)
    git("add", ".", cwd=origin)
    git("commit", "-q", "-m", "one", cwd=origin)
    return origin


def test_server_mode_sets_up_each_commit_once(tmp_path, monkeypatch):
    origin = make_family(tmp_path)
    monkeypatch.setenv("SCRAPE_FAMILY_GIT", str(tmp_path / "origin"))
    logs = []
    fams = tmp_path / "families"
    ready = worker.ensure_family("fam", fams, logs.append)
    assert ready, logs
    src, manifest, python = ready
    assert manifest["tasks"]["crawl"] == ["python", "crawl.py"] and Path(python).exists()
    assert (src / "setup.log").read_text().count("set-up") == 1
    out = subprocess.run([python, "crawl.py"], cwd=src, env=dict(os.environ, SCRAPE_POOL_PROJECT="p1"), capture_output=True, text=True)
    assert out.stdout.strip() == "ok p1"                       # the sdk is in the family's venv
    assert worker.ensure_family("fam", fams, logs.append)     # same commit: no second setup
    assert not (src / "setup.log").exists() or (src / "setup.log").read_text().count("set-up") == 1
    (origin / "crawl.py").write_text("print('two')\n")
    git("commit", "-q", "-am", "two", cwd=origin)
    monkeypatch.setattr(worker, "FETCH_EVERY_S", 0)
    src, _, python = worker.ensure_family("fam", fams, logs.append)
    assert (src / "crawl.py").read_text() == "print('two')\n"
    assert sum("is set up" in m for m in logs) == 2


def test_bad_family_names_are_refused(tmp_path):
    logs = []
    assert worker.ensure_family("../etc", tmp_path, logs.append) is None
    assert "bad family name" in logs[0]
