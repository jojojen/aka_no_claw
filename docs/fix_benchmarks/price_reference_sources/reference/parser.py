"""Reference parser for synthetic price reference source fixtures."""
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


def _clean(value: object | None) -> str | None:
    if value is None:
        return None
    text = unescape(str(value))
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _first(pattern: str, html: str) -> str | None:
    match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
    return _clean(match.group(1)) if match else None


def _raw_first(pattern: str, html: str) -> str | None:
    match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1) if match else None


def _attr(attrs: str, name: str) -> str | None:
    match = re.search(
        rf"\b{re.escape(name)}\s*=\s*([\"'])(.*?)\1",
        attrs,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _clean(match.group(2)) if match else None


def _primary_tile(html: str) -> tuple[str, str] | None:
    for match in re.finditer(r"<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>", html, flags=re.IGNORECASE | re.DOTALL):
        attrs = match.group("attrs")
        if _attr(attrs, "data-primary-result") == "true":
            return attrs, match.group("body")
    return None


def _class_value(class_fragment: str, html: str) -> str | None:
    return _first(
        rf"<[^>]+class=[\"'][^\"']*{re.escape(class_fragment)}[^\"']*[\"'][^>]*>(.*?)</[^>]+>",
        html,
    )


def _labeled_price(attrs: str) -> tuple[str | None, int | None]:
    label = _attr(attrs, "aria-label")
    if not label:
        return None, None
    for pattern in (
        r"^(.+?)\s*-\s*¥\s*([\d,]+)\s*$",
        r"^(.+?)\s*¥\s*([\d,]+)\s*$",
        r"^(.+?)\s*-\s*([\d,]+)\s*円\s*$",
    ):
        match = re.match(pattern, label)
        if match:
            return _clean(match.group(1)), _price(match.group(2))
    return None, None


def _price(value: object | None) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = _clean(value)
    if not text:
        return None
    digits = re.sub(r"[^0-9]", "", text)
    return int(digits) if digits else None


def _token(value: object | None) -> str | None:
    text = _clean(value)
    if not text:
        return None
    lowered = text.lower()
    replacements = {
        "near mint": "near_mint",
        "very good": "very_good",
        "in stock": "in_stock",
        "current bid": "current_bid",
    }
    return replacements.get(lowered, lowered.replace(" ", "_"))


def _json_record(html: str) -> dict[str, object]:
    raw = _first(r'<script[^>]+id="__PRICE_REFERENCE__"[^>]*>(.*?)</script>', html)
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


def _dl_value(label: str, html: str) -> str | None:
    return _first(
        rf"<dt>\s*{re.escape(label)}\s*</dt>\s*<dd[^>]*>(.*?)</dd>",
        html,
    )


def _row_cells(html: str) -> list[str]:
    row = _raw_first(r'<tr[^>]+data-primary-result="true"[^>]*>(.*?)</tr>', html)
    if not row:
        return []
    return [_clean(cell) or "" for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.DOTALL)]


def _page_kind(html: str) -> str | None:
    return _first(r'data-page-kind="([^"]+)"', html)


def parse(html: str) -> dict[str, object | None]:
    record = _json_record(html)
    price_record = record.get("price") if isinstance(record.get("price"), dict) else {}
    page_kind = _page_kind(html)
    cells = _row_cells(html)
    tile = _primary_tile(html)
    tile_attrs, tile_body = tile if tile is not None else ("", "")
    tile_label_title, tile_label_price = _labeled_price(tile_attrs)

    source_code = _clean(record.get("_source_code")) or _first(r'data-source-code="([^"]+)"', html)
    source_type = (
        _clean(record.get("_source_type"))
        or _first(r'data-source-type="([^"]+)"', html)
        or (page_kind.replace("-", "_") if page_kind else None)
    )
    if source_type == "auction_listing":
        source_type = "auction"

    item_id = (
        _clean(record.get("item_id"))
        or _first(r'data-testid="item-id"[^>]*>(.*?)</', html)
        or _attr(tile_attrs, "data-item-id")
        or _first(r'<meta[^>]+name="fixture:item-id"[^>]+content="([^"]+)"', html)
        or (cells[0] if len(cells) >= 1 else None)
    )

    title = (
        _clean(record.get("title"))
        or _first(r'data-testid="item-title"[^>]*>(.*?)</', html)
        or tile_label_title
        or _class_value("productName", tile_body)
        or _first(r'<h1[^>]+class="release-title"[^>]*>(.*?)</h1>', html)
        or _first(r'<article[^>]+class="auction-detail"[^>]*>.*?<h1[^>]*>(.*?)</h1>', html)
        or (cells[1] if len(cells) >= 2 else None)
    )

    price_amount = price_record.get("amount") if isinstance(price_record, dict) else None
    price = (
        _price(price_amount)
        or _price(_first(r'data-testid="item-price"[^>]*>(.*?)</', html))
        or tile_label_price
        or _price(_class_value("productPrice", tile_body))
        or _price(_first(r'<dd[^>]+class="msrp"[^>]*>(.*?)</dd>', html))
        or _price(_first(r'data-current-bid-jpy="([^"]+)"', html))
        or ( _price(cells[2]) if len(cells) >= 3 else None )
    )

    price_kind = (
        _token(price_record.get("kind") if isinstance(price_record, dict) else None)
        or _token(_first(r'data-testid="price-kind"[^>]*>(.*?)</', html))
        or _token(_attr(tile_attrs, "data-price-kind"))
        or _token(_class_value("priceKind", tile_body))
        or _token(_dl_value("販売区分", html))
        or _token(_first(r'<span[^>]*>\s*価格種別\s*</span>\s*<span[^>]*>(.*?)</span>', html))
        or (_token(cells[3]) if len(cells) >= 4 else None)
    )

    availability = (
        _token(record.get("availability"))
        or _token(_first(r'data-testid="availability"[^>]*>(.*?)</', html))
        or _token(_attr(tile_attrs, "data-availability"))
        or _token(_class_value("availability", tile_body))
        or _token(_dl_value("販売状況", html))
        or _token(_first(r'<span[^>]*>\s*入札状況\s*</span>\s*<span[^>]*>(.*?)</span>', html))
        or (_token(cells[4]) if len(cells) >= 5 else None)
    )

    seller_or_store = (
        _clean(record.get("seller_or_store"))
        or _first(r'data-testid="seller-name"[^>]*>(.*?)</a>', html)
        or _class_value("seller", tile_body)
        or _dl_value("販売元", html)
        or _first(r'data-aucl-seller="([^"]+)"', html)
        or (cells[5] if len(cells) >= 6 else None)
    )

    condition = (
        _token(record.get("condition"))
        or _token(_first(r'data-testid="condition"[^>]*>(.*?)</', html))
        or _token(_attr(tile_attrs, "data-condition"))
        or _token(_class_value("condition", tile_body))
        or _token(_dl_value("状態", html))
        or _token(_first(r'<section[^>]+class="item-state"[^>]*>.*?<p[^>]*>(.*?)</p>', html))
        or (_token(cells[6]) if len(cells) >= 7 else None)
    )

    return {
        "source_code": source_code,
        "source_type": source_type,
        "item_id": item_id,
        "title": title,
        "price_jpy": price,
        "price_kind": price_kind,
        "availability": availability,
        "seller_or_store": seller_or_store,
        "condition": condition,
    }
