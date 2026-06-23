"""Broken parser under repair for the seller snapshot benchmark.

It handles direct semantic fields but still fails profile metric layouts with
badge/response-time decoys and ignores role-separated review tab counts.
"""
from __future__ import annotations

import re
from html import unescape


SCHEMA_KEYS = (
    "source_code",
    "profile_id",
    "display_name",
    "total_reviews",
    "listing_count",
    "followers_count",
    "following_count",
    "verified_badge",
    "seller_positive",
    "seller_negative",
    "buyer_positive",
    "buyer_negative",
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


def _int(value: object | None) -> int | None:
    text = _clean(value)
    if text is None:
        return None
    digits = re.sub(r"[^0-9]", "", text)
    return int(digits) if digits else None


def _bad_total_fallback(html: str) -> int | None:
    # Historical failure shape: picks up "12時間以内返信" after a good-rating
    # badge instead of the standalone profile review total.
    match = re.search(r"高評価.*?(\d+)\s*時間以内返信", html, flags=re.DOTALL)
    return int(match.group(1)) if match else None


def parse(html: str) -> dict[str, object | None]:
    return {
        "source_code": _first(r'data-source-code="([^"]+)"', html),
        "profile_id": _first(r'data-profile-id="([^"]+)"', html),
        "display_name": _first(r'data-testid="display-name"[^>]*>(.*?)</', html),
        "total_reviews": (
            _int(_first(r'data-testid="total-reviews"[^>]*>(.*?)</', html))
            or _bad_total_fallback(html)
        ),
        "listing_count": _int(_first(r'data-testid="listing-count"[^>]*>(.*?)</', html)),
        "followers_count": _int(_first(r'data-testid="followers-count"[^>]*>(.*?)</', html)),
        "following_count": _int(_first(r'data-testid="following-count"[^>]*>(.*?)</', html)),
        "verified_badge": bool(_first(r'data-testid="verified-badge"[^>]*>(.*?)</', html)),
        "seller_positive": _int(_first(r'data-testid="seller-positive"[^>]*data-count="(\d+)"', html)) or 0,
        "seller_negative": _int(_first(r'data-testid="seller-negative"[^>]*data-count="(\d+)"', html)) or 0,
        "buyer_positive": _int(_first(r'data-testid="buyer-positive"[^>]*data-count="(\d+)"', html)) or 0,
        "buyer_negative": _int(_first(r'data-testid="buyer-negative"[^>]*data-count="(\d+)"', html)) or 0,
    }
