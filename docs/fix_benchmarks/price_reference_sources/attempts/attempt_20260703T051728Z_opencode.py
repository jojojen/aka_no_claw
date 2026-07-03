"""Repaired parser for the price reference benchmark.

Supports:
- JSON islands (<script id="__PRICE_REFERENCE__">)
- data‑testid attributes (knsr v1)
- product tiles with data‑primary‑result (knsr v3)
- auction detail pages (aucl)
- publisher release definition lists (pubr)
- catalog tables with primary rows (tcgw)
"""
from __future__ import annotations

import json
import re
from html import unescape


SCHEMA_KEYS = (
    "source_code",
    "source_type",
    "item_id",
    "title",
    "price_jpy",
    "price_kind",
    "availability",
    "seller_or_store",
    "condition",
)


# ---------------------------------------------------------------------------
# General purpose helpers
# ---------------------------------------------------------------------------

def _clean(value: object | None) -> str | None:
    """Strip HTML tags, collapse whitespace, unescape entities."""
    if value is None:
        return None
    text = unescape(str(value))
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _first(pattern: str, html: str) -> str | None:
    """Return the first captured group of *pattern* in *html*."""
    match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
    return _clean(match.group(1)) if match else None


def _price(value: object | None) -> int | None:
    """Extract an integer from *value* (int, string with commas, etc.)."""
    if isinstance(value, int):
        return value
    if not value:
        return None
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else None


def _token(value: object | None) -> str | None:
    """Lowercase, replace whitespace with underscores."""
    text = _clean(value)
    if not text:
        return None
    return text.lower().replace(" ", "_")


# ---------------------------------------------------------------------------
# JSON island extraction
# ---------------------------------------------------------------------------

def _json_record(html: str) -> dict[str, object]:
    raw = _first(
        r'<script[^>]+id="__PRICE_REFERENCE__"[^>]*>(.*?)</script>',
        html,
    )
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    record = payload.get("record") if isinstance(payload, dict) else None
    if not isinstance(record, dict):
        return {}
    record = dict(record)
    record["_source_code"] = payload.get("source_code")
    record["_source_type"] = payload.get("source_type")
    return record


# ===========================================================================
# Main parser
# ===========================================================================

