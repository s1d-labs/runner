import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backfill  # noqa: E402


class FakeHub:
    def __init__(self, todo):
        self.todo, self.patches, self.queue = todo, [], {}

    def backfill_todo(self, target, org_id, year, limit=3000):
        assert target == "eprocure-tenders"
        return self.todo[:limit]

    def read_urls(self, target, keys):
        return {k: "https://example.invalid/" + k for k in keys}

    def patch_records(self, target, patches):
        self.patches += patches


def item():
    return {"key": "2019/5", "payload": {"org_id": "5", "year": "2019"}}


def test_every_record_is_fetched_parsed_and_patched_in_chunks():
    todo = [{"record_id": f"R{i}", "pages_key": f"k{i}"} for i in range(250)]
    hub = FakeHub(todo)
    status, err, patched = backfill.process_item(hub, item(), fetch=lambda rid, key, url: (rid, {"nit_documents": [], "from": url[-3:]}))
    assert (status, err, patched) == ("done", "", 250) and len(hub.patches) == 250
    assert hub.patches[0][0] == "R0" and hub.patches[0][1]["from"] == "/k0"


def test_unreadable_pages_fail_the_item_but_the_rest_is_still_patched():
    todo = [{"record_id": f"R{i}", "pages_key": f"k{i}"} for i in range(10)]
    hub = FakeHub(todo)
    status, err, patched = backfill.process_item(hub, item(), fetch=lambda rid, key, url: (rid, None if rid in ("R3", "R7") else {"x": 1}))
    assert status == "failed" and "2 of 10" in err and patched == 8 and len(hub.patches) == 8


def test_nothing_to_do_is_done():
    assert backfill.process_item(FakeHub([]), item(), fetch=None) == ("done", "", 0)


def test_claim_next_waits_then_stops_when_everything_is_done():
    import time

    class H:
        def __init__(self):
            self.n = 0
            self.queue = {}

        def claim(self, limit=1, steal=False, retry_failed=False):
            self.n += 1
            if self.n == 1:
                self.queue = {"pending": 0, "claimed": 2, "retry": 0}
                return []
            if self.n == 2:
                return [{"key": "k"}]
            self.queue = {"pending": 0, "claimed": 0, "retry": 0}
            return []
    h, sleeps = H(), []
    assert backfill.claim_next(h, time.time() + 600, sleep=sleeps.append) == [{"key": "k"}] and len(sleeps) == 1
    assert backfill.claim_next(h, time.time() + 600, sleep=sleeps.append) == []


def test_a_big_org_year_goes_back_to_the_queue_so_other_runners_share_it():
    todo = [{"record_id": f"R{i}", "pages_key": f"k{i}"} for i in range(backfill.TODO_LIMIT + 50)]
    hub = FakeHub(todo)
    status, err, patched = backfill.process_item(hub, item(), fetch=lambda rid, key, url: (rid, {"x": 1}))
    assert status == "pending" and patched == backfill.TODO_LIMIT and "more left" in err         # not done: the hub still lists 50 without sections
