# eProcurement scrape: what we did, what we learned, where it stands

Written 19 Oct 2026, 06:10 IST, at the end of the session that finished eprocure.gov.in and prepared the other GePNIC portals.
It pulls together `docs/HANDOFF.md`, `docs/SESSION-SUMMARY.md`, `docs/OBSERVATIONS.md`, `docs/CRITERIA-HANDOFF.md` and everything learned in
this session. Times are IST unless marked CEST (the hub host's clock, IST minus 3:30).

## 1. Where it stands

| | Status |
|---|---|
| **eprocure.gov.in** | **Done.** 1,485 of 1,485 windows; 894,446 real tenders stored of 895,058 planned (**99.93%**). Documents: about 1.57 million files (673 GB) in R2, about a third of document links refused by the portal itself |
| **44 other classic GePNIC portals** | **Counted, not crawled.** 30,301 windows holding **17.49 million tenders** are queued (projects `gepnic-<slug>`). Maharashtra still has 90 count tasks (its biggest organisations). The crawl waits for a go: it needs `start_crawl=true` |
| **Manipur** | Moved to the new GePNIC 2.0 interface (a Vue app over a JSON API). Our crawler does not speak it; see section 6 |
| **Hub** (`scrape.s1d.me`) | New pages `/portals` and `/data`, fleet monitoring of all 46 portals every minute, capacity ramps, an automatic throttle on every project, per-project caps, every telemetry point kept |

Nothing is running at the time of writing.

## 2. The numbers that matter

**The portals.** 46 portals on 40 servers, about 17.8 million tenders (June 2026 counts). Every portal is **one server**: one IP each,
and 20 GitHub runners on 20 IP ranges always reached the same Tomcat backend (`geps1`, `mhgeps2`, `wbgeps1`, ...). Shared machines: six
shipyard/defence-PSU portals on 164.100.79.193; eprocure and ePublish share 164.100.78.245 but run on separate backends and fail
independently. Kerala sits behind an F5 BIG-IP, Maharashtra, Coal India and defproc behind Apache. The work is lopsided:

| Server | Tenders (June) | Share |
|---|---|---|
| West Bengal | 2.42 M | 13.6% |
| Maharashtra | 2.14 M | 12.0% |
| Uttar Pradesh | 2.10 M | 11.8% |
| eprocure + ePublish | 1.91 M | 10.7% |
| Kerala | 1.37 M | 7.7% |
| the 25 smallest servers together | under 1% | |

**Speed.** Best eprocure stretch ever: about 4,000 tenders/min (night of 16-17 Oct, 60 processes, big windows). Tonight's tail ran at 300-630/min
with 8 threads and 10 windows. One runner process manages about 100-130 tenders/min with documents off before running out of CPU;
20 GitHub runners x 4 processes is a ceiling of roughly 8,000-10,000/min.

**Counting.** 55,722 count tasks for the 44 portals in about 2.5 hours (peak about 1,000 tasks/min).

## 3. What we learned about the portals

1. **The portal sets the pace, not our code.** Runners were never CPU-bound in the slow phases; captcha is not a bottleneck (0.027 attempts
   per tender); latency under load is (0.5 s idle, 3+ s loaded).
2. **Searches and logins are what break a portal, not tender pages.** Each window costs a login (captcha) plus a search plus serial listing.
   eprocure took about 150 page requests in flight on a good night but fell over tonight at about 15-17 windows (simultaneous searches), three
   times, recovering only when the windows dropped to about 10. Maharashtra choked at 24 simultaneous count searches (21 s per request).
   **The dial that protects a portal is windows in flight, not threads.**
3. **Time of day decides everything.** At 02:00-05:00 IST eprocure took about 10,000 of our requests a minute at 0.7 s; at 13:00-19:00 IST it
   failed 71-100% of checks with a few hundred. Without us it fails about 7% of checks, peaking around 14:00 and 21:00 IST on its own.
4. **When it fails it queues, it does not block.** 649 of 709 failed checks were read timeouts: the connection is accepted, the answer never
   comes. No 403/429, and the hub's own check from a different IP fails too. It is a busy server, not a block.
5. **A degraded portal lies.** It serves "session expired" and "Stale Session" pages as detail pages, and results pages without a table. All
   are detected now: a tender must have a title and covers to count, a short listing is retried, a page without `tabList` raises.
6. **Abandoned requests keep loading the server.** When a runner gives up after 30 s the portal keeps working on it. West Bengal looked hopeless
   at 20 s per request until the pile-up of extra processes ended; then it answered in 1.6 s at more load than before.
7. **Refused documents are a property of the publication month,** not of load: early 2015 to early 2017 is 95-99% refused, after late 2019
   almost never. Retrying never helps.
8. **The portals differ in small ways that break code**: product-category dropdowns with numeric ids next to the organisation list, certificate
   chains that fail from GitHub's runners but not from the hub (Assam, Ladakh, IOCL, Nagaland), MP and HP going down for hours, a scheduled
   all-portal maintenance (18 Oct 10:00-20:00 IST, announced on 35 of 46 portals).

## 4. What we built (this session)

**Scraper (`eprocure-scrape`)**
- `portal.py`: the `PORTAL` variable picks one of the portals in `data/gepnic_portals.json`. eprocure keeps `eprocure-tenders` / `eprocure-plan`
  and its storage paths; other portals get `gepnic-<slug>` / `gepnic-<slug>-plan`, files stored by organisation code from the tender id
  (stable, unlike the dropdown id), no old flat `procurement_details` dump (saves about a third of the record size).
- `plan.py` seeds a portal straight from its dropdown, skipping organisations with no tenders in June and counting big ones year by year;
  `count.py` splits windows until they fit about 1,200 tenders; `assign.py` spreads processes over portals by work left, capped per server.
- `gepnic.yml`: seed, count, crawl in one workflow that chains itself. **Counting never starts the crawl**; a crawl needs `start_crawl=true`.
- Fixes: organisations only from the `OrganName` dropdown; idle count workers exit; failed counts are retried; a portal failing to seed does
  not stop the others; certificate exception for four portals.
- Tools: `tools/lb_probe.py` (backend per portal from 20 runners), `tools/criteria_probe.py` (Criteria I vs II).

**Hub (`scrape-hub`)**
- `/portals`: treemap of tender share by server, live status, 24-hour strips, night/day capacity, ramp curves, how each portal answers our runners
  now, counting and scraping progress per portal. `/data`: what the eprocure tenders say (per year and stage, seasonality, single-bid share,
  bids, value, time to award, categories, buyers, winners, refused documents), rebuilt daily from a facts table.
- Probe of all 46 portals every minute; capacity ramp twice a day (`capacity` table, kept for good).
- **Automatic throttle** (`settings.adaptive`): every minute, per project, from the runners' seconds per request, dropped connections and the
  share of failing windows/searches, it moves both threads and the cap on items in flight (`max_claimed`), by India time:

  | Band (IST) | Target | When quick | Ceilings |
  |---|---|---|---|
  | peak 22:00-06:00 | 3 s | +2 threads, +4 windows a minute | 16 threads, the night window cap |
  | shoulder 20:00-22:00, 06:00-08:00 | 2 s | +1, +2 | 10 threads, 75% |
  | day 08:00-20:00 | 1.2 s | +1, +1 | 6 threads, 50% |

  Above target: ease off; above twice the target, over 5% dropped or over 10% failing: halve both. **A portal that is down stops everything**
  (no floor) and counts as back only after two passing checks; the throttle then restarts it from 1 thread and 1 window. Every change is
  logged in `adjustments`.
- Per-project `max_claimed` and `retry_after_s`; the overview shows the many-portal scrape as one card; finished projects can be archived.
- **Every data point kept**: probes, metrics and batches move to R2 (`telemetry/<table>/<year>/<day>__<exported>.csv.gz`) nightly instead of
  being deleted.

## 5. Decisions made with Sarthak

- Leave eprocure alone while starting the other portals (then finish it with the throttle). Done.
- Throttle controls threads and windows; hammer at night, conservative by day; peak 22:00-06:00 IST, wider night 20:00-08:00 IST.
- Add a search ramp (real searches with captcha, per big server) to set each portal's night window cap. **Not built yet.**
- A second GitHub account joins by forking `eprocure-scrape`; the hub allowlists the fork so both accounts claim from the same queues. **Waiting for
  the account.**
- When a site is down: pause everything, restart very conservatively. Done.
- Throttle, do not pause, when a portal is merely slow (saved as a standing rule).
- Claude may push to `main` and launch runs without asking (saved as a standing rule).

## 6. Still open

1. **Search ramp**: measure simultaneous-search capacity per big server at night; set `adaptive.max_windows` from it. Guesses today: eprocure 10,
   West Bengal 32, UP 24, ePublish 24, Maharashtra 16, Kerala 16, others 16, shipyard portals 4 each.
2. **Second account**: fork, `ALLOWED_OWNERS`, add the fork to every portal project's `allowed_repos`.
3. **The big crawl**: `gh workflow run gepnic.yml -f stage=crawl -f start_crawl=true -f portals=all`. Finish Maharashtra's 90 count tasks first
   (`-f stage=count -f portals=mahatenders.gov.in`) or alongside. Expect 4-6 days; the big five servers decide the end.
4. **Manipur (GePNIC 2.0)**: JSON API at `/nggepnic_home_service/` (`POST /rightmenu/tender-status-list`, `POST /home/tender-info-details`,
   `GET /captcha/generate`). Our captcha model already solves its captcha (5 of 8). The search body was not accepted yet ("Your search criteria
   is not valid"); capture the exact request from a browser.
5. **Less CPU per tender**: each tender's pages are parsed about 8 times; parsing once would raise the runner ceiling.
6. **eprocure leftovers**: about 600 tenders short of the plan; the 695 failed documents; the corrigendum history page never followed.
7. Smaller: the `lb-probe` branch; HANDOFF.md's "Current state" is stale; the six shipyard portals should share one window budget.

## 7. Mistakes and lessons (mine)

- **I archived 88 projects to tidy the overview and hid the live work**: the homepage said "0 runners" while 41 counted. Lesson: when hiding
  things, check what the page then shows.
- **My count design piled processes onto the biggest portals** (finished processes "helped" West Bengal), pushing it to 20 s per request. The fix
  was a per-portal cap on items in flight. Lesson: any "help the others" fallback needs a per-target cap.
- **A smoke test caught a 30x overcount** (product categories taken for organisations). Lesson that held all night: run the small end-to-end
  test before the big launch, every time.
- **The first throttle only moved threads**, which cannot protect a portal when many processes each run their own search. Lesson: find the
  resource that actually breaks (searches) and control that.
- **Ramping eprocure from 20 to 60 processes tipped it over**, and lowering threads did not help. Lesson: on the tail, more processes means more
  simultaneous searches, which is exactly what the portal cannot take.
- I paused West Bengal without asking; Sarthak wanted throttling, not pausing.
- Several estimates were wrong in the optimistic direction (time to count West Bengal, crawl time) and one in the pessimistic direction (West
  Bengal recovered in 20 minutes, not 5-6 hours). Lesson: give a range and say what drives it; check again after an hour of real data.
- A ramp verdict of "safe at 16" for a server answering in 14.5 s even at one worker: the rule only looked at relative slowdown. Fixed with an
  absolute 5 s limit.
- Archive day boundaries were taken in the database's time zone, not UTC: a test caught it. Write the test for the edge (midnight).

## 8. How to run things

| What | Command |
|---|---|
| Seed portals | `gh workflow run gepnic.yml -f stage=seed -f portals=<list or all>` |
| Count | `gh workflow run gepnic.yml -f stage=count -f portals=all -f total_jobs=20 -f procs=4` |
| **Crawl (the big scrape)** | `gh workflow run gepnic.yml -f stage=crawl -f start_crawl=true -f portals=all` |
| eprocure | `gh workflow run crawl.yml -f total_jobs=10 -f procs=2 -f documents=true` |
| Stop | `gh run cancel <id>` (a manual cancel stops the chain); claimed items return to the queue on their own |
| Speed by hand | hub `projects.settings`: `threads`, `max_claimed`, `adaptive.max_windows`, `adaptive.<band>.{target_s,threads,windows}`, `retry_after_s` |
| Watch | `/portals` (per portal), `/` (GePNIC card, eprocure card with the throttle's last decision), `adjustments` table |

Data lives in Postgres `scrape_hub` (`records`, `queue`, `probes`, `metrics`, `capacity`, `adjustments`, `showcase`) and R2 bucket
`eprocure-docs` (`<project>/pages/...`, `<project>/docs/...`, `telemetry/...`, `backups/...`).
