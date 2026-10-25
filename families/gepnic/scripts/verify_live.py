#!/usr/bin/env python3
"""Download the documents of a few real tenders and check that each one is what it claims to be.

Per document: the bytes' real type (PDF / Excel / HTML error page), whether the size matches what the portal stated, and for
PDFs whether the text carries the tender id (scanned PDFs have no text and are reported as such, not as wrong).
usage: verify_live.py samples.json   # [{"tender_id","view_link","org_id","from","to"}]
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "captcha"))

import documents  # noqa: E402
import scrape_organization as so  # noqa: E402
from pypdf import PdfReader  # noqa: E402


def pdf_check(content, tender_id):
    import io
    try:
        r = PdfReader(io.BytesIO(content))
        txt = " ".join((p.extract_text() or "") for p in r.pages[:3])
    except Exception as e:
        return "unreadable PDF: " + str(e)[:40]
    if not txt.strip():
        return f"{len(r.pages)}p scanned/image (no text to check)"
    return f"{len(r.pages)}p, tender id in text" if tender_id in re.sub(r"\s+", "", txt) else f"{len(r.pages)}p, tender id NOT in text: {txt.strip()[:50]!r}"


def main():
    rows = []
    for s in json.load(open(sys.argv[1])):
        try:
            _, h0, h1, h2 = so.fetch_tender_details(s["view_link"], s["tender_id"], org_id=s["org_id"], output_dir="/tmp", from_date=s["from"], to_date=s["to"])
        except Exception as e:
            rows.append((s["tender_id"], "-", "FETCH FAILED", str(e)[:60])); continue
        docs = documents.extract_documents(h2)
        if not docs:
            rows.append((s["tender_id"], "-", "no documents on page", "")); continue
        sess = so.thread_local.session
        for d in docs:
            try:
                r = sess.get(d["url"], timeout=40)
                v = documents.verify_download(d, r.content)
                note = pdf_check(r.content, s["tender_id"]) if v["content_type"] == "pdf" else (v["reason"] or v["content_type"])
                size = f"{v['bytes']}B vs {v['declared_bytes']}B " + ("ok" if v["size_ok"] else ("n/a" if v["size_ok"] is None else "MISMATCH"))
                rows.append((s["tender_id"], d["name"][:30], ("OK " if v["ok"] else "BAD ") + v["content_type"], f"{size}; {note}"))
            except Exception as e:
                rows.append((s["tender_id"], d["name"][:30], "DOWNLOAD FAILED", str(e)[:60]))
    for r in rows:
        print(f"{r[0]:22s} {r[1]:31s} {r[2]:18s} {r[3]}")
    ok = sum(1 for r in rows if r[2].startswith("OK")); bad = [r for r in rows if not r[2].startswith("OK")]
    print(f"\n{ok} documents verified OK, {len(bad)} not OK of {len(rows)} checked")


if __name__ == "__main__":
    main()
