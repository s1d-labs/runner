# Scrape observations and optimisation log

Rule for this project: **observe, write it down, optimise everything known, verify with a small test, then run.** Do not cancel or
restart a healthy big run on a guess. Update this file before every large run.

## Run 1: 2026-10-16, 12:33:55 to 12:59 (stopped by the operator at 26 minutes)

Setup: 20 GitHub runners, one Python process each, no caps, documents on. Speed 4 threads per runner (Normal) until 12:48:57,
then 16 (Max). Queue: 1,485 windows, 895,058 tenders, balanced 74 to 75 windows per slice.

Result at the stop: **30,424 tenders stored**, 18 windows finished, about 65,000 documents stored and 15,500 refused (19%).

### Measured (facts)
| What | Value |
|---|---|
| Rate at 4 threads, all 20 runners working | 1,650 to 2,010 tenders/min (12:42 to 12:48) |
| Per active runner | 82 to 105 tenders/min |
| Per active runner after 4 -> 16 threads | 87 to 105 tenders/min (**no change**) |
| Runners storing nothing in a given minute | 0 to 1 until 12:49, then 8 to 11 of 20 |
| Total rate after the 12:49 window turnover | 630 to 1,260 tenders/min |
| Portal pushback (429/403/503/blocked) | **none**, 0 blocks, 1 connection error in 26 min |
| Hub load | CPU idle, about 14 requests/s, Postgres 1 active connection, 0 errors |
| Parsed fields | 100% of 30,018 checked tenders had type and publish date, 0 parse errors |
| Document refusals | 1% in the first minute, 17 to 22% later: depends on organisation and year |
| Captcha first-try success | 55 to 60%; bursts of 26 attempts/min when windows start |
| CPU per tender in Python (this VPS, 6 cores) | about 170 ms: 59 ms three html.parser soups, 25 ms link search, 30 ms document search, 53 ms September parser, 3 ms gzip |
| Failures | 1 tender (`ChunkedEncodingError`, IncompleteRead), documents refused are in the failures table |

### Interpretation (what the facts suggest)
- Threads did not raise a runner's speed, so something per runner is the limit. A single Python process parsing about 170 ms per
  tender on this fast machine tops out near 350 tenders/min before network time; slower GitHub CPUs and the GIL would put it near
  the observed 100/min. **This is the leading hypothesis and it is NOT verified** (see below).
- The drop after 12:49 comes from window boundaries: a runner that finishes a window pages through that window's whole result list
  (about 120 pages, serially) and builds a fresh captcha-solved session per thread before fetching anything. All 20 runners started on
  similar windows, so they hit the boundary together and about half sat idle at any moment.
- Raising threads therefore added captcha sessions (more start-up work per window) without adding throughput.

### NOT verified (do not treat as fact)
1. (Refuted by experiment A: runners use about 3% of their cores.) That runners are CPU-bound. GitHub runner CPU count and utilisation were never measured. A per-IP rate limit on the portal that is
   silent (no 429) would look the same and was not ruled out.
2. That 4 processes per runner scales (the code exists; it was started and stopped within a minute, no data).
3. The split of time per phase on a runner (list pages, fetch, parse, documents, upload, hub calls). Nothing is timed yet.
4. That a GitHub-hosted runner has 4 cores (documentation says so for public repos; not checked in a run).

## Experiment A (2026-10-16, 13:06): 2 runners, 1 process each, 4 threads, capped, instrumented

Measured on the runners themselves (the first real timing data):

