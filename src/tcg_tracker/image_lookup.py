from __future__ import annotations

import warnings as runtime_warnings
import logging
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

from assistant_runtime import AssistantSettings, get_settings
from market_monitor.models import MarketOffer
from market_monitor.normalize import normalize_card_number, normalize_text

from .catalog import TcgCardSpec
from .hot_cards import TcgHotCardService
from .local_vision import LocalVisionCardCandidate, LocalVisionTimeoutError, build_local_vision_clients
from .service import TcgLookupResult, TcgPriceService

logger = logging.getLogger(__name__)

POKEMON_NUMBER_RE = re.compile(r"(?P<number>\d{1,3}/\d{1,3})(?:\s*(?P<rarity>[A-Z]{1,5}))?", re.IGNORECASE)
POKEMON_PROMO_NUMBER_RE = re.compile(
    r"(?P<number>\d{1,3})\s*/\s*(?P<set_code>(?:SVP(?:\s*EN)?|SV-P|M-P|SM-P|S-P|XY-P|BW-P))\b",
    re.IGNORECASE,
)
POKEMON_PREFIX_PROMO_NUMBER_RE = re.compile(
    r"\b(?P<set_code>(?:SVP(?:\s*EN)?|SV-P|M-P|SM-P|S-P|XY-P|BW-P))\s*#?(?P<number>\d{1,3})\b",
    re.IGNORECASE,
)
POKEMON_NOISY_NUMBER_RE = re.compile(
    r"(?<!\d)(?P<number>\d{1,3})\s*(?P<separator>[/\\|)\]}>-]?)\s*(?P<denominator>\d{2,3})(?!\d)(?:\s*[/\\|)\]}>-]?\s*(?P<rarity>[A-Z]{1,5}))?",
    re.IGNORECASE,
)
WS_NUMBER_RE = re.compile(r"(?P<number>[A-Z0-9]{2,6}/[A-Z0-9]{1,6}-\d{2,3}[A-Z]{0,4})", re.IGNORECASE)
SET_CODE_RE = re.compile(
    r"\b(SVP(?:\s*EN)?|SV-P|MC(?:\s*JP)?|M-P|SM-P|S-P|XY-P|BW-P|SV\d{1,2}[A-Z]?|M\d{1,2}[A-Z]?|SM\d{1,2}[A-Z]?|S\d{1,2}[A-Z]?|PJS|SMP|KMS)\b",
    re.IGNORECASE,
)
RARITY_RE = re.compile(
    r"\b(SSP|SEC\+|SEC|SAR|CSR|CHR|UR|SR|AR|RRR|RR|PR\+|PR|SP|OFR|SSP|SPM|SS|R|U|C|MA|MUR)\b",
    re.IGNORECASE,
)
POKEMON_SPECIAL_DENOMINATORS = {742}
SLAB_RARITY_TOKENS = {
    "BGS",
    "BGS10",
    "BGS95",
    "BLACKLABEL",
    "CGC",
    "CGC10",
    "CGC95",
    "GEM",
    "GEMMT",
    "MINT",
    "PRISTINE",
    "PSA",
    "PSA10",
    "PSA9",
}
PROMO_SET_CODE_SUFFIXES = {
    "bw-p": "BW-P",
    "m-p": "M-P",
    "s-p": "S-P",
    "sm-p": "SM-P",
    "svp": "SV-P",
    "xy-p": "XY-P",
}

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
    "NINTENDO",
    "CREATURE",
    "GAME FREAK",
    "GAMEFREAK",
    "GAMEPRE",
    "COPYRIGHT",
)
BLOCKED_JAPANESE_TEXT_MARKERS = (
    "エネル",
    "エネルギー",
    "ダメージ",
    "トラッシュ",
    "サイド",
    "ベンチ",
    "相手",
    "えらび",
    "えらぶ",
    "きぜつ",
    "ルール",
    "ポケモンについている",
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
        self._easyocr_reader: object | None = None
        self._easyocr_reader_attempted = False
        self._local_vision_clients = build_local_vision_clients(self._settings)

    def is_available(self) -> bool:
        return self._tesseract_path is not None or bool(self._local_vision_clients)

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
        parsed, spec = self._prepare_lookup_spec(parsed)
        if parsed.status in {"unavailable", "unresolved"} or spec is None:
            return TcgImageLookupOutcome(
                status=parsed.status,
                parsed=parsed,
                warnings=parsed.warnings,
            )

        result = self._lookup_with_hot_card_fallback(spec, persist=persist)
        if not _parsed_matches_spec(parsed, result.spec):
            parsed = _apply_spec_to_parsed(
                parsed,
                result.spec,
                warning=(
                    "Resolved the card title from OCR metadata fallback: "
                    f"{result.spec.title} / {result.spec.card_number or 'n/a'} / {result.spec.rarity or 'n/a'}"
                ),
            )
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
        resolved_title_hint = title_hint or hint_title
        path_title_hint = _derive_title_hint_from_path(resolved_path)

        if self._tesseract_path is None and not self._local_vision_clients:
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

        raw_text = ""
        extraction_warnings: tuple[str, ...] = ()
        if self._tesseract_path is not None:
            raw_text, extraction_warnings = self._extract_text(resolved_path)
        elif self._local_vision_clients:
            extraction_warnings = (
                "Tesseract OCR was unavailable, so OpenClaw tried the configured local vision fallback instead.",
            )
        parsed = parse_tcg_ocr_text(
            raw_text,
            game_hint=resolved_game_hint,
            title_hint=resolved_title_hint,
        )
        if path_title_hint:
            parsed = _merge_path_title_hint(parsed, path_title_hint)
        warnings = [*parsed.warnings, *extraction_warnings]
        if self._should_try_local_vision(parsed):
            vision_candidate, vision_warnings = self._run_local_vision_fallback(
                resolved_path,
                game_hint=parsed.game or resolved_game_hint,
                title_hint=resolved_title_hint,
            )
            warnings.extend(vision_warnings)
            if vision_candidate is not None:
                parsed = _merge_local_vision_candidate(parsed, vision_candidate)
        warnings = _dedupe_preserve_order([*warnings, *parsed.warnings])
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
            warnings=tuple(warnings),
        )

    def _should_try_local_vision(self, parsed: ParsedCardImage) -> bool:
        if not self._local_vision_clients:
            return False
        if parsed.game is None:
            return True
        if parsed.card_number is None:
            return True
        if parsed.title is None:
            return True
        return not _title_looks_usable(parsed.title)

    def _run_local_vision_fallback(
        self,
        image_path: Path,
        *,
        game_hint: str | None,
        title_hint: str | None,
    ) -> tuple[LocalVisionCardCandidate | None, tuple[str, ...]]:
        if not self._local_vision_clients:
            return None, ()
        warnings: list[str] = []
        candidates: list[LocalVisionCardCandidate] = []
        for client in self._local_vision_clients:
            if getattr(client, "is_temporarily_disabled", lambda: False)():
                remaining = getattr(client, "cooldown_remaining_seconds", lambda: 0)()
                logger.info(
                    "Local vision fallback skipped image=%s backend=%s cooldown_seconds=%s",
                    image_path,
                    client.descriptor,
                    remaining,
                )
                warnings.append(
                    f"Local vision fallback via {client.descriptor} is cooling down after a recent timeout."
                )
                continue

            started_at = time.monotonic()
            logger.info(
                "Local vision fallback starting image=%s backend=%s game_hint=%s title_hint=%s",
                image_path,
                client.descriptor,
                game_hint,
                title_hint,
            )
            try:
                candidate = client.analyze_card_image(
                    image_path,
                    game_hint=game_hint,
                    title_hint=title_hint,
                )
            except LocalVisionTimeoutError as exc:
                descriptor = client.descriptor
                mark_timeout_cooldown = getattr(client, "mark_timeout_cooldown", None)
                if callable(mark_timeout_cooldown):
                    mark_timeout_cooldown()
                cooldown_seconds = getattr(client, "cooldown_remaining_seconds", lambda: 0)()
                logger.warning(
                    "Local vision fallback timed out image=%s backend=%s elapsed_seconds=%.2f timeout_seconds=%s cooldown_seconds=%s error=%s",
                    image_path,
                    descriptor,
                    time.monotonic() - started_at,
                    exc.timeout_seconds,
                    cooldown_seconds,
                    exc.detail,
                )
                warnings.append(
                    f"Local vision fallback via {descriptor} timed out and was put on cooldown."
                )
                continue
            except Exception:
                descriptor = client.descriptor
                logger.exception("Local vision fallback failed image=%s backend=%s", image_path, descriptor)
                warnings.append(f"Local vision fallback via {descriptor} failed.")
                continue

            logger.info(
                "Local vision fallback completed image=%s backend=%s elapsed_seconds=%.2f candidate_game=%s candidate_title=%s candidate_card_number=%s candidate_rarity=%s candidate_set_code=%s",
                image_path,
                client.descriptor,
                time.monotonic() - started_at,
                None if candidate is None else candidate.game,
                None if candidate is None else candidate.title,
                None if candidate is None else candidate.card_number,
                None if candidate is None else candidate.rarity,
                None if candidate is None else candidate.set_code,
            )

            if candidate is None:
                warnings.append(f"Local vision fallback via {client.descriptor} did not return a usable candidate.")
                continue

            candidate = _sanitize_local_vision_candidate(candidate)
            candidates.append(candidate)
            warnings.extend(candidate.warnings)
            if _local_vision_candidate_is_complete(candidate):
                break

        best_candidate = _select_best_local_vision_candidate(candidates)
        if best_candidate is None:
            return None, tuple(_dedupe_preserve_order(warnings))
        return best_candidate, tuple(_dedupe_preserve_order(warnings))

    def _prepare_lookup_spec(self, parsed: ParsedCardImage) -> tuple[ParsedCardImage, TcgCardSpec | None]:
        spec = parsed.to_spec()
        if parsed.status == "unavailable":
            return parsed, spec

        resolved_spec = self._resolve_spec_from_ocr_metadata(parsed)
        if resolved_spec is not None:
            return (
                _apply_spec_to_parsed(
                    parsed,
                    resolved_spec,
                    warning=(
                        "Resolved the card title from OCR metadata fallback: "
                        f"{resolved_spec.title} / {resolved_spec.card_number or 'n/a'} / {resolved_spec.rarity or 'n/a'}"
                    ),
                ),
                resolved_spec,
            )

        if spec is not None and _title_looks_usable(spec.title):
            return parsed, spec

        if spec is not None and parsed.status != "unresolved":
            return parsed, spec
        return parsed, None

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
                    ("card_code_strip", processed.crop((0, int(height * 0.82), width, int(height * 0.98))), "jpn+eng", (11, 6, 7)),
                )
                temporary_paths: list[Path] = []
                try:
                    for region_name, region_image, language, psm_values in regions:
                        scale_factors = (1, 4) if region_name == "card_code_strip" else (1,)
                        for scale_factor in scale_factors:
                            scaled_region = region_image
                            if scale_factor > 1:
                                scaled_region = region_image.resize(
                                    (region_image.width * scale_factor, region_image.height * scale_factor)
                                )
                            with tempfile.NamedTemporaryFile(
                                mode="w+b",
                                suffix=".png",
                                prefix=f"{region_name}-",
                                dir=self._workspace_temp_dir,
                                delete=False,
                            ) as handle:
                                region_path = Path(handle.name)
                            temporary_paths.append(region_path)
                            scaled_region.save(region_path)
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
        if self._should_try_easyocr(raw_text):
            easyocr_text, easyocr_warnings = self._run_easyocr_fallback(image_path)
            warnings.extend(easyocr_warnings)
            if easyocr_text:
                raw_text = "\n".join(
                    _dedupe_preserve_order(
                        _split_ocr_lines("\n".join([raw_text, easyocr_text]))
                    )
                )
        logger.debug(
            "Image OCR extracted path=%s warnings=%s text=%s",
            image_path,
            warnings,
            raw_text,
        )
        return raw_text, tuple(warnings)

    def _should_try_easyocr(self, raw_text: str) -> bool:
        if not raw_text.strip():
            return True
        parsed = parse_tcg_ocr_text(raw_text)
        if parsed.card_number is None:
            return True
        if parsed.title is None:
            return True
        return not _title_looks_usable(parsed.title)

    def _run_easyocr_fallback(self, image_path: Path) -> tuple[str, tuple[str, ...]]:
        reader = self._get_easyocr_reader()
        if reader is None:
            return "", ("EasyOCR fallback was unavailable while the initial OCR text still looked incomplete.",)

        try:
            from PIL import Image, ImageOps
        except ImportError:
            return "", ("Pillow is required for EasyOCR region preprocessing.",)

        lines: list[str] = []
        with Image.open(image_path) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            width, height = image.size
            regions = (
                image,
                image.crop((0, 0, width, int(height * 0.30))),
                image.crop((0, int(height * 0.76), width, height)),
            )
            temporary_paths: list[Path] = []
            try:
                for index, region in enumerate(regions, start=1):
                    if index == 3:
                        region = region.resize((region.width * 4, region.height * 4))
                    with tempfile.NamedTemporaryFile(
                        mode="w+b",
                        suffix=".png",
                        prefix=f"easyocr-{index}-",
                        dir=self._workspace_temp_dir,
                        delete=False,
                    ) as handle:
                        region_path = Path(handle.name)
                    temporary_paths.append(region_path)
                    region.save(region_path)
                    with runtime_warnings.catch_warnings():
                        runtime_warnings.filterwarnings("ignore", message=".*pin_memory.*")
                        detected = reader.readtext(str(region_path), detail=0, paragraph=False)
                    lines.extend(str(value).strip() for value in detected if str(value).strip())
            finally:
                for temporary_path in temporary_paths:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except PermissionError:
                        logger.debug("Could not remove temporary EasyOCR region path=%s", temporary_path)

        return "\n".join(_dedupe_preserve_order(lines)), ()

    def _get_easyocr_reader(self) -> object | None:
        if self._easyocr_reader_attempted:
            return self._easyocr_reader

        self._easyocr_reader_attempted = True
        try:
            import easyocr
        except ImportError:
            logger.debug("EasyOCR is not installed; skipping OCR fallback.")
            self._easyocr_reader = None
            return None
        except Exception:
            logger.exception("EasyOCR import failed unexpectedly.")
            self._easyocr_reader = None
            return None

        try:
            self._easyocr_reader = easyocr.Reader(["ja", "en"], gpu=False, verbose=False)
        except Exception:
            logger.exception("EasyOCR reader initialization failed.")
            self._easyocr_reader = None
        return self._easyocr_reader

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

        resolved_spec = self._resolve_spec_from_lookup_hints(spec)
        if resolved_spec is None:
            try:
                resolved_spec = TcgHotCardService().resolve_lookup_spec(spec)
            except Exception:
                logger.exception("Image lookup hot-card fallback failed title=%s", spec.title)
                resolved_spec = None

        if resolved_spec is None:
            return service.lookup(spec, persist=persist) if persist else initial
        return service.lookup(resolved_spec, persist=persist)

    def _resolve_spec_from_ocr_metadata(self, parsed: ParsedCardImage) -> TcgCardSpec | None:
        if parsed.game not in {"pokemon", "ws"}:
            return None
        if not any((parsed.card_number, parsed.rarity, parsed.set_code)):
            return None

        if parsed.card_number:
            direct_aliases = list(parsed.aliases)
            if parsed.title and _title_looks_usable(parsed.title) and parsed.title not in direct_aliases:
                direct_aliases.append(parsed.title)
            direct_specs = (
                TcgCardSpec(
                    game=parsed.game,
                    title=parsed.card_number,
                    card_number=parsed.card_number,
                    rarity=parsed.rarity,
                    set_code=parsed.set_code,
                    aliases=tuple(direct_aliases),
                ),
                TcgCardSpec(
                    game=parsed.game,
                    title=parsed.card_number,
                    card_number=parsed.card_number,
                    aliases=tuple(direct_aliases),
                ),
            )
            for direct_spec in direct_specs:
                initial = TcgPriceService(db_path=self._db_path).lookup(direct_spec, persist=False)
                if initial.offers:
                    inferred_spec = _infer_spec_from_offers(direct_spec, initial.offers)
                    if inferred_spec is not None and _inferred_spec_is_compatible_with_parsed(parsed, inferred_spec):
                        return inferred_spec

        candidate_title = (
            parsed.title
            if parsed.title and _title_looks_usable(parsed.title)
            else parsed.card_number or parsed.rarity or parsed.set_code or "ocr-partial"
        )
        candidate_spec = TcgCardSpec(
            game=parsed.game,
            title=candidate_title,
            card_number=parsed.card_number,
            rarity=parsed.rarity,
            set_code=parsed.set_code,
            aliases=parsed.aliases,
        )
        slab_number = _extract_slab_label_metadata(list(parsed.extracted_lines))[2]
        if candidate_spec.game == "pokemon" and candidate_spec.card_number is None and slab_number is not None:
            resolved_from_slab = _resolve_spec_from_slab_lookup_hints(candidate_spec, slab_number)
            if resolved_from_slab is not None:
                return resolved_from_slab
        return self._resolve_spec_from_lookup_hints(candidate_spec)

    def _resolve_spec_from_lookup_hints(self, spec: TcgCardSpec) -> TcgCardSpec | None:
        try:
            hints = TcgHotCardService().search_lookup_hints(spec, limit=2)
        except Exception:
            logger.exception("Image lookup hint resolution failed title=%s", spec.title)
            return None

        if not hints:
            return None

        best_hint = hints[0]
        if best_hint.confidence < 26.0:
            return None
        if len(hints) > 1 and hints[1].confidence >= best_hint.confidence - 6.0:
            return None

        aliases = list(spec.aliases)
        if _title_looks_usable(spec.title) and best_hint.title != spec.title and spec.title not in aliases:
            aliases.append(spec.title)
        matched_card_number = bool(
            spec.card_number
            and best_hint.card_number
            and normalize_card_number(spec.card_number) == normalize_card_number(best_hint.card_number)
        )
        return replace(
            spec,
            title=best_hint.title,
            card_number=spec.card_number or best_hint.card_number,
            rarity=best_hint.rarity if matched_card_number and best_hint.rarity else (spec.rarity or best_hint.rarity),
            set_code=best_hint.set_code if matched_card_number and best_hint.set_code else (spec.set_code or best_hint.set_code),
            aliases=tuple(aliases),
        )


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
    slab_title = _extract_slab_title(lines)
    slab_set_code, slab_rarity, _ = _extract_slab_label_metadata(lines)
    english_name = _pick_best_title(lines, prefer_japanese=False)
    preferred_name = _pick_best_title(lines, prefer_japanese=True)
    title = (
        preferred_name
        if preferred_name is not None and _title_looks_clean_japanese(preferred_name)
        else slab_title or english_name or preferred_name
    )
    if title_hint and _title_looks_usable(title_hint):
        title = title_hint

    card_number, number_rarity = _extract_card_number_and_rarity(lines)
    game = _detect_game(normalized_text, lines, game_hint=game_hint, card_number=card_number)
    extracted_rarity = number_rarity or _extract_rarity(normalized_text)
    rarity = _coalesce_rarity(game, extracted_rarity, slab_rarity)
    set_code = _extract_set_code(normalized_text) or slab_set_code

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
    promo_candidates: list[tuple[str, str | None, int]] = []
    exact_candidates: list[tuple[str, str | None, int]] = []
    noisy_candidates: list[tuple[str, str | None, int]] = []
    for line in lines:
        ws_match = WS_NUMBER_RE.search(line)
        if ws_match:
            return ws_match.group("number").upper(), None

        promo_candidates.extend(_extract_pokemon_promo_candidates(line))
        for pokemon_match in POKEMON_NUMBER_RE.finditer(line.upper()):
            card_number, rarity = _normalize_pokemon_number_candidate(
                pokemon_match.group("number"),
                pokemon_match.group("rarity"),
            )
            if card_number is None:
                continue
            score = 100
            if rarity is not None:
                score += 15
            if any(token in line.upper() for token in ("SAR", "SR", "MA", "AR", "UR")):
                score += 5
            exact_candidates.append((card_number, rarity, score))

        noisy_candidates.extend(_extract_noisy_pokemon_candidates(line))

    if promo_candidates:
        card_number, rarity, _ = max(promo_candidates, key=lambda item: item[2])
        return card_number, rarity
    if exact_candidates:
        card_number, rarity, _ = max(exact_candidates, key=lambda item: item[2])
        return card_number, rarity
    if noisy_candidates:
        card_number, rarity, _ = max(noisy_candidates, key=lambda item: item[2])
        return card_number, rarity
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
    return _canonicalize_pokemon_set_code(match.group(1))


