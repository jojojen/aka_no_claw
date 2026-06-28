"""Broken parser under repair for the price reference benchmark.

This file intentionally mirrors attempts/attempt_02_json_dom.py. It represents
a real mid-repair state: KNSR direct DOM and JSON-island fixtures pass, but
shuffled product tiles, official-release definition lists, and catalog tables
are still unsupported.
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


def _price(value: object | None) -> int | None:
    if isinstance(value, int):
        return value
    if not value:
        return None
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else None


def _token(value: object | None) -> str | None:
    text = _clean(value)
    if not text:
        return None
    return text.lower().replace(" ", "_")


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


def parse(html: str) -> dict[str, object | None]:
    record = _json_record(html)
    price_record = record.get("price") if isinstance(record.get("price"), dict) else {}
    price_amount = price_record.get("amount") if isinstance(price_record, dict) else None
    price_kind = price_record.get("kind") if isinstance(price_record, dict) else None

    return {
        "source_code": _clean(record.get("_source_code")) or _first(r'data-source-code="([^"]+)"', html),
        "source_type": _clean(record.get("_source_type")) or _first(r'data-source-type="([^"]+)"', html),
        "item_id": _clean(record.get("item_id")) or _first(r'data-testid="item-id"[^>]*>(.*?)</', html),
        "title": _clean(record.get("title")) or _first(r'data-testid="item-title"[^>]*>(.*?)</', html),
        "price_jpy": _price(price_amount) or _price(_first(r'data-testid="item-price"[^>]*>(.*?)</', html)),
        "price_kind": _token(price_kind) or _token(_first(r'data-testid="price-kind"[^>]*>(.*?)</', html)),
        "availability": _token(record.get("availability")) or _token(_first(r'data-testid="availability"[^>]*>(.*?)</', html)),
        "seller_or_store": _clean(record.get("seller_or_store")) or _first(r'data-testid="seller-name"[^>]*>(.*?)</a>', html),
        "condition": _token(record.get("condition")) or _token(_first(r'data-testid="condition"[^>]*>(.*?)</', html)),
    }
