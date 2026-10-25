import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import documents  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(tid):
    return open(os.path.join(FIX, f"summary_{tid}.html"), encoding="utf-8", errors="ignore").read()


def test_corrigendum_and_summary_are_kept_and_certificate_icons_dropped():
    docs = documents.extract_documents(load("2018_NTA_366797_1"))
    assert [(d["name"], d["kind"]) for d in docs] == [("Cor.pdf", "document"), ("techsummary_385179.pdf", "evaluation_summary")]


def test_stated_size_is_read_from_the_document_row():
    docs = {d["name"]: d for d in documents.extract_documents(load("2018_NTA_366797_1"))}
    assert abs(docs["techsummary_385179.pdf"]["declared_bytes"] - 606.33 * 1024) < 2          # the real file is 620,886 bytes


def test_boq_chart_is_found_and_bidder_number_links_are_ignored():
    docs = documents.extract_documents(load("2021_NTA_628462_1"))
    kinds = {d["name"]: d["kind"] for d in docs}
    assert kinds == {"techsummary_661270.pdf": "evaluation_summary", "BOQ_Comparative_Chart": "boq_comparative_chart"}


def test_technical_and_financial_summaries_both_found():
    names = [d["name"] for d in documents.extract_documents(load("2019_NTA_454225_1"))]
    assert names == ["techsummary_475760.pdf", "finsummary_475760.pdf"]


def test_no_link_is_a_digital_signature_page():
    for p in glob.glob(os.path.join(FIX, "summary_*.html")):
        for d in documents.extract_documents(open(p, encoding="utf-8", errors="ignore").read()):
            assert not d["name"].startswith("doc_") and d["name"]


def test_sniff_identifies_real_content_not_file_names():
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<x/>")
    assert documents.sniff(b"%PDF-1.4\n...") == "pdf"
    assert documents.sniff(buf.getvalue()) == "xlsx"
    assert documents.sniff(b"<html><body>Document Digital Signature</body></html>") == "html"
    assert documents.sniff(b"\x00\x01\x02") == "unknown"


def test_verify_rejects_html_posing_as_a_pdf_and_flags_a_wrong_size():
    doc = {"name": "doc_1.pdf", "declared_bytes": None}
    bad = documents.verify_download(doc, b"<html>Document Digital Signature</html>")
    assert bad["ok"] is False and "html" in bad["reason"]
    pdf = b"%PDF-1.4 " + b"x" * 10000
    good = documents.verify_download({"name": "techsummary_1.pdf", "declared_bytes": len(pdf)}, pdf)
    assert good["ok"] and good["size_ok"] and good["filename"] == "techsummary_1.pdf" and len(good["sha256"]) == 64
    off = documents.verify_download({"name": "x.pdf", "declared_bytes": 500000}, pdf)
    assert off["ok"] and off["size_ok"] is False


def test_an_excel_chart_named_pdf_is_saved_with_its_real_extension():
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<x/>")
    v = documents.verify_download({"name": "BOQ_Comparative_Chart", "declared_bytes": None}, buf.getvalue())
    assert v["ok"] and v["content_type"] == "xlsx" and v["filename"] == "BOQ_Comparative_Chart.xlsx"


def test_attach_documents_replaces_the_old_list_and_keeps_no_dead_urls():
    details = {"Tender ID": "x", "pdf_documents": [{"filename": "x_doc_1.pdf", "url": "https://eprocure.gov.in/?sp=dead"}]}
    out = documents.attach_documents(details, load("2018_NTA_366797_1"))
    assert "pdf_documents" not in out
    assert [d["name"] for d in out["documents"]] == ["Cor.pdf", "techsummary_385179.pdf"]
    assert all("url" not in d for d in out["documents"])           # session-bound URLs are useless once the session ends
    assert out["Tender ID"] == "x"


def test_paths_are_safe_and_organised_by_year_org_tender():
    assert documents.tender_prefix("2018_NTA_366797_1", 684) == "2018/684/2018_NTA_366797_1"
    assert documents.tender_prefix("weird id", "a b") == "unknown/a_b/weird_id"
    assert documents.safe_part("Corrigendum 3 (final).pdf") == "Corrigendum_3_final_.pdf"
    assert documents.safe_part("../../etc/passwd") == "etc_passwd" and documents.safe_part("") == "file"


def test_pages_bundle_round_trips_all_three_pages():
    import gzip, json
    b = documents.pages_bundle("<s/>", "<d>é</d>", "<m/>")
    assert json.loads(gzip.decompress(b)) == {"status": "<s/>", "details": "<d>é</d>", "summary": "<m/>"}


class FakeSession:
    def __init__(self, bodies):
        self.bodies = bodies

    def get(self, url, timeout=0):
        v = self.bodies[url]
        if isinstance(v, Exception):
            raise v
        final_url = "https://eprocure.gov.in/eprocure/app?page=CommonErrorPage&service=page" if b"expired" in v else url

        class R:
            content = v
        R.url = final_url
        return R


