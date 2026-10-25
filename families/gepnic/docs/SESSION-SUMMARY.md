# eprocure scrape: what happened, what we learned, where it stands

Written 2026-10-17 after the scrape reached 94.6%. Times are CEST (the hub host's clock). `docs/OBSERVATIONS.md` holds the earlier
experiment log; this file pulls the whole story together.

## Current state
- **846,707 of 895,058 tenders stored with real data (94.6%).** A further 1,171 rows are empty captures from the portal outages; they are fetched again on the next pass. About 25,000 are missing (about 2.8% of the total), all in windows that
  failed while the portal was down. The rest of the gap to 100% is tenders the plan counted that the portal may no longer list.
- Queue: 1,236 windows done, 209 failed (retryable), 40 pending. No run is active; the operator stopped the last one on the evening of 17 Oct, after 20:44.
- Documents: 1.49 million files stored (about 627 GB, 1.25M PDFs, 241k Excel). 720,330 document links (32%) are refused by the portal
  itself. 687 failed on connection errors and are retryable.
- Running now: the hub, the uptime monitor (one check a minute), the dashboard panels. The auto-retry and self-restart code is
  deployed but has never run to the end of a run.

## What was built
- **Hub** (`scrape.s1d.me`, repo `scrape-hub`): FastAPI + Postgres. GitHub Actions OIDC identity, presigned R2 uploads (keys never leave
  the hub), work queue with slices, work stealing, stale-claim reissue, atomic replace, known-record and window-stored lookups,
  failures table with CSV export, metrics, run/IP history, dashboard, shared password with status.s1d.me.
- **Scraper** (`eprocure-scrape`): plan/count/consolidate pipeline that turns org-year counts into 1,485 date windows (bucketed across
  20 runners), the crawler (sessions pool, adjustable gate, hub-controlled speed), the September parser port (62 columns), document
  extraction and verification, Postgres views.
- **Added this session**: uptime probe with dashboard panel and auto-hold, a zoomable rate explorer (`/explore`), reports route,
  `min_threads` floor, `http_error` in auto-backoff, 4 hub workers, an index for per-window counts.

## What happened, in order
1. **First full run (12:34 on 16 Oct)**: 20 runners stored 30,424 tenders in 26 minutes. Throughput looked capped, so I guessed CPU-bound,
   cancelled a healthy run and started a 4-process variant. You stopped that: *note, optimise, verify, then run.* That rule is saved.
2. **Experiments A to D, B, B2** (2 runners, same windows, 600 tenders per process):

   | | Setup | Tenders/min per runner |
   |---|---|---|
   | A | 1 process x 4 threads, docs on | 31 |
   | C | docs off | 106 to 133 |
   | D | 1 x 16 threads | 77 |
   | B | 4 x 8 | 154 |
   | B2 | 8 x 16 (80 to 86% of 4 cores) | 361 |

   Throughput scales about linearly with total threads until the runner's CPU is near its limit. My CPU-bound guess was wrong.
3. **Refused documents** (experiment E): same 120 tenders at 1 and 16 threads gave identical results (230 stored, 159 refused), so refusals
   are a property of the document, not of load. They are concentrated in bands of months (see the table below).
4. **First 6k/min attempt (14:52)**: 160 processes x 32 threads. Zero tenders in 14 minutes. The portal's search page timed out and
   returned pages without a search form. Cancelled.
5. **Optimisations, verified on a 120-tender test**: parallel document downloads inside a tender (docs 6.5 to 4.6 s per tender, same
   230/159 result), spare worker threads so uploads overlap fetching, staggered starts, a pause before retrying an empty search,
   a session cap per window, `--project` so experiments use separate data.
6. **Second attempt and the empty-record incident (15:24)**: from 15:24 the portal answered every detail page with "session expired",
   even to a brand-new session from this host. The scraper stored those pages as tenders: about 6,000 empty records in the real project and
   all 6,925 in the eval project. Fixed: a tender whose pages are still expired after a fresh login now fails; the hub's skip check
   ignores records with an empty title, so they are fetched again.
7. **The good stretch (17:51 on 16 Oct to 02:20 on 17 Oct)**: 60 processes stored 295,544 then 460,845 tenders. Best 5-minute average
   3,975 tenders/min at about 01:00; a whole hour averaged 3,480. This took the total from about 40,000 to about 780,000.
8. **The long tail**: from then on every launch stored a little, then the search page stopped answering. About 17 outages in 29 hours.
9. **Monitoring and controls built along the way**: uptime probe (1 check a minute of the real search page), runners hold after 3 failed
   checks, a minimum-threads floor, a dashboard panel for portal reachability, the rate explorer, and finally auto-retry.

## What we learned
**Speed**
- Cost per tender (two big runs, 756,389 tenders): fetching the 3 pages 9.7 s (portal latency about 3 s per request under load, 0.4 to
  0.6 s when idle), documents 3.3 s, uploads 2.7 s, listing 0.9 s, parsing 0.1 s.
- **Captcha is not the bottleneck**: 20,154 attempts for 756k tenders (0.027 per tender), 0.05 s of CPU each, about half fail. It adds a
  few seconds of set-up per window.
- A window of up to 100 tenders takes about 1.6 minutes (mostly set-up and listing), 12.7 minutes for 301 to 600, 16.1 for 901+.
  **Window size is not the problem**: 803 of the 814 large windows (601 to 1,200 tenders) finished; the 311 left at 14:50 were one
  window per organisation, mostly small, and 242 of them had nothing stored (the portal was down when they ran).
- Listing is serial (10 tenders a page, about 3.4 s a page). That was fixed by fetching while listing.
- Rates reached 3,975/min when the portal was healthy, but the average over the run is low because of outages.

**The portal**
- It works in bursts. The search app (not the front page, which still answered) stopped answering about 17 times, usually a few
  minutes after load rose. The longest outage was 1 h 06 min. 26% of monitored time was down.
- I could not prove load causes the outages. The pattern is suggestive (15:24, 17:25, 18:02, 19:51, 02:20, 13:49) but the portal also fails
  at lower load.
- A degraded portal answers "session expired" for detail pages, so a tender must be checked for real content before it is stored.

**Documents**
- Refusals follow the tender's publication month, not the tender. By year: 2015 92.7% refused, 2016 100%, 2017 34.9%, 2018 38.6%,
  2019 17.8%, 2020 on about 0.1%. By month: February 2015 to January 2017 is 95 to 99.7% refused every month; other bands are
  Oct to Nov 2014, Oct to Dec 2017, Jan 2018, Aug to Sep 2018, Mar 2019 and Oct 2019. From November 2019 it never exceeds about 1.5%.
  It looks like the portal lost or cannot serve those files; a real browser got the same error page for the two tenders checked.
- The tender's own documents (NIT notices, BOQ) are on the details page as names, descriptions and sizes with **no download links**
  (checked on one DRDO tender). `procurement_details` holds them mangled (the September parser's flat key/value dump collapses
  several documents into one). The raw HTML in R2 keeps everything, so a clean list can be rebuilt.

**Volume**
- About 703 GB uploaded to R2 (2.47 million files, 701 GB counting retries and tests), about 0.7 to 0.8 TB downloaded from the portal
  (an estimate: documents are measured, tender HTML is estimated at 20 to 110 GB), 2.96 million portal requests, about 8 to 10 GB through
  the hub (825,203 records, 7.8 GB of JSON), about 945,000 hub calls from 80 runner IPs during the big scrape.

## What we fixed
| Problem | Fix |
|---|---|
| Cancelled/Retender tenders had no fields | exact link matching on the status page |
| 43% of "documents" were signature pop-ups | link rules plus byte sniffing |
| Empty records stored when the portal said "session expired" | fail the tender; known-records ignores empty titles |
| Window listing blocked fetching | stream pages into the workers |
| A window already stored still got listed | skip it when stored count >= 98% of the plan |
| 60 processes all searching at once | start staggering, retry pause |
| Runs ended while failed windows waited | `claim_next` waits; hub hands failed windows out again after 3 minutes, never while the target is down, up to 40 tries |
| Nobody restarted runs | final workflow job restarts if windows remain (never after a manual cancel) |
| Runners hammering a dead portal | hold after 3 failed checks; `min_threads` floor keeps a minimum going |
| Dropped connections ignored by auto-backoff | counted (20 or more and above half the records) |
| Hub overloaded risk | 4 workers, pool of 20, index for per-window counts |
| Dashboard password not working | the hub user could not read the shared password file; added to the group |

## What I got wrong (so you can discount my earlier statements)
- Cancelled a healthy run on a CPU guess; the data later showed the guess was wrong.
- Said the speed was captcha-bound and window-size-bound at different times; the numbers above show neither.
- Said "it's cheap to leave an experiment running": the 20-runner limit means a running pass blocks any new one.
- Deployed a hub change once with one test failing (a queue-count shape check); fixed right after.
- Estimated an 8-hour finish at 1,700/min from the good stretch; the portal's outages made that unreachable.

## Conclusions that led to the current plan
1. **The portal, not our code or captchas, sets the pace.** We reached 3,975 tenders/min when it was up; the time it was down
   decided the total.
2. **Keep the load moderate and be patient.** More processes did not help once the search page stopped answering, so the runs now stay up
   and retry by themselves instead of needing a restart each time.
3. **Don't trust "success" without content.** Every record must have a real title, or it is retried.
4. **Documents from some months cannot be recovered through the portal.** They are listed in the failures table (CSV export).

## Still to do
- Let the retry loop finish the roughly 25,000 missing tenders (start it again; it now runs until all windows are done or given up).
- Retry the 687 failed documents (no pass exists yet, because stored tenders are skipped).
- Rebuild the tender document lists (NIT and work-item documents) from the saved pages in R2 using the runners; needs a hub endpoint to read
  pages and one to write the parsed list back.
- Compare stored tenders per window against the plan and report every window that is short.
- Optional: count bytes received per request in the crawler; save the month-by-month refused table as a CSV.
- Unverified: the restart job at the end of a run, and the explorer page in a real browser.

## Update, 17 to 18 Oct (after the first version of this file)
- **Auto-retry**: runs now stay up until every window is done; failed windows come back after 3 minutes (never while the portal is down,
  at most 40 tries); a final job restarts the workflow if work remains. `min_threads` keeps a floor; `http_error` counts in auto-backoff.
- **Fetch while listing** and **skip windows already complete** (stored >= 98% of the plan) are in; they help the tail little because 242
  of the 311 remaining windows had nothing stored when the portal was down.
- **Tender document lists**: `sections.py` parses the NIT and work-item document tables, covers, payment instruments and corrigenda from
  the saved details page (382 sampled pages from 2011 to 2026, 0 failures). Backfilled on all 847,691 stored records using the runners
  (about 41,500 records/min, no portal traffic). The crawler now writes them for new tenders. These documents have no download links.
- **New finding**: 1,785 captures (801 with a title) were the portal's *"Stale Session"* page, a second wording for expired sessions the
  crawler did not detect. Fixed in the crawler and the hub; their 160 windows are back in the queue.
- **Documents downloaded**: 1,572,905 files stored (673 GB), 734,337 refused, 695 failed; 502,050 of 847,878 tenders have at least one file.
- **New pages**: `/explore` (rate and outage chart you can zoom), `/tender` (look up one tender), `/reports/` (saved charts).
- Where to pick up: see `docs/HANDOFF.md`.
