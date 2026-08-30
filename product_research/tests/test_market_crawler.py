"""Market context is for positioning; it must be relevant and politely gathered."""

from src.crawler.market_crawler import _is_on_topic, _is_usable


def test_google_redirect_wrappers_are_dropped():
    """About a third of search results wrap the URL and are not fetchable."""

    assert not _is_usable("https://www.google.com/goto?url=CAESkwEB6zswFYUZ")
    assert not _is_usable("https://www.google.com/url?q=https://example.com")
    assert _is_usable("https://troypoint.com/best-iptv-services")


def test_walled_sites_are_dropped_but_forums_are_kept():
    """Complaint threads are the most honest record of what a market worries about."""

    assert not _is_usable("https://www.facebook.com/groups/iptv")
    assert not _is_usable("https://www.youtube.com/watch?v=abc")
    assert _is_usable("https://www.reddit.com/r/iptv/comments/x")
    assert _is_usable("https://techkings.org/threads/iptv")


def test_non_http_urls_are_dropped():
    for url in ("javascript:alert(1)", "ftp://example.com", "", "//example.com"):
        assert not _is_usable(url)


def test_pages_that_only_mention_the_term_are_dropped():
    """A commercial query attracts spam; one run surfaced a diabetes retailer."""

    on_topic = "iptv streaming iptv channels iptv subscription"
    off_topic = "glucose sensor shop diabetes supplies, also we mention iptv once"
    assert _is_on_topic(on_topic, ["iptv", "streaming"])
    assert not _is_on_topic(off_topic, ["iptv", "streaming"])


def test_no_terms_means_nothing_is_on_topic():
    assert not _is_on_topic("anything at all", [])
