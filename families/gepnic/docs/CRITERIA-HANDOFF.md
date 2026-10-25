# Search Criteria I vs II for the crawler: assessment and handoff

Written 2026-10-18 for a new chat. Read `docs/HANDOFF.md` (where everything lives, how to run it) first, then this file. This one is about a single
decision: **how should the crawler slice a GePNIC portal into windows, by search Criteria I or Criteria II**, now that the scrape of
eprocure.gov.in is 94.7% done and the plan is 26 more portals with about 16 million tenders.

Everything below is either marked **verified** (I ran it, numbers given) or **not verified**. Nothing here is a promise that Criteria I works on
the other portals.

## Short answer
- **eprocure.gov.in (already scraped):** keep Criteria II. It is built, stable between passes, and only about 41,700 tenders are left.
- **The 26 new portals:** Criteria I looks better as the main way to slice, **if three gates pass** (section 8). Its strengths are big windows with
  almost no overhead, no organisation list to maintain, and cheap counts. Its weaknesses are windows that drift as tenders change status, a
  few unknowns about other portals, and that our code and storage paths assume an organisation id.
- **Separate from the criteria question:** the hub database will not hold 16 million records in the current layout (section 9).

## 1. What the portal offers
Page: `https://eprocure.gov.in/eprocure/app?page=WebTenderStatusLists&service=page`. One form, three independent search criteria. The portal
runs **one at a time**, with precedence III, then II, then I, even if several are filled in. The page says: "In Search criteria I, Tender status is
mandatory. In criteria II, either Organisation Name or Tender Reference Number is mandatory. Tender ID finds exact match for Search Criteria III."
Captcha is required every time.

| | Criteria I | Criteria II | Criteria III |
|---|---|---|---|
| Mandatory | Tender Status (9 options) | Organisation (872 options) or Tender Ref. No | Tender ID (exact) |
| Filters | Keyword, **From Date / To Date**, Form of Contract (23), Tender Category (Goods/Services/Works), Tender Type (5), Product Category (154) | Department, Division, Sub Division (empty lists), **Published From / To**, Keyword | none |
| Scope | all organisations at once | one organisation (or one ref no) | one tender |

Results: a table (S.No, Tender ID, Title and Ref.No., Organisation Chain, Tender Stage, Status), 10 rows a page, and a footer "Total records: N" on
page 1, so a count needs one search and no paging (`counting.parse_total`).

What the crawler uses today is **Criteria II** (organisation + published dates), with `tenderStatus=0`. Criteria I and III are not used.

## 2. What I tested (all on eprocure.gov.in, 18 Oct 2026). Rerun with `tools/criteria_probe.py`
1. **The 9 statuses cover (almost) everything: verified.** Criteria I, dates 2000 to 2030 (a wider range than 2011 to today gave the same numbers):

   | Status | Tenders |
   |---|---|
   | To Be Opened | 90,395 |
   | Technical Bid Opening | 112,385 |
   | Technical Evaluation | 105,525 |
   | Financial Bid Opening | 197,355 |
   | Financial Evaluation | 71,555 |
   | AOC | 159,505 |
   | Retender | 45,116 |
   | Cancelled | 107,483 |
   | Concluded | 0 |
   | **Sum** | **889,319** |

   Our plan (Criteria II, per organisation and year) counted **895,058**: the two methods agree within 5,739 (0.6%). **Not known** why they differ
   (tenders the status filter cannot see, or stale plan counts). We hold 848,718 records, 846,785 of them real, so about **41,700 are missing
   against the Criteria I total** (the plan suggested about 48,700).
2. **Same universe, not "more tenders": consistent with the data.** AOC via Criteria I is 159,505; we stored 151,010 AOC tenders via Criteria II
   = 94.67%. Overall we hold 847,570 of 895,058 = 94.69%. The match to 0.02 points says both reach the same set.
3. **The From/To date means the date the tender reached its current status, not published/opening/closing: verified.** AOC for the single day
   15 Jan 2019 returned 89 tenders. Opening 8: **0 were published that day**, only 2 had their bid opening on it, 0 had submission end then.
   But I then looked for "15-Jan-2019" in the first 6 of them: **all 6** carry it, in 9 pages in total, mostly as the timestamp of a bid decision on the summary
   report (e.g. "Accepted-AOC L1 15-Jan-2019 01:49 PM") and in two cases as the bid opening. Small sample; one status only.
4. **Detail pages for Criteria I results load normally: verified.** Fetching a result's 3 detail pages after a Criteria I search: real pages (31, 32 and
   45 KB), no session renewals, no new logins. (An earlier attempt of mine printed empty values because I read the wrong metadata keys; the pages were fine.)
