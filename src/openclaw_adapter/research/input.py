"""Research request normalization (R3.2)."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from .models import (
    ResearchTarget,
    _MERCARI_HOSTS,
    _MERCARI_ITEM_PATH_RE,
    _MERCARI_SHOPS_PATH_RE,
)

def parse_research_target(raw_input: str) -> ResearchTarget:
    cleaned = " ".join((raw_input or "").split()).strip()
    if not cleaned:
        raise ValueError("請提供商品名稱或 Mercari 商品網址。")
    mercari = normalize_mercari_item_url(cleaned)
    if mercari is not None:
        item_id = _extract_mercari_item_id(mercari)
        return ResearchTarget(
            mode="mercari_url",
            raw_input=cleaned,
            display_text=mercari,
            canonical_url=mercari,
            item_id=item_id,
        )
    shops = normalize_mercari_shops_url(cleaned)
    if shops is not None:
        canonical_url, token = shops
        return ResearchTarget(
            mode="mercari_url",
            raw_input=cleaned,
            display_text=canonical_url,
            canonical_url=canonical_url,
            item_id=token,
        )
    return ResearchTarget(mode="text_query", raw_input=cleaned, display_text=cleaned)


def normalize_mercari_item_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.netloc or "").lower()
    if host not in _MERCARI_HOSTS:
        return None
    match = _MERCARI_ITEM_PATH_RE.match(parsed.path or "")
    if not match:
        return None
    canonical_path = f"/item/{match.group(1).lower()}"
    return urlunsplit(("https", "jp.mercari.com", canonical_path, "", ""))


def _extract_mercari_item_id(url: str) -> str | None:
    match = _MERCARI_ITEM_PATH_RE.match(urlsplit(url).path or "")
    return match.group(1).lower() if match else None


def normalize_mercari_shops_url(url: str) -> tuple[str, str] | None:
    """Return (canonical_url, token) for a Mercari Shops product URL, else None.

    Shops pages render price client-side (absent from static HTML), but the
    product name is in og:title — enough to drive entity recognition + market
    search, so we route them through the same mercari_url fetch path.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.netloc or "").lower()
    if host not in _MERCARI_HOSTS:
        return None
    match = _MERCARI_SHOPS_PATH_RE.match(parsed.path or "")
    if not match:
        return None
    token = match.group(1)
    canonical_path = f"/shops/product/{token}"
    canonical_url = urlunsplit(("https", "jp.mercari.com", canonical_path, "", ""))
    return canonical_url, token
