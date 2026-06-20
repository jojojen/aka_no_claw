"""URL canonicalization (issue #9 D1): no-network redirect unwrap + tracking strip."""

from __future__ import annotations

from openclaw_adapter.url_canonicalize import (
    canonicalize_url,
    is_traceable_source,
    source_domain,
)


def test_strips_tracking_params_keeps_meaningful_ones():
    raw = "https://www.suruga-ya.jp/product/detail/12345?utm_source=x&id=9&fbclid=abc&gclid=z"
    assert canonicalize_url(raw) == "https://www.suruga-ya.jp/product/detail/12345?id=9"


def test_drops_fragment_and_trailing_slash_and_lowercases_host():
    raw = "https://Example.COM/path/?ref=foo#section"
    assert canonicalize_url(raw) == "https://example.com/path"


def test_unwraps_google_redirect():
    raw = "https://www.google.com/url?q=https%3A%2F%2Fwww.suruga-ya.jp%2Fitem%2F1&sa=D"
    assert canonicalize_url(raw) == "https://www.suruga-ya.jp/item/1"


def test_unwraps_ddg_redirect():
    raw = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa%3Futm_term%3Dz"
    assert canonicalize_url(raw) == "https://example.org/a"


def test_opaque_yahoo_redirect_left_as_is_but_normalized():
    # No network, no encoded destination → cannot unwrap; kept (host lowered).
    raw = "https://rd.listing.yahoo.co.jp/p/search/GU=opaqueblob;/?ep=more&v=2"
    out = canonicalize_url(raw)
    assert out.startswith("https://rd.listing.yahoo.co.jp/")
    assert source_domain(raw) == "rd.listing.yahoo.co.jp"


def test_generic_query_not_treated_as_redirect():
    # x.com/search?q=... is a real query, NOT a redirect — must not be unwrapped.
    raw = "https://x.com/search?q=foo&utm_source=bar"
    assert canonicalize_url(raw) == "https://x.com/search?q=foo"


def test_deterministic_dedup_across_wrappers():
    a = "https://www.google.com/url?q=https%3A%2F%2Fsite.jp%2Fx%3Futm_source%3Da"
    b = "https://site.jp/x?fbclid=zzz"
    assert canonicalize_url(a) == canonicalize_url(b) == "https://site.jp/x"


def test_source_domain_strips_www():
    assert source_domain("https://www.suruga-ya.jp/foo") == "suruga-ya.jp"
    assert source_domain("https://x.com/search?q=a") == "x.com"


def test_opaque_yahoo_redirect_not_traceable():
    # rd.listing.yahoo.co.jp blobs 400 on fetch and can't be unwrapped offline →
    # not a usable citation, so the registry must refuse them.
    raw = "https://rd.listing.yahoo.co.jp/p/search/GU=opaqueblob;/?ep=more&v=2"
    assert is_traceable_source(raw) is False


def test_real_yahoo_search_url_is_traceable():
    raw = "https://auctions.yahoo.co.jp/search/search?p=foo"
    assert is_traceable_source(raw) is True


def test_empty_and_non_http_not_traceable():
    assert is_traceable_source("") is False
    assert is_traceable_source("mailto:a@b.com") is False


def test_empty_and_non_http_passthrough():
    assert canonicalize_url("") == ""
    assert canonicalize_url("   ") == ""
    assert canonicalize_url("mailto:a@b.com") == "mailto:a@b.com"
    assert source_domain("not a url") == ""
