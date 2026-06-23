"""Attempt 01: naive DOM parser.

This was the first implementation attempt. It only handles the direct
data-testid layout and fails when data moves to JSON, definition lists, or
tables.
"""
from __future__ import annotations

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


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = unescape(re.sub(r"<[^>]+>", "", value))
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def _first(pattern: str, html: str) -> str | None:
    match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
    return _clean(match.group(1)) if match else None


def _price(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else None


def _norm(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lower().replace(" ", "_")


def parse(html: str) -> dict[str, object | None]:
    return {
        "source_code": _first(r'data-source-code="([^"]+)"', html),
        "source_type": _first(r'data-source-type="([^"]+)"', html),
        "item_id": _first(r'data-testid="item-id"[^>]*>(.*?)</', html),
        "title": _first(r'data-testid="item-title"[^>]*>(.*?)</', html),
        "price_jpy": _price(_first(r'data-testid="item-price"[^>]*>(.*?)</', html)),
        "price_kind": _norm(_first(r'data-testid="price-kind"[^>]*>(.*?)</', html)),
        "availability": _norm(_first(r'data-testid="availability"[^>]*>(.*?)</', html)),
        "seller_or_store": _first(r'data-testid="seller-name"[^>]*>(.*?)</a>', html),
        "condition": _norm(_first(r'data-testid="condition"[^>]*>(.*?)</', html)),
    }
