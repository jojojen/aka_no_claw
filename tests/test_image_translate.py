from __future__ import annotations

import json
from pathlib import Path

from openclaw_adapter import image_translate
from openclaw_adapter.image_translate import (
    build_image_ocr_translate_renderer,
    call_ollama_vision,
    _parse_translation_json,
)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_call_ollama_vision_sends_base64_image_in_payload(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(request, timeout=None, context=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["url"] = request.full_url
        return _FakeResponse({"response": "辨識到的文字"})

    monkeypatch.setattr(image_translate, "urlopen", fake_urlopen)
    out = call_ollama_vision(
        endpoint="http://127.0.0.1:11434",
        model="qwen2.5vl:7b",
        prompt="ocr",
        image_b64="QUJD",
        timeout_seconds=30,
    )
    assert out == "辨識到的文字"
    assert captured["body"]["images"] == ["QUJD"]
    assert captured["body"]["model"] == "qwen2.5vl:7b"
    assert captured["url"].endswith("/api/generate")


def test_parse_translation_json_reads_language_and_translation() -> None:
    raw = '{"source_language": "日文", "translation": "你好"}'
    assert _parse_translation_json(raw) == ("日文", "你好")


def test_parse_translation_json_strips_code_fence() -> None:
    raw = '```json\n{"source_language": "英文", "translation": "哈囉"}\n```'
    assert _parse_translation_json(raw) == ("英文", "哈囉")


def test_parse_translation_json_falls_back_when_not_json() -> None:
    lang, translation = _parse_translation_json("這只是純文字翻譯")
    assert lang == "未知"
    assert translation == "這只是純文字翻譯"


def test_renderer_returns_structured_translation_and_ocr() -> None:
    renderer = build_image_ocr_translate_renderer(
        vision_fn=lambda path: "メルペイ\nhttps://help.jp.mercari.com/x",
        translate_fn=lambda text: ("日文", "Merpay\nhttps://help.jp.mercari.com/x"),
    )
    result = renderer(Path("/tmp/x.jpg"), "翻譯")
    assert result.ok
    assert result.source_language == "日文"
    assert result.ocr_text == "メルペイ\nhttps://help.jp.mercari.com/x"
    assert result.translation == "Merpay\nhttps://help.jp.mercari.com/x"
    # URL preserved verbatim in both the OCR and the translation.
    assert "https://help.jp.mercari.com/x" in result.ocr_text
    assert "https://help.jp.mercari.com/x" in result.translation


def test_renderer_keeps_ocr_when_translation_raises() -> None:
    def boom(_text: str):
        raise RuntimeError("llm down")

    renderer = build_image_ocr_translate_renderer(
        vision_fn=lambda path: "原始文字",
        translate_fn=boom,
    )
    result = renderer(Path("/tmp/x.jpg"), None)
    assert not result.ok
    assert "翻譯失敗" in result.message
    assert result.ocr_text == "原始文字"


def test_renderer_reports_when_no_text_detected() -> None:
    renderer = build_image_ocr_translate_renderer(
        vision_fn=lambda path: "   ",
        translate_fn=lambda text: ("x", "y"),
    )
    result = renderer(Path("/tmp/x.jpg"), "翻譯")
    assert not result.ok
    assert "沒有辨識到任何文字" in result.message


def _fake_embedder(text: str):
    """Map translate-ish text to one axis, card-price-ish text to another."""
    t = text.lower()
    translateish = any(k in t for k in ("翻", "translate", "ocr", "圖", "寫什麼", "截圖"))
    cardish = any(k in t for k in ("查", "價", "卡", "price", "估價", "行情", "市價", "scan"))
    if translateish and not cardish:
        return [1.0, 0.0, 0.0]
    if cardish and not translateish:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


def test_caption_recognizer_matches_paraphrase_not_card_query() -> None:
    recognize = image_translate.build_image_translate_caption_recognizer(
        object(), embedder=_fake_embedder
    )
    assert recognize is not None
    assert recognize("翻譯")
    assert recognize("這張圖寫什麼")  # paraphrase, no literal keyword
    assert not recognize("查價")
    assert not recognize("pokemon pikachu")
    assert not recognize(None)


def test_caption_recognizer_none_without_embedder() -> None:
    assert (
        image_translate.build_image_translate_caption_recognizer(object(), embedder=None)
        is None
    )


def test_caption_translation_keyword_detection() -> None:
    from openclaw_adapter.telegram_bot import _caption_requests_image_translation

    assert _caption_requests_image_translation("翻譯")
    assert _caption_requests_image_translation("幫我 OCR 這張")
    assert _caption_requests_image_translation("translate please")
    assert not _caption_requests_image_translation("查價")
    assert not _caption_requests_image_translation(None)


def test_composite_photo_renderer_routes_by_caption(monkeypatch) -> None:
    from price_monitor_bot.bot import TelegramPhotoQuery, PhotoLookupReply
    from openclaw_adapter import telegram_bot

    monkeypatch.setattr(
        telegram_bot, "default_photo_renderer", lambda settings, research_renderer=None: (lambda query: "CARD")
    )
    monkeypatch.setattr(
        telegram_bot,
        "build_image_ocr_translate_renderer_from_settings",
        lambda settings: (
            lambda image_path, caption=None: image_translate.ImageTranslateResult(
                ok=True, source_language="日文", ocr_text="メルペイ原文",
                translation="Merpay 譯文", message="",
            )
        ),
    )

    render = telegram_bot.build_photo_renderer(object())
    translate_q = TelegramPhotoQuery(chat_id="1", image_path=Path("/tmp/x.jpg"), caption="翻譯")
    card_q = TelegramPhotoQuery(chat_id="1", image_path=Path("/tmp/x.jpg"), caption="查價")

    translate_reply = render(translate_q)
    assert isinstance(translate_reply, PhotoLookupReply)
    # Default view shows the translation + detected language, NOT the原文.
    assert "Merpay 譯文" in translate_reply.text
    assert "偵測語言：日文" in translate_reply.text
    assert "メルペイ原文" not in translate_reply.text
    # A 顯示原文 button is attached.
    button = translate_reply.reply_markup["inline_keyboard"][0][0]
    assert button["text"] == "顯示原文"
    assert button["callback_data"].startswith("imgtr:")

    assert render(card_q) == "CARD"


def test_composite_renderer_returns_message_on_failure(monkeypatch) -> None:
    from price_monitor_bot.bot import TelegramPhotoQuery
    from openclaw_adapter import telegram_bot

    monkeypatch.setattr(
        telegram_bot, "default_photo_renderer", lambda settings, research_renderer=None: (lambda query: "CARD")
    )
    monkeypatch.setattr(
        telegram_bot,
        "build_image_ocr_translate_renderer_from_settings",
        lambda settings: (
            lambda image_path, caption=None: image_translate.ImageTranslateResult(
                ok=False, source_language="", ocr_text="", translation="",
                message="這張圖片裡沒有辨識到任何文字。",
            )
        ),
    )
    render = telegram_bot.build_photo_renderer(object())
    q = TelegramPhotoQuery(chat_id="1", image_path=Path("/tmp/x.jpg"), caption="翻譯")
    assert render(q) == "這張圖片裡沒有辨識到任何文字。"


def test_image_translate_button_callback_reveals_original() -> None:
    from openclaw_adapter import telegram_bot

    cache = telegram_bot._ImageTranslateOriginalCache()
    token = cache.put(chat_id="42", ocr_text="日文原文內容")
    handler = telegram_bot._build_image_translate_callback_handler(cache)

    toast, new_text, markup = handler(token, "Merpay 譯文", "42")
    assert "已顯示原文" in str(toast)
    assert "【原文】" in new_text and "日文原文內容" in new_text
    assert markup is None

    # Wrong chat_id (or expired token) → no原文 leaked.
    _toast2, new_text2, _m2 = handler(token, "Merpay 譯文", "99")
    assert new_text2 is None
