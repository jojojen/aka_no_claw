from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from assistant_runtime import AssistantSettings
from market_monitor.models import FairValueEstimate, MarketOffer, TrackedItem

from tcg_tracker.catalog import TcgCardSpec
from tcg_tracker.image_lookup import (
    ParsedCardImage,
    TcgImagePriceService,
    parse_image_caption_hints,
    parse_tcg_ocr_text,
)
from tcg_tracker.local_vision import LocalVisionCardCandidate
from tcg_tracker.service import TcgLookupResult

CHARIZARD_JP = "\u30ea\u30b6\u30fc\u30c9\u30f3ex"
RIRIE_JP = "\u30ea\u30fc\u30ea\u30a8\u306e\u30d4\u30c3\u30d4ex"


def test_parse_image_caption_hints_supports_scan_prefix() -> None:
    assert parse_image_caption_hints("/scan pokemon Pikachu ex") == ("pokemon", "Pikachu ex")
    assert parse_image_caption_hints("ws Hatsune Miku") == ("ws", "Hatsune Miku")
    assert parse_image_caption_hints(None) == (None, None)


def test_parse_tcg_ocr_text_extracts_charizard_reference_fields() -> None:
    raw_text = "\n".join(
        [
            "2025 POKEMON M2a JP",
            "MEGA CHARIZARD X ex",
            "GEM MT 10",
            CHARIZARD_JP,
            "223/193 MA",
        ]
    )

    parsed = parse_tcg_ocr_text(raw_text)

    assert parsed.status == "success"
    assert parsed.game == "pokemon"
    assert parsed.title == CHARIZARD_JP
    assert parsed.card_number == "223/193"
    assert parsed.rarity == "MA"
    assert parsed.set_code == "m2a"
    assert "MEGA CHARIZARD X ex" in parsed.aliases


def test_parse_tcg_ocr_text_extracts_ririe_reference_fields() -> None:
    raw_text = "\n".join(
        [
            "2025 POKEMON SV9 JP",
            "LILLIE'S CLEFAIRY ex",
            "SPECIAL ART RARE",
            RIRIE_JP,
            "126/100 SAR",
        ]
    )

    parsed = parse_tcg_ocr_text(raw_text)

    assert parsed.status == "success"
    assert parsed.game == "pokemon"
    assert parsed.title == RIRIE_JP
    assert parsed.card_number == "126/100"
    assert parsed.rarity == "SAR"
    assert parsed.set_code == "sv9"


def test_parse_tcg_ocr_text_rejects_copyright_noise_as_title() -> None:
    raw_text = "\n".join(
        [
            "Seta Fakerion/nintendo/Cieatures/GAMEPREAK sample",
            "@2023 Pokemon/Nintendo/Creatures/GAMEFREAK",
            "201/16559",
        ]
    )

    parsed = parse_tcg_ocr_text(raw_text, game_hint="pokemon")

    assert parsed.status == "unresolved"
    assert parsed.game == "pokemon"
    assert parsed.title is None
    assert parsed.card_number == "201/165"


def test_parse_tcg_ocr_text_accepts_easyocr_style_japanese_title() -> None:
    raw_text = "\n".join(
        [
            CHARIZARD_JP,
            "201/16559",
            "@2023 Pokemon/Nintendo/Creatures/GAMEFREAK",
        ]
    )

    parsed = parse_tcg_ocr_text(raw_text, game_hint="pokemon")

    assert parsed.status == "success"
    assert parsed.game == "pokemon"
    assert parsed.title == CHARIZARD_JP
    assert parsed.card_number == "201/165"


def test_image_service_reports_unavailable_when_tesseract_is_missing() -> None:
    settings = AssistantSettings(
        monitor_db_path="data/test-image-lookup.sqlite3",
        openclaw_tesseract_path="C:/missing/tesseract.exe",
    )
    service = TcgImagePriceService(db_path=settings.monitor_db_path, settings=settings)
    sample_path = Path(__file__).resolve().parents[1] / "fwdspecptcg" / "charizard.jpg"

    outcome = service.lookup_image(sample_path, caption="/scan pokemon")

    assert outcome.status == "unavailable"
    assert outcome.lookup_result is None
    assert any("OPENCLAW_TESSERACT_PATH" in warning for warning in outcome.warnings)


