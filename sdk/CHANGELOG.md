# scrape-hub-client changelog

Families install the client from the tag `sdk-stable`:
`scrape-hub-client @ git+https://github.com/s1d-labs/runner@sdk-stable#subdirectory=sdk` (in their workflows, for example
`check.yml` and `setup.yml`). A family's `requirements.txt` does not list the client: pool jobs install `./sdk` of the runner ref.
Changes are additive: a new version does not rename or remove names that families use.

## Tags and releases

Two kinds of tag mark versions of the client in this repository:

- `sdk-vX.Y.Z` (annotated): the commit of version X.Y.Z. It never moves. Use it to pin one exact version.
- `sdk-stable`: the newest version that ran well for some days. It moves forward only. Families pin this tag.

`main` can have changes that are not released yet. Do not install from `main` in a family repo.

To release a version:

1. Change `version` in `sdk/pyproject.toml` and add a section to this file. Run `python -m pytest sdk/tests`. Push to `main`.
2. Tag the commit and push the tag:

   ```sh
   git tag -a sdk-vX.Y.Z <sha> -m "scrape-hub-client X.Y.Z: <summary>"
   git push origin sdk-vX.Y.Z
   ```

3. Let the version run for some days (`scrape-check` and family check runs that install `@sdk-vX.Y.Z`). Then move
   `sdk-stable` to the same commit:

   ```sh
   git fetch --tags --force
   git tag -f sdk-stable <sha>
   git push -f origin sdk-stable
   ```

   Families get the new version at their next install. pip keeps an installed copy of the same version, so a version that
   changes code needs a new version number.

In a pool job, a pin in the family's setup (for example in `requirements.txt`) wins over `./sdk` of the runner ref when the
versions differ, and `runner/worker.py` then runs with the pinned client. For this reason family repos keep the client out of
`requirements.txt`.

Never move or delete an `sdk-vX.Y.Z` tag.

| Tag | Commit | Date |
|---|---|---|
| `sdk-v0.10.0` | `0908465` | 2026-11-12 |
| `sdk-stable` | `0908465` (0.10.0) | 2026-11-12 |

## 0.10.0 (2026-11-12)

Helpers for code that was copied into many family repos. Nothing that existed before changed behaviour.

- `stop_flag(install=True)`: a `threading.Event` that SIGINT and SIGTERM set (the runner's "time is up"). The same Event on every
  call; `install=False` gives the Event without installing the handlers (for module level code that tests also import).
- `timed_request(hub, session, method, url, raise_errors=False, **kw)`, `timed_get(...)`, `timed_post(...)`: the request with the
  metrics the hub reads: `n_http`, `t_http_s`, `http_<code>` for each status other than 200, `hub.block()` for 403 / 429 / 503, and
  `http_error` + `hub.error()` when there is no answer (then None, or the exception with `raise_errors=True`). `timeout` is 30 s by
  default. `hub=None` gives no metrics.
- `count_request(hub, status, seconds)`: the same metrics for code that uses another HTTP client (httpx, Playwright).
- `LocalHub(project="local", threads=1, delay_ms=0, save_dir=None, items=None)`: a stand-in for `HubClient` in a task's `--local`
  mode. Counters in `.counts` (`errors`, `blocked` from `error()` / `block()`), records to stdout as JSON lines, uploads to
  `save_dir`, `claim()` from `items`.
- `ca_bundle(extra_pems=())` (also `scrape_hub_client.tls.ca_bundle`): path of a PEM file with certifi's roots and 13 public
  intermediate CA certificates that some Indian government servers do not send (`scrape_hub_client/certs/`, one per file). Use it
  as `session.verify`. Verification stays on.
- `scrape-check`: when setup writes every pool entry with `pool=False` (a paused family), "pool entries" is a WARN and no task runs
  (exit code 0). `--project NAME` runs that project's task anyway.
- Dependency `certifi` is now explicit (requests already needs it).

## 0.9.0 (2026-11-02)

`set_pool(proxy_only=[...])`; `set_probes` targets may say `"proxy": "IN"`.

## 0.8.0 (2026-11-01)

`set_pool(threads=, delay_ms=)`.

## 0.7.0 (2026-11-01)

`set_pool(proxy=)`; the runner gives a lent proxy to the task as `HTTPS_PROXY` / `HTTP_PROXY` / `SCRAPE_PROXY`.

## 0.6.0 (2026-10-30)

Server mode for pool servers; `set_pool(geo=)`.

## 0.5.0 (2026-10-25)

`scrape-check` and `scrape_hub_client.testhub.TestHub`.

## 0.4.0 and older (2026-10-25)

`set_pool`, `set_probes`, `PoolClient`, `HubClient`.