def parse(html: str) -> dict[str, object | None]:
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # 1. JSON island (knsr v2)
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    json_data = _json_record(html)
    price_record = (
        json_data["price"]
        if isinstance(json_data.get("price"), dict)
        else {}
    )

    # convenience – pull a clean value from json_data
    def _json_clean(key: str) -> str | None:
        val = json_data.get(key)
        return _clean(val) if val is not None else None

    # convenienve – pull a token from json_data
    def _json_token(key: str) -> str | None:
        val = json_data.get(key)
        return _token(val) if val is not None else None

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # 2. Source data
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    source_code = _json_clean("_source_code") or _first(
        r'data-source-code="([^"]+)"', html
    )

    source_type = _json_clean("_source_type") or _first(
        r'data-source-type="([^"]+)"', html
    )
    if not source_type:
        page_kind = _first(r'data-page-kind="([^"]+)"', html)
        kind_map = {
            "auction-listing": "auction",
            "secondary-market-listing": "secondary_market",
            "secondary-market-grid": "secondary_market",
            "publisher-product-release": "publisher_release",
            "card-shop-catalog": "shop_catalog",
        }
        source_type = kind_map.get(page_kind)  # may be None – acceptable

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # 3. item_id
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    item_id = _json_clean("item_id")
    if not item_id:
        item_id = _first(
            r'<meta name="fixture:item-id" content="([^"]+)"', html
        )
    if not item_id:
        item_id = _first(
            r'data-testid="item-id"[^>]*>\s*([^<]+)\s*<', html
        )
    if not item_id:
        item_id = _first(
            r'<a[^>]*data-primary-result="true"[^>]*data-item-id="([^"]+)"',
            html,
        )
    if not item_id:
        item_id = _first(
            r'<tr[^>]*data-primary-result="true"[^>]*>.*?'
            r'<span class="mono">([^<]+)</span>',
            html,
        )

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # 4. title
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    title = _json_clean("title")
    if not title:
        title = _first(
            r'<meta property="fixture:title" content="([^"]+)"', html
        )
    if not title:
        title = _first(
            r'data-testid="item-title"[^>]*>\s*([^<]+)\s*<', html
        )
    if not title:
        title = _first(
            r'<a[^>]*data-primary-result="true"[^>]*>.*?'
            r'<span class="fx-productName">([^<]+)</span>',
            html,
        )
    if not title:
        title = _first(
            r'<article[^>]*class="auction-detail"[^>]*>.*?'
            r'<h1>([^<]+)</h1>',
            html,
        )
    if not title:
        title = _first(
            r'<h1[^>]*class="release-title"[^>]*>([^<]+)</h1>', html
        )
    if not title:
        title = _first(
            r'<tr[^>]*data-primary-result="true"[^>]*>.*?'
            r'<td>.*?<a[^>]*>([^<]+)</a>',
            html,
        )

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # 5. price_jpy (int)
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    price_jpy: int | None = None
    if "amount" in price_record:
        price_jpy = _price(price_record["amount"])
    if not price_jpy:
        price_jpy = _price(
            _first(r'data-testid="item-price"[^>]*data-price-jpy="(\d+)"', html)
        )
    if not price_jpy:
        # fallback to the element text (12,800 JPY)
        price_jpy = _price(
            _first(r'data-testid="item-price"[^>]*>\s*([^<]+)\s*<', html)
        )
    if not price_jpy:
        price_jpy = _price(
            _first(r'data-current-bid-jpy="(\d+)"', html)
        )
    if not price_jpy:
        price_jpy = _price(
            _first(
                r'<a[^>]*data-primary-result="true"[^>]*>.*?'
                r'<span class="fx-productPrice">([^<]+)</span>',
                html,
            )
        )
    if not price_jpy:
        price_jpy = _price(
            _first(
                r'<dd[^>]*class="msrp"[^>]*>.*?([\d,]+)円', html
            )
        )
    if not price_jpy:
        price_jpy = _price(
            _first(
                r'<tr[^>]*data-primary-result="true"[^>]*>.*?'
                r'<span class="price">.*?([\d,]+)円',
                html,
            )
        )

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # 6. price_kind
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    price_kind = _token(price_record.get("kind"))
    if not price_kind:
        price_kind = _token(
            _first(r'data-testid="price-kind"[^>]*>\s*([^<]+)\s*<', html)
        )
    if not price_kind:
        price_kind = _token(
            _first(
                r'<a[^>]*data-primary-result="true"[^>]*>.*?'
                r'<span class="fx-priceKind">([^<]+)</span>',
                html,
            )
        )
    if not price_kind:
        price_kind = _token(
            _first(
                r'<span class="label">価格種別</span>\s*<span>([^<]+)</span>',
                html,
            )
        )
    if not price_kind:
        price_kind = _token(
            _first(
                r'<dt>販売区分</dt>\s*<dd>.*?'
                r'<span[^>]*class="pill"[^>]*>([^<]+)</span>',
                html,
            )
        )
    if not price_kind:
        price_kind = _token(
            _first(
                r'<tr[^>]*data-primary-result="true"[^>]*>.*?'
                r'<td><span class="kind">([^<]+)</span>',
                html,
            )
        )

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # 7. availability
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    availability = _json_token("availability")
    if not availability:
        availability = _token(
            _first(r'data-testid="availability"[^>]*>\s*([^<]+)\s*<', html)
        )
    if not availability:
        availability = _token(
            _first(
                r'<a[^>]*data-primary-result="true"[^>]*>.*?'
                r'<span class="fx-availability">([^<]+)</span>',
                html,
            )
        )
    if not availability:
        availability = _token(
            _first(
                r'<span class="label">入札状況</span>\s*<span>([^<]+)</span>',
                html,
            )
        )
    if not availability:
        availability = _token(
            _first(
                r'<span[^>]*data-state="[^"]*"[^>]*>([^<]+)</span>',
                html,
            )
        )
    if not availability:
        availability = _token(
            _first(
                r'<tr[^>]*data-primary-result="true"[^>]*>.*?'
                r'<td><span class="stock[^"]*"[^>]*>([^<]+)</span>',
                html,
            )
        )

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # 8. seller_or_store
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    seller_or_store = _json_clean("seller_or_store")
    if not seller_or_store:
        seller_or_store = _clean(
            _first(r'data-testid="seller-name"[^>]*>\s*([^<]+)\s*<', html)
        )
    if not seller_or_store:
        seller_or_store = _clean(
            _first(r'data-aucl-seller="([^"]+)"', html)
        )
    if not seller_or_store:
        seller_or_store = _clean(
            _first(
                r'<a[^>]*data-primary-result="true"[^>]*>.*?'
                r'<span class="fx-seller">([^<]+)</span>',
                html,
            )
        )
    if not seller_or_store:
        seller_or_store = _clean(
            _first(r'<dt>販売元</dt>\s*<dd>([^<]+)</dd>', html)
        )
    if not seller_or_store:
        seller_or_store = _clean(
            _first(
                r'<tr[^>]*data-primary-result="true"[^>]*>.*?'
                r'<td><span class="store">([^<]+)</span>',
                html,
            )
        )

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # 9. condition
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    condition = _json_token("condition")
    if not condition:
        condition = _token(
            _first(r'data-testid="condition"[^>]*>\s*([^<]+)\s*<', html)
        )
    if not condition:
        condition = _token(
            _first(
                r'<a[^>]*data-primary-result="true"[^>]*>.*?'
                r'<span class="fx-condition">([^<]+)</span>',
                html,
            )
        )
    if not condition:
        condition = _token(
            _first(
                r'<section class="item-state">.*?<p>([^<]+)</p>',
                html,
            )
        )
    if not condition:
        condition = _token(
            _first(r'<dt>状態</dt>\s*<dd>([^<]+)</dd>', html)
        )
    if not condition:
        condition = _token(
            _first(
                r'<tr[^>]*data-primary-result="true"[^>]*>.*?'
                r'<td><span class="grade">([^<]+)</span>',
                html,
            )
        )

    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    # Return the assembled result
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    return {
        "source_code": source_code,
        "source_type": source_type,
        "item_id": item_id,
        "title": title,
        "price_jpy": price_jpy,
        "price_kind": price_kind,
        "availability": availability,
        "seller_or_store": seller_or_store,
        "condition": condition,
    }