5. **A long window pages cleanly: verified up to 2,226 rows.** AOC 1 to 31 Jan 2019: Total records 2,226; listed 2,226 rows, 2,226 unique ids, 223 pages,
   326 s, clean end. Paging deeper than that is **not verified**. Year-long counts work (AOC 2019 = 19,320; Cancelled 2019 = 11,610; AOC 2011 to 2026 = 159,505)
   but I did not page windows of that size.
6. **How much of that window did we already hold: verified.** Of those 2,226 AOC tenders we hold 2,131 (95.7%); the 95 missing are mostly tenders published
   in 2018 whose AOC fell in January 2019 (their windows are in the 2018 plan).
7. **The search is not stable over time for Criteria I (by construction), stable for II:** tenders move between statuses.
8. **"Concluded" returned 0** for every range tried. Probably an unused status on this portal.

## 3. Cost of each operation (measured from the hub host, one request at a time)
| Operation | Median time | Size |
|---|---|---|
| Search form GET | 0.88 s | 120 KB (the organisation dropdown makes it heavy) |
| Captcha attempt (reload form + POST) | about 1.4 s | about 245 KB |
| A login (about 1.8 attempts, 45% of attempts fail) | about 2.4 s | about 440 KB |
| Results page (10 tenders) | 1.26 s | 139 KB |
| A tender's 3 detail pages | 1.14 s | 93 KB |
| A document file | 0.75 s | about 245 KB |

A tender costs about 2.7 s of portal time (details, about 2 documents, its share of listing). Over two big runs (756,389 tenders) the time per tender
was: fetching pages 9.7 s (mostly the portal being slow under load), documents 3.3 s, uploads 2.7 s, listing 0.9 s, parsing 0.1 s. **Captcha is not the
bottleneck** (0.027 attempts per tender, 0.05 s of CPU each), but every new session needs a login.

**Window overhead (estimate from the table above).** Each window needs one search and roughly one login per 4 tenders, up to the number of threads.

| Tenders in the window | Extra portal time per tender | Share of normal work |
|---|---|---|
| 10 | about 0.7 s | about 27% |
| 50 | about 0.6 s | about 23% |
| 100 | about 0.4 s | about 13% |
| 300 | about 0.1 s | about 4% |
| 1,000 | about 0.04 s | about 1% |

This is why the tail of this scrape is slow: of the 256 unfinished windows on 18 Oct, **248 are the only window of their organisation** and the median has
78 tenders (finished windows: median 843). Criteria II produces that tail by design (one window per small organisation).

## 4. Comparison
| | Criteria I (status + status date) | Criteria II (organisation + published date) |
|---|---|---|
| Window size | any, cut by date; no tiny tail | depends on the organisation; small organisations give tiny windows |
| Needs an organisation list | no | yes (872 options, ids shift when organisations are added; plan, count and consolidate pipeline) |
| Planning | one cheap count per window, bisect dates until the window is at most about 2,000 rows | per organisation and year, then merge and split |
| Overhead per tender | 1 to 4% for windows of 300 or more | 20 to 30% for windows of 50 or fewer |
| Stable between passes | **no**: status changes move tenders between windows | yes: publish date never changes |
| Completeness check | per window (rows listed against "Total records") and status totals | per window against the planned count |
| Fits incremental updates | well: "tenders with a status date in the last day" | by organisation, less natural |
| Needs code changes | planning, search, window type, storage paths (section 6) | none (already built) |
| Risk of silently missing tenders | status drift; a status the dropdown does not offer | truncated listings (fixed, see below), stale plan counts |

## 5. Challenges with Criteria I, worst first
1. **Status drift.** The key (current status, status date) changes as a tender progresses. A tender can move into a window you already scanned and be missed;
   it can also appear twice (harmless: the tender id is the key and we skip tenders we already hold). Mitigation: scan statuses in lifecycle order
   (statuses move forward, so a tender leaving a not-yet-scanned status lands in a later one), repeat the recent date windows in a second sweep, and reconcile
   the totals at the end. Backward moves are rare; **not measured**.
2. **Statuses the dropdown does not offer.** Our stored data has stage labels "Bid Opening" (63,903 tenders) and "Evaluation" (8,933) that are not among the 9
   options. The sum above suggests they are covered by the 9 (total within 0.6% of the plan), but the 5,739 difference is unexplained.
3. **Date meaning.** The window date is the status date, not the publish date, so you cannot plan once and be finished, and you cannot compare windows
   with our stored publish dates month for month.
