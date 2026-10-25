import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import parsing  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "pages")
TENDERS = sorted(os.path.basename(p) for p in glob.glob(os.path.join(FIX, "20*")))


def load(tid):
    rd = lambda n: open(os.path.join(FIX, tid, n), encoding="utf-8", errors="ignore").read()
    return rd("status.html"), rd("details.html"), rd("summary.html"), json.load(open(os.path.join(FIX, tid, "details.json"))), \
        json.load(open(os.path.join(FIX, tid, "golden_from_september_parser.json")))


def test_fixtures_exist():
    assert len(TENDERS) == 3


# Fields the October 2026 parser deliberately reads differently from the September one (exact labels, real stage table, NULL when
# there is no bid list); everything else must still match the September output exactly.
FIXED = {"tia_name", "tia_address", "bids_count", "qualified_bids_count", "winner_bidder_name", "awarded_value", "award_date"}
REDONE_OUTCOMES = {"stages", "technical_evaluation", "technical_opening", "finance_opening", "finance_evaluation", "aoc", "additional_sections"}


def test_every_unchanged_field_still_matches_what_the_september_parser_produced():
    for tid in TENDERS:
        status, details, summary, dj, gold = load(tid)
        fields, d2, out = parsing.parse_tender(tid, gold["fields"]["org_id"], gold["fields"]["org_name"], dj, status, details, summary, gold["files"])
        assert {k: v for k, v in fields.items() if k in gold["fields"] and k not in FIXED} == {k: v for k, v in gold["fields"].items() if k not in FIXED}, tid
        assert d2 == gold["procurement_details"], tid
        assert {k: v for k, v in out.items() if k in gold["outcomes_without_files"] and k not in REDONE_OUTCOMES} == \
            {k: v for k, v in gold["outcomes_without_files"].items() if k not in REDONE_OUTCOMES}, tid
        assert sorted(out["commercial_files"]) == sorted(gold["commercial_files"]), tid


def test_labels_are_read_exactly_in_their_section():
    want = {"2018_NTA_366797_1": "Director,Admin", "2021_NTA_628462_1": "Joint Director", "2023_NTA_745082_1": "Joint Director (Procurement), NTA"}
    for tid, name in want.items():
        status, details, summary, dj, gold = load(tid)
        f, _, out = parsing.parse_tender(tid, "x", "x", dj, status, details, summary, [])
        assert f["tia_name"] == name, tid                                  # the September parser said "Description" for all three
        assert "Block C" in f["tia_address"] or "NSIC" in f["tia_address"], tid
        assert out["stages"][0]["process_or_stage"] != "Tender Title :"    # the real stage table, not the header table


def test_the_fields_are_actually_filled_in_not_just_equal_empties():
    status, details, summary, dj, gold = load("2023_NTA_745082_1")
    f, _, _ = parsing.parse_tender("2023_NTA_745082_1", "", "NTA", dj, status, details, summary, [])
    assert f["tender_type"] == "Limited" and f["tender_category"] == "Services" and f["location"] == "Okhla, New Delhi"
    assert f["published_date"] == "16-Mar-2023 11:00 AM" and f["bid_submission_end_date"] == "22-Mar-2023 09:00 AM"
    assert list(f) == parsing.COLUMNS and len(parsing.COLUMNS) == 75


def test_a_missing_page_gives_empty_fields_not_an_error():
    f, d2, out = parsing.parse_tender("T", "1", "Org", {"Tender Title": "x", "bids": []}, "", "", "", [])
    assert f["tender_title"] == "x" and f["tender_type"] == "" and f["bids_count"] is None and d2 == {} and out["stages"] == []
    assert f["winners"] == [] and f["l1_bidder_name"] == "" and f["date_flags"] == [] and f["contract_value_aoc_total"] is None


def test_rank_labels():
    r = parsing.rank
    assert [r(x) for x in ("L1", "L-1", "l 1", "L01", "1", "01", "H1", "R2", "First", "Ist", "2nd", "Highest")] == \
        [(1, "L"), (1, "L"), (1, "L"), (1, "L"), (1, "L"), (1, "L"), (1, "H"), (2, "R"), (1, "L"), (1, "L"), (2, "L"), (1, "H")]
    assert r("Rate Low") == (1, "rate_low") and r("rate up") == (1, "rate_up")
    assert r("QUAL BIDR") == (None, "other") and r("") == (None, "other")


