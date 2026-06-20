"""Deterministic, no-network URL canonicalization for the source registry.

Raw source URLs collected during RAG research are frequently redirect /
tracking wrappers — Yahoo listing redirects, Google ``/url?q=`` links, DDG
``/l/?uddg=`` links, and URLs stuffed with UTM / fbclid / gclid parameters.
Storing and rendering those verbatim wastes tokens, breaks deduplication, and
makes Telegram citations unreadable.

This module turns a raw URL into a stable *canonical* URL so the source
registry can dedup on it.

**No network policy (deliberate).** Issue #9 asks to resolve redirects "where
safe and practical". We resolve only redirects whose destination is already
encoded in a query parameter of a *known redirector host* — purely string
work, no HTTP request. Resolving an opaque redirect (e.g. Yahoo's
``rd.listing.yahoo.co.jp`` blob) would require fetching a third-party host,
which risks IP rate-limiting / bans (priority ②不被封鎖, SKILL.md C7). Such
opaque URLs are left as-is; the registry still stores them and renders a
compact domain label, so citations stay clean and traceable.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

# Query parameters that never identify the resource — pure tracking noise.
TRACKING_PARAMS: frozenset[str] = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "yclid", "dclid", "msclkid",
    "ref", "ref_src", "ref_url", "referrer",
    "tracking_id", "mc_cid", "mc_eid", "igshid", "spm",
    "_ga", "_gl", "vero_id", "oly_enc_id", "oly_anon_id",
})

# Known redirector hosts → the query param(s) that carry the real destination.
# Only these hosts get unwrapped; a generic ``?q=`` / ``?url=`` on an arbitrary
# host is left alone (it is usually a real query, not a redirect).
_REDIRECTOR_DEST_PARAMS: dict[str, tuple[str, ...]] = {
    "www.google.com": ("url", "q"),
    "google.com": ("url", "q"),
    "duckduckgo.com": ("uddg",),
    "l.facebook.com": ("u",),
    "lm.facebook.com": ("u",),
    "out.reddit.com": ("url",),
    "www.youtube.com": ("q",),  # youtube.com/redirect?q=
    "t.umblr.com": ("z",),
}

_MAX_UNWRAP_HOPS = 3

# Hosts that serve *opaque* redirects: the real destination is buried in an
# encrypted/session-bound path blob (not a query param), so we cannot unwrap it
# without a network fetch — and fetching one directly returns HTTP 400 (the
# token is single-use / session-bound). Such a URL is therefore **not
# traceable**: a citation pointing at it can never be expanded back to the
# original article. We refuse to register these as sources (issue #9 requires
# every stored source be traceable back to its origin). The snippet text still
# feeds the LLM summary; it just earns no [S] citation.
_OPAQUE_REDIRECT_HOSTS: frozenset[str] = frozenset({
    "rd.listing.yahoo.co.jp",
})


def _unwrap_once(url: str) -> str | None:
    """If *url* is a known redirector whose destination is in a query param,
    return that destination. Otherwise None."""
    parsed = urlsplit(url)
    host = parsed.netloc.lower()
    dest_params = _REDIRECTOR_DEST_PARAMS.get(host)
    if not dest_params:
        return None
    qs = {k.lower(): v for k, v in parse_qsl(parsed.query, keep_blank_values=True)}
    for key in dest_params:
        candidate = qs.get(key.lower())
        if not candidate:
            continue
        candidate = unquote(candidate)
        if candidate.startswith(("http://", "https://")):
            return candidate
    return None


def canonicalize_url(raw: str) -> str:
    """Return a stable canonical form of *raw* (no network).

    - Unwraps known-redirector links to their encoded destination.
    - Strips tracking parameters; keeps meaningful ones (order preserved).
    - Lower-cases scheme + host, drops the fragment and trailing slashes.
    - Non-http(s) input is returned stripped but otherwise untouched.

    Deterministic: the same final URL always yields the same output, so two
    different redirect/tracking wrappers around one destination collapse to one
    canonical URL (the basis for registry deduplication)."""
    url = (raw or "").strip()
    if not url:
        return ""

    for _ in range(_MAX_UNWRAP_HOPS):
        nxt = _unwrap_once(url)
        if not nxt or nxt == url:
            break
        url = nxt

    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https"):
        return url

    kept = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(kept)

    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))


def is_traceable_source(url: str) -> bool:
    """True if *url* can serve as a citation that resolves back to the original.

    False for empty/non-http(s) input and for opaque-redirect hosts whose
    destination we cannot unwrap without a (banned, and anyway HTTP-400) network
    fetch. The registry uses this to refuse non-traceable sources so every
    stored ``[S<n>]`` citation can be expanded back to a real article."""
    canonical = canonicalize_url(url)
    if not canonical:
        return False
    parsed = urlsplit(canonical)
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    return parsed.netloc.lower() not in _OPAQUE_REDIRECT_HOSTS


def source_domain(url: str) -> str:
    """Human-readable domain label for a (raw or canonical) URL, e.g.
    ``https://www.suruga-ya.jp/...`` → ``suruga-ya.jp``. Empty string if the
    URL has no recognizable host."""
    netloc = urlsplit(canonicalize_url(url)).netloc.lower()
    if not netloc:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc.split(":", 1)[0]  # drop any :port