def test_download_documents_separates_good_files_portal_refusals_and_errors():
    docs = [{"name": "a.pdf", "url": "u1", "declared_bytes": None}, {"name": "b.pdf", "url": "u2", "declared_bytes": None},
            {"name": "c.pdf", "url": "u3", "declared_bytes": None}]
    refusal = b"<html><body>Your session in the client area has expired.</body></html>"
    out = documents.download_documents(FakeSession({"u1": b"%PDF-1.4 " + b"x" * 50, "u2": refusal, "u3": TimeoutError("slow")}), docs)
    (d1, c1, v1, e1), (d2, c2, v2, e2), (d3, c3, v3, e3) = out
    assert v1["ok"] and c1.startswith(b"%PDF") and e1 is None
    assert v2["ok"] is False and v2["reason"] == documents.PORTAL_ERROR and e2 is None   # refused by the portal: a verdict, not an exception
    assert v3 is None and c3 is None and "TimeoutError" in e3


def test_parallel_downloads_give_the_same_answers_in_the_same_order():
    import time as _t
    refusal = b"<html><body>Your session in the client area has expired.</body></html>"
    bodies = {f"u{i}": (refusal if i % 3 == 0 else b"%PDF-1.4 " + bytes([65 + i]) * 40) for i in range(9)}
    docs = [{"name": f"{i}.pdf", "url": f"u{i}", "declared_bytes": None} for i in range(9)]

    class Slow(FakeSession):
        def get(self, url, timeout=0):
            _t.sleep(0.05 * (9 - int(url[1:])) / 9)        # later documents finish first
            return super().get(url, timeout)

    seq = documents.download_documents(Slow(bodies), docs, parallel=1)
    par = documents.download_documents(Slow(bodies), docs, parallel=4)
    assert [(d["name"], c, v["ok"], e) for d, c, v, e in seq] == [(d["name"], c, v["ok"], e) for d, c, v, e in par]


class RecordingHub:
    def __init__(self):
        self.failures, self.cleared = [], []

    def failure(self, kind, key, reason="", **detail):
        self.failures.append((kind, key, reason, detail))

    def clear_failure(self, kind, key):
        self.cleared.append((kind, key))


def test_document_outcomes_go_to_the_failures_list_and_successes_clear_old_failures():
    import crawl
    hub = RecordingHub()
    entries = [{"name": "ok.pdf", "kind": "document", "declared_bytes": 10, "status": "stored"},
               {"name": "bad.pdf", "kind": "evaluation_summary", "declared_bytes": 99, "status": "refused", "reason": documents.PORTAL_ERROR},
               {"name": "err.pdf", "kind": "document", "declared_bytes": None, "status": "failed", "reason": "TimeoutError: slow"},
               {"name": "skip.pdf", "kind": "document", "declared_bytes": None, "status": "not_stored", "reason": "not requested"}]
    crawl.report_document_outcomes(hub, "2019_X_1", "223", "DRDO", ["01/01/2019", "31/12/2019"], entries)
    assert hub.cleared == [("document", "2019_X_1/ok.pdf")]
    assert [(k, key) for k, key, _, _ in hub.failures] == [("document", "2019_X_1/bad.pdf"), ("document", "2019_X_1/err.pdf")]
    kind, key, reason, detail = hub.failures[0]
    assert reason == documents.PORTAL_ERROR and detail["org_name"] == "DRDO" and detail["window"] == ["01/01/2019", "31/12/2019"] and detail["declared_bytes"] == 99
    assert len(documents.PORTAL_ERROR) < 110                                   # fits the dashboard's reason column without being cut


STATUS_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def test_a_cancelled_tenders_status_page_yields_its_notice_and_not_the_signature_icon():
    html = open(os.path.join(STATUS_FIX, "status_cancelled_2024_DOP_809543_1.html"), encoding="utf-8", errors="ignore").read()
    docs = documents.extract_status_documents(html)
    assert [(d["name"], d["kind"]) for d in docs] == [("Tender Cancelled corrigendum document", "status_document")]


def test_a_normal_status_page_has_no_status_documents():
    html = open(os.path.join(STATUS_FIX, "pages", "2021_NTA_628462_1", "status.html"), encoding="utf-8", errors="ignore").read()
    assert documents.extract_status_documents(html) == []


def test_details_link_is_the_exact_view_more_details_link_even_when_extra_links_follow():
    import scrape_organization as so
    cancelled = open(os.path.join(STATUS_FIX, "status_cancelled_2024_DOP_809543_1.html"), encoding="utf-8", errors="ignore").read()
    p1, p2 = so.find_detail_links(cancelled)
    assert "component=%24DirectLink&" in p1 and "component=%24DirectLink_0&" in p2           # not _1 (View Document) or _2 (signature)
    normal = open(os.path.join(STATUS_FIX, "pages", "2021_NTA_628462_1", "status.html"), encoding="utf-8", errors="ignore").read()
    n1, n2 = so.find_detail_links(normal)
    assert "component=%24DirectLink&" in n1 and "component=%24DirectLink_0&" in n2
    assert so.find_detail_links("<html></html>") == (None, None)
