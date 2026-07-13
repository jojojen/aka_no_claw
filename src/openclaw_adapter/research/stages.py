"""Explicit stage functions for the research pipeline (R3.4)."""

from __future__ import annotations

from .input import parse_research_target
from .models import ResearchJobContext, ResearchSectionResult

def parse_input(service, ctx: ResearchJobContext) -> str:
    ctx.target = parse_research_target(ctx.raw_input)
    if ctx.target.mode == "mercari_url":
        return f"已正規化 Mercari 商品網址（{ctx.target.item_id}）"
    return "已辨識為商品名稱研究"

def fetch_item_data(service, ctx: ResearchJobContext) -> str:
    assert ctx.target is not None
    if ctx.target.mode != "mercari_url":
        result = ResearchSectionResult(
            section_name="取得商品資料",
            status="unavailable",
            confidence=1.0,
            sample_count=0,
            evidence_count=0,
            summary="名稱模式暫不抓單一商品頁。",
        )
        ctx.add_section_result(result)
        return result.summary
    try:
        item = service._item_fetcher.fetch(ctx.target)
    except Exception as exc:
        message = f"Mercari 商品頁抓取失敗：{exc}"
        result = ResearchSectionResult(
            section_name="取得商品資料",
            status="unavailable",
            confidence=0.0,
            sample_count=0,
            evidence_count=0,
            summary=message,
            evidence_urls=(ctx.target.canonical_url or "",),
            warnings=(
                message,
                f"建議跟進：/new 抓取 mercari 商品 {ctx.target.item_id} 的完整欄位與圖片清單",
            ),
        )
        ctx.add_section_result(result)
        return message
    ctx.item_data = item
    warnings: list[str] = []
    status = "ok"
    if item.seller_id is None or item.condition_label is None:
        status = "partial"
        warnings.append("Mercari 頁面部分欄位缺漏，商品資料只有部分可信。")
    result = ResearchSectionResult(
        section_name="取得商品資料",
        status=status,
        confidence=item.source_confidence,
        sample_count=1,
        evidence_count=1 + len(item.image_urls),
        summary=(
            f"已抓到商品頁：{item.title} / ¥{item.listed_price_jpy:,}" if item.listed_price_jpy is not None
            else f"已抓到商品頁：{item.title} / 價格缺失"
        ),
        evidence_urls=(item.item_url, *item.image_urls[:2]),
        warnings=tuple(warnings),
    )
    ctx.add_section_result(result)
    return (
        f"標題「{item.title}」，價格 ¥{item.listed_price_jpy:,}，"
        f"狀態 {item.condition_label or '未知'}，賣家 {item.seller_id or '未知'}"
        if item.listed_price_jpy is not None
        else f"標題「{item.title}」，但未抓到價格"
    )


def identify_entities(service, ctx: ResearchJobContext) -> str:
    """Persist item facts and isolate canonical-entity recognition (R3.5)."""
    if ctx.item_data is not None:
        service._persist_item_knowledge(ctx.item_data)
        profile = service._recognize_entity(ctx.item_data)
        if profile is not None:
            ctx.entity_profile = profile
            service._persist_entity_aliases(ctx.item_data, profile)
            identity = " / ".join(part for part in (
                profile.card_name, profile.series, profile.character, profile.rarity,
            ) if part) or profile.canonical_query
            summary = (
                f"已辨識實體：{identity}；canonical 查詢「{profile.canonical_query}」"
                f"（alias {len(profile.aliases)} 筆）已寫入 knowledge DB。"
            )
            result = ResearchSectionResult(
                section_name="實體辨識", status="ok",
                confidence=min(0.88, ctx.item_data.source_confidence + 0.1),
                sample_count=1, evidence_count=1, summary=summary,
                evidence_urls=(ctx.item_data.item_url,), warnings=(),
            )
            ctx.add_section_result(result)
            return summary
        summary = "已把商品基礎事實寫入 knowledge DB（origin=research_command）"
        result = ResearchSectionResult(
            section_name="實體辨識", status="partial",
            confidence=min(0.85, ctx.item_data.source_confidence),
            sample_count=1, evidence_count=1, summary=summary,
            evidence_urls=(ctx.item_data.item_url,),
            warnings=("M2 僅寫入商品頁基礎事實，LLM 實體辨識未能定位 canonical 卡名（資料不足或不確定）。",),
        )
        ctx.add_section_result(result)
        return summary
    warning = "沒有商品頁基礎資料可供實體辨識，knowledge DB 寫回略過。"
    result = ResearchSectionResult(
        section_name="實體辨識", status="unavailable", confidence=0.0,
        sample_count=0, evidence_count=0, summary=warning, warnings=(warning,),
    )
    ctx.add_section_result(result)
    return warning
