import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sections  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(name):
    return open(os.path.join(FIX, name), encoding="utf-8").read()


def test_tender_documents_are_separate_rows_with_names_descriptions_and_sizes():
    out = sections.parse_sections(load("details_2025_DRDO_tendernotices.html"))
    assert [d["name"] for d in out["nit_documents"]] == [f"Tendernotice_{i}.pdf" for i in range(1, 5)]       # the September parser kept only the last
    assert [d["description"] for d in out["nit_documents"]] == ["itt", "undertaking", "Enquiry", "NIT"]
    assert [d["size_kb"] for d in out["nit_documents"]] == [729.59, 270.92, 2765.55, 651.09]
    assert out["work_item_documents"] == [{"no": "1", "type": "BOQ", "name": "BOQ_920119.xls", "description": "boq", "size_kb": 2901.0}]


def test_covers_payment_instruments_and_corrigenda():
    out = sections.parse_sections(load("details_2025_DRDO_tendernotices.html"))
    assert [(c["no"], c["cover"], c["document_type"]) for c in out["covers"]] == [("1", "Fee/PreQual/Technical", ".pdf"), ("2", "Finance", ".xls")]
    assert out["cover_count"] == 2
    assert [(p["mode"], p["type"]) for p in out["payment_instruments"]] == [("Offline", "Demand Draft"), ("Offline", "FDR"), ("Offline", "Bankers Cheque")]
    assert out["corrigenda"] == [{"no": "1", "title": "Corrigendum no.1", "type": "Retender"}] and out["corrigendum_history_link"] is True


def test_a_cover_with_several_documents_names_itself_once_and_the_rest_inherit_it():
    out = sections.parse_sections(load("details_2011_DIT_7_multi_document_cover.html"))
    assert out["cover_count"] == 2 and len(out["covers"]) == 5
    assert {c["no"] for c in out["covers"]} == {"1", "2"}                                   # rows without their own number belong to the cover above
    assert [c["cover"] for c in out["covers"]] == ["Fee/PreQual/Technical"] * 3 + ["Finance"] * 2
    assert all(d["size_kb"] is not None for d in out["nit_documents"] + out["work_item_documents"])


def test_a_page_without_the_sections_gives_empty_lists_not_an_error():
    out = sections.parse_sections("<html><body>Your session in the client area has expired.</body></html>")
    assert out["nit_documents"] == [] and out["work_item_documents"] == [] and out["covers"] == [] and out["cover_count"] is None
    assert sections.parse_sections("")["corrigenda"] == []
