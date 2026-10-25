import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "captcha"))

import pytest  # noqa: E402

import scrape_organization as so  # noqa: E402


def page(text):
    return SimpleNamespace(content=text.encode())


def test_both_wordings_of_the_portals_expired_page_are_recognised():
    assert so._is_expired(page("<html>Your session in the client area has expired. A session will time out</html>"))
    assert so._is_expired(page("<html><body>Stale Session Your session has timed out. Web applications store information</body></html>"))
    assert not so._is_expired(page("<html>Tender Details Basic Details Organisation Chain</html>"))


def test_an_expired_page_after_relogin_fails_the_tender():
    with pytest.raises(so.PortalSessionExpired):
        so._must_not_be_expired(page("<html>Stale Session Your session has timed out.</html>"), "https://eprocure.gov.in/x")
    so._must_not_be_expired(page("<html>Tender Details</html>"), "https://eprocure.gov.in/x")