def _extract_slab_label_metadata(lines: list[str]) -> tuple[str | None, str | None, str | None]:
    set_code = None
    rarity = None
    slab_number = None
    for index, line in enumerate(lines):
        upper_line = line.upper()
        if set_code is None and index < 12:
            set_code = _extract_slab_set_code(upper_line)
        if rarity is None:
            rarity = _extract_slab_label_rarity(upper_line)
        if slab_number is None and index < 12:
            match = re.search(r"#\s*(\d{1,3})\b", upper_line)
            if match:
                slab_number = str(int(match.group(1)))
        if set_code is not None and rarity is not None and slab_number is not None:
            break
    return set_code, rarity, slab_number


def _extract_slab_set_code(text: str) -> str | None:
    compact_text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text.upper()))
    match = re.search(r"POKEMON([A-Z0-9-]{1,6})JP\b", compact_text)
    if not match:
        return None
    normalized_token = _normalize_slab_set_code_token(match.group(1))
    return _canonicalize_pokemon_set_code(normalized_token)


def _normalize_slab_set_code_token(value: str) -> str:
    token = "".join(character for character in value.upper() if character.isalnum() or character == "-")
    if not token:
        return ""
    if token.startswith("SV"):
        suffix = token[2:].translate(str.maketrans({"O": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5"}))
        return f"SV{suffix}"
    if token.startswith("M"):
        suffix = token[1:].translate(str.maketrans({"O": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5"}))
        return f"M{suffix}"
    return token


def _extract_slab_label_rarity(text: str) -> str | None:
    upper_text = unicodedata.normalize("NFKC", text.upper())
    if "SPECIAL" in upper_text and any(token in upper_text for token in ("ART", "RARE", "AARE", "ATAARE")):
        return "SAR"
    if any(token in upper_text for token in ("ILLUSTRATION", "LLUSTRATION")) and any(
        token in upper_text for token in ("RARE", "AARE")
    ):
        return "AR"
    return None


def _coalesce_rarity(game: str | None, extracted_rarity: str | None, slab_rarity: str | None) -> str | None:
    if game == "pokemon" and slab_rarity is not None and extracted_rarity in {None, "SS"}:
        return slab_rarity
    return extracted_rarity or slab_rarity


def _extract_pokemon_promo_candidates(line: str) -> list[tuple[str, str | None, int]]:
    normalized_line = unicodedata.normalize("NFKC", line or "")
    candidates: list[tuple[str, str | None, int]] = []
    for pattern, bonus in (
        (POKEMON_PROMO_NUMBER_RE, 24),
        (POKEMON_PREFIX_PROMO_NUMBER_RE, 16),
    ):
        for match in pattern.finditer(normalized_line.upper()):
            set_code = _canonicalize_pokemon_set_code(match.group("set_code"))
            card_number = _build_pokemon_promo_card_number(match.group("number"), set_code)
            if card_number is None:
                continue
            rarity = _extract_rarity(normalized_line)
            score = 105 + bonus
            if rarity is not None:
                score += 6
            candidates.append((card_number, rarity, score))
    return candidates


def _extract_noisy_pokemon_candidates(line: str) -> list[tuple[str, str | None, int]]:
    upper_line = line.upper()
    candidates: list[tuple[str, str | None, int]] = []
    for noisy_match in POKEMON_NOISY_NUMBER_RE.finditer(upper_line):
        prefix = upper_line[: noisy_match.start()]
        suffix = upper_line[noisy_match.end() :]
        has_code_context = bool(_extract_set_code(prefix) or _extract_set_code(suffix))
        rarity = noisy_match.group("rarity")
        if rarity is None:
            rarity = _extract_rarity(f"{prefix} {suffix}")
        has_rarity_context = rarity is not None
        separator = noisy_match.group("separator") or ""
        has_separator = bool(separator)
        total_digits = len(noisy_match.group("number")) + len(noisy_match.group("denominator"))
        if not has_separator and total_digits < 6:
            continue
        if not (has_separator or has_code_context or has_rarity_context):
            continue

        card_number, normalized_rarity = _normalize_pokemon_number_candidate(
            f"{noisy_match.group('number')}/{noisy_match.group('denominator')}",
            rarity,
        )
        if card_number is None:
            continue

        denominator = int(card_number.split("/", 1)[1])
        score = 40
        if has_separator:
            score += 20
        if has_code_context:
            score += 15
        if normalized_rarity is not None:
            score += 12
        if denominator in {86, 100, 165, 190, 193}:
            score += 8
        candidates.append((card_number, normalized_rarity, score))
    return candidates


def _normalize_pokemon_number_candidate(
    card_number: str,
    rarity: str | None,
) -> tuple[str | None, str | None]:
    match = POKEMON_NUMBER_RE.search(card_number.upper())
    if match is None:
        return None, None

    numerator, denominator = match.group("number").split("/", 1)
    numerator_width = len(numerator)
    denominator_width = len(denominator)
    numerator_value = int(numerator)
    denominator_value = int(denominator)
    if numerator_value == 0 or denominator_value == 0:
        return None, None
    if denominator_value > 300 and denominator.endswith("00"):
        denominator = "100"
        denominator_value = 100
    if denominator_value > 300 and denominator_value not in POKEMON_SPECIAL_DENOMINATORS:
        return None, None
    normalized_rarity = rarity.upper() if rarity and RARITY_RE.fullmatch(rarity.upper()) else None
    return (
        f"{numerator_value:0{numerator_width}d}/{denominator_value:0{len(denominator)}d}",
        normalized_rarity,
    )


def _canonicalize_pokemon_set_code(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = "".join(
        character for character in unicodedata.normalize("NFKC", value).upper()
        if character.isalnum() or character == "-"
    )
    if not collapsed:
        return None
    aliases = {
        "BW-P": "bw-p",
        "MC": "mc",
        "MCJP": "mc",
        "M-P": "m-p",
        "S-P": "s-p",
        "SM-P": "sm-p",
        "SV-P": "svp",
        "SVP": "svp",
        "SVPEN": "svp",
        "XY-P": "xy-p",
    }
    return aliases.get(collapsed, collapsed.lower())


def _build_pokemon_promo_card_number(number: str, set_code: str | None) -> str | None:
    if set_code is None:
        return None
    suffix = PROMO_SET_CODE_SUFFIXES.get(set_code)
    if suffix is None:
        return None
    try:
        value = int(number)
    except ValueError:
        return None
    return f"{value:03d}/{suffix}"


def _normalize_pokemon_card_number_value(value: str | None, *, set_code_hint: str | None = None) -> str | None:
    normalized = unicodedata.normalize("NFKC", value or "").strip()
    if not normalized:
        return None

    for pattern in (POKEMON_PROMO_NUMBER_RE, POKEMON_PREFIX_PROMO_NUMBER_RE):
        match = pattern.search(normalized.upper())
        if match:
            card_number = _build_pokemon_promo_card_number(
                match.group("number"),
                _canonicalize_pokemon_set_code(match.group("set_code")),
            )
            if card_number is not None:
                return card_number

    collapsed = normalize_card_number(normalized).lstrip("#")
    if not collapsed:
        return None

    if "/" in collapsed:
        numerator, suffix = collapsed.split("/", 1)
        promo_card_number = _build_pokemon_promo_card_number(numerator, _canonicalize_pokemon_set_code(suffix))
        if promo_card_number is not None:
            return promo_card_number
        normalized_number, _ = _normalize_pokemon_number_candidate(collapsed, None)
        if normalized_number is not None:
            return normalized_number
        return collapsed

    if collapsed.isdigit():
        promo_card_number = _build_pokemon_promo_card_number(collapsed, set_code_hint)
        if promo_card_number is not None:
            return promo_card_number
        return str(int(collapsed))
    return collapsed


def _detect_game(
    text: str,
    lines: list[str],
    *,
    game_hint: str | None,
    card_number: str | None,
) -> str | None:
    if game_hint in {"pokemon", "ws"}:
        return game_hint
    upper_text = text.upper()
    has_ws_number = bool(card_number and WS_NUMBER_RE.fullmatch(card_number)) or any(WS_NUMBER_RE.search(line) for line in lines)
    has_ws_keyword = any(token in upper_text for token in ("WEISS", "SCHWARZ", "WS/")) or any(
        token in text for token in ("ヴァイス", "シュヴァルツ")
    )
    has_pokemon_keyword = "POKEMON" in upper_text or any(token in text for token in ("ポケモン", "ポケカ"))
    has_complete_pokemon_number = bool(card_number and _pokemon_card_number_looks_complete(card_number))
    has_pokemon_number_signal = any(
        POKEMON_PROMO_NUMBER_RE.search(line) or POKEMON_NUMBER_RE.search(line) or POKEMON_NOISY_NUMBER_RE.search(line)
        for line in lines
    )

    if has_ws_number or (has_ws_keyword and not has_pokemon_keyword):
        return "ws"
    if has_pokemon_keyword:
        return "pokemon"
    if has_complete_pokemon_number and not has_ws_keyword:
        return "pokemon"
    if has_ws_keyword:
        return "ws"
    if has_pokemon_number_signal:
        return "pokemon"
    return None


def _pick_best_title(lines: list[str], *, prefer_japanese: bool) -> str | None:
    best_line = None
    best_score = -999
    for index, line in enumerate(lines):
        cleaned = _clean_title_candidate(line)
        if not cleaned:
            continue
        if not _title_looks_usable(cleaned):
            continue
        if _is_blocked_title_candidate(cleaned):
            continue
        score = _score_title_candidate(cleaned)
        has_japanese = _contains_japanese(cleaned)
        score += max(0, 18 - (index * 2))
        if prefer_japanese and has_japanese:
            score += 30
        if not prefer_japanese and has_japanese:
            score -= 20
        if "EX" in cleaned.upper():
            score += 10
        if re.search(r"\d{2,}", cleaned):
            score -= 15
        if score > best_score:
            best_line = cleaned
            best_score = score
    return best_line if best_score > 0 else None


def _extract_slab_title(lines: list[str]) -> str | None:
    for index, line in enumerate(lines):
        upper_line = line.upper()
        if "POKEMON" not in upper_line:
            continue
        if index > 6:
            continue
        for offset in range(1, 4):
            if index + offset >= len(lines):
                break
            candidate = _clean_title_candidate(lines[index + offset].lstrip("| "))
            if not candidate:
                continue
            upper_candidate = candidate.upper()
            if any(token in upper_candidate for token in ("GEM MT", "SPECIAL ART RARE", "ILLUSTRATION RARE")):
                continue
            if not any(character.isalpha() for character in candidate):
                continue
            if "EX" in upper_candidate or "'" in candidate or len(candidate) >= 12:
                return candidate
    return None


def _title_looks_clean_japanese(value: str) -> bool:
    if not _title_looks_usable(value):
        return False
    if not _contains_japanese(value):
        return False
    if len(value) > 22:
        return False
    if " " in value:
        return False
    if any(token in value for token in ("@", "【", "】", "『", "』", ";")):
        return False
    if re.search(r"[A-DF-WYZa-df-wyz]", value):
        return False
    return True


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
    if len(value) > 18:
        score -= (len(value) - 18) * 2
    if re.search(r"[。.!?]$", value):
        score -= 18
    if "、" in value:
        score -= 8
    if "/" in value:
        score -= 6
    return score


def _is_blocked_title_candidate(value: str) -> bool:
    upper_value = value.upper()
    if any(pattern in upper_value for pattern in BLOCKED_NAME_PATTERNS):
        return True
    if re.search(r"20\d{2}", upper_value) and any(
        token in upper_value for token in ("POKEMON", "NINTENDO", "CREATURE", "GAME")
    ):
        return True
    return False


def _title_looks_usable(value: str) -> bool:
    stripped = value.strip()
    if len(stripped) < 4:
        return False
    if _is_blocked_title_candidate(value):
        return False
    signal = letters_and_japanese = sum(
        1
        for char in value
        if char.isalpha() or "\u3040" <= char <= "\u30ff" or "\u4e00" <= char <= "\u9fff"
    )
    total = max(len(stripped), 1)
    if signal / total < 0.55:
        return False
    if sum(char in {"|", "\\", "="} for char in value) >= 2:
        return False
    if len(stripped) > 30:
        return False
    if re.search(r"[。.!?]$", stripped):
        return False
    return True


def _is_blocked_title_candidate_v2(value: str) -> bool:
    upper_value = value.upper()
    normalized_value = value.strip()
    if any(pattern in upper_value for pattern in BLOCKED_NAME_PATTERNS):
        return True
    if normalized_value in {"ヴァイスシュヴァルツ", "ヴァイス", "ポケモンカード", "ポケモンカードゲーム", "Weiss Schwarz"}:
        return True
    if any(marker in value for marker in BLOCKED_JAPANESE_TEXT_MARKERS):
        return True
    if re.search(r"20\d{2}", upper_value) and any(
        token in upper_value for token in ("POKEMON", "NINTENDO", "CREATURE", "GAME")
    ):
        return True
    return False


def _title_looks_usable_v2(value: str) -> bool:
    stripped = value.strip()
    if len(stripped) < 4:
        return False
    if _is_blocked_title_candidate_v2(value):
        return False
    signal = sum(
        1
        for char in value
        if char.isalpha() or "\u3040" <= char <= "\u30ff" or "\u4e00" <= char <= "\u9fff"
    )
    total = max(len(stripped), 1)
    if signal / total < 0.55:
        return False
    if not _contains_japanese(stripped):
        latin_words = re.findall(r"[A-Za-z']+", stripped)
        if latin_words:
            total_letters = sum(len(word) for word in latin_words)
            vowel_count = sum(1 for word in latin_words for char in word.lower() if char in {"a", "e", "i", "o", "u"})
            short_word_count = sum(1 for word in latin_words if len(word) <= 3)
            if total_letters >= 6:
                vowel_ratio = vowel_count / total_letters
                if vowel_ratio > 0.72:
                    return False
            if len(latin_words) >= 4 and short_word_count >= len(latin_words) - 1:
                return False
            if len(latin_words) == 1 and "-" in stripped and not re.search(
                r"\b(EX|GX|VMAX|VSTAR|LV\.?\d+)\b",
                stripped.upper(),
            ):
                return False
    if sum(char in {"|", "\\", "="} for char in value) >= 2:
        return False
    if len(stripped) > 30:
        return False
    if re.search(r"[。.!?]$", stripped):
        return False
    return True


_is_blocked_title_candidate = _is_blocked_title_candidate_v2
_title_looks_usable = _title_looks_usable_v2


def _merge_local_vision_candidate(
    parsed: ParsedCardImage,
    candidate: LocalVisionCardCandidate,
) -> ParsedCardImage:
    candidate = _sanitize_local_vision_candidate(candidate)
    metadata_compatible = _local_vision_metadata_is_compatible(parsed, candidate)
    aliases = _dedupe_preserve_order([*parsed.aliases, *candidate.aliases])
    title = parsed.title
    if metadata_compatible and _should_prefer_local_vision_title(parsed.title, candidate.title):
        title = candidate.title
    elif title is None and candidate.title is not None:
        title = candidate.title

    parsed_title_usable = bool(parsed.title and _title_looks_usable(parsed.title))
    candidate_title_usable = bool(candidate.title and _title_looks_usable(candidate.title))
    if parsed_title_usable and candidate_title_usable and normalize_text(parsed.title or "") != normalize_text(candidate.title or ""):
        if candidate.title not in aliases:
            aliases.append(candidate.title)
    if parsed.title and title != parsed.title and parsed.title not in aliases:
        aliases.append(parsed.title)

    game = parsed.game or candidate.game
    card_number = parsed.card_number
    if metadata_compatible:
        if _card_number_quality(game, candidate.card_number) > _card_number_quality(game, parsed.card_number):
            card_number = candidate.card_number
        elif card_number is None:
            card_number = candidate.card_number

    rarity = parsed.rarity
    if metadata_compatible:
        if _rarity_quality(parsed.rarity) < _rarity_quality(candidate.rarity):
            rarity = candidate.rarity
        elif rarity is None:
            rarity = candidate.rarity

    set_code = parsed.set_code
    if metadata_compatible:
        if _set_code_quality(candidate.set_code) > _set_code_quality(parsed.set_code):
            set_code = candidate.set_code
        elif set_code is None:
            set_code = candidate.set_code

    applied_warning = f"Applied local vision fallback via {candidate.descriptor}."
    if not metadata_compatible:
        applied_warning = (
            f"Applied local vision fallback via {candidate.descriptor}, "
            "but ignored conflicting card metadata because the detected title did not line up."
        )
    warnings = _dedupe_preserve_order([*parsed.warnings, *candidate.warnings, applied_warning])
    status = "success" if game is not None and title is not None else "unresolved"
    return replace(
        parsed,
        status=status,
        game=game,
        title=title,
        aliases=tuple(aliases),
        card_number=card_number,
        rarity=rarity,
        set_code=set_code,
        warnings=tuple(warnings),
    )


def _select_best_local_vision_candidate(
    candidates: list[LocalVisionCardCandidate],
) -> LocalVisionCardCandidate | None:
    if not candidates:
        return None

    best = max(
        candidates,
        key=lambda candidate: (
            _score_local_vision_candidate(candidate),
            candidate.confidence or 0.0,
            len(candidate.title or ""),
        ),
    )

    merged = best
    for candidate in candidates:
        if candidate is best:
            continue
        if not _local_vision_candidates_are_compatible(merged, candidate):
            continue
        merged = _merge_local_vision_candidates(merged, candidate)
    return merged


def _local_vision_candidate_is_complete(candidate: LocalVisionCardCandidate) -> bool:
    if candidate.game not in {"pokemon", "ws"}:
        return False
    if candidate.title is None or not _title_looks_usable(candidate.title):
        return False
    if candidate.game == "pokemon":
        return _pokemon_card_number_looks_complete(candidate.card_number)
    return bool(candidate.card_number and WS_NUMBER_RE.fullmatch(candidate.card_number))


def _score_local_vision_candidate(candidate: LocalVisionCardCandidate) -> float:
    score = 0.0
    if candidate.game in {"pokemon", "ws"}:
        score += 20.0
    if candidate.title and _title_looks_usable(candidate.title):
        score += 40.0
    elif candidate.title:
        score += 8.0
    score += _card_number_quality(candidate.game, candidate.card_number) * 10.0
    if _rarity_quality(candidate.rarity) > 0:
        score += 10.0
    if _set_code_quality(candidate.set_code) > 0:
        score += 8.0
    if candidate.title and _contains_japanese(candidate.title):
        score += 4.0
    if candidate.confidence is not None:
        score += max(0.0, min(candidate.confidence, 1.0)) * 10.0
    return score


def _local_vision_candidates_are_compatible(
    left: LocalVisionCardCandidate,
    right: LocalVisionCardCandidate,
) -> bool:
    if left.game and right.game and left.game != right.game:
        return False
    if left.card_number and right.card_number:
        return normalize_card_number(left.card_number) == normalize_card_number(right.card_number)
    if left.title and right.title:
        return normalize_text(left.title) == normalize_text(right.title)
    return True


def _merge_local_vision_candidates(
    primary: LocalVisionCardCandidate,
    secondary: LocalVisionCardCandidate,
) -> LocalVisionCardCandidate:
    aliases = _dedupe_preserve_order([*primary.aliases, *secondary.aliases])
    title = primary.title
    if _should_prefer_local_vision_title(title, secondary.title):
        title = secondary.title
    elif title and secondary.title and normalize_text(title) != normalize_text(secondary.title):
        aliases.append(secondary.title)

    confidence = primary.confidence
    if confidence is None or ((secondary.confidence or 0.0) > confidence):
        confidence = secondary.confidence

    warnings = _dedupe_preserve_order([*primary.warnings, *secondary.warnings])
    return LocalVisionCardCandidate(
        backend=primary.backend,
        model=primary.model,
        game=primary.game or secondary.game,
        title=title,
        aliases=tuple(_dedupe_preserve_order(aliases)),
        card_number=primary.card_number if _card_number_quality(primary.game, primary.card_number) >= _card_number_quality(primary.game, secondary.card_number) else secondary.card_number,
        rarity=primary.rarity if _rarity_quality(primary.rarity) >= _rarity_quality(secondary.rarity) else secondary.rarity,
        set_code=primary.set_code if _set_code_quality(primary.set_code) >= _set_code_quality(secondary.set_code) else secondary.set_code,
        confidence=confidence,
        raw_response=primary.raw_response or secondary.raw_response,
        warnings=tuple(warnings),
    )


def _sanitize_local_vision_candidate(candidate: LocalVisionCardCandidate) -> LocalVisionCardCandidate:
    if candidate.game != "pokemon":
        return candidate
    set_code = _canonicalize_pokemon_set_code(candidate.set_code)
    card_number = _normalize_pokemon_card_number_value(candidate.card_number, set_code_hint=set_code)
    rarity = candidate.rarity.upper() if candidate.rarity else None
    if rarity in SLAB_RARITY_TOKENS:
        rarity = None
    if card_number and "/742" in card_number:
        rarity = None
    if card_number and _pokemon_card_number_looks_complete(card_number) and any(card_number.endswith(f"/{suffix}") for suffix in PROMO_SET_CODE_SUFFIXES.values()):
        rarity = None if rarity in {"P", "PROMO"} else rarity
    return replace(
        candidate,
        card_number=card_number,
        rarity=rarity,
        set_code=set_code,
    )


def _pokemon_card_number_looks_complete(card_number: str | None) -> bool:
    normalized = _normalize_pokemon_card_number_value(card_number)
    if normalized is None or "/" not in normalized:
        return False
    numerator, suffix = normalized.split("/", 1)
    if not numerator.isdigit():
        return False
    if suffix.isdigit():
        return _normalize_pokemon_number_candidate(normalized, None)[0] is not None
    return any(normalized.endswith(f"/{promo_suffix}") for promo_suffix in PROMO_SET_CODE_SUFFIXES.values())


def _card_number_quality(game: str | None, card_number: str | None) -> int:
    if not card_number:
        return 0
    normalized = normalize_card_number(card_number)
    if game == "pokemon":
        if _pokemon_card_number_looks_complete(normalized):
            return 4
        if "/" in normalized:
            return 2
        if normalized.isdigit():
            return 1
        return 1
    if game == "ws":
        return 4 if WS_NUMBER_RE.fullmatch(normalized) else 1
    return 1


def _rarity_quality(rarity: str | None) -> int:
    if rarity is None:
        return 0
    return 0 if rarity.upper() in SLAB_RARITY_TOKENS else 2


def _set_code_quality(set_code: str | None) -> int:
    if not set_code:
        return 0
    return 2 if len(set_code) <= 6 else 1


def _should_prefer_local_vision_title(parsed_title: str | None, candidate_title: str | None) -> bool:
    if candidate_title is None or not _title_looks_usable(candidate_title):
        return False
    if parsed_title is None or not _title_looks_usable(parsed_title):
        return True
    parsed_score = _score_title_candidate(parsed_title)
    candidate_score = _score_title_candidate(candidate_title)
    if candidate_score >= parsed_score + 8:
        return True
    if _contains_japanese(candidate_title) and not _contains_japanese(parsed_title) and candidate_score >= parsed_score:
        return True
    return False


def _merge_path_title_hint(parsed: ParsedCardImage, path_title_hint: str) -> ParsedCardImage:
    if not path_title_hint:
        return parsed

    aliases = list(parsed.aliases)
    if parsed.title is None and _title_looks_usable(path_title_hint):
        return replace(parsed, title=path_title_hint, aliases=tuple(aliases))

    normalized_hint = normalize_text(path_title_hint)
    normalized_title = normalize_text(parsed.title or "")
    if normalized_hint and normalized_hint != normalized_title and path_title_hint not in aliases:
        aliases.append(path_title_hint)
    return replace(parsed, aliases=tuple(aliases))


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


def _local_vision_metadata_is_compatible(
    parsed: ParsedCardImage,
    candidate: LocalVisionCardCandidate,
) -> bool:
    if not (parsed.title and _title_looks_usable(parsed.title)):
        return True
    if not (candidate.title and _title_looks_usable(candidate.title)):
        return True

    parsed_name = normalize_text(parsed.title)
    candidate_name = normalize_text(candidate.title)
    if not parsed_name or not candidate_name:
        return True
    return (
        parsed_name == candidate_name
        or parsed_name in candidate_name
        or candidate_name in parsed_name
    )


def _resolve_spec_from_slab_lookup_hints(spec: TcgCardSpec, slab_number: str) -> TcgCardSpec | None:
    try:
        hints = TcgHotCardService().search_lookup_hints(spec, limit=5)
    except Exception:
        logger.exception("Image lookup slab hint resolution failed title=%s", spec.title)
        return None

    matches = [
        hint
        for hint in hints
        if _lookup_hint_matches_slab_number(hint, slab_number) and _lookup_hint_matches_spec_metadata(spec, hint)
    ]
    if not matches:
        return None

    best_hint = max(
        matches,
        key=lambda hint: (
            hint.confidence,
            bool(hint.card_number),
            bool(hint.rarity),
            bool(hint.set_code),
        ),
    )
    return replace(
        spec,
        title=best_hint.title,
        card_number=best_hint.card_number or spec.card_number,
        rarity=best_hint.rarity or spec.rarity,
        set_code=best_hint.set_code or spec.set_code,
    )


def _lookup_hint_matches_slab_number(hint, slab_number: str) -> bool:
    card_number = normalize_card_number(hint.card_number or "")
    if "/" not in card_number:
        return False
    numerator, _ = card_number.split("/", 1)
    if not numerator.isdigit():
        return False
    return int(numerator) == int(slab_number)


def _lookup_hint_matches_spec_metadata(spec: TcgCardSpec, hint) -> bool:
    if spec.set_code and hint.set_code and normalize_text(spec.set_code) != normalize_text(hint.set_code):
        return False
    if spec.rarity and hint.rarity and normalize_text(spec.rarity) != normalize_text(hint.rarity):
        return False
    return True


def _parsed_matches_spec(parsed: ParsedCardImage, spec: TcgCardSpec) -> bool:
    return (
        parsed.game == spec.game
        and parsed.title == spec.title
        and parsed.card_number == spec.card_number
        and parsed.rarity == spec.rarity
        and parsed.set_code == spec.set_code
    )


def _apply_spec_to_parsed(
    parsed: ParsedCardImage,
    spec: TcgCardSpec,
    *,
    warning: str | None = None,
) -> ParsedCardImage:
    warnings = list(parsed.warnings)
    if warning and warning not in warnings:
        warnings.append(warning)

    aliases = _dedupe_preserve_order([*parsed.aliases, *spec.aliases])
    return replace(
        parsed,
        status="success",
        game=spec.game,
        title=spec.title,
        aliases=tuple(aliases),
        card_number=spec.card_number,
        rarity=spec.rarity,
        set_code=spec.set_code,
        warnings=tuple(warnings),
    )


def _infer_spec_from_offers(base_spec: TcgCardSpec, offers: tuple[MarketOffer, ...] | list[MarketOffer]) -> TcgCardSpec | None:
    inferred_title = _infer_title_from_offers(offers)
    if inferred_title is None:
        return None

    inferred_card_number = _infer_offer_attribute(
        offers,
        extractor=lambda offer: offer.attributes.get("card_number", ""),
        normalizer=normalize_card_number,
    )
    inferred_rarity = _infer_offer_attribute(
        offers,
        extractor=lambda offer: offer.attributes.get("rarity", ""),
        normalizer=normalize_text,
    )
    inferred_set_code = _infer_offer_attribute(
        offers,
        extractor=lambda offer: offer.attributes.get("version_code", "") or offer.attributes.get("set_code", ""),
        normalizer=normalize_text,
    )

    aliases = list(base_spec.aliases)
    if _title_looks_usable(base_spec.title) and normalize_text(base_spec.title) != normalize_text(inferred_title):
        aliases.append(base_spec.title)
    for candidate in _secondary_offer_titles(offers, primary_title=inferred_title):
        if candidate not in aliases:
            aliases.append(candidate)

    return replace(
        base_spec,
        title=inferred_title,
        card_number=base_spec.card_number or inferred_card_number,
        rarity=inferred_rarity or base_spec.rarity,
        set_code=inferred_set_code or base_spec.set_code,
        aliases=tuple(_dedupe_preserve_order(aliases)),
    )


def _inferred_spec_is_compatible_with_parsed(parsed: ParsedCardImage, inferred_spec: TcgCardSpec) -> bool:
    if parsed.game and inferred_spec.game and parsed.game != inferred_spec.game:
        return False
    return True


def _infer_title_from_offers(offers: tuple[MarketOffer, ...] | list[MarketOffer]) -> str | None:
    scored_titles: dict[str, tuple[str, int, float]] = {}
    for offer in offers:
        title = offer.title.strip()
        if not title or not _offer_title_looks_usable(title):
            continue
        key = normalize_text(title)
        display_title, count, score_total = scored_titles.get(key, (title, 0, 0.0))
        preferred_title = title if len(title) < len(display_title) else display_title
        scored_titles[key] = (
            preferred_title,
            count + 1,
            score_total + (offer.score or 0.0),
        )

    if not scored_titles:
        return None

    best_title, _, _ = max(
        scored_titles.values(),
        key=lambda item: (item[1], item[2], -len(item[0])),
    )
    return best_title


def _offer_title_looks_usable(value: str) -> bool:
    if _title_looks_usable(value):
        return True
    stripped = value.strip()
    if len(stripped) < 4 or len(stripped) > 72:
        return False
    if _is_blocked_title_candidate(value):
        return False
    signal = sum(
        1
        for char in value
        if char.isalpha() or "\u3040" <= char <= "\u30ff" or "\u4e00" <= char <= "\u9fff"
    )
    if signal / max(len(stripped), 1) < 0.5:
        return False
    if sum(char in {"|", "\\", "="} for char in value) >= 2:
        return False
    return True


def _secondary_offer_titles(
    offers: tuple[MarketOffer, ...] | list[MarketOffer],
    *,
    primary_title: str,
) -> tuple[str, ...]:
    values: list[str] = []
    primary_key = normalize_text(primary_title)
    for offer in offers:
        title = offer.title.strip()
        if not title or not _title_looks_usable(title):
            continue
        if normalize_text(title) == primary_key:
            continue
        values.append(title)
    return tuple(_dedupe_preserve_order(values))


def _infer_offer_attribute(
    offers: tuple[MarketOffer, ...] | list[MarketOffer],
    *,
    extractor,
    normalizer,
) -> str | None:
    scored_values: dict[str, tuple[str, int, float]] = {}
    for offer in offers:
        raw_value = str(extractor(offer) or "").strip()
        if not raw_value:
            continue
        key = normalizer(raw_value)
        if not key:
            continue
        display_value, count, score_total = scored_values.get(key, (raw_value, 0, 0.0))
        scored_values[key] = (
            display_value,
            count + 1,
            score_total + (offer.score or 0.0),
        )

    if not scored_values:
        return None

    best_value, _, _ = max(
        scored_values.values(),
        key=lambda item: (item[1], item[2], -len(item[0])),
    )
    return best_value
