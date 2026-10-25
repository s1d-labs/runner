"""Many GePNIC portals: names and URLs per portal, storage paths, seeding from the dropdown, and spreading processes over portals."""
import importlib
import os

import pytest


@pytest.fixture
def as_portal(monkeypatch):
    """Reload the portal-aware modules as if PORTAL were set, and restore eprocure afterwards."""
    import documents
    import portal

    def use(name):
        monkeypatch.setenv("PORTAL", name)
        importlib.reload(portal)
        importlib.reload(documents)
        return portal, documents
    yield use
    monkeypatch.delenv("PORTAL", raising=False)
    importlib.reload(portal)
    importlib.reload(documents)


def test_eprocure_keeps_its_names_and_paths(as_portal):
    p, d = as_portal("eprocure.gov.in")
    assert (p.TENDERS, p.PLAN, p.PROBE, p.LEGACY) == ("eprocure-tenders", "eprocure-plan", "eprocure", True)
    assert p.SEARCH_URL == "https://eprocure.gov.in/eprocure/app?page=WebTenderStatusLists&service=page"
    assert d.tender_prefix("2018_NTA_366797_1", "204") == "2018/204/2018_NTA_366797_1"


def test_other_portals_get_their_own_projects_urls_and_org_code_paths(as_portal):
    p, d = as_portal("wbtenders.gov.in")
    assert (p.TENDERS, p.PLAN, p.PROBE, p.LEGACY) == ("gepnic-wbtenders", "gepnic-wbtenders-plan", "gepnic:wbtenders.gov.in", False)
    assert p.APP == "https://wbtenders.gov.in/nicgep/app" and p.APP_PATH == "/nicgep/app" and p.ORIGIN == "https://wbtenders.gov.in"
    assert p.url("/nicgep/app?component=x&amp;page=y") == "https://wbtenders.gov.in/nicgep/app?component=x&page=y"
    assert d.BASE == "https://wbtenders.gov.in"
    assert d.tender_prefix("2025_ZPD_901234_1", "77") == "2025/ZPD/2025_ZPD_901234_1"      # org code, not the shifting dropdown id
    assert as_portal("eproc.rajasthan.gov.in")[0].TENDERS == "gepnic-eproc-rajasthan"
    assert as_portal("eprocure.gov.in/epublish")[0].APP == "https://eprocure.gov.in/epublish/app"


def test_unknown_and_new_interface_portals_are_refused(monkeypatch):
    import portal
    for name in ("example.gov.in", "manipurtenders.gov.in"):
        monkeypatch.setenv("PORTAL", name)
        with pytest.raises(SystemExit):
            importlib.reload(portal)
    monkeypatch.delenv("PORTAL")
    importlib.reload(portal)


def test_bootstrap_skips_empty_orgs_and_counts_big_ones_by_year():
    import plan
    live = {"1": "Zilla Parishad", "2": "Tiny Board", "3": "Gone Quiet", "4": "Brand New Office"}
    june = {"Zilla Parishad": 1_292_073, "Tiny Board": 40, "Gone Quiet": 0}
    items, summary = plan.bootstrap_items(live, june, 2005, 2027, 1200)
    keys = [i["key"] for i in items]
    assert sum(k.startswith("count:1:") for k in keys) == 23                       # one per year 2005..2027
    assert "count:2:2005-01-01:2027-12-31" in keys and "count:4:2005-01-01:2027-12-31" in keys   # small or unknown: whole period
    assert not any(k.startswith("count:3:") for k in keys)                          # no tenders in June: skipped
    assert summary["skipped_no_tenders_in_june"] == 1 and summary["counted_by_year"] == 1 and summary["count_tasks"] == 25


def test_allocation_gives_every_portal_a_process_and_caps_each_server():
    import assign
    ps = assign.portals()
    assert "eprocure.gov.in" not in {p["portal"] for p in ps} and "manipurtenders.gov.in" not in {p["portal"] for p in ps}
    alloc = assign.allocate(ps, 80, per_server=8)
    assert sum(alloc.values()) == 80 and min(alloc.values()) >= 1
    by_server = {}
    for p in ps:
        by_server[p["server_ip"]] = by_server.get(p["server_ip"], 0) + alloc[p["portal"]]
    assert max(by_server.values()) <= 8
    assert alloc["wbtenders.gov.in"] >= alloc["sikkimtender.gov.in"]
    slots = assign.order(alloc, ps)
    assert len(slots) == 80 and slots[0] == ps[0]["portal"]


def test_only_the_organisation_dropdown_counts_as_organisations():
    import orgs
    html = """<form><select name="ProductCategory"><option value="0">-Select-</option><option value="63">Pumps/Motors</option></select>
              <select name="OrganName"><option value="0">-Select-</option><option value="3">Buildings and Housing Department</option>
              <option value="7">Rural Development</option></select></form>"""
    assert orgs.parse_options(html) == {"3": "Buildings and Housing Department", "7": "Rural Development"}


def test_tender_bundle_ranges_point_at_each_file():
    import documents
    files = [("pages.json.gz", b"\x1f\x8b" + b"p" * 1000), ("docs/01_a.pdf", b"%PDF" + b"a" * 5000), ("docs/02_b.xlsx", b"PK" + b"b" * 3)]
    tar, where = documents.tender_bundle(files)
    for name, data in files:
        off, size = where[name]
        assert tar[off:off + size] == data                      # what a Range request for that slice returns
        assert documents.bundle_member(tar, name) == data      # and the tar is a normal tar
    assert len(tar) < sum(len(d) for _, d in files) + 512 * 12
