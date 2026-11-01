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


def test_setup_works_with_a_read_only_runner_checkout(tmp_path, monkeypatch):
    make_family(tmp_path)
    monkeypatch.setenv("SCRAPE_FAMILY_GIT", str(tmp_path / "origin"))
    ro = tmp_path / "runner"
    import shutil
    shutil.copytree(ROOT / "sdk", ro / "sdk", ignore=shutil.ignore_patterns("*.egg-info", "__pycache__", "build"))
    monkeypatch.setattr(worker, "ROOT", ro)
    for d in [ro, *ro.rglob("*")]:
        if d.is_dir():
            d.chmod(0o555)
    try:
        logs = []
        assert worker.ensure_family("fam", tmp_path / "families", logs.append), logs
    finally:
        for d in [ro, *ro.rglob("*")]:
            if d.is_dir():
                d.chmod(0o755)


def test_bad_family_names_are_refused(tmp_path):
    logs = []
    assert worker.ensure_family("../etc", tmp_path, logs.append) is None
    assert "bad family name" in logs[0]


def test_proxy_env_reaches_the_task_and_skips_the_hub(monkeypatch, capsys):
    class C:
        hub = "https://scrape.s1d.me"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    env = worker.proxy_env({"url": "http://user:pw%2F1@95.1.1.1:50100", "label": "in-03", "exit": "95.1.1.1"}, C())
    assert env["HTTPS_PROXY"] == env["SCRAPE_PROXY"] == "http://user:pw%2F1@95.1.1.1:50100" and env["SCRAPE_PROXY_LABEL"] == "in-03"
    assert "scrape.s1d.me" in env["NO_PROXY"].split(",")
    out = capsys.readouterr().out
    assert "::add-mask::http://user:pw%2F1@95.1.1.1:50100" in out and "::add-mask::pw%2F1" in out
    assert worker.proxy_env(None, C()) == {} and worker.proxy_env({"url": "junk"}, C()) == {}
