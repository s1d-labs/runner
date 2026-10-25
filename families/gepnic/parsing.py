"""Turn a tender's three portal pages into the same structured fields your September database had.

A faithful port of parse_single_tender() from the September build script (build_sqlite_and_json_dataset.py), but working on
page text instead of files, so it can run inside the scraper. Values are kept as the portal wrote them (strings, with the same
handful of integers) so the output can be compared field for field with the original. Typed columns (dates, amounts) are made
in Postgres by the views in sql/tenders.sql.
"""
import calendar
import json
import re

from bs4 import BeautifulSoup

COLUMNS = [
    "tender_id", "org_id", "org_name", "org_chain", "tender_ref_no", "tender_title", "stage", "sno", "view_link",
    "tender_type", "form_of_contract", "tender_category", "no_of_covers", "payment_mode",
    "is_multi_currency_boq", "is_multi_currency_fee", "allow_two_stage_bidding", "withdrawal_allowed",
    "general_tech_eval_allowed", "itemwise_tech_eval_allowed",
    "tender_fee", "fee_payable_to", "fee_payable_at", "tender_fee_exemption_allowed",
    "emd_amount", "emd_fee_type", "emd_percentage", "emd_exemption_allowed", "emd_payable_to", "emd_payable_at",
    "work_description", "pre_qualification", "iem_remarks", "tender_value", "product_category", "sub_category", "contract_type",
    "bid_validity_days", "period_of_work_days", "location", "pincode",
    "pre_bid_meeting_place", "pre_bid_meeting_address", "pre_bid_meeting_date", "bid_opening_place",
    "published_date", "bid_opening_date", "bid_download_start_date", "bid_download_end_date",
    "clarification_start_date", "clarification_end_date", "bid_submission_start_date", "bid_submission_end_date",
    "tia_name", "tia_address",
    "bids_count", "qualified_bids_count", "winner_bidder_name", "awarded_value", "award_date", "has_boq_chart", "has_tech_summary",
    # added October 2026: exact-label reading, the summary page's bid and award lists, stage ticks, date sanity flags
    "financial_bid_opening_date", "contract_value_aoc_total", "aoc_currency", "awarded_value_sum", "winners",
    "priced_bidders", "l1_bidder_name", "l1_bid_value", "l1_rank_raw", "l1_inferred",
    "stage_done", "last_stage_completed", "date_flags",
]

_WS = re.compile(r"\s+")


def _t(el):
    return _WS.sub(" ", el.get_text(" ", strip=True)).strip() if el is not None else ""


def norm_label(label):
    """A label as the parser compares it: case, colons, asterisks, spacing and the rupee spelling (₹, Rs, Rs., INR) do not matter."""
    s = (label or "").replace("₹", " inr ").replace("\u20b9", " inr ")
    s = re.sub(r"\bRs\.?(?=\s|$)", " inr ", s, flags=re.I)
    s = re.sub(r"[:*]", " ", s)
    return _WS.sub(" ", s).strip().lower()


def norm_section(head):
    """'Tender Fee Details, [Total Fee in ₹ * - 590.00]' -> 'tender fee details'; 'Work /Item(s)' -> 'work/item(s)'."""
    head = (head or "").split(",")[0].split("[")[0]
    return re.sub(r"\s*/\s*", "/", _WS.sub(" ", head)).strip().lower()


TABLE_SECTIONS = {"tender documents", "other important documents", "latest corrigendum list", "cover details"}   # column headers, not labels