def test_money():
    assert parsing.money("27,44,000.00") == 2744000.0 and parsing.money("INR\u00a02,450,000") == 2450000.0
    assert parsing.money("") is None and parsing.money("NA") is None


def bid_page(rows, awarded):
    fin = "".join(f"<tr><td>{i}</td><td>{100 + i}</td><td>{b}</td><td>{v}</td><td>{rk}</td></tr>" for i, (b, v, rk) in enumerate(rows, 1))
    aw = "".join(f"<tr><td>{i}</td><td>{100 + i}</td><td>{b}</td><td>INR</td><td>{v}</td></tr>" for i, (b, v) in enumerate(awarded, 1))
    return f"""<table class="table_list"><tr><td>Finance Evaluation Summary Details</td></tr><tr><td>
      <table class="table_list"><tr><td>Financial Evaluation Bid List</td></tr>
      <tr><td>S.No</td><td>Bid Number</td><td>Bidder Name</td><td>Value</td><td>Rank</td></tr>{fin}</table></td></tr></table>
      <table class="table_list"><tr><td>Awarded Bids List</td></tr>
      <tr><td>S.No</td><td>Bid Number</td><td>Bidder Name</td><td>Awarded Currency</td><td>Awarded Value</td></tr>{aw}</table>
      <table class="table_list"><tr><td>AOC</td></tr><tr><td>Contract Date :</td><td>20-Jul-2026</td></tr>
      <tr><td>Total Contract Value :</td><td>INR\u00a02,700,000</td></tr></table>"""


def test_bid_and_award_lists_give_l1_and_winners():
    page = bid_page([("Voith; Turbo", "27,44,000.00", "L-1"), ("Other Co", "29,00,000", "L2")], [("Voith; Turbo", "27,00,000.00")])
    f, _, out = parsing.parse_tender("2025_NALCO_1_1", "1", "x", {"tender_id": "2025_NALCO_1_1"}, "", "", page, [])
    assert f["winners"] == ["Voith; Turbo"]                          # a real list: a name with ';' is not split
    assert f["l1_bidder_name"] == "Voith; Turbo" and f["l1_bid_value"] == 2744000.0 and f["l1_rank_raw"] == "L-1" and not f["l1_inferred"]
    assert f["contract_value_aoc_total"] == 2700000.0 and f["aoc_currency"] == "INR" and f["awarded_value_sum"] == 2700000.0
    assert f["award_date"] == "20-Jul-2026" and f["priced_bidders"] == 2 and len(out["finance_bid_list"]) == 2


def test_a_single_priced_bidder_is_l1_but_marked_inferred():
    f, _, _ = parsing.parse_tender("2025_X_1_1", "1", "x", {}, "", "", bid_page([("Solo Ltd", "5,000", "QUAL BIDR")], []), [])
    assert f["l1_bidder_name"] == "Solo Ltd" and f["l1_inferred"] is True


def test_stage_ticks_and_date_flags():
    status = """<table id="tabList"><tr><td>Stages</td><td>Status</td></tr><tr><td>Bid Opening</td><td><img src="images/tick.png"></td></tr>
      <tr><td>Technical Evaluation</td><td><img src="images/tick.png"></td></tr><tr><td>AOC</td><td></td></tr></table>"""
    done, last = parsing.stage_ticks(status)
    assert done == ["Bid Opening", "Technical Evaluation"] and last == "Technical Evaluation"
    flags = parsing.date_flags("2019_X_1_1", {"published_date": "10-Jan-2026 10:00 AM", "bid_submission_end_date": "29-Feb-2019", "bid_opening_date": "30-Feb-2019"})
    assert "year_far:published_date" in flags and "impossible_date:bid_opening_date" in flags
    assert parsing.date_flags("2024_X_1_1", {"published_date": "10-Mar-2024", "bid_submission_end_date": "01-Mar-2024"}) == ["closes_before_publish"]
