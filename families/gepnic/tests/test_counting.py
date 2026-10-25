import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "captcha"))

import counting  # noqa: E402


def page(body):
    return f"<html><body>{body}</body></html>"


def test_total_records_from_the_footer():
    assert counting.parse_total(page('<table id="tabList"><tr><td class="list_footer">Total records: 3,592</td></tr></table>')) == 3592


def test_total_records_footer_plain_number():
    assert counting.parse_total(page('<table id="tabList"></table><td class="list_footer">Total records: 41</td>')) == 41


def test_no_tenders_page_is_zero():
    assert counting.parse_total(page('<table id="tabList"><tr><td colspan="6"><span class="error">No Tenders found.</span></td></tr></table>')) == 0


def test_rows_are_counted_when_the_footer_is_missing():
    rows = "".join(f"<tr><td>{i}.</td><td>a</td><td>b</td><td>c</td><td>d</td><td>e</td></tr>" for i in range(1, 4))
    assert counting.parse_total(page(f'<table id="tabList">{rows}</table>')) == 3


def test_a_non_results_page_is_none():
    assert counting.parse_total(page("<form>captcha</form>")) is None