4. **Slicing without losing tenders.** Do not slice by category, tender type or product category: a tender with no category would fall out of every slice. Slice
   by **date only**, bisecting like `windows.split_window`, so each tender sits in exactly one window per status.
5. **Deep paging.** Verified to 2,226 rows only. Keep windows at most about 2,000 rows until deeper paging is checked.
6. **Truncated listings.** On 18 Oct we found that a results page coming back without a table (error or expired session) ended the listing silently and the
   window was marked done: Delhi University 01/12/2020 to 31/10/2021 stored 30 of 1,172, and listing it again returned all 1,172. **Fixed** in
   `crawl.list_pages` (a page without `id="tabList"` raises) and a listing more than 10% short is retried up to 3 times before being accepted. Longer windows make
   this matter more, so keep the fix and the per-window self-check.
7. **The organisation is not in the window.** Our code uses the organisation id for storage paths, the "already complete" check, the backfill and the SQL
   views. A Criteria I row gives the organisation *name* only (the "Organisation Chain").
8. **The portal is one server.** See section 7. This is true for either criterion.
9. **Other portals are untested.** Form, captcha style, status names, limits, maintenance windows may differ. Our captcha model was trained on this portal's captcha.

## 6. What would change in our code if we adopt Criteria I
Unchanged: fetching the 3 detail pages, `parsing.py`, `documents.py`, `sections.py`, uploads, the hub API, the retry loop (`claim_next`), the session pool,
fetch-while-listing, probes, dashboard, explorer, and everything already stored.

Changes (all in the front half):
| Piece | Files | Change |
|---|---|---|
| Plan and counts | `plan.py`, `count.py`, `counting.py`, `consolidate.py`, `windows.py`, `orgs.py`, `data/org_year_counts.json` | new generator: for each status, count a date range, bisect until at most about 2,000 rows; queue payload `{kind: "status", status, from_date, to_date, expected}` with no organisation |
| Search | `scrape_organization.fetch_search_page(org_id, ...)`, `crawl.list_pages`, `counting.window_count` | accept a status (and no organisation); `tools/criteria_probe.py` shows the payload that works |
| Sessions | `scrape_organization.get_thread_session/renew_thread_session`, `crawl.fetch_pooled` | a new session must repeat the same search; today it repeats an organisation search |
| Records | `crawl.crawl_org` (`org_id`, `org_name`, `label_matches`) | `label_matches` not needed; take the organisation from the row's chain; derive the organisation code from the tender id |
| Storage paths | `documents.tender_prefix(tender_id, org_id)` | see below |
| Skip checks | hub `window-stored`, `backfill/todo` (by organisation and window) | rely on `known-records` (by tender id) and the per-window self-check |
| SQL views and indexes | `sql/make_tenders_sql.py`, `scripts/ops/tender_index.sql` in the hub repo | tolerate a null organisation id; `eprocure.org_index` maps ids to codes |

**Storage path and the organisation id.** Today objects live under `<project>/pages/<year>/<organisation id>/<tender id>.json.gz` (and `docs/...`). The id is the
portal's dropdown id at scrape time and can be renumbered; each record stores the exact key, so nothing breaks, but the folder is not durable. The tender id
carries a stable **organisation code** (`2019_DRDO_875304_1` is DRDO). Data check over 848,718 records: 577 organisation ids, 593 codes, **598 id/code pairs**;
15 ids carry more than one code (id 298, "Government of Andhra Pradesh", has 5), 4 codes appear under more than one id (MFPD under 2 ids, 5,309 tenders), and
81 year+number pairs repeat across different codes. Recommendation for new portals: `<project>/pages/<year>/<org code>/<tender id>.json.gz`, one project per
portal. Do not move the existing 2.4 million objects. `eprocure.org_index` (hub repo, `scripts/ops/tender_index.sql`) records which id and code own which tenders and the path
pattern, for future lookup.

