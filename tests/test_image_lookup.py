from __future__ import annotations

from pathlib import Path

from assistant_runtime import AssistantSettings

from tcg_tracker.image_lookup import TcgImagePriceService, parse_image_caption_hints, parse_tcg_ocr_text


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
            "メガリザードンXex",
            "223/193 MA",
        ]
    )

    parsed = parse_tcg_ocr_text(raw_text)

    assert parsed.status == "success"
    assert parsed.game == "pokemon"
    assert parsed.title == "メガリザードンXex"
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
            "リーリエのピッピex",
            "126/100 SAR",
        ]
    )

    parsed = parse_tcg_ocr_text(raw_text)

    assert parsed.status == "success"
    assert parsed.game == "pokemon"
    assert parsed.title == "リーリエのピッピex"
    assert parsed.card_number == "126/100"
    assert parsed.rarity == "SAR"
    assert parsed.set_code == "sv9"


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
