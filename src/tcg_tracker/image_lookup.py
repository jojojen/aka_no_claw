from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from assistant_runtime import AssistantSettings, get_settings

from .catalog import TcgCardSpec
from .hot_cards import TcgHotCardService
from .service import TcgLookupResult, TcgPriceService

logger = logging.getLogger(__name__)

POKEMON_NUMBER_RE = re.compile(r"(?P<number>\d{1,3}/\d{1,3})(?:\s*(?P<rarity>[A-Z]{1,5}))?", re.IGNORECASE)
WS_NUMBER_RE = re.compile(r"(?P<number>[A-Z0-9]{2,6}/[A-Z0-9]{1,6}-\d{2,3}[A-Z]{0,4})", re.IGNORECASE)
SET_CODE_RE = re.compile(r"\b(SVP|SV\d+[A-Z]?|M\d+[A-Z]?|SM\d+[A-Z]?|S\d+[A-Z]?|PJS|SMP|KMS)\b", re.IGNORECASE)
RARITY_RE = re.compile(
    r"\b(SSP|SEC\+|SEC|SAR|CSR|CHR|UR|SR|AR|RRR|RR|PR\+|PR|SP|OFR|SSP|SPM|SS|R|U|C|MA|MUR)\b",
    re.IGNORECASE,
)

FULL_TEXT_RARITY_MAP = {
    "SPECIAL ART RARE": "SAR",
    "ILLUSTRATION RARE": "AR",
}

BLOCKED_NAME_PATTERNS = (
    "POKEMON",
    "GEM MT",
    "PSA",
    "CERT",
    "TEXTURE",
    "SPECIAL ART RARE",
    "ILLUSTRATION RARE",
)


@dataclass(frozen=True, slots=True)
class ParsedCardImage:
    status: str
    game: str | None
    title: str | None
    aliases: tuple[str, ...]
    card_number: str | None
    rarity: str | None
    set_code: str | None
    raw_text: str
    extracted_lines: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def to_spec(self) -> TcgCardSpec | None:
        if self.game is None or self.title is None:
            return None
        return TcgCardSpec(
            game=self.game,
            title=self.title,
            card_number=self.card_number,
            rarity=self.rarity,
            set_code=self.set_code,
            aliases=self.aliases,
        )


@dataclass(frozen=True, slots=True)
class TcgImageLookupOutcome:
    status: str
    parsed: ParsedCardImage
    lookup_result: TcgLookupResult | None = None
    warnings: tuple[str, ...] = ()