def test_lookup_image_uses_card_number_placeholder_to_recover_title(monkeypatch, tmp_path) -> None:
    calls: list[TcgCardSpec] = []

    class StubService:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def lookup(self, spec: TcgCardSpec, *, persist: bool = True) -> TcgLookupResult:
            calls.append(spec)
            if spec.card_number == "201/165":
                offer = MarketOffer(
                    source="cardrush_pokemon",
                    listing_id="sv2a-201-165",
                    url="https://example.com/charizard",
                    title=CHARIZARD_JP,
                    price_jpy=60800,
                    price_kind="ask",
                    captured_at=datetime.now(timezone.utc),
                    source_category="specialty_store",
                    attributes={"card_number": "201/165", "rarity": "SAR", "version_code": "sv2a"},
                )
                fair_value = FairValueEstimate(
                    item_id="tcg-charizard",
                    amount_jpy=60800,
                    confidence=0.82,
                    sample_count=1,
                    reasoning=("stub",),
                )
                item = TrackedItem(
                    item_id="tcg-charizard",
                    item_type="tcg_card",
                    category="tcg",
                    title=spec.title,
                    attributes={"game": "pokemon", "card_number": "201/165", "rarity": "SAR", "set_code": "sv2a"},
                )
                return TcgLookupResult(
                    spec=spec,
                    item=item,
                    offers=(offer,),
                    fair_value=fair_value,
                    notes=(),
                )

            item = TrackedItem(
                item_id="tcg-empty",
                item_type="tcg_card",
                category="tcg",
                title=spec.title,
                attributes={"game": spec.game},
            )
            return TcgLookupResult(
                spec=spec,
                item=item,
                offers=(),
                fair_value=None,
                notes=("No matching offers were found.",),
            )

    monkeypatch.setattr("tcg_tracker.image_lookup.TcgPriceService", StubService)

    settings = AssistantSettings(
        monitor_db_path=str(tmp_path / "monitor.sqlite3"),
        openclaw_tesseract_path="C:/missing/tesseract.exe",
    )
    service = TcgImagePriceService(db_path=settings.monitor_db_path, settings=settings)
    parsed = ParsedCardImage(
        status="unresolved",
        game="pokemon",
        title=None,
        aliases=(),
        card_number="201/165",
        rarity="SAR",
        set_code=None,
        raw_text="201/16559",
        extracted_lines=("201/16559",),
        warnings=(),
    )
    monkeypatch.setattr(service, "parse_image", lambda *args, **kwargs: parsed)

    outcome = service.lookup_image(tmp_path / "telegram-upload-charizard.jpg", persist=False)

    assert outcome.status == "success"
    assert outcome.lookup_result is not None
    assert outcome.lookup_result.spec.title == CHARIZARD_JP
    assert outcome.parsed.title == CHARIZARD_JP
    assert any("Resolved the card title from OCR metadata fallback" in warning for warning in outcome.warnings)
    assert calls[0].title == "201/165"
    assert calls[-1].title == CHARIZARD_JP


def test_parse_image_uses_local_vision_when_tesseract_is_missing(tmp_path) -> None:
    image_path = tmp_path / "telegram-upload-charizard.jpg"
    image_path.write_bytes(b"fake-image")

    settings = AssistantSettings(
        monitor_db_path=str(tmp_path / "monitor.sqlite3"),
        openclaw_tesseract_path="C:/missing/tesseract.exe",
        openclaw_local_vision_model="qwen2.5vl:3b",
    )
    service = TcgImagePriceService(db_path=settings.monitor_db_path, settings=settings)

    class StubVisionClient:
        descriptor = "ollama:qwen2.5vl:3b"

        def analyze_card_image(
            self,
            image_path: Path,
            *,
            game_hint: str | None = None,
            title_hint: str | None = None,
        ) -> LocalVisionCardCandidate:
            return LocalVisionCardCandidate(
                backend="ollama",
                model="qwen2.5vl:3b",
                game="pokemon",
                title=CHARIZARD_JP,
                aliases=("Charizard ex",),
                card_number="201/165",
                rarity="SAR",
                set_code="sv2a",
                confidence=0.93,
            )

    service._local_vision_clients = (StubVisionClient(),)

    parsed = service.parse_image(image_path)

    assert service.is_available() is True
    assert parsed.status == "success"
    assert parsed.game == "pokemon"
    assert parsed.title == CHARIZARD_JP
    assert parsed.card_number == "201/165"
    assert parsed.rarity == "SAR"
    assert parsed.set_code == "sv2a"
    assert any("Applied local vision fallback via ollama:qwen2.5vl:3b." in warning for warning in parsed.warnings)


def test_parse_image_escalates_to_second_local_vision_model_when_first_is_incomplete(tmp_path) -> None:
    image_path = tmp_path / "telegram-upload-charizard.jpg"
    image_path.write_bytes(b"fake-image")

    settings = AssistantSettings(
        monitor_db_path=str(tmp_path / "monitor.sqlite3"),
        openclaw_tesseract_path="C:/missing/tesseract.exe",
        openclaw_local_vision_model="qwen2.5vl:3b,gemma3:4b",
    )
    service = TcgImagePriceService(db_path=settings.monitor_db_path, settings=settings)

    class FastButIncompleteClient:
        descriptor = "ollama:qwen2.5vl:3b"

        def analyze_card_image(
            self,
            image_path: Path,
            *,
            game_hint: str | None = None,
            title_hint: str | None = None,
        ) -> LocalVisionCardCandidate:
            return LocalVisionCardCandidate(
                backend="ollama",
                model="qwen2.5vl:3b",
                game="pokemon",
                title=CHARIZARD_JP,
                aliases=("Charizard ex",),
                card_number=None,
                rarity=None,
                set_code=None,
                confidence=0.72,
            )

    class SlowerCompleteClient:
        descriptor = "ollama:gemma3:4b"

        def analyze_card_image(
            self,
            image_path: Path,
            *,
            game_hint: str | None = None,
            title_hint: str | None = None,
        ) -> LocalVisionCardCandidate:
            return LocalVisionCardCandidate(
                backend="ollama",
                model="gemma3:4b",
                game="pokemon",
                title=CHARIZARD_JP,
                aliases=("Charizard ex",),
                card_number="201/165",
                rarity="SAR",
                set_code="sv2a",
                confidence=0.91,
            )

    service._local_vision_clients = (FastButIncompleteClient(), SlowerCompleteClient())

    parsed = service.parse_image(image_path)

    assert parsed.status == "success"
    assert parsed.title == CHARIZARD_JP
    assert parsed.card_number == "201/165"
    assert parsed.rarity == "SAR"
    assert parsed.set_code == "sv2a"
    assert any("Applied local vision fallback via ollama:gemma3:4b." in warning for warning in parsed.warnings)
