"""Date windows for splitting an organisation's tenders into scrapable chunks.

Portal dates are DD/MM/YYYY and the search filters on publication date, inclusive on both ends.
"""
import calendar
from datetime import date, datetime, timedelta

FMT = "%d/%m/%Y"


def fmt(d):
    return d.strftime(FMT)


def parse(s):
    return datetime.strptime(s, FMT).date()


def iso(s):
    return parse(s).isoformat()


def year_window(y):
    return f"01/01/{y}", f"31/12/{y}"


def month_windows(y):
    return [(fmt(date(y, m, 1)), fmt(date(y, m, calendar.monthrange(y, m)[1]))) for m in range(1, 13)]


def split_window(frm, to):
    """Two halves that together cover the window exactly, or None if it is already a single day."""
    a, b = parse(frm), parse(to)
    if a >= b:
        return None
    mid = a + (b - a) // 2
    return (fmt(a), fmt(mid)), (fmt(mid + timedelta(days=1)), fmt(b))


def key(org_id, frm, to):
    return f"{org_id}:{iso(frm)}:{iso(to)}"


def crawl_item(org_id, name, frm, to, expected, partition_id=None):
    it = {"key": key(org_id, frm, to),
          "payload": {"org_id": str(org_id), "org_name": name, "from_date": frm, "to_date": to, "expected": int(expected)}}
    if partition_id is not None:
        it["partition_id"] = partition_id
    return it


def count_item(org_id, name, frm, to, chunk_max):
    return {"key": "count:" + key(org_id, frm, to),
            "payload": {"kind": "count", "org_id": str(org_id), "org_name": name, "from_date": frm, "to_date": to,
                        "chunk_max": int(chunk_max), "expected": 0}}


def build_plan(orgs, from_year, to_year, split_over=600, chunk_max=1200):
    """orgs: {org_id: {"name": str, "years": {"2018": n}}}.

    An org-year with at most `split_over` tenders is one crawl chunk (its year count is already known). A bigger one is
    counted month by month first; count tasks then halve any window that still has more than `chunk_max` tenders.
    Returns (crawl_items, count_items, summary). Crawl items are ranked biggest first so the long chunks start early.
    """
    crawl, counts, direct, split_years, direct_tenders, split_tenders = [], [], 0, 0, 0, 0
    for oid, o in orgs.items():
        for y_s, n in o["years"].items():
            y = int(y_s)
            if n <= 0 or not (from_year <= y <= to_year):
                continue
            if n <= split_over:
                crawl.append(crawl_item(oid, o["name"], *year_window(y), n))
                direct += 1
                direct_tenders += n
            else:
                counts.extend(count_item(oid, o["name"], f, t, chunk_max) for f, t in month_windows(y))
                split_years += 1
                split_tenders += n
    crawl.sort(key=lambda it: -it["payload"]["expected"])
    for rank, it in enumerate(crawl):
        it["partition_id"] = rank          # round-robin over slices, biggest first within each
    return crawl, counts, {"direct_chunks": direct, "direct_tenders": direct_tenders, "split_org_years": split_years,
                           "split_tenders": split_tenders, "count_tasks": len(counts)}


# ---- consolidating the queue: merge small adjacent windows, then balance them across runners

# Seconds of runner time: a fixed cost per window (captcha sessions, listing) plus a cost per tender, fitted from real runs
# at 4 threads (it reproduced seven runs within a few seconds).
PER_WINDOW_S = 13.1
PER_TENDER_S = 0.331


def merge_adjacent(items, cap):
    """Merge each organisation's windows that sit next to each other in time, as long as the total stays within `cap`.

    items: crawl items (see crawl_item). Returns [(members, merged_item)], one per resulting window; `members` are the
    original items it replaces. The merged window spans from the first member's start to the last member's end, so gaps
    between members (years with no tenders) are simply included.
    """
    by_org = {}
    for it in items:
        by_org.setdefault(it["payload"]["org_id"], []).append(it)
    out = []
    for org_items in by_org.values():
        org_items.sort(key=lambda it: parse(it["payload"]["from_date"]))
        group, total = [], 0
        for it in org_items + [None]:
            n = it["payload"]["expected"] if it else 0
            if it is not None and group and total + n <= cap:
                group.append(it)
                total += n
                continue
            if group:
                p = group[0]["payload"]
                merged = crawl_item(p["org_id"], p["org_name"], p["from_date"], group[-1]["payload"]["to_date"], total)
                out.append((group, merged))
            group, total = ([it], n) if it is not None else ([], 0)
    return out


def bucketize(items, buckets, per_window=PER_WINDOW_S, per_tender=PER_TENDER_S):
    """Longest-first assignment of items to `buckets` runner slices so all finish together.

    Sets item["partition_id"] = bucket + buckets * rank, which the hub reads as: slice = partition_id % buckets, and within a
    slice the lowest partition_id (biggest item) first. Returns the estimated seconds per bucket.
    """
    import heapq
    heap = [(0.0, b) for b in range(buckets)]
    heapq.heapify(heap)
    ranks = [0] * buckets
    totals = [0.0] * buckets
    for it in sorted(items, key=lambda i: (-i["payload"]["expected"], i["key"])):
        w = per_window + per_tender * it["payload"]["expected"]
        total, b = heapq.heappop(heap)
        it["partition_id"] = b + buckets * ranks[b]
        ranks[b] += 1
        totals[b] = total + w
        heapq.heappush(heap, (total + w, b))
    return totals
