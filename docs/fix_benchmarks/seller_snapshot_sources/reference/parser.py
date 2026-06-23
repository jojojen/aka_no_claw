"""Reference parser for synthetic seller snapshot fixtures."""
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


def _attr(pattern: str, html: str) -> str | None:
    return _first(pattern, html)


def _int(value: object | None) -> int | None:
    text = _clean(value)
    if text is None:
        return None
    digits = re.sub(r"[^0-9]", "", text)
    return int(digits) if digits else None


def _lines(html: str) -> list[str]:
    text = unescape(re.sub(r"<[^>]+>", "\n", html))
    return [line.strip() for line in re.sub(r"\n+", "\n", text).splitlines() if line.strip()]


def _metric_line(label: str, lines: list[str]) -> int | None:
    for line in lines:
        match = re.search(rf"(\d[\d,]*)\s*{re.escape(label)}", line)
        if match:
            return _int(match.group(1))
    return None


def _standalone_total(display_name: str | None, lines: list[str]) -> int | None:
    if not display_name:
        return None
    try:
        start = lines.index(display_name)
    except ValueError:
        start = 0
    for line in lines[start + 1:start + 6]:
        if re.fullmatch(r"\d[\d,]*", line):
            return _int(line)
        if "時間以内返信" in line or "高評価" in line:
            continue
    return None


def _review_count(html: str, role: str, rating: str) -> int:
    total = 0
    pattern = (
        rf"<[^>]+data-review-role=[\"']{re.escape(role)}[\"']"
        rf"[^>]+data-review-rating=[\"']{re.escape(rating)}[\"']"
        rf"[^>]+data-count=[\"'](\d+)[\"'][^>]*>"
    )
    for match in re.finditer(pattern, html, flags=re.IGNORECASE | re.DOTALL):
        total += int(match.group(1))
    return total


def parse(html: str) -> dict[str, object | None]:
    lines = _lines(html)
    display_name = (
        _first(r'data-testid="display-name"[^>]*>(.*?)</', html)
        or _first(r'<h2[^>]+class="profile-name"[^>]*>(.*?)</h2>', html)
    )

    total_reviews = (
        _int(_first(r'data-testid="total-reviews"[^>]*>(.*?)</', html))
        or _standalone_total(display_name, lines)
    )

    return {
        "source_code": _attr(r'data-source-code="([^"]+)"', html),
        "profile_id": _attr(r'data-profile-id="([^"]+)"', html),
        "display_name": display_name,
        "total_reviews": total_reviews,
        "listing_count": (
            _int(_first(r'data-testid="listing-count"[^>]*>(.*?)</', html))
            or _metric_line("出品数", lines)
        ),
        "followers_count": (
            _int(_first(r'data-testid="followers-count"[^>]*>(.*?)</', html))
            or _metric_line("フォロワー", lines)
        ),
        "following_count": (
            _int(_first(r'data-testid="following-count"[^>]*>(.*?)</', html))
            or _metric_line("フォロー中", lines)
        ),
        "verified_badge": bool(
            _first(r'data-testid="verified-badge"[^>]*>(.*?)</', html)
            or any("本人確認済" in line for line in lines)
        ),
        "seller_positive": _review_count(html, "seller", "positive"),
        "seller_negative": _review_count(html, "seller", "negative"),
        "buyer_positive": _review_count(html, "buyer", "positive"),
        "buyer_negative": _review_count(html, "buyer", "negative"),
    }