def details_sections(details_html):
    """{section: {label: value}} from the Tender Details page, in page order. Labels are normalised (norm_label); within a section the
    first occurrence of a label wins and later duplicates are kept under '<label> #2' and so on, so nothing is dropped silently."""
    out, sec = {}, ""
    for tr in _soup(details_html).find_all("tr"):
        if tr.find("table"):
            continue
        sh = tr.find("td", class_="section_head")
        if sh is not None:
            sec = norm_section(_t(sh))
            out.setdefault(sec, {})
            continue
        cells = tr.find_all(["td", "th"], recursive=False)
        row_caption = "td_caption" in (tr.get("class") or [])
        i = 0
        while i < len(cells) - 1:
            c = cells[i]
            lab = _t(c)
            is_label = "caption" in (c.get("class") or []) or "td_caption" in (c.get("class") or []) or row_caption or c.find("b") is not None
            if is_label and 0 < len(lab) < 80:
                key, n = norm_label(lab), 2
                bucket = out.setdefault(sec, {})
                while key in bucket:
                    key, n = f"{norm_label(lab)} #{n}", n + 1
                bucket[key] = cells[i + 1].text.strip()       # the value exactly as the portal wrote it (line breaks kept)
                i += 2
            else:
                i += 1
    return out


def lookup(secs, sections, labels, default=""):
    """The value of the first of `labels` (exact, normalised) in the first of `sections` that has it; if no listed section has the page
    at all, any section that is not a table of column headers. Never a substring match."""
    want = [norm_label(x) for x in labels]
    names = [norm_section(x) for x in sections]
    for sname in names:
        for lab in want:
            if lab in secs.get(sname, {}):
                return secs[sname][lab]
    if not any(n in secs for n in names):
        for sname, kv in secs.items():
            if sname in TABLE_SECTIONS:
                continue
            for lab in want:
                if lab in kv:
                    return kv[lab]
    return default


_MONEY = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def money(raw):
    """'27,44,000.00' or 'INR 2,450,000' -> 2744000.0 / 2450000.0; anything without a number -> None."""
    m = _MONEY.search((raw or "").replace("\u00a0", " "))
    if not m:
        return None
    try:
        return float(m.group().replace(",", ""))
    except ValueError:
        return None


_WORD_RANK = {"first": 1, "ist": 1, "1st": 1, "highest": 1, "lowest": 1, "second": 2, "2nd": 2, "iind": 2, "third": 3, "3rd": 3, "iiird": 3}


def rank(raw):
    """(number, kind) for a rank label as the portal writes it. kind: 'L' (lowest wins), 'H' (highest wins), 'R' (ranked), 'rate_low',
    'rate_up', or 'other' when it is not a rank at all (e.g. 'QUAL BIDR'). (None, 'other') when nothing can be read."""
    s = _WS.sub(" ", (raw or "").strip()).lower()
    if not s:
        return None, "other"
    if "rate" in s and "low" in s:
        return 1, "rate_low"
    if "rate" in s and ("up" in s or "high" in s):
        return 1, "rate_up"
    m = re.fullmatch(r"([lhr])\s*[-.]?\s*0*(\d{1,3})", s)
    if m:
        return int(m.group(2)), m.group(1).upper()
    m = re.fullmatch(r"0*(\d{1,3})", s)
    if m:
        return int(m.group(1)), "L"
    if s in _WORD_RANK:
        return _WORD_RANK[s], "H" if s == "highest" else "L"
    return None, "other"


COMMERCIAL_KEYWORDS = ["BOQ", "Comparative", "technical", "finance", "finsummary", "techsummary", "corrigendum", "cancel"]


def _soup(html):
    return BeautifulSoup(html or "", "lxml")


