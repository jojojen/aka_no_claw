"""Telegram-side dispatcher for the ``/quiz`` command (JLPT quiz feature).

aka_no_claw owns the ``QuizDatabase`` + ``QuizGenerator``; price_monitor_bot's
``/quiz`` branch just calls the handlers built here. The handler returns either a
bare string or a ``(text, reply_markup)`` tuple (an inline keyboard carries the
multiple-choice option buttons). The callback handler grades the answer and also
serves the QA loop (``review`` / delete / paging).

Subcommands:
  /quiz [<level>] [<theme>]   — serve one verified question (generate on-demand
                                if the pool is empty), with option buttons.
  /quiz review [page]         — answer-revealed paginated list (the QA review).
  /quiz gen20 [n]             — bootstrap: generate N (default 20) questions.
  /quiz teach <知識點>         — distil a reviewer correction into the authoring KB.

Callback payloads (prefix ``quiz`` is stripped by bot.py, so we see the rest):
  a:<question_id>:<choice>    — grade an answer
  p:<page>                    — re-render review page
  d:<question_id>             — delete a question, re-render current page
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable

from assistant_runtime import AssistantSettings, build_ssl_context

from .quiz_db import is_reading_exam_point as _is_reading

logger = logging.getLogger(__name__)

_DEFAULT_LEVEL = "JLPT N1"
_DEFAULT_THEME = "miku"
_LETTERS = "ABCDEFGHIJ"
_REVIEW_PAGE_SIZE = 3
# source_text_url usually points to lyrics for these source types — but a song item
# can instead be grounded on a 賞析/解説 article (e.g. reading-comprehension items),
# in which case the URL, not source_type, is the truthful signal. See _is_commentary_url.
_LYRIC_SOURCE_TYPES = {"vocaloid_song", "jpop_song"}
# Path markers that identify a commentary/解説/考察 article rather than a lyric page.
# (utaten.com hosts BOTH /lyric/ and /specialArticle/, so we must look at the path.)
_COMMENTARY_URL_MARKERS = (
    "specialarticle", "dic.nicovideo", "hatenablog", "ameblo",
    "note.com", "/blog", "blog.", "/v/", "考察", "解説",
)


def _is_commentary_url(url: str | None) -> bool:
    u = (url or "").lower()
    return any(m in u for m in _COMMENTARY_URL_MARKERS)
# Only reading-comprehension types get a 本文 block. Cloze types (漢字読み/文脈規定/
# 文法…) are self-contained in the stem; showing their grounding line would leak
# the answer (it IS the clozed/extracted sentence). The canonical predicate lives
# in quiz_db so the insert-time grounding gate and this renderer never drift.


# ── shared construction ───────────────────────────────────────────────────────


def _select_model(settings: AssistantSettings) -> str:
    raw = getattr(settings, "openclaw_local_text_model", "") or ""
    first = next((p.strip() for p in raw.split(",") if p.strip()), None)
    return first or "qwen3:14b"


def _open_db(settings: AssistantSettings):
    from .quiz_db import QuizDatabase

    return QuizDatabase(settings.quiz_db_path)


def _build_generator(settings: AssistantSettings, db):
    from .quiz_generator import QuizGenerator

    endpoint = settings.openclaw_local_text_endpoint
    ssl_ctx = build_ssl_context(settings) if endpoint.startswith("https://") else None
    return QuizGenerator(
        db=db,
        endpoint=endpoint,
        model=_select_model(settings),
        timeout_seconds=max(1, settings.openclaw_local_text_timeout_seconds),
        ssl_context=ssl_ctx,
    )


def _ensure_provider_registered(theme: str) -> None:
    """Register the provider backing ``theme`` (idempotent). Today only miku."""
    from .quiz_sources import get_provider

    if get_provider(theme) is not None:
        return
    if theme == "miku":
        from .miku_ranking import register

        register()


# ── parsing helpers ───────────────────────────────────────────────────────────


def _normalize_level(token: str, default: str = _DEFAULT_LEVEL) -> str:
    m = re.search(r"[nN]\s*([1-5])", token or "")
    return f"JLPT N{m.group(1)}" if m else default


def _parse_serve_args(text: str) -> tuple[str, str]:
    """Split free-form args into (level, theme). A token containing N1–N5 (or
    starting with 'jlpt') is the level; the first remaining token is the theme."""
    level = _DEFAULT_LEVEL
    theme = _DEFAULT_THEME
    remaining: list[str] = []
    for part in (text or "").split():
        if part.lower().startswith("jlpt") or re.search(r"[nN][1-5]", part):
            level = _normalize_level(part)
        else:
            remaining.append(part)
    if remaining:
        theme = remaining[0].lower()
    return level, theme


# ── question rendering ─────────────────────────────────────────────────────────


def _question_view(q) -> tuple[str, dict]:
    src = f"（出自《{q.source_name}》）" if q.source_name else ""
    lines = [f"🎴 {q.level} 測驗{src}"]
    if q.exam_point:
        lines.append(f"考點：{q.exam_point}")
    lines.append("")
    if q.source_excerpt and _is_reading(q.exam_point):
        lines.append("【本文】")
        lines.append(q.source_excerpt.replace("／", "\n"))
        lines.append("")
    lines.append(q.stem)
    lines.append("")
    for i, opt in enumerate(q.options):
        letter = _LETTERS[i] if i < len(_LETTERS) else str(i)
        lines.append(f"{letter}. {opt}")
    lines.append("")
    lines.append("請點選你的答案：")
    buttons = [
        {
            "text": _LETTERS[i] if i < len(_LETTERS) else str(i),
            "callback_data": f"quiz:a:{q.question_id}:{i}",
        }
        for i in range(len(q.options))
    ]
    rows = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
    return "\n".join(lines), {"inline_keyboard": rows}


def _grade_view(q, original_text: str, chosen: int) -> tuple[str, str]:
    """Return (toast, new_text) for a graded answer."""
    correct = q.answer_index
    ok = chosen == correct

    def letter(i: int) -> str:
        return _LETTERS[i] if 0 <= i < len(_LETTERS) else str(i)

    parts = [original_text, ""]
    parts.append("✅ 正解！" if ok else "❌ 答錯了")
    parts.append(f"你的答案：{letter(chosen)}　正解：{letter(correct)}. {q.options[correct]}")
    if q.explanation:
        parts.append(f"💡 {q.explanation}")
    if q.source_text_url:
        is_lyric = (
            q.source_type in _LYRIC_SOURCE_TYPES
            and not _is_commentary_url(q.source_text_url)
        )
        text_label = "歌詞原文" if is_lyric else "賞析・解説原文"
        parts.append(f"📖 {text_label}：{q.source_text_url}")
    if q.source_media_url:
        parts.append(f"🎵 音檔：{q.source_media_url}")
    toast = "✅ 答對了！" if ok else "❌ 答錯了"
    return toast, "\n".join(parts)


def _render_review_page(db, page: int) -> tuple[str, dict]:
    questions = db.recent_questions(limit=60)
    total = len(questions)
    if total == 0:
        return "題庫目前是空的。先用 /quiz gen20 產題，或 /quiz JLPTN1 miku 即時出一題。", {
            "inline_keyboard": []
        }
    pages = max(1, (total + _REVIEW_PAGE_SIZE - 1) // _REVIEW_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * _REVIEW_PAGE_SIZE
    chunk = questions[start : start + _REVIEW_PAGE_SIZE]

    lines = [f"🧐 題庫檢視　第 {page + 1}/{pages} 頁　共 {total} 題", ""]
    del_buttons: list[list[dict]] = []
    for offset, q in enumerate(chunk):
        n = start + offset + 1
        lines.append(f"#{n}　[{q.level} / {q.exam_point}]　來源：{q.source_name or '—'}")
        if q.source_excerpt and _is_reading(q.exam_point):
            lines.append("【本文】" + q.source_excerpt.replace("／", "\n"))
        lines.append(q.stem)
        for i, opt in enumerate(q.options):
            mark = "✓" if i == q.answer_index else "　"
            letter = _LETTERS[i] if i < len(_LETTERS) else str(i)
            lines.append(f"  {mark}{letter}. {opt}")
        if q.explanation:
            lines.append(f"  💡 {q.explanation}")
        lines.append("")
        del_buttons.append(
            [{"text": f"🗑️ 刪除 #{n}", "callback_data": f"quiz:d:{q.question_id}"}]
        )

    nav: list[dict] = []
    if page > 0:
        nav.append({"text": "⬅️ 上一頁", "callback_data": f"quiz:p:{page - 1}"})
    nav.append({"text": f"{page + 1}/{pages}", "callback_data": "noop"})
    if page < pages - 1:
        nav.append({"text": "下一頁 ➡️", "callback_data": f"quiz:p:{page + 1}"})

    keyboard = del_buttons + [nav]
    return "\n".join(lines), {"inline_keyboard": keyboard}


# ── distillation (/quiz teach) ─────────────────────────────────────────────────


def _distill_authoring(settings, db, lesson: str) -> str:
    from .opportunity_agent import _call_ollama_json

    endpoint = settings.openclaw_local_text_endpoint
    ssl_ctx = build_ssl_context(settings) if endpoint.startswith("https://") else None
    prompt = (
        "你是 JLPT 出題品管。使用者剛指出一個出題上的問題或心得，請把它抽象成一條"
        "**通用、可遷移**的 JLPT 出題技巧規則（不要綁特定歌曲／題目／選項）。\n"
        "category 從這些挑最貼切的："
        "grammar|vocabulary|reading|distractor_design|level_calibration|source_grounding。\n"
        '只輸出 JSON：{"category": "...", "title": "短標題", '
        '"technique": "一兩句通則", "keywords": ["關鍵字"]}。\n\n'
        f"使用者的指正／心得：{lesson}\n"
    )
    try:
        raw = _call_ollama_json(
            endpoint=endpoint,
            model=_select_model(settings),
            prompt=prompt,
            timeout_seconds=max(1, settings.openclaw_local_text_timeout_seconds),
            ssl_context=ssl_ctx,
        )
        data = json.loads(_strip_fence(raw))
    except Exception as exc:
        logger.exception("quiz teach: distil failed")
        return f"知識點蒸餾失敗：{exc}"
    if not isinstance(data, dict):
        return "知識點蒸餾失敗：模型未回傳有效 JSON。"
    title = str(data.get("title", "")).strip()
    technique = str(data.get("technique", "")).strip()
    if not title or not technique:
        return "知識點蒸餾失敗：缺 title 或 technique。"
    entry = db.upsert_authoring_knowledge(
        category=str(data.get("category", "source_grounding")).strip() or "source_grounding",
        title=title,
        technique=technique,
        keywords=tuple(str(k).strip() for k in data.get("keywords", []) if str(k).strip()),
        origin="distilled",
        confidence=0.6,
    )
    return f"✅ 已寫入出題技巧知識庫：[{entry.category}] {entry.title}\n　{entry.technique}"


def _strip_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    return text


# ── handler builders ───────────────────────────────────────────────────────────


def build_quiz_handler(
    settings: AssistantSettings,
) -> Callable[[str, str], "tuple[str, object] | str"]:
    db = _open_db(settings)

    def handler(raw: str, chat_id: str):
        text = (raw or "").strip()
        head, _, rest = text.partition(" ")
        action = head.lower().strip()
        rest = rest.strip()
        try:
            if action == "review":
                page = 0
                if rest:
                    try:
                        page = max(0, int(rest) - 1)
                    except ValueError:
                        page = 0
                return _render_review_page(db, page)
            if action == "gen20":
                count = 20
                if rest:
                    try:
                        count = max(1, min(50, int(rest)))
                    except ValueError:
                        count = 20
                return _do_gen(settings, db, count)
            if action == "teach":
                if not rest:
                    return "用法：/quiz teach <要教它的出題知識點>"
                return _distill_authoring(settings, db, rest)
            # default: serve one question
            return _do_serve(settings, db, text)
        except Exception as exc:
            logger.exception("quiz handler: action=%s failed", action)
            return f"測驗指令失敗：{exc}"

    return handler


def _do_serve(settings, db, text: str):
    level, theme = _parse_serve_args(text)
    _ensure_provider_registered(theme)
    question = db.random_question(level=level, prefer_unserved=True)
    if question is None:
        # Pool empty → generate one on-demand (runs in background via bot.py).
        gen = _build_generator(settings, db)
        question = gen.generate_one_question(level=level, theme=theme, question_type="単語")
    if question is None:
        return (
            f"目前 {level} 題庫是空的，且即時出題失敗（地端模型或素材來源可能暫時不可用）。"
            "稍後再試，或先跑 /quiz gen20 批次產題。"
        )
    db.mark_served(question.question_id)
    return _question_view(question)


def _do_gen(settings, db, count: int) -> str:
    _ensure_provider_registered(_DEFAULT_THEME)
    gen = _build_generator(settings, db)
    made = 0
    for _ in range(count):
        if gen.generate_one_question(
            level=_DEFAULT_LEVEL, theme=_DEFAULT_THEME, question_type="単語"
        ):
            made += 1
    return (
        f"📝 批次出題完成：成功驗證並入庫 {made}/{count} 題。\n"
        "用 /quiz review 逐題檢查；發現問題就用 /quiz teach <知識點> 教它改善。"
    )


def build_quiz_callback_handler(
    settings: AssistantSettings,
) -> Callable[[str, str], "tuple[object, str, object]"]:
    db = _open_db(settings)

    def handler(payload: str, original_text: str) -> "tuple[object, str, object]":
        action, _, rest = (payload or "").partition(":")
        try:
            if action == "a":
                qid, _, idx_str = rest.rpartition(":")
                question = db.get_question(qid)
                if question is None:
                    return "找不到這題（可能已刪除）", None, None
                try:
                    chosen = int(idx_str)
                except ValueError:
                    return "答案格式錯誤", None, None
                toast, new_text = _grade_view(question, original_text, chosen)
                db.mark_served(qid)
                return toast, new_text, None  # clear keyboard
            if action == "p":
                try:
                    page = max(0, int(rest))
                except ValueError:
                    page = 0
                new_text, markup = _render_review_page(db, page)
                return None, new_text, markup
            if action == "d":
                removed = db.delete_question(rest)
                page = _guess_review_page(original_text)
                new_text, markup = _render_review_page(db, page)
                toast = "🗑️ 已刪除" if removed else "找不到該題"
                return toast, new_text, markup
        except Exception:
            logger.exception("quiz callback failed payload=%s", payload)
            return "操作失敗，請看 log", None, None
        return "未知操作", None, None

    return handler


_REVIEW_HEADER_RE = re.compile(r"第\s*(\d+)\s*/\s*\d+\s*頁")


def _guess_review_page(text: str) -> int:
    m = _REVIEW_HEADER_RE.search(text or "")
    return max(0, int(m.group(1)) - 1) if m else 0


# ── daily scheduler ────────────────────────────────────────────────────────────


def start_quiz_daily_scheduler(settings: AssistantSettings):
    """Start the daemon that generates a few verified questions per day.
    Best-effort: returns None (and logs) if anything needed is unavailable."""
    try:
        from .quiz_generator import QuizDailyScheduler

        _ensure_provider_registered(_DEFAULT_THEME)
        db = _open_db(settings)
        generator = _build_generator(settings, db)
        scheduler = QuizDailyScheduler(
            generator=generator,
            level=_DEFAULT_LEVEL,
            theme=_DEFAULT_THEME,
            per_day=2,
            hour=4,
        )
        scheduler.start()
        return scheduler
    except Exception:
        logger.exception("start_quiz_daily_scheduler: failed to start")
        return None
