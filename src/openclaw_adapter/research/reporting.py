"""Pure research report synthesis helpers (R3.3)."""

from __future__ import annotations

from collections.abc import Iterable


def ordered_sections(sections: Iterable[object], order: dict[str, int]) -> tuple[object, ...]:
    return tuple(sorted(sections, key=lambda item: order.get(getattr(item, "section_name", ""), len(order) + 1)))


def unique_warnings(warnings: Iterable[str], *, marketplace_timed_out: bool) -> tuple[str, ...]:
    unique = list(dict.fromkeys(warnings))
    if marketplace_timed_out:
        unique.insert(0, "市場搜尋逾時，已用目前取得的資料回答；價格／成交資料可能不完整。")
    return tuple(unique)