def procurement_details(details_html):
    """Every label/value pair on the Tender Details page (first occurrence of a label wins)."""
    d2 = {}
    for tr in _soup(details_html).find_all("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        i = 0
        while i < len(cells) - 1:
            k = cells[i].text.strip().replace(":", "").strip()
            v = cells[i + 1].text.strip()
            if 0 < len(k) < 60 and "\n" not in k:
                if k not in d2:
                    d2[k] = v
                i += 2
            else:
                i += 1
    return d2


def _summary_title(rows):
    return _t(rows[0]) if rows else ""


def _innermost_tables(soup):
    return [t for t in soup.find_all("table") if t.find("table") is None]


def _list_table(table):
    """A titled list table (title row, then a header row starting with S.No, then data rows) -> (title, header, [row dicts]); else None."""
    rows = [r for r in table.find_all("tr") if r.find_parent("table") is table]
    if len(rows) < 2:
        return None
    hdr_i = next((i for i, r in enumerate(rows[:3]) if [_t(c).lower() for c in r.find_all(["td", "th"])][:1] in (["s.no"], ["sl.no"], ["s. no"])), None)
    if hdr_i is None:
        return None
    header = [_t(c) for c in rows[hdr_i].find_all(["td", "th"])]
    title = _t(rows[0]) if hdr_i > 0 else ""
    if not title:          # the title often sits in the row just above the table
        prev = table.find_previous("tr")
        title = _t(prev)[:80] if prev is not None else ""
    data = []
    for r in rows[hdr_i + 1:]:
        cells = [_t(c) for c in r.find_all(["td", "th"])]
        if len(cells) >= 2 and any(cells):
            data.append(dict(zip(header, cells)))
    return title, header, data


def _col(row, *names):
    keys = {norm_label(k): v for k, v in row.items()}
    for n in names:
        if norm_label(n) in keys:
            return keys[norm_label(n)]
    return ""


def bid_and_award_lists(summary_html):
    """The summary page's Financial Evaluation Bid List (bidder, value, rank) and Awarded Bids List (bidder, currency, value), as lists."""
    fin, awd, seen = [], [], set()
    for table in _innermost_tables(_soup(summary_html)):
        parsed = _list_table(table)
        if not parsed:
            continue
        title, header, data = parsed
        tl, hl = title.lower(), [norm_label(h) for h in header]
        if "rank" in hl and ("financial evaluation bid list" in tl or "finance evaluation bid list" in tl or "value" in hl) and "fin" not in seen:
            seen.add("fin")
            for r in data:
                raw_rank = _col(r, "Rank")
                num, kind = rank(raw_rank)
                fin.append({"bid_number": _col(r, "Bid Number"), "bidder": _col(r, "Bidder Name"), "value_raw": _col(r, "Value"),
                            "value": money(_col(r, "Value")), "rank_raw": raw_rank, "rank_num": num, "rank_kind": kind})
        elif "awarded bids list" in tl and "awd" not in seen:
            seen.add("awd")
            for r in data:
                awd.append({"bid_number": _col(r, "Bid Number"), "bidder": _col(r, "Bidder Name"), "currency": _col(r, "Awarded Currency"),
                            "value_raw": _col(r, "Awarded Value"), "value": money(_col(r, "Awarded Value"))})
    return fin, awd


def stage_ticks(status_html):
    """Stages on the status page's table#tabList that carry the tick image, in order: (done stages, last one done)."""
    tl = _soup(status_html).find("table", id="tabList")
    done = []
    if tl is not None:
        for r in tl.find_all("tr"):
            cells = r.find_all("td")
            if cells and r.find("img", src=re.compile(r"tick", re.I)) is not None:
                name = _t(cells[0])
                if name:
                    done.append(name)
    return done, (done[-1] if done else "")


def commercial_outcomes(tender_id, bids_list, status_html, summary_html, file_names):
    outcomes = {"tender_id": tender_id, "bids_list": bids_list, "stages": [], "technical_opening": {}, "technical_evaluation": {},
                "finance_opening": {}, "finance_evaluation": {}, "aoc": {}, "commercial_files": [], "additional_sections": {},
                "finance_bid_list": [], "awarded_list": [], "stage_done": [], "has_bids_list": False}
    outcomes["stage_done"], _ = stage_ticks(status_html)
    st = _soup(status_html).find("table", id="tabList")
    for r in (st.find_all("tr")[1:] if st is not None else []):
        cs = [_t(c) for c in r.find_all("td") if _t(c)]
        if cs:
            outcomes["stages"].append({"process_or_stage": cs[0], "status": "done" if r.find("img", src=re.compile(r"tick", re.I)) else (cs[1] if len(cs) > 1 else "")})
    soup = _soup(summary_html)
    for tbl in soup.find_all("table", class_="table_list"):
        rows = [r for r in tbl.find_all("tr") if r.find_parent("table") is tbl]
        if not rows:
            continue
        title = _summary_title(rows)
        tl = title.lower()
        if "bids list" in tl or "bid list" in tl:
            outcomes["has_bids_list"] = outcomes["has_bids_list"] or tl.startswith("bids list")
            continue                                   # lists are read by bid_and_award_lists
        if tl.startswith("organisation chain"):
            continue
        kv = {}
        for r in rows[1:]:
            cs = [_t(c) for c in r.find_all(["td", "th"], recursive=False)]
            i = 0
            while i < len(cs) - 1:
                if cs[i]:
                    kv.setdefault(cs[i].replace(":", "").strip(), cs[i + 1])
                i += 2
        target = None
        if "technical bid opening" in tl:
            target = "technical_opening"
        elif "finance bid opening" in tl or "financial bid opening" in tl:
            target = "finance_opening"
        elif tl.startswith("bid opening summary"):
            target = "finance_opening" if kv.get("Type", "").lower().startswith("fin") else "technical_opening"
        elif "technical evaluation" in tl:
            target = "technical_evaluation"
        elif "finance evaluation" in tl or "financial evaluation" in tl:
            target = "finance_evaluation"
        elif tl == "aoc" or tl.startswith("aoc ") or "award of contract" in tl:
            target = "aoc"
        if target:
            outcomes[target].update(kv)
        elif kv:
            outcomes["additional_sections"][title] = [[k, v] for k, v in kv.items()]
    outcomes["finance_bid_list"], outcomes["awarded_list"] = bid_and_award_lists(summary_html)
    outcomes["commercial_files"] = [f for f in file_names if any(kw in f for kw in COMMERCIAL_KEYWORDS)]
    return outcomes


_DATE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def date_flags(tender_id, fields):
    """Sanity flags on the portal's dates, which are kept exactly as written (never repaired): 'year_far:<field>' when the year is more
    than one year after the tender id's year (or before it), 'impossible_date:<field>' (e.g. 30-Feb), 'closes_before_publish'."""
    flags, parsed = [], {}
    id_year = int(tender_id[:4]) if (tender_id or "")[:4].isdigit() else None
    for k in ("published_date", "bid_opening_date", "bid_submission_start_date", "bid_submission_end_date", "award_date"):
        m = _DATE.search(fields.get(k) or "")
        if not m:
            continue
        d, mon, y = int(m.group(1)), _MONTHS.get(m.group(2).lower()), int(m.group(3))
        if not mon or d < 1 or d > calendar.monthrange(y, mon)[1]:
            flags.append(f"impossible_date:{k}")
            continue
        parsed[k] = (y, mon, d)
        if id_year and k != "award_date" and (y > id_year + 1 or y < id_year - 1):
            flags.append(f"year_far:{k}")
    if "published_date" in parsed and "bid_submission_end_date" in parsed and parsed["bid_submission_end_date"] < parsed["published_date"]:
        flags.append("closes_before_publish")
    return flags


QUALIFIED_STATUSES = ("accepted", "admitted", "qualified")      # a bid status starting with one of these counts as qualified


def parse_tender(tender_id, org_id, org_name, dj, status_html, details_html, summary_html, file_names):
    """dj: what the old details.json held (the scraper's details dict plus stage, sno, view_link, tender_id).

    Returns (fields, procurement_details, outcomes). `fields` has the COLUMNS above. Labels are read by exact (normalised) name within
    their page section; a label that is not on the page gives "" (or None for numbers), never a neighbouring field.
    """
    bids_list = dj.get("bids", [])
    tender_id = dj.get("tender_id") or tender_id
    tender_ref_no = dj.get("Tender Ref No") or dj.get("tender_ref_no") or ""
    tender_title = dj.get("Tender Title") or dj.get("tender_title") or ""
    org_chain = dj.get("Organisation Chain") or dj.get("org_chain") or ""
    d2 = procurement_details(details_html)
    secs = details_sections(details_html)
    BASIC, FEE, EMD, WORK, DATES, TIA = ["Basic Details"], ["Tender Fee Details"], ["EMD Fee Details"], ["Work/Item(s)", "Work Item Details"], \
        ["Critical Dates"], ["Tender Inviting Authority"]

    def g(sections, labels, default=""):
        return lookup(secs, sections, labels, default)

    def as_int(s):
        return int(s) if (s or "").isdigit() else None

    if not tender_ref_no:
        tender_ref_no = g(BASIC, ["Tender Reference Number", "Tender Ref No"])
    if not tender_title:
        tender_title = g(WORK, ["Title"]) or g(WORK, ["Work Description"])
    out = commercial_outcomes(tender_id, bids_list, status_html, summary_html, file_names)
    aoc, fin, awd = out["aoc"], out["finance_bid_list"], out["awarded_list"]
    aoc_total_raw = aoc.get("Total Contract Value") or aoc.get("Contract Value") or ""
    cur = re.match(r"\s*([A-Za-z]{3})\b", aoc_total_raw.replace("\u00a0", " "))
    winners = [w["bidder"] for w in awd if w["bidder"]] or ([aoc[k] for k in ("Contractor Name", "Selected Bidder", "Contractor") if aoc.get(k)][:1])
    awarded_sum = round(sum(w["value"] for w in awd if w["value"] is not None and (w["currency"] or "INR").upper() in ("INR", "")), 2) if awd else None
    priced = [b for b in fin if b["value"] is not None]
    l1 = [b for b in fin if b["rank_num"] == 1 and b["rank_kind"] in ("L", "H", "R", "rate_low", "rate_up")]
    inferred = False
    if not l1 and len(priced) == 1:
        l1, inferred = priced, True
    has_list = bool(bids_list) or out["has_bids_list"]
    f = {
        "tender_id": tender_id, "org_id": org_id, "org_name": org_name, "org_chain": org_chain or g(BASIC, ["Organisation Chain"]), "tender_ref_no": tender_ref_no,
        "tender_title": tender_title, "stage": dj.get("stage") or "", "sno": dj.get("sno") or "", "view_link": dj.get("view_link") or "",
        "tender_type": g(BASIC, ["Tender Type"]), "form_of_contract": g(BASIC, ["Form of contract"]), "tender_category": g(BASIC, ["Tender Category"]),
        "no_of_covers": as_int(g(BASIC, ["No. of Covers", "No of Covers"])), "payment_mode": g(BASIC, ["Payment Mode"]),
        "is_multi_currency_boq": g(BASIC, ["Is Multi Currency Allowed For BOQ"]), "is_multi_currency_fee": g(BASIC, ["Is Multi Currency Allowed For Fee"]),
        "allow_two_stage_bidding": g(BASIC, ["Allow Two Stage Bidding"]), "withdrawal_allowed": g(BASIC, ["Withdrawal Allowed"]),
        "general_tech_eval_allowed": g(BASIC, ["General Technical Evaluation Allowed"]), "itemwise_tech_eval_allowed": g(BASIC, ["ItemWise Technical Evaluation Allowed"]),
        "tender_fee": g(FEE, ["Tender Fee in ₹", "Total Fee in ₹", "Tender Fee"]), "fee_payable_to": g(FEE, ["Fee Payable To"]), "fee_payable_at": g(FEE, ["Fee Payable At"]),
        "tender_fee_exemption_allowed": g(FEE, ["Tender Fee Exemption Allowed"]),
        "emd_amount": g(EMD, ["EMD Amount in ₹", "EMD Amount"]), "emd_fee_type": g(EMD, ["EMD Fee Type"]), "emd_percentage": g(EMD, ["EMD Percentage"]),
        "emd_exemption_allowed": g(EMD, ["EMD Exemption Allowed"]), "emd_payable_to": g(EMD, ["EMD Payable To"]), "emd_payable_at": g(EMD, ["EMD Payable At"]),
        "work_description": g(WORK, ["Work Description"]) or tender_title, "pre_qualification": g(WORK, ["Pre Qualification Details"]),
        "iem_remarks": g(WORK, ["Independent External Monitor/Remarks"]), "tender_value": g(WORK, ["Tender Value in ₹", "Tender Value"]),
        "product_category": g(WORK, ["Product Category"]), "sub_category": g(WORK, ["Sub category", "Sub-category"]), "contract_type": g(WORK, ["Contract Type"]),
        "bid_validity_days": as_int(g(WORK, ["Bid Validity(Days)", "Bid Validity (Days)"])), "period_of_work_days": as_int(g(WORK, ["Period Of Work(Days)", "Period Of Work (Days)"])),
        "location": g(WORK, ["Location"]), "pincode": g(WORK, ["Pincode", "Pin Code"]),
        "pre_bid_meeting_place": g(WORK, ["Pre Bid Meeting Place"]), "pre_bid_meeting_address": g(WORK, ["Pre Bid Meeting Address"]),
        "pre_bid_meeting_date": g(WORK, ["Pre Bid Meeting Date"]), "bid_opening_place": g(WORK, ["Bid Opening Place"]),
        "published_date": g(DATES, ["Publish Date", "Published Date"]), "bid_opening_date": g(DATES, ["Bid Opening Date"]),
        "bid_download_start_date": g(DATES, ["Document Download / Sale Start Date", "Document Download Start Date", "Sale Start Date"]),
        "bid_download_end_date": g(DATES, ["Document Download / Sale End Date", "Document Download End Date", "Sale End Date"]),
        "clarification_start_date": g(DATES, ["Clarification Start Date"]), "clarification_end_date": g(DATES, ["Clarification End Date"]),
        "bid_submission_start_date": g(DATES, ["Bid Submission Start Date"]), "bid_submission_end_date": g(DATES, ["Bid Submission End Date"]),
        "tia_name": g(TIA, ["Name"]), "tia_address": g(TIA, ["Address"]),
        "bids_count": len(bids_list) if has_list else None,
        "qualified_bids_count": sum(1 for b in bids_list if str(b.get("status", "")).strip().lower().startswith(QUALIFIED_STATUSES)) if has_list else None,
        "winner_bidder_name": "; ".join(winners),
        "awarded_value": aoc_total_raw or (str(awarded_sum) if awarded_sum is not None else ""),
        "award_date": aoc.get("Contract Date") or aoc.get("Award Date") or "",
        "has_boq_chart": 1 if any("BOQ" in x or "Comparative" in x for x in out["commercial_files"]) else 0,
        "has_tech_summary": 1 if any("techsummary" in x or "technical" in x for x in out["commercial_files"]) else 0,
        "financial_bid_opening_date": g(DATES, ["Financial Bid Opening Date"]),
        "contract_value_aoc_total": money(aoc_total_raw), "aoc_currency": (cur.group(1).upper() if cur else ""),
        "awarded_value_sum": awarded_sum, "winners": winners,
        "priced_bidders": len(priced) if fin else None,
        "l1_bidder_name": "; ".join(b["bidder"] for b in l1), "l1_bid_value": l1[0]["value"] if len(l1) == 1 else None,
        "l1_rank_raw": "; ".join(b["rank_raw"] for b in l1), "l1_inferred": inferred,
        "stage_done": out["stage_done"], "last_stage_completed": out["stage_done"][-1] if out["stage_done"] else "",
        "date_flags": [],
    }
    f["date_flags"] = date_flags(tender_id, f)
    assert list(f) == COLUMNS
    return f, d2, out