class TcgImagePriceService:
    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        settings: AssistantSettings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._db_path = Path(db_path or self._settings.monitor_db_path)
        self._tesseract_path = _resolve_tesseract_path(self._settings.openclaw_tesseract_path)
        self._tessdata_dir = _resolve_tessdata_dir(self._settings.openclaw_tessdata_dir)
        self._workspace_temp_dir = Path.cwd() / ".openclaw_tmp"
        self._workspace_temp_dir.mkdir(parents=True, exist_ok=True)

    def is_available(self) -> bool:
        return self._tesseract_path is not None

    def lookup_image(
        self,
        image_path: str | Path,
        *,
        caption: str | None = None,
        game_hint: str | None = None,
        title_hint: str | None = None,
        persist: bool = False,
    ) -> TcgImageLookupOutcome:
        parsed = self.parse_image(
            image_path,
            caption=caption,
            game_hint=game_hint,
            title_hint=title_hint,
        )
        spec = parsed.to_spec()
        if parsed.status in {"unavailable", "unresolved"} or spec is None:
            return TcgImageLookupOutcome(
                status=parsed.status,
                parsed=parsed,
                warnings=parsed.warnings,
            )

        result = self._lookup_with_hot_card_fallback(spec, persist=persist)
        status = "success" if result.offers else "partial"
        return TcgImageLookupOutcome(
            status=status,
            parsed=parsed,
            lookup_result=result,
            warnings=parsed.warnings,
        )

    def parse_image(
        self,
        image_path: str | Path,
        *,
        caption: str | None = None,
        game_hint: str | None = None,
        title_hint: str | None = None,
    ) -> ParsedCardImage:
        resolved_path = Path(image_path)
        hint_game, hint_title = parse_image_caption_hints(caption)
        resolved_game_hint = game_hint or hint_game
        resolved_title_hint = title_hint or hint_title or _derive_title_hint_from_path(resolved_path)

        if self._tesseract_path is None:
            warning = (
                "Image OCR is unavailable on this host. Install Tesseract OCR and set "
                "OPENCLAW_TESSERACT_PATH in .env if it is not on PATH."
            )
            return ParsedCardImage(
                status="unavailable",
                game=resolved_game_hint,
                title=resolved_title_hint,
                aliases=(),
                card_number=None,
                rarity=None,
                set_code=None,
                raw_text="",
                extracted_lines=(),
                warnings=(warning,),
            )

        raw_text, extraction_warnings = self._extract_text(resolved_path)
        parsed = parse_tcg_ocr_text(
            raw_text,
            game_hint=resolved_game_hint,
            title_hint=resolved_title_hint,
        )
        warnings = (*parsed.warnings, *extraction_warnings)
        status = parsed.status
        if status == "success" and parsed.to_spec() is None:
            status = "unresolved"
        return ParsedCardImage(
            status=status,
            game=parsed.game,
            title=parsed.title,
            aliases=parsed.aliases,
            card_number=parsed.card_number,
            rarity=parsed.rarity,
            set_code=parsed.set_code,
            raw_text=parsed.raw_text,
            extracted_lines=parsed.extracted_lines,
            warnings=warnings,
        )

    def _extract_text(self, image_path: Path) -> tuple[str, tuple[str, ...]]:
        warnings: list[str] = []
        texts: list[str] = []
        pil_warning = None
        try:
            from PIL import Image, ImageFilter, ImageOps
        except ImportError:
            Image = None
            ImageFilter = None
            ImageOps = None
            pil_warning = "Pillow is not installed, so OCR fell back to the original image without region preprocessing."

        if pil_warning is not None:
            warnings.append(pil_warning)

        if Image is not None and ImageFilter is not None and ImageOps is not None:
            with Image.open(image_path) as opened:
                image = ImageOps.exif_transpose(opened)
                processed = ImageOps.autocontrast(image.convert("L"))
                processed = processed.filter(ImageFilter.UnsharpMask(radius=2, percent=180, threshold=3))
                width, height = processed.size
                regions = (
                    ("slab_top", processed.crop((0, 0, width, int(height * 0.24))), "eng", (11, 7)),
                    ("card_title", processed.crop((0, int(height * 0.20), width, int(height * 0.40))), "jpn+eng", (7, 11)),
                    ("card_body", processed.crop((0, int(height * 0.38), width, int(height * 0.78))), "jpn+eng", (6, 11)),
                    ("card_footer", processed.crop((0, int(height * 0.78), width, height)), "jpn+eng", (11, 6)),
                )
                temporary_paths: list[Path] = []
                try:
                    for region_name, region_image, language, psm_values in regions:
                        with tempfile.NamedTemporaryFile(
                            mode="w+b",
                            suffix=".png",
                            prefix=f"{region_name}-",
                            dir=self._workspace_temp_dir,
                            delete=False,
                        ) as handle:
                            region_path = Path(handle.name)
                        temporary_paths.append(region_path)
                        region_image.save(region_path)
                        for psm in psm_values:
                            text = self._run_tesseract(region_path, language=language, psm=psm)
                            if text:
                                texts.append(text)
                finally:
                    for temporary_path in temporary_paths:
                        try:
                            temporary_path.unlink(missing_ok=True)
                        except PermissionError:
                            logger.debug("Could not remove temporary OCR region path=%s", temporary_path)

        if not texts:
            for psm in (6, 11):
                text = self._run_tesseract(image_path, language="jpn+eng", psm=psm)
                if text:
                    texts.append(text)

        raw_text = "\n".join(_dedupe_preserve_order(_split_ocr_lines("\n".join(texts))))
        logger.debug(
            "Image OCR extracted path=%s warnings=%s text=%s",
            image_path,
            warnings,
            raw_text,
        )
        return raw_text, tuple(warnings)

    def _run_tesseract(self, image_path: Path, *, language: str, psm: int) -> str:
        if self._tesseract_path is None:
            return ""
        command = [
            self._tesseract_path,
            str(image_path),
            "stdout",
            "--oem",
            "1",
            "--psm",
            str(psm),
            "-l",
            language,
        ]
        if self._tessdata_dir is not None:
            command.extend(["--tessdata-dir", str(self._tessdata_dir)])
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        if completed.returncode != 0:
            logger.warning(
                "Tesseract OCR failed path=%s language=%s psm=%s stderr=%s",
                image_path,
                language,
                psm,
                completed.stderr.strip(),
            )
            return ""
        return completed.stdout.strip()

    def _lookup_with_hot_card_fallback(self, spec: TcgCardSpec, *, persist: bool) -> TcgLookupResult:
        service = TcgPriceService(db_path=self._db_path)
        initial = service.lookup(spec, persist=False)
        if initial.offers:
            return service.lookup(spec, persist=persist) if persist else initial

        try:
            resolved_spec = TcgHotCardService().resolve_lookup_spec(spec)
        except Exception:
            logger.exception("Image lookup hot-card fallback failed title=%s", spec.title)
            resolved_spec = None

        if resolved_spec is None:
            return service.lookup(spec, persist=persist) if persist else initial
        return service.lookup(resolved_spec, persist=persist)


