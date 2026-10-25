"""The tender-details page sections the September parser flattens into one key/value dump.

The 'Tender Documents' block lists the tender's own files (NIT notices, the BOQ) as plain text: number, name, description, size. The
portal's download link for them sits inside an HTML comment and is switched off for public visitors, so there is nothing to download;
the rows are all there is. This parses each table into a proper list, from the page HTML we already save.
"""
import re

from bs4 import BeautifulSoup

_NUM = re.compile(r"[-+]?\d[\d,]*\.?\d*")


def _text(td):
    return re.sub(r"\s+", " ", td.get_text(" ", strip=True)).strip()


def _size(s):
    m = _NUM.search(s or "")
    try:
        return float(m.group().replace(",", "")) if m else None
    except ValueError:
        return None


def _data_rows(table):
    """The table's own body rows (not rows of tables nested inside it), as lists of cell texts. Header rows (captions) are skipped."""
    out = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        tds = tr.find_all("td")
        if not tds or any("caption" in (td.get("class") or []) for td in tds) or "caption" in (tr.get("class") or []) or "td_caption" in (tr.get("class") or []):
            continue
        if tr.find("table"):
            continue
        out.append([_text(td) for td in tds])
    return out


def _headers(table):
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        cells = [_text(td) for td in tr.find_all(["td", "th"]) if "caption" in (td.get("class") or []) or "caption" in (tr.get("class") or []) or "td_caption" in (tr.get("class") or [])]
        if cells:
            return [c.lower() for c in cells]
    return []


def parse_sections(details_html):
    """Returns {"nit_documents", "work_item_documents", "covers", "payment_instruments", "corrigenda", "corrigendum_history_link", "cover_count"}."""
    soup = BeautifulSoup(details_html or "", "html.parser")
    out = {"nit_documents": [], "work_item_documents": [], "covers": [], "payment_instruments": [], "corrigenda": [],
           "corrigendum_history_link": False, "cover_count": None}
    for table in soup.find_all("table"):
        hdr = _headers(table)
        if not hdr:
            continue
        rows = _data_rows(table)
        if "document name" in hdr and "document type" in hdr:                       # Work Item Documents
            for r in rows:
                if len(r) >= 5:
                    out["work_item_documents"].append({"no": r[0], "type": r[1], "name": r[2], "description": r[3], "size_kb": _size(r[4])})
        elif "document name" in hdr and "s.no" in hdr:                               # NIT Document
            for r in rows:
                if len(r) >= 4:
                    out["nit_documents"].append({"no": r[0], "name": r[1], "description": r[2], "size_kb": _size(r[3])})
        elif hdr[:1] == ["cover no"]:
            no = cover = ""
            for r in rows:                                  # a cover with several documents names itself only on its first row
                if len(r) >= 4:
                    no, cover = r[0] or no, r[1] or cover
                    out["covers"].append({"no": no, "cover": cover, "document_type": r[2], "description": r[3]})
        elif "instrument type" in hdr:
            label = ""
            holder = table.find_parent("td")
            cap = holder.find_previous_sibling("td") if holder else None
            if cap is not None:
                label = _text(cap)
            if not label and table.get("id"):
                label = "Online" if table["id"].lower().startswith("online") else "Offline" if table["id"].lower().startswith("offline") else ""
            for r in rows:
                if len(r) >= 2:
                    out["payment_instruments"].append({"mode": label, "type": r[1]})
        elif table.get("id") == "corrigendumDocumenttable" or "corrigendum title" in hdr:
            for r in rows:
                if len(r) >= 3:
                    out["corrigenda"].append({"no": r[0], "title": r[1], "type": r[2]})
    for a in soup.find_all("a"):
        if (a.get("title") or "").strip().lower() == "view corrigendum history":
            out["corrigendum_history_link"] = True
    head = soup.find("td", class_="section_head", string=re.compile(r"Cover Details"))
    if head:
        m = re.search(r"No\.?\s*Of\s*Covers\s*-\s*(\d+)", head.get_text(), re.I)
        out["cover_count"] = int(m.group(1)) if m else None
    return out
