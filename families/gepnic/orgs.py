"""Current organisation dropdown from the portal. Its numeric ids are not stable (new organisations shift them),
so queue items are keyed by the id at seed time and verified against the live label before crawling."""
import requests

from bs4 import BeautifulSoup
import portal  # noqa: E402

SEARCH_URL = portal.SEARCH_URL
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def live_options():
    """{org_id: label} for every organisation currently in the search form."""
    s = requests.Session()
    s.headers["User-Agent"] = UA
    portal.cookie(s)
    out = parse_options(s.get(SEARCH_URL, timeout=30).text)
    if len(out) < portal.MIN_ORGS:
        raise RuntimeError(f"organisation dropdown looks wrong ({len(out)} options)")
    return out


def parse_options(html):
    """{org_id: label} from the search form's organisation dropdown."""
    out = {}
    # only the organisation list: the form has other numeric dropdowns too (product category, form of contract, ...), and searching
    # with one of their ids as an "organisation" returns every tender on the portal
    sel = BeautifulSoup(html, "html.parser").find("select", attrs={"name": "OrganName"})
    for o in (sel.find_all("option") if sel else []):
        v = (o.get("value") or "").strip()
        if v.isdigit() and v != "0":
            out[v] = o.text.strip()
    return out


def norm(name):
    return " ".join(name.split()).casefold()