def parse_image_caption_hints(caption: str | None) -> tuple[str | None, str | None]:
    if caption is None:
        return None, None
    content = caption.strip()
    if not content:
        return None, None

    lowered = content.lower()
    for prefix in ("/scan", "/image", "/photo"):
        if lowered.startswith(prefix):
            content = content[len(prefix):].strip()
            break

    if not content:
        return None, None

    tokens = content.split()
    if not tokens:
        return None, None

    game_hint = tokens[0].lower() if tokens[0].lower() in {"pokemon", "ws"} else None
    if game_hint is not None:
        title_hint = " ".join(tokens[1:]).strip() or None
        return game_hint, title_hint
    return None, content


def parse_tcg_ocr_text(
    raw_text: str,
    *,
    game_hint: str | None = None,
    title_hint: str | None = None,
) -> ParsedCardImage:
    normalized_text = unicodedata.normalize("NFKC", raw_text or "")
    lines = _split_ocr_lines(normalized_text)
    warnings: list[str] = []
    english_name = _pick_best_title(lines, prefer_japanese=False)
    preferred_name = _pick_best_title(lines, prefer_japanese=True)
    title = preferred_name or english_name
    if title_hint:
        title = title_hint

    card_number, number_rarity = _extract_card_number_and_rarity(lines)
    rarity = number_rarity or _extract_rarity(normalized_text)
    set_code = _extract_set_code(normalized_text)
    game = _detect_game(normalized_text, lines, game_hint=game_hint, card_number=card_number)

    aliases = tuple(
        alias
        for alias in _dedupe_preserve_order(
            [title_hint or "", english_name or ""]
        )
        if alias and alias != title
    )

    status = "success"
    if title is None:
        warnings.append("Could not confidently extract a card name from the image.")
        status = "unresolved"
    if game is None:
        warnings.append("Could not determine whether the card belongs to pokemon or ws.")
        status = "unresolved"

    return ParsedCardImage(
        status=status,
        game=game,
        title=title,
        aliases=aliases,
        card_number=card_number,
        rarity=rarity,
        set_code=set_code,
        raw_text=normalized_text,
        extracted_lines=tuple(lines),
        warnings=tuple(warnings),
    )


def _resolve_tesseract_path(configured_path: str | None) -> str | None:
    if configured_path:
        path = Path(configured_path)
        if path.exists():
            return str(path)
    discovered = shutil.which("tesseract")
    if discovered:
        return discovered
    return None


def _resolve_tessdata_dir(configured_path: str | None) -> Path | None:
    if configured_path:
        configured = Path(configured_path)
        if configured.exists():
            return configured
    local_default = Path.cwd() / ".openclaw_ocr" / "tessdata"
    if local_default.exists():
        return local_default
    return None


def _extract_card_number_and_rarity(lines: list[str]) -> tuple[str | None, str | None]:
    for line in lines:
        ws_match = WS_NUMBER_RE.search(line)
        if ws_match:
            return ws_match.group("number").upper(), None

        pokemon_match = POKEMON_NUMBER_RE.search(line.upper())
        if pokemon_match:
            card_number = pokemon_match.group("number").upper()
            rarity = pokemon_match.group("rarity")
            return card_number, rarity.upper() if rarity else None
    return None, None


