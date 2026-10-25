"""Count the tenders in one organisation + date window with a single portal search.

The results page footer carries "Total records: N", so no paging is needed.
"""
import re

import tempfile

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

import scrape_organization as so
import portal  # noqa: E402


def parse_total(html):
    """Total number of matches on a results page, or None if the page is not a results page."""
    soup = BeautifulSoup(html, "html.parser")
    for td in soup.find_all("td", class_="list_footer"):
        m = re.search(r"Total records:\s*([\d,]+)", td.text)
        if m:
            return int(m.group(1).replace(",", ""))
    text = soup.get_text()
    if any(m in text for m in ("No Tenders found", "No Records Found", "No records found", "No tenders found")):
        return 0
    table = soup.find("table", id="tabList")
    if table:    # footer missing: count the rows shown (a lower bound, one page)
        return sum(1 for r in table.find_all("tr") if len(r.find_all("td")) >= 6 and r.find_all("td")[0].text.strip().endswith("."))
    return None


def window_count(org_id, from_date, to_date, retries=10):
    s = requests.Session()
    s.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=3))
    portal.cookie(s)
    s.headers.update({"User-Agent": so.USER_AGENT, "Accept-Language": "en-US,en;q=0.5"})
    html = so.fetch_search_page(s, org_id=str(org_id), max_retries=retries, output_dir=tempfile.gettempdir(),
                                from_date=from_date, to_date=to_date)
    n = parse_total(html)
    if n is None:
        raise RuntimeError("search returned no recognisable results page")
    return n
