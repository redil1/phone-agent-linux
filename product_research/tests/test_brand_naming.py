"""The company name is spoken in the greeting, so it must not read as a typo."""

import pytest
from src.extractor.extractor import brand_from_domain


@pytest.mark.parametrize(
    "url,expected",
    [
        # Taking only the first label produced "Iptv", so the agent said it was
        # calling "from Iptv, about Iptv".
        ("https://iptv.shopping", "IPTV Shopping"),
        ("https://mytv.store", "MYTV Store"),
        # A generic ending is not part of the brand.
        ("https://vapi.ai", "Vapi"),
        ("https://streamly.com", "Streamly"),
        ("https://www.acme-corp.com", "Acme-corp"),
        ("https://bbc.co", "BBC"),
    ],
)
def test_brand_is_derived_readably(url: str, expected: str):
    assert brand_from_domain(url) == expected


def test_ports_and_www_are_ignored():
    assert brand_from_domain("https://www.streamly.com:8443") == "Streamly"


def test_an_unparseable_url_does_not_raise():
    assert brand_from_domain("not a url") == ""