| Per process | Slice 0 | Slice 1 |
|---|---|---|
| Tenders per minute over its life | 33 | 32 |
| CPU used | 12% of one core (3% of the runner's 4 cores) | 13% (3%) |
| Seconds per tender, summed over threads: fetching the three pages | 1.45 | 1.02 |
| ...of that, waiting on the portal (about 3.3 requests, 380 to 414 ms each) | 1.38 | 1.22 |
| ...downloading documents (about 3 per tender) | **6.25** | **5.16** |
| ...uploading pages and documents to R2 (hub link + PUTs) | **3.45** | **2.27** |
| ...parsing (September parser, link and document search) | 0.04 | 0.04 |
| ...listing the window | 0.16 | 0.32 |
| ...waiting for a free thread slot | 33 | 36 |

### What this changes
- **The CPU-bound hypothesis is wrong.** The runner is almost idle. Parsing is negligible (0.04 s). Several processes per runner would
  not help; that change (still in the code) should be kept only if a later test shows a gain, and can default to 1.
- **Documents and uploads are about 85% of the time per tender** (about 8 to 9 of about 10 thread-seconds). Each tender downloads
  about 3 documents one after another, about 1.8 s each, then uploads about 4 objects one after another.
- Threads were queuing for a slot (33 to 36 thread-seconds of waiting per tender), so the thread limit was binding at 4. In run 1,
  16 threads still gave about 100 tenders/min per runner. If concurrency were the only limit, 16 threads would have helped, so there
  is probably a throughput ceiling per runner on the portal side or in the network: at 100 tenders/min x about 3 documents x about
  0.5 MB that is about 2.4 MB/s per runner. **Not yet verified.**
- The second window of each process ran at about 18 tenders/min against about 100 for the first. Cause unknown; compare windows.

### Next experiments (in order)
- **C: documents off** (same windows, caps and threads). If the rate jumps several times, documents are the bottleneck and the plan is
  a metadata pass first, with documents as a separate lower-priority pass. Run first because it is the cheapest decisive test.
- **D: documents on, 16 threads, 1 process**, to separate "more concurrency" from "a ceiling per runner".
- Only then decide on pipelining downloads/uploads, parallel downloads inside a tender, or two passes.

## Experiment C (2026-10-16, 13:28): same as A but documents OFF

| Per process | Slice 0 | Slice 1 | (A, documents on) |
|---|---|---|---|
| Tenders per minute | **133** | **106** | 31 and 30 |
| Time for 600 tenders | 4.5 min | 5.7 min | 19.6 and 19.9 min |
| CPU | 60% of one core (15% of the 4 cores) | 43% (11%) | 11 to 13% |
| Fetching pages (s per tender) | 1.08 | 1.05 | 1.4 and 1.0 |
| Uploading (s per tender) | 1.11 | 0.99 | 3.5 and 2.6 |
| Parse timer (s per tender, includes waiting for the GIL) | 0.27 | 0.20 | 0.04 |

### What this shows
- **Documents cost about 75% of the time**: dropping them makes a process **3.9x faster** (30.5 -> 119.5 tenders/min on average).
- **Uploading pages alone still costs about 1 s per tender** (a hub call plus one PUT): worth batching or moving to a background pool.
- **CPU now matters.** With documents off a process uses 43 to 60% of one core, and the parse timer rose from 0.04 s to 0.2 to 0.27 s
  per tender, which is threads queueing for the interpreter lock. A single Python process would saturate at roughly 200 to 250
  tenders/min. So in metadata-only mode, **more processes per runner (or more cores in use) can help**; in documents-on mode they cannot.
  This refines the experiment A conclusion: the limit moves from "documents" to "one Python process" once documents are out of the way.
- The pause between windows is visible again (a minute near 25 tenders at the window boundary).

### Plan this supports (estimates, to verify)
- **Two passes:** pass 1 metadata only (pages + parsed fields), pass 2 documents separately. At about 120/min/process and one
  process per runner, 20 runners do about 2,400/min, so 895k tenders take about 6 hours; with more threads or processes, less.
- Open questions for the next tests: does throughput scale with threads (D) and with processes (metadata-only B)?

## Experiment D (2026-10-16, 13:35): same as A but 16 threads (documents ON)

| Per process | Slice 0 | Slice 1 | (A, 4 threads) |
|---|---|---|---|
| Tenders per minute | **79** | **75** | 31 and 30 |
| Time for 600 tenders | 7.6 min | 8.0 min | 19.6 and 19.9 min |
| CPU | 54% of one core (13% of the 4 cores) | 51% (13%) | 11 to 13% |
| Documents (s per tender, summed over threads) | 6.6 | 6.1 | 6.7 and 5.8 |
| Uploading (s per tender) | 3.6 | 3.1 | 3.5 and 2.6 |
| Portal wait per request | 369 ms | 482 ms | 406 and 366 ms |
| Portal pushback (429/403/503/connection errors) | none | none | none |
| Errors / blocked | 0 / 0 | 0 / 0 | 0 / 0 |

### What this shows
- **Threads do help with documents on**: 16 threads gave **2.5x** the rate (about 31 -> 77 tenders/min per process). Time per document did not
  grow (still about 1.8 s), so documents are limited by concurrency here, not by a bandwidth ceiling at this level.
- **No portal pushback at all** at 16 threads (portal wait per request 369 to 482 ms, similar to before).
- This **does not match run 1**, where raising 4 -> 16 threads mid-run changed nothing per active runner. Run 1 is confounded: threads were
  raised during the run, half the runners were idle at window boundaries, and each new window needs 16 fresh sessions. Treat run 1's
  "no change" as unreliable, not as evidence of a ceiling.
- **CPU per tender is about 0.4 s with documents on** (247 s CPU / 600 tenders), higher than the 0.17 s parsing benchmark: TLS for the
  document bytes, hashing, gzip, JSON and the captcha model add up. At 16 threads one process uses about 54% of a core and the parse
  timer inflates (0.5 to 1.2 s) from waiting on the interpreter lock, so **a single process saturates at roughly 140 tenders/min**.

### Summary of A, C, D (per process, 600 tenders each, the same windows)
| | Documents | Threads | Tenders/min | CPU of one core |
|---|---|---|---|---|
| A | on | 4 | 30 to 31 | 11 to 13% |
| C | off | 4 | 106 to 133 | 43 to 60% |
| D | on | 16 | 75 to 79 | 51 to 54% |

### Open
- Does throughput scale further with **more processes** (each has its own interpreter lock and a runner has 4 cores)? **Experiment B**, now
  meaningful: 4 processes at 8 threads each, documents on, compared with D's about 77/min per runner.
- Only if B scales: the portal's total tolerance at 20 runners x several processes is untested.

## Experiments B and B2 (more processes per runner, 2 runners, documents on, 600 tenders per process, same windows)
| | Processes x threads | Total threads/runner | Tenders/min per runner | CPU (of 4 cores) | Connection errors | Errors / blocked |
|---|---|---|---|---|---|---|
| A | 1 x 4 | 4 | 31 | 11 to 13% of one core | 0 | 0 / 0 |
| D | 1 x 16 | 16 | 77 | 54% of one core | 0 | 0 / 0 |
| B | 4 x 8 | 32 | 146 to 162 (avg 154) | 23 to 27% | 0 | 0 / 0 |
| B2 | 8 x 16 | 128 | 351 to 371 (avg 361) | 80 to 86% | 48 | 0 / 0 |

### What this shows
- **Throughput scales roughly linearly with total threads** (about 4.8, 4.8, 2.8 tenders/min per thread for D, B, B2) up to 128 threads
  per runner. Processes are a convenient way to get there; a runner is not limited to one interpreter's ~140/min.
- **B2 is near the runner's CPU limit** (80 to 86% of 4 cores). The phase timers inflate (parse 6.2 s per tender vs 0.24 s in B) from
  waiting on the CPU, so thread-seconds at that load are not real costs. Per-thread yield fell in B2 for the same reason.
- **The portal did not push back** (no 429/403/503; wait per request 768 ms vs 395 ms in B). Only 48 dropped connections (`http_error`)
  across 31,762 requests, no failed tenders. Auto-backoff ignores `http_error`, so it would not have reacted; see backlog item 9.
- Captcha first-try failure was 42% in B2 (413/975) vs 43% in B, so no degradation with load.
- Portal-refused documents were 31% in B2 (8,049 of 26,125) vs 16% in B (2,009 of 12,744). Not yet explained; check whether refusal
  depends on load before the full run (backlog item 10).
- **Untested**: 20 runners at these settings. B2 x 20 would be about 7,200 tenders/min aggregate (about 2 hours for 895k). Ramp in
  steps while watching `http_error`, 429 and captcha failure rates.

### Refused documents are not load-related (experiment E, 2026-10-16)
Same two windows, 120 tenders, documents on, `--rescrape`, 1 runner x 1 process: **1 thread** stored 230 / refused 159, **16 threads**
stored 230 / refused 159 (identical). So the portal's "session expired" refusal is a property of the document, not of load or
concurrency. The B (16%) vs B2 (31%) difference is the windows each process happened to reach. Failures table: 20,674 of 20,681
document failures are this refusal; by kind: evaluation_summary 13,773, BOQ comparative chart 2,992, other documents 2,606, status
documents 1,309; concentrated in older windows (2011 to 2019). A real browser gets the same page. Nothing to fix; retrying is
pointless (attempts 2 to 5 in the table all still fail).

Hub changes made before the 6k/min attempt: auto-backoff now also halves threads when dropped connections in 3 min are at least 20 and
exceed half the records stored; hub runs 4 uvicorn workers (was 1) with a DB pool of 20 each, because 160 processes will report at
about 100 tenders/s.

### First 6k/min attempt failed (2026-10-16 14:52, run 38903566352) and the fixes
20 runners x 8 processes x 32 threads, all starting together. In 14 minutes **zero tenders were stored**; 13 windows failed with "search
failed after 5 attempts". Runner logs show the portal's search page timing out (ReadTimeout at 25 s) and returning pages without a
search form, while the portal answered a single request from the hub host in 2.4 s. So the weak point at that scale is the search/listing
step, not document downloads. Cancelled by the operator; queue reset (176 items back to pending). Not yet known: whether the cause is
total simultaneous searches (160) or per-IP limits; the ramp below measures it.
Changes: (1) documents inside one tender download 4 at a time (verified: the same 230 stored / 159 refused on the 120-tender test, docs
6.5 -> 4.6 thread-s, 49 -> 60 tenders/min at 16 threads); (2) 64 worker threads for a gate of at most 32, so uploads and parsing no longer
occupy gated slots; (3) `--stagger` spreads process start-up (workflow input `stagger`, default 120 s); (4) a 10 to 30 s pause before the
retry when a search returns nothing. Ramp plan: procs=2 (40 processes), then add more only while tenders keep arriving.

## Bugs found and fixed so far
- Cancelled/Retender tenders returned no fields: the details link was the last `$DirectLink` on the status page (a signature pop-up).
  Same cause as the 8,818 empty rows in the September data (6,554 Cancelled + 2,264 Retender). Fixed, with the cancellation notice
  now stored as a document.
- 43% of captured "documents" were digital-signature pop-ups; the BOQ chart is an Excel file named .pdf. Fixed, identified by bytes.
- Some documents are refused by the portal itself (its download servlet redirects to `CommonErrorPage`, same in a real browser).
- The database wipe at 12:33 silently rolled back (savepoint inside an implicit transaction); fixed by committing explicitly and
  verifying from a second connection. Lesson: verify destructive operations from a fresh connection.
- Hub latency metric was always 0 (timed before sending). Fixed.

## Optimisation backlog (ranked by expected gain; none applied yet unless marked)
1. **Instrument first**: per-phase timers (list, fetch, parse, documents, upload) and runner CPU%/cores as metrics. Prerequisite for
   everything else, so the next decision is made on data.
2. **Overlap listing with fetching**: list the next window while the current one is being fetched. Targets the 8 to 11 idle runners.
3. **Parse each page once**: the pages are parsed about 8 times per tender (3 soups, link search, two document searches, the parser).
   Share one lxml tree; expected 170 -> roughly 60 to 80 ms (an estimate to verify).
4. **Several processes per runner** (implemented, unverified; needs experiment 1 below).
5. **Reuse authenticated sessions across windows** of the same organisation to avoid re-solving about 16 x 1.75 captchas per window.
   Untested; earlier evidence is encouraging (detail links worked in a different session).
6. Pipeline document downloads and uploads so they overlap the next tender's fetch.
7. Captcha model accuracy (55 to 60% first try).
9. Done: `http_error` counted in auto-backoff. Still to do: add a "stopped" run state so operator stops are not failures.
10. Done: refusals are deterministic per document (experiment E), no action.
8. Done: skip tenders already stored when a window is retried or resumed (`--rescrape` overrides).

## Experiments to run before the next big run
1. **1 vs 4 processes on the same windows**: 2 runners, capped, same windows. Compare tenders/min per runner and CPU%.
   Decides whether the processes-per-runner change is worth keeping.
2. **Phase timing** from the instrumented crawler on one runner: where the 100 tenders/min actually goes.
3. After optimisations: repeat 1 and confirm a gain before launching the full run.

## Checklist before any large run
- Observations of the last run written here.
- Known optimisations applied, each verified by a small test or marked as unverified.
- Hub backup taken (nightly timer; take one manually before a wipe) and a restore known to work.
- Database changes verified from a fresh connection.
- Speed set on the dashboard; auto-backoff on.
