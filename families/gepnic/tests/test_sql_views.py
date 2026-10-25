"""Runs sql/tenders.sql against a real Postgres (needs pgserver and psycopg; skipped where they are not installed)."""
import json
import os

import pytest

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "pages", "2023_NTA_745082_1", "golden_from_september_parser.json")


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    server = pgserver.get_server(str(tmp_path_factory.mktemp("pg")))
    conn = psycopg.connect(server.get_uri(), autocommit=True)
    conn.execute("CREATE TABLE records (project text, record_id text, data jsonb, scraped_at timestamptz DEFAULT now())")
    conn.execute(open(os.path.join(ROOT, "sql", "tenders.sql")).read())
    yield conn
    conn.close()


def insert(conn, rid, fields, bids=(), docs=()):
    data = {"org_id": "5", "org_name": "Org", "window": ["01/03/2023", "31/03/2023"],
            "details": {"fields": fields, "bids": list(bids), "documents": list(docs), "pages_key": "p/k", "procurement_details": {"a": "b"}}}
    conn.execute("INSERT INTO records (project, record_id, data) VALUES ('eprocure-tenders', %s, %s::jsonb)", (rid, json.dumps(data)))


def test_dates_amounts_and_counts_become_real_types_and_junk_becomes_null(pg):
    gold = json.load(open(FIX))["fields"]
    f = {**gold, "emd_amount": "65,870", "tender_value": "NA", "tender_fee": "1,200.50", "published_date": "28-May-2025 06:30 PM",
         "bid_submission_end_date": "not a date", "award_date": "15-Jun-2025", "no_of_covers": 2, "bids_count": 3}
    insert(pg, "T1", f)
    r = pg.execute("select emd_amount, tender_value, tender_fee, published_at, bid_submission_end_at, awarded_at, no_of_covers, bids_count, "
                   "pg_typeof(emd_amount)::text, pg_typeof(published_at)::text from eprocure.tenders where tender_id='T1'").fetchone()
    assert float(r[0]) == 65870 and r[1] is None and float(r[2]) == 1200.50
    assert str(r[3]) == "2025-05-28 18:30:00" and r[4] is None and str(r[5]) == "2025-06-15 00:00:00"
    assert (r[6], r[7], r[8], r[9]) == (2, 3, "numeric", "timestamp without time zone")


def test_text_columns_and_window_and_storage_key_come_through(pg):
    gold = json.load(open(FIX))["fields"]
    insert(pg, "T2", gold)
    r = pg.execute("select tender_type, tender_category, location, window_from, pages_key, procurement_details->>'a' from eprocure.tenders where tender_id='T2'").fetchone()
    assert r == (gold["tender_type"], gold["tender_category"], gold["location"], "01/03/2023", "p/k", "b")


def test_bids_and_documents_views_unnest_the_lists(pg):
    gold = json.load(open(FIX))["fields"]
    insert(pg, "T3", gold, bids=[{"sno": "1", "bid_number": "9", "bidder_name": "Acme", "submitted_date": "06-Jun-2024 01:29 PM", "status": "Accepted", "remarks": "", "status_updated_on": "19-Jun-2024 03:54 PM"}],
           docs=[{"name": "a.pdf", "kind": "document", "status": "stored", "declared_bytes": 10, "bytes": 12, "sha256": "x", "key": "k1"},
                 {"name": "b.pdf", "kind": "document", "status": "refused", "reason": "portal"}])
    assert pg.execute("select bidder_name, status, submitted_at from eprocure.tender_bids where tender_id='T3'").fetchone()[:2] == ("Acme", "Accepted")
    docs = pg.execute("select name, status, bytes, storage_key from eprocure.tender_documents where tender_id='T3' order by name").fetchall()
    assert docs == [("a.pdf", "stored", 12, "k1"), ("b.pdf", "refused", None, None)]


def test_a_record_without_parsed_fields_still_appears_with_nulls(pg):
    pg.execute("INSERT INTO records (project, record_id, data) VALUES ('eprocure-tenders', 'T4', %s::jsonb)", (json.dumps({"org_id": "1", "details": {}}),))
    assert pg.execute("select tender_type, published_at from eprocure.tenders where tender_id='T4'").fetchone() == (None, None)
