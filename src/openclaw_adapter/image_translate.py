"""Image OCR + auto-language-detect translation to Traditional Chinese.

Two deliberate steps: the locally-configured Ollama vision model (qwen2.5vl)
transcribes the image verbatim (its strength), then the stronger text model
(qwen3:14b) detects the source language and translates to Traditional Chinese
(its strength). Language detection is done by the LLM, not a hardcoded table.
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from assistant_runtime import AssistantSettings, build_ssl_context

from .embedding_match import cosine, embed_unit_vectors, l2_normalize

VisionOcrFn = Callable[[Path], str]
TranslateFn = Callable[[str], "tuple[str, str]"]


@dataclass(frozen=True)
class ImageTranslateResult:
    """Outcome of OCR + translate for one image.

    On success: translation/source_language/ocr_text are populated. On failure
    (OCR error, no text, translation error) ok is False and `message` carries a
    ready-to-show friendly string; the Telegram layer renders the translation
    by default and keeps the OCR原文 behind a button, so they live as separate
    fields rather than one pre-formatted blob."""

    ok: bool
    source_language: str
    ocr_text: str
    translation: str
    message: str


ImageTranslateRenderer = Callable[[Path, "str | None"], ImageTranslateResult]
ImageTranslateCaptionRecognizer = Callable[["str | None"], bool]

_OCR_PROMPT = (
    "請逐字辨識這張圖片中的所有文字，原樣輸出。"
    "保留原本的換行、空白、數字、符號與網址（URL）。"
    "不要翻譯、不要解說、不要加任何前綴或引號，只輸出辨識到的文字。"
)

_TRANSLATE_INSTRUCTION = (
    "你是專業翻譯。先判斷下列原文的語言，再翻成自然、通順的繁體中文（台灣用語）。"
    "保留 URL、專有名詞、產品名。"
    '只輸出 JSON，格式為 {"source_language": "<原文語言的中文名稱>", '
    '"translation": "<繁體中文譯文>"}。不要輸出 JSON 以外的任何文字。'
)

NOT_CONFIGURED_MESSAGE = "圖片翻譯功能尚未啟用（本地視覺模型未設定）。"


def encode_image_base64(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def encode_image_for_vision(path: Path, *, max_side: int = 1280) -> str:
    """Downscale the image (longest side -> max_side) before base64-encoding.

    Full-resolution phone screenshots blow up the vision model's token count
    (qwen2.5vl runs at a small context window), which stalls OCR. Shrinking the
    longest side keeps screen text legible while making inference fast. Falls
    back to the raw bytes if Pillow is unavailable."""
    try:
        from PIL import Image
    except ImportError:
        return encode_image_base64(path)
    with Image.open(path) as im:
        im = im.convert("RGB")
        width, height = im.size
        longest = max(width, height)
        if longest > max_side:
            scale = max_side / longest
            im = im.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS)
        buffer = io.BytesIO()
        im.save(buffer, format="JPEG", quality=90)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def call_ollama_vision(
    *,
    endpoint: str,
    model: str,
    prompt: str,
    image_b64: str,
    timeout_seconds: int,
    ssl_context=None,
) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
    }
    request = Request(
        f"{endpoint.rstrip('/')}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds, context=ssl_context) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"視覺 LLM HTTP {exc.code}.") from exc
    except URLError as exc:
        raise RuntimeError(f"視覺 LLM request failed: {exc.reason}") from exc
    body = json.loads(raw)
    result = body.get("response", "")
    if not isinstance(result, str):
        raise RuntimeError(f"視覺 LLM response type was {type(result).__name__}.")
    return result.strip()


def call_ollama_translate_json(
    *,
    endpoint: str,
    model: str,
    text: str,
    timeout_seconds: int,
    ssl_context=None,
) -> tuple[str, str]:
    prompt = f"{_TRANSLATE_INSTRUCTION}\n\n原文：\n{text}\n\nJSON："
    payload = {
        "model": model,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2},
    }
    request = Request(
        f"{endpoint.rstrip('/')}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds, context=ssl_context) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"翻譯 LLM HTTP {exc.code}.") from exc
    except URLError as exc:
        raise RuntimeError(f"翻譯 LLM request failed: {exc.reason}") from exc
    body = json.loads(raw)
    response_text = body.get("response", "")
    if not isinstance(response_text, str):
        raise RuntimeError(f"翻譯 LLM response type was {type(response_text).__name__}.")
    return _parse_translation_json(response_text)


def _parse_translation_json(raw: str) -> tuple[str, str]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        # Model ignored the JSON contract — treat the whole response as the译文.
        return "未知", raw.strip()
    language = str(data.get("source_language") or "").strip() or "未知"
    translation = str(data.get("translation") or "").strip()
    if not translation:
        return language, raw.strip()
    return language, translation


def build_image_ocr_translate_renderer(
    *,
    vision_fn: VisionOcrFn,
    translate_fn: TranslateFn,
) -> ImageTranslateRenderer:
    def render(image_path: Path, caption: "str | None" = None) -> ImageTranslateResult:
        try:
            ocr_text = (vision_fn(Path(image_path)) or "").strip()
        except Exception as exc:  # noqa: BLE001 - renderer must never raise.
            return ImageTranslateResult(
                ok=False, source_language="", ocr_text="", translation="",
                message=f"圖片文字辨識失敗：{exc}",
            )
        if not ocr_text:
            return ImageTranslateResult(
                ok=False, source_language="", ocr_text="", translation="",
                message="這張圖片裡沒有辨識到任何文字。",
            )
        try:
            source_language, translation = translate_fn(ocr_text)
        except Exception as exc:  # noqa: BLE001 - keep OCR even if翻譯掛了.
            return ImageTranslateResult(
                ok=False, source_language="未知", ocr_text=ocr_text, translation="",
                message=f"翻譯失敗：{exc}\n\n【原文】\n{ocr_text}",
            )
        translation = (translation or "").strip() or "本地模型沒有回傳可用譯文。"
        source_language = (source_language or "未知").strip() or "未知"
        return ImageTranslateResult(
            ok=True, source_language=source_language, ocr_text=ocr_text,
            translation=translation, message="",
        )

    return render


def build_image_ocr_translate_renderer_from_settings(
    settings: AssistantSettings,
) -> "ImageTranslateRenderer | None":
    vision_backend = (settings.openclaw_local_vision_backend or "").strip().lower()
    vision_endpoint = settings.openclaw_local_vision_endpoint
    vision_model = settings.openclaw_local_vision_model
    text_backend = (settings.openclaw_local_text_backend or "").strip().lower()
    text_endpoint = settings.openclaw_local_text_endpoint
    text_model = next(
        (part.strip() for part in (settings.openclaw_local_text_model or "").split(",") if part.strip()),
        None,
    )
    if vision_backend != "ollama" or not vision_endpoint or not vision_model:
        return None
    if text_backend != "ollama" or not text_endpoint or not text_model:
        return None

    vision_timeout = max(1, settings.openclaw_local_vision_timeout_seconds)
    text_timeout = max(1, settings.openclaw_local_text_timeout_seconds)
    vision_ssl = build_ssl_context(settings) if vision_endpoint.startswith("https://") else None
    text_ssl = build_ssl_context(settings) if text_endpoint.startswith("https://") else None

    def vision_fn(image_path: Path) -> str:
        return call_ollama_vision(
            endpoint=vision_endpoint,
            model=vision_model,
            prompt=_OCR_PROMPT,
            image_b64=encode_image_for_vision(image_path),
            timeout_seconds=vision_timeout,
            ssl_context=vision_ssl,
        )

    def translate_fn(text: str) -> tuple[str, str]:
        return call_ollama_translate_json(
            endpoint=text_endpoint,
            model=text_model,
            text=text,
            timeout_seconds=max(text_timeout, 120),
            ssl_context=text_ssl,
        )

    return build_image_ocr_translate_renderer(vision_fn=vision_fn, translate_fn=translate_fn)


# Canonical ways a user asks "OCR + translate this image". These are EXAMPLE
# phrasings for a semantic (embedding) match, NOT a keyword table: a caption that
# means the same thing without these exact words (e.g.「這張圖寫什麼」) still
# matches by cosine similarity. The negatives anchor the other thing a photo
# caption commonly means — card price lookup — so a card query isn't mistaken for
# a translate request. (Rule G: open-world intent recognition via embeddings.)
_IMAGE_TRANSLATE_PHRASINGS = (
    "翻譯",
    "幫我把這張圖片的文字翻成中文",
    "這張圖上寫什麼 幫我翻譯",
    "翻譯這張截圖的內容",
    "translate the text in this image",
    "ocr this screenshot and translate it",
    "圖片裡的日文幫我翻成繁體中文",
    "這張圖寫什麼意思",
    "幫我看看這張圖寫什麼",
)
_IMAGE_TRANSLATE_NEGATIVE_PHRASINGS = (
    "查價",
    "幫我查這張卡多少錢",
    "這張卡的行情多少",
    "市價多少",
    "scan this card and check its price",
    "這是什麼卡 幫我估價",
)
_IMAGE_TRANSLATE_MIN_SCORE = 0.62
_IMAGE_TRANSLATE_MARGIN = 0.02


def build_image_translate_caption_recognizer(
    settings: AssistantSettings,
    *,
    embedder=None,
    min_score: float = _IMAGE_TRANSLATE_MIN_SCORE,
    margin: float = _IMAGE_TRANSLATE_MARGIN,
) -> "ImageTranslateCaptionRecognizer | None":
    """Return recognize(caption) -> bool deciding, via a local-embedding semantic
    match, whether a photo caption asks to OCR + translate the image.

    Returns None when no embedder is configured, so the caller can degrade to a
    keyword check. A caption is a translate request only when it scores at least
    `min_score` against the translate phrasings AND beats the card-price negatives
    by `margin` — so card lookups stay with the card pipeline."""
    if embedder is None:
        try:
            from .kb_embedder import build_kb_embedder
        except Exception:  # noqa: BLE001 - embedder optional; degrade to keyword.
            return None
        embedder = build_kb_embedder(settings)
    if embedder is None:
        return None

    positives = embed_unit_vectors(embedder, _IMAGE_TRANSLATE_PHRASINGS)
    if not positives:
        return None
    negatives = embed_unit_vectors(embedder, _IMAGE_TRANSLATE_NEGATIVE_PHRASINGS)

    def recognize(caption: "str | None") -> bool:
        if not caption or not caption.strip():
            return False
        try:
            qvec = embedder(caption.strip())
        except Exception:  # noqa: BLE001 - embed outage must not misroute.
            return False
        if not qvec:
            return False
        nq = l2_normalize(qvec)
        if nq is None:
            return False
        pos = max(cosine(nq, row) for row in positives)
        neg = max((cosine(nq, row) for row in negatives), default=0.0)
        return pos >= min_score and (pos - neg) >= margin

    return recognize