def _extract_rarity(text: str) -> str | None:
    upper_text = text.upper()
    for phrase, normalized in FULL_TEXT_RARITY_MAP.items():
        if phrase in upper_text:
            return normalized
    match = RARITY_RE.search(upper_text)
    if match:
        return match.group(1).upper()
    return None


def _extract_set_code(text: str) -> str | None:
    match = SET_CODE_RE.search(text.upper())
    if not match:
        return None
    return match.group(1).lower()


def _detect_game(
    text: str,
    lines: list[str],
    *,
    game_hint: str | None,
    card_number: str | None,
) -> str | None:
    if game_hint in {"pokemon", "ws"}:
        return game_hint
    if card_number and WS_NUMBER_RE.fullmatch(card_number):
        return "ws"
    upper_text = text.upper()
    if "POKEMON" in upper_text or any("/" in line and re.search(r"\d/\d", line) for line in lines):
        return "pokemon"
    if any(WS_NUMBER_RE.search(line) for line in lines):
        return "ws"
    return None


def _pick_best_title(lines: list[str], *, prefer_japanese: bool) -> str | None:
    best_line = None
    best_score = -999
    for line in lines:
        cleaned = _clean_title_candidate(line)
        if not cleaned:
            continue
        score = _score_title_candidate(cleaned)
        has_japanese = _contains_japanese(cleaned)
        if prefer_japanese and has_japanese:
            score += 30
        if not prefer_japanese and has_japanese:
            score -= 20
        if "EX" in cleaned.upper():
            score += 10
        if any(pattern in cleaned.upper() for pattern in BLOCKED_NAME_PATTERNS):
            score -= 40
        if re.search(r"\d{2,}", cleaned):
            score -= 15
        if score > best_score:
            best_line = cleaned
            best_score = score
    return best_line if best_score > 0 else None


def _clean_title_candidate(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    candidate = re.sub(r"\s+", " ", candidate)
    candidate = candidate.strip("-_/ ")
    if len(candidate) < 3:
        return None
    if candidate.isdigit():
        return None
    return candidate


def _score_title_candidate(value: str) -> int:
    letters = sum(char.isalpha() for char in value)
    japanese = sum(
        1
        for char in value
        if "\u3040" <= char <= "\u30ff" or "\u4e00" <= char <= "\u9fff"
    )
    digits = sum(char.isdigit() for char in value)
    noisy = sum(char in {"|", "\\", "=", "_", ":", ";", ","} for char in value)
    words = [word for word in re.split(r"\s+", value) if word]
    long_words = sum(len(word) >= 3 for word in words)
    short_words = sum(len(word) == 1 for word in words)

    score = letters + japanese
    score += long_words * 4
    score -= digits * 2
    score -= noisy * 8
    score -= short_words * 3
    if "/" in value:
        score -= 6
    return score


def _title_looks_usable(value: str) -> bool:
    if len(value.strip()) < 4:
        return False
    signal = letters_and_japanese = sum(
        1
        for char in value
        if char.isalpha() or "\u3040" <= char <= "\u30ff" or "\u4e00" <= char <= "\u9fff"
    )
    total = max(len(value.strip()), 1)
    if signal / total < 0.55:
        return False
    if sum(char in {"|", "\\", "="} for char in value) >= 2:
        return False
    return True


def _derive_title_hint_from_path(image_path: Path) -> str | None:
    stem = image_path.stem.strip()
    if not stem:
        return None
    lowered = stem.lower()
    if lowered.startswith("telegram-upload-"):
        return None
    if lowered in {"image", "photo", "scan"}:
        return None
    return stem.replace("_", " ").replace("-", " ")


def _split_ocr_lines(value: str) -> list[str]:
    lines = [
        re.sub(r"\s+", " ", segment).strip()
        for segment in value.replace("\r", "\n").split("\n")
    ]
    return [line for line in lines if line]


def _contains_japanese(value: str) -> bool:
    return any(
        "\u3040" <= char <= "\u30ff" or "\u4e00" <= char <= "\u9fff"
        for char in value
    )


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if value and value not in deduped:
            deduped.append(value)
    return deduped
