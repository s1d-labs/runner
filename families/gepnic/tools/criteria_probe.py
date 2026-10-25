#!/usr/bin/env python3
"""Probe the GePNIC search page's three search criteria. Used to compare Criteria I (tender status + status date, all organisations)
with Criteria II (organisation + publish date). Sends a handful of searches, so run it politely and not during the portal's
maintenance window. Needs the captcha model (torch) and the repo's modules on the path.

  python tools/criteria_probe.py status-totals [--from 01/01/2000 --to 31/12/2030]   # Total records for each of the 9 statuses
  python tools/criteria_probe.py count --status 6 --from 01/01/2019 --to 31/01/2019   # one count (add --org 159 for Criteria II)
  python tools/criteria_probe.py page --status 6 --from 01/01/2019 --to 31/01/2019    # list a whole window page by page, check it ends cleanly
  python tools/criteria_probe.py form                                                 # print the search form's fields and options

Another portal: --base https://<portal>/eprocure/app (same GePNIC software is assumed; check `form` first).
"""
import argparse
import os
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "captcha"))

import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

import counting  # noqa: E402
import scrape_organization as so  # noqa: E402

STATUSES = {1: "To Be Opened", 2: "Technical Bid Opening", 3: "Technical Evaluation", 4: "Financial Bid Opening", 5: "Financial Evaluation",
            6: "AOC", 7: "Retender", 8: "Cancelled", 9: "Concluded"}


def search(base, over, tries=12, out_dir="/tmp/criteria_probe"):
    """Run one search with the given form overrides ({field: value}); returns (results html, session) or (None, session)."""
    os.makedirs(out_dir, exist_ok=True)
    url = f"{base}?page=WebTenderStatusLists&service=page"
    s = requests.Session()
    s.cookies.set("cookieWorked", "yes", domain=urllib.parse.urlparse(base).hostname, path="/")
    s.headers.update({"User-Agent": so.USER_AGENT})
    for _ in range(tries):
        html = so.safe_request(s, "GET", url).text
        soup = BeautifulSoup(html, "html.parser")
        form, img = soup.find("form"), soup.find("img", id="captchaImage")
        if form is None or img is None:
            time.sleep(3)
            continue
        cap = so.solve_captcha(s, img.get("src"), output_dir=out_dir)
        if not cap or len(cap) != 6:
            continue
        payload = []
        for n in ["formids", "seedids", "component", "page", "service", "session", "submitmode", "submitname"]:
            v = form.find("input", {"name": n})
            if v:
                payload.append((n, v.get("value", "")))
        for f in form.find("input", {"name": "formids"}).get("value", "").split(","):
            if not f:
                continue
            if f == "captchaText":
                payload.append((f, cap))
            elif f == "Search":
                payload.append((f, "Search"))
            elif f in over:
                payload.append((f, over[f]))
            else:
                i, sel = form.find("input", {"name": f}), form.find("select", {"name": f})
                payload.append((f, i.get("value", "") if i else ("0" if sel else "")))
        s.headers.update({"Origin": f"{urllib.parse.urlparse(base).scheme}://{urllib.parse.urlparse(base).netloc}", "Referer": url,
                          "Content-Type": "application/x-www-form-urlencoded"})
        r = so.safe_request(s, "POST", urllib.parse.urljoin(base, form.get("action", base)), data=payload)
        if 'id="tabList"' in r.text or counting.parse_total(r.text) is not None:
            return r.text, s
    return None, s


def overrides(a):
    """Criteria I: tenderStatus + fromDate/toDate (the date the tender reached that status), no organisation.
    Criteria II (--org): OrganName + publishedFromDate/publishedToDate."""
    if a.org:
        return {"OrganName": str(a.org), "tenderStatus": "0", "publishedFromDate": a.date_from, "publishedToDate": a.date_to}
    return {"tenderStatus": str(a.status), "fromDate": a.date_from, "toDate": a.date_to, "OrganName": "0"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status-totals", "count", "page", "form"])
    ap.add_argument("--base", default="https://eprocure.gov.in/eprocure/app")
    ap.add_argument("--status", type=int, default=6)
    ap.add_argument("--org", default="")
    ap.add_argument("--from", dest="date_from", default="01/01/2000")
    ap.add_argument("--to", dest="date_to", default="31/12/2030")
    a = ap.parse_args()
    if a.cmd == "form":
        s = requests.Session()
        html = s.get(f"{a.base}?page=WebTenderStatusLists&service=page", headers={"User-Agent": so.USER_AGENT}, timeout=30).text
        form = BeautifulSoup(html, "html.parser").find("form")
        for t in form.find_all(["input", "select"]):
            if t.get("type") == "hidden":
                continue
            opts = [o.get_text(strip=True) for o in t.find_all("option")] if t.name == "select" else []
            print(f"{(t.get('name') or t.get('id'))!s:22s} {t.get('type', t.name):8s} {len(opts) or '':>4} {', '.join(opts[:6])}")
        return
    if a.cmd == "status-totals":
        total = 0
        for k, name in STATUSES.items():
            html, _ = search(a.base, {"tenderStatus": str(k), "fromDate": a.date_from, "toDate": a.date_to, "OrganName": "0"})
            n = counting.parse_total(html) if html else None
            total += n or 0
            print(f"{name:24s} {n}", flush=True)
            time.sleep(1)
        print("SUM over the 9 statuses:", total)
    elif a.cmd == "count":
        html, _ = search(a.base, overrides(a))
        print("Total records:", counting.parse_total(html) if html else None)
    else:
        html, s = search(a.base, overrides(a))
        total = counting.parse_total(html)
        rows, nxt = so.parse_results_page(html)
        ids, pages, t0, clean = [r["tender_id"] for r in rows], 1, time.time(), True
        while nxt:
            h = so.safe_request(s, "GET", nxt).text
            if 'id="tabList"' not in h:
                print(f"page {pages + 1} came back without a results table: the listing would have ended silently")
                clean = False
                break
            r2, nxt = so.parse_results_page(h)
            ids += [r["tender_id"] for r in r2]
            pages += 1
            time.sleep(0.2)
        print(f"portal total {total} | listed {len(ids)} rows, {len(set(ids))} unique, {pages} pages, {time.time() - t0:.0f}s | clean end: {clean}")


if __name__ == "__main__":
    main()
