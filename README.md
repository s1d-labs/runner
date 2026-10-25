# s1d-labs/runner

The generic side of the s1d-labs scrape pool: the hub's Python client (`sdk/`), the pool worker (`runner/`) and the reusable
workflow that members' repos call (`.github/workflows/runner.yml`). Nothing here knows any scraper: each family's code is its own
repository, `s1d-labs/<family>` (for example [s1d-labs/gepnic](https://github.com/s1d-labs/gepnic)).

[scrape-hub](https://scrape.s1d.me) is the coordinator: it holds the queues, starts runs in members' accounts, signs uploads into
each family's storage and tracks runners, IPs, requests and hosts. Runners prove who they are with a GitHub Actions OIDC token
(audience `https://scrape.s1d.me`), so no secrets are stored in this repository or in members' repositories.

## Layout

| Path | What |
|---|---|
| `sdk/` | `scrape-hub-client` (module `scrape_hub_client`): `HubClient`, `register_project`, `seed_queue`, ..., and `PoolClient` for pool assignments. `pip install ./sdk`, or from anywhere: `pip install "scrape-hub-client @ git+https://github.com/s1d-labs/runner@main#subdirectory=sdk"` |
| `runner/` | The pool worker, `python -m runner.worker` (standard library + the sdk) |
| `.github/workflows/runner.yml` | Reusable workflow: `jobs` runners x `procs` workers, each running the family's code from `s1d-labs/<family>` |

## How the pool works

1. A member's `scrape-runner` repo (made from the [s1d-labs/scrape-runner](https://github.com/s1d-labs/scrape-runner) template)
   has one workflow, which only calls `s1d-labs/runner/.github/workflows/runner.yml@main`.
2. The hub starts it through the GitHub API with the member's token (`workflow_dispatch`, inputs `jobs`, `procs`, `ref`, `family`,
   `hub`, `max_hours`).
3. `runner.yml` checks out this repo at `ref` and `s1d-labs/<family>` at `main` (into `family/`), installs the sdk and runs the
   family's `setup` commands from its `scrape.json`.
4. Each worker asks the hub what to do:

   ```
   POST {hub}/api/v1/pool/assign
   Authorization: Bearer <GitHub OIDC token>
   {"family": "gepnic", "job_index": 3, "total_jobs": 8, "worker": "<runner name>/<pid>"}
   ```

   and gets `{"work": null, "reason": "wait"}` (ask again in 30-120 s), `{"work": null, "reason": "done"}` (stop), or

   ```json
   {"work": {"family": "gepnic", "project": "gepnic-wbtenders", "task": "crawl", "target": "wbtenders.gov.in",
             "env": {"PORTAL": "wbtenders.gov.in"}, "args": [], "max_minutes": 60}}
   ```

   The hub never interprets `task`, `target` or `env`: the family wrote them into its projects' settings. `target` is the host the
   work hits; the hub caps workers per target and holds a target whose health check fails.
5. The worker runs the family's command for that task in `family/`, with the assignment's env, its args appended, and
   `SCRAPE_JOB_INDEX`, `SCRAPE_TOTAL_JOBS`, `SCRAPE_MAX_HOURS`, `SCRAPE_HUB_URL`, `SCRAPE_POOL_PROJECT`, `SCRAPE_POOL_TARGET`.
   It asks again when the command exits and stops at `--max-hours` (default 5.5, under the 6 h job limit).

Local check against a family checkout, without a hub:

```sh
pip install ./sdk
python -m runner.worker --family gepnic --family-dir ../gepnic --dry-run --hub-fake --fake-env PORTAL=wbtenders.gov.in
```

## Checking a scraper: `scrape-check`

The client ships a local stand-in for the hub (`scrape_hub_client.testhub`) and a checker that uses it:

```sh
pip install "scrape-hub-client @ git+https://github.com/s1d-labs/runner@main#subdirectory=sdk"
cd my-family-repo && scrape-check            # or: python -m scrape_hub_client.check
```

It reads `scrape.json`, runs `setup_family.py` against the stand-in, then runs one task exactly as the pool runner does on 3 real queue
items (the hub reports "paused" for the first 5 seconds), and again on the same items. It reports PASS / WARN / FAIL per check: run
started and closed, every claimed item completed, records stored, `n_http` / `t_http_s` reported, pause respected, exit when the
queue is empty, calls accepted, stable record ids on the rerun. Exit code 1 if anything failed; details in `.scrape-check/`.
Families without `setup_family.py`: `scrape-check --project P --task T --env K=V --item '{"key": ..., "payload": {...}}'`.
The checker's own tests: `python -m pytest sdk/tests`.

## Joining the pool (members)

1. On [s1d-labs/scrape-runner](https://github.com/s1d-labs/scrape-runner), click **Use this template** and create
   `<you>/scrape-runner` as a **public** repository (public repositories get free Actions minutes).
2. Create a fine-grained personal access token: resource owner you, only the `scrape-runner` repository, permissions
   **Actions: read and write** (Metadata: read is added automatically). Expiry up to a year.
3. On https://scrape.s1d.me -> **Runners**, add the repository and the token.

The hub starts the runs when there is work, and code updates arrive by themselves because your workflow always runs this
repository's `runner.yml`.

## Adding a family

1. The hub owner adds the family on scrape.s1d.me/runners (name, title, owner).
2. Create `s1d-labs/<name>` from [s1d-labs/family-template](https://github.com/s1d-labs/family-template) (public): a complete,
   working example family with `scrape.json`, a scraper, its projects and health checks (`family.py`), and a **setup** workflow.
3. Change it for your site, then run the setup workflow: from GitHub Actions the family's repo may put its own projects in the pool
   (`set_pool`) and register its health checks (`set_probes`); the hub trusts it through the OIDC token, no secrets needed.
   Your family's data model, records and pages stay in your repo.

## Versioning

The hub passes `ref` (a branch, tag or commit of this repository) when it starts a run. Members' workflow files point at
`runner.yml@main` and never change; tags let the hub pin a known-good version. Family repos run at `main`.
