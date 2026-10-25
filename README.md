# s1d-labs/scrapers

The scrapers behind [scrape-hub](https://scrape.s1d.me), in one public repository, plus the pool runner that lets members
lend their own GitHub Actions minutes to the scrape.

scrape-hub is the coordinator: it holds the work queues, the records and the dashboard, and decides who scrapes what. The code
that talks to the target sites lives here. Runners prove who they are with a GitHub Actions OIDC token (audience
`https://scrape.s1d.me`), so no secrets are stored in this repository or in members' repositories.

## Layout

| Path | What |
|---|---|
| `sdk/` | `scrape-hub-client` (module `scrape_hub_client`), the hub's Python client: `HubClient`, `register_project`, `seed_queue`, ..., and `PoolClient` for pool assignments. `pip install ./sdk` |
| `families/gepnic/` | The GePNIC tender scraper (eprocure.gov.in and the state, central and PSU portals in `data/gepnic_portals.json`). Copied from eprocure-scrape; see `families/gepnic/ORIGIN.md` |
| `runner/` | The generic pool worker, `python -m runner.worker` (standard library + the sdk) |
| `.github/workflows/runner.yml` | Reusable workflow that members' repos call. It starts `jobs` runners with `procs` workers each |
| `.github/workflows/gepnic-*.yml` | The GePNIC family's own workflows (seed, count, crawl, consolidate, backfill), for manual runs inside the org |

## How the pool works

1. A member's `scrape-runner` repo (made from the [s1d-labs/scrape-runner](https://github.com/s1d-labs/scrape-runner) template)
   has one workflow, which only calls `s1d-labs/scrapers/.github/workflows/runner.yml@main`.
2. The hub starts that workflow through the GitHub API with the member's token (`workflow_dispatch`, inputs `jobs`, `procs`,
   `ref`, `family`, `hub`, `max_hours`).
3. Each worker process asks the hub what to do:

   ```
   POST {hub}/api/v1/pool/assign
   Authorization: Bearer <GitHub OIDC token>
   {"family": "gepnic", "job_index": 3, "total_jobs": 8, "worker": "<runner name>/<pid>"}
   ```

   and gets either `{"work": null, "reason": "wait"}` (nothing yet: ask again in 30-120 s), `{"work": null, "reason": "done"}`
   (stop), or

   ```json
   {"work": {"family": "gepnic", "portal": "wbtenders.gov.in", "project": "gepnic-wbtenders",
             "stage": "crawl", "args": [], "max_minutes": 60}}
   ```

4. For `gepnic` the worker runs, in `families/gepnic`,
   `PORTAL=<portal> python crawl.py|count.py --job-index <i> --total-jobs <n> --max-hours <max_minutes/60> <args...>`,
   streams its output, and asks again when it exits. It stops at `--max-hours` (default 5.5, under the 6 h job limit).

The worker's own flags: `--family`, `--job-index`, `--total-jobs`, `--hub`, `--max-hours`, `--worker`, `--dry-run` (print the
command instead of running it, stop after one assignment) and `--hub-fake` (with `--dry-run`: no hub call, one made-up
assignment). Local check:

```sh
pip install ./sdk
python -m runner.worker --family gepnic --dry-run --hub-fake
```

## Joining the pool (members)

1. On [s1d-labs/scrape-runner](https://github.com/s1d-labs/scrape-runner), click **Use this template** and create
   `<you>/scrape-runner` as a **public** repository (public repositories get free Actions minutes).
2. Create a fine-grained personal access token: resource owner you, only the `scrape-runner` repository, permissions
   **Actions: read and write** (Metadata: read is added automatically). Expiry up to a year.
3. On https://scrape.s1d.me -> **Runners**, add your GitHub username, the repository name and the token.

That is all. The hub starts the runs when there is work; you never press anything, and code updates arrive by themselves
because your workflow always runs this repository's `runner.yml`.

## Adding a family

1. Put the scraper under `families/<name>/` with a `requirements.txt`. It reports to the hub with `scrape_hub_client` from `sdk/`.
2. In `runner/worker.py`, write `run_<name>(work, ctx)`: check the assignment, build the command and return
   `ctx.run(cmd, cwd, env, minutes)` (its exit code). Register it in `FAMILIES = {"gepnic": run_gepnic, "<name>": run_<name>}`.
3. If it needs more than `pip install -r requirements.txt` (like gepnic's CPU-only torch), add a case to the Install step of
   `runner.yml`.
4. Teach the hub to hand out `{"family": "<name>", ...}` assignments.

## Versioning

The hub chooses the code: it passes `ref` (a branch, tag or commit of this repository) when it starts a run, and every runner
checks out exactly that. Members' workflow files point at `runner.yml@main` and never change; release tags (`v0.1.0`, ...) let
the hub pin a known-good version while `main` moves on. The sdk carries its own version in `sdk/pyproject.toml`.