## 7. The portal is a single instance, and that changes how you scrape
Verified: one public IP (164.100.78.245); the session cookie ends in a node name `eprocgep1` (Tomcat's jvmRoute); 25 cookie-less requests all returned `eprocgep1`.
The front page and static pages kept answering during outages while the tender application did not. The page banner on 18 Oct announced maintenance 10:00 to 20:00
(probably IST) for that day.

Uptime probe data (1 check a minute since 16 Oct 17:05, about 2,300 checks):
- Portal checks failed 5% of the time with none of our Actions running, 44% with them running and sending requests, 62% with them running but quiet.
- Same hour of day, with and without our Actions: 10:00 CEST 74% vs 0%, 12:00 80% vs 11%, 13:00 73% vs 0%, 16:00 92% vs 0%, 18:00 70% vs 6%. At night
  (23:00 to 01:00 CEST, about 02:30 to 04:30 IST) 0 to 2% failed **with or without** us, even at 3,000 to 4,000 tenders a minute.
- Reading: the portal copes at night and in the Indian working day and evening it fails once we run. Cannot separate "our rate" from "any runners at all".
- Peak: 3,975 tenders a minute (5-minute average). Best sustained stretch about 3 hours at 2,000 to 4,000 a minute, around 23:00 to 02:20 CEST.

Practical rules (not yet encoded anywhere except the scheduled night pass):
- Run heavy at night in India; stay at or near zero in the Indian daytime.
- Different portals are different servers, so 26 portals can run in parallel without sharing capacity. Each one is still a single machine.
- Prefer fewer processes with more threads each (fewer concurrent searches and logins), larger windows, fewer captcha failures.
- Keep the uptime probe and the auto-hold; it is per target (`settings.probe`, `PROBE_TARGETS` env in `scripts/probe.py`).

## 8. Gates before committing a new portal to Criteria I
1. **Status totals tie out.** `criteria_probe.py status-totals` summed over the 9 statuses is within about 1% of an independent count (organisation totals or the portal's own
   statistics). Pass on eprocure.gov.in (0.6%). Unknown for the others.
2. **Deep paging.** A window of about 2,000 rows lists to the end with unique ids equal to "Total records". Pass at 2,226 rows on eprocure.gov.in. Try a larger window too.
3. **Form and captcha.** `criteria_probe.py form --base <portal>` shows the same fields and our captcha model solves the portal's captcha (measure the first-try rate).
4. **Detail pages open after a Criteria I search** on that portal (3 pages, no expired page).
5. **Drift check.** Scan a date range twice, a few hours apart, and count tenders that appear in the second scan but not the first.
6. **Per-window self-check in production.** Mark a window done only when unique ids listed equal its "Total records" at listing time (allow a small tolerance after retries), and reconcile the status totals at the end.

## 9. Scale: 26 portals, about 16 million tenders (estimates)
- **Database does not fit as is.** Today: 848,718 records take 4.1 GB in `records` (about 4.9 KB a tender); the whole database is 8.8 GB; `tender_index` 373 MB, `file_index` 799 MB.
  Extrapolated to 16 million: records about 78 GB, `tender_index` about 7 GB, `file_index` about 15 GB, failures and metrics several GB: roughly **105 to 135 GB**. The host
  has **about 85 GB free** of 193 GB. Options: keep only parsed fields in Postgres and the raw pages and documents in R2; trim what each record stores (the old
  flat `procurement_details` dump duplicates the new `sections`); or add disk.
- **R2.** Saved pages about 19.5 KB gzipped a tender, so about 300 GB. Documents averaged 0.74 MB a tender here (1.57M files, 628 GiB for 848,000 tenders); at that rate 16 million
  tenders would be about 11.8 TB. Make document downloads optional per portal (and note that the portal refuses about a third of document links for 2015 to early 2017
  tenders here; other portals may differ).
- **Time.** Peak 3,500 tenders a minute on one portal. Spread over 26 portals at about 1,500 a minute each, about 8 to 10 hours of good portal time in total, only if the others cope as well as
  this one does at night. This portal alone lost about a quarter of its time to outages.
- **GitHub Actions.** 20 concurrent jobs per GitHub account (a running pass blocks any other); each job is capped at 6 hours (the crawl workflow restarts itself).

## 10. Open questions
- Why the Criteria I total (889,319) is 5,739 below the plan's 895,058.
- Whether tenders that change status mid-scan are lost often enough to matter (gate 5).
- Whether the From/To date for Criteria I behaves the same for every status (verified for AOC only; "To Be Opened" tenders have future dates).
- Whether a logged-in session can be reused across windows (would cut logins; never tested).
- How the other 26 portals differ, if at all.
- Whether the portal's maintenance is daily (the 18 Oct banner says that day only).

## 11. Where the evidence and tools are
- `tools/criteria_probe.py` (this repo): `form`, `count`, `status-totals`, `page` against any GePNIC portal with `--base`.
- `docs/OBSERVATIONS.md` (experiments), `docs/SESSION-SUMMARY.md` (the story), `docs/HANDOFF.md` (operations).
- Hub repo `~/projects/scrape-hub/scripts/ops/` (operator scripts, `tender_index.sql` builds `eprocure.tender_index`, `file_index`, `org_index`).
- The uptime probe history is in the hub table `probes` and on the dashboard (`/`, panel "Portal reachability") and `/explore`.
