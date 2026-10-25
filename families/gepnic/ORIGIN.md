# Origin

This directory is a copy of [Sarthak-Sidhant/eprocure-scrape](https://github.com/Sarthak-Sidhant/eprocure-scrape)
(the GePNIC scraper) at commit:

    d84faa0f240f5aafccef7cbb566162d2bfc5009c   (2026-10-20, "A pause stops tenders already queued behind the gate ...")

Copied on 2026-10-25 without its git history. Changes made here, and only these:

- `requirements.txt`: the `scrape-hub-client @ git+https://github.com/Sarthak-Sidhant/scrape-hub@main#subdirectory=client`
  line is gone (that repo is private now). Install the client from this monorepo instead: `pip install ../../sdk`.
- `.github/workflows/*.yml` moved to the repository root as `gepnic-*.yml` (GitHub only runs workflows from the root):
  `gepnic.yml` -> `gepnic-portals.yml`, `crawl.yml` -> `gepnic-crawl.yml`, `plan.yml` -> `gepnic-plan.yml`,
  `consolidate.yml` -> `gepnic-consolidate.yml`, `backfill.yml` -> `gepnic-backfill.yml`. Each now runs in
  `working-directory: families/gepnic`, installs `../../sdk`, uploads logs from `families/gepnic/*.log`, and the chaining
  steps start the renamed workflow.

The scripts themselves (portal.py, plan.py, count.py, crawl.py, ...) are unchanged. Pool runners (`python -m runner.worker`
at the repo root) run `count.py` / `crawl.py` from here with `PORTAL=<portal>` and the usual `--job-index`, `--total-jobs`
and `--max-hours` flags.

Docs under `docs/` and the README describe the original single-repo layout; paths there are relative to this directory.
