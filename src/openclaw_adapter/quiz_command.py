"""Telegram-side dispatcher for the ``/quiz`` command (JLPT quiz feature).

aka_no_claw owns the ``QuizDatabase`` + ``QuizGenerator``; price_monitor_bot's
``/quiz`` branch just calls the handlers built here. The handler returns either a
bare string or a ``(text, reply_markup)`` tuple (an inline keyboard carries the
multiple-choice option buttons). The callback handler grades the answer and also
serves the QA loop (``review`` / delete / paging).

Subcommands:
  /quiz [<level>] [<theme>]          — show the 題型 (exam_point) selection menu;
                                       picking a type serves a weighted question
                                       from that type.
  /quiz [<level>] [<theme>] byauthor — show the 出題者 (author) selection menu first;
                                       picking an author then shows that author's
                                       題型 menu, and serving stays scoped to them.
  /quiz [<level>] [<theme>] random   — skip the menu, serve one weighted question
                                       from any 題型 (generate on-demand if empty).
  /quiz wrong [<level>]              — 錯題本: re-serve a question you last got wrong
                                       (weighted); drops out once you re-answer right.
  /quiz stats                        — per-題型 accuracy, weakest 考点, 混淆選項分析.
  /quiz vocab [mode|word]            — 單字卡：弱點/全部/錯題/隨機/查詞.
  /quiz grammar [mode|pattern]       — 文法卡：弱點/全部/錯題/隨機/查句型.
  /quizlikesong <youtube_url>         — 收藏歌曲並預先抓歌詞/NLP.
  /quiz review [page]                — answer-revealed paginated list (QA review).
  /quiz gen20 [n]                    — bootstrap: generate N (default 20) questions.
  /quiz teach <知識點>                — distil a reviewer correction into the KB.

Callback payloads (prefix ``quiz`` is stripped by bot.py, so we see the rest):
  a:<question_id>:<choice>    — grade an answer
  t:<level>:<exam_point>      — type-menu pick ('*' = random/all, '!' = 錯題本) → serve
  au:<level>:<author_code>    — byauthor author pick → show that author's 題型 menu
  ta:<level>:<exam_point>:<author_code>
                              — author-scoped type pick → serve (author-filtered)
  vb:<level>:<mode>:<index>   — vocabulary-card browsing
  vr:<vocab_id>               — serve one question related to the vocabulary card
  vc:<vocab_id>               — show a specific vocab card directly (from grade result)
  gb:<level>:<mode>:<index>   — grammar-card browsing
  gr:<card_id>                — serve one question related to the grammar card
  gc:<card_id>                — show a specific grammar card directly
  ga:<card_id>                — play the grammar-card example audio
  p:<page>                    — re-render review page
  d:<question_id>             — delete a question, re-render current page

``author_code`` is a colon-free ASCII token (_author_code) so it survives the
':'-split of callback_data; it's reversed against the live author list at click
time (_author_from_code), so no author tag is hardcoded.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable

from assistant_runtime import AssistantSettings, build_ssl_context
from telegram_core.transport import TelegramBotClient

from .quiz_db import is_reading_exam_point as _is_reading
from .quiz_vocab_audio import (
    QuizVocabAudioError,
    build_vocab_audio_cache_dir,
    build_vocab_synthesizer,
)

logger = logging.getLogger(__name__)

_DEFAULT_LEVEL = "JLPT N1"
_DEFAULT_THEME = "miku"
_LETTERS = "ABCDEFGHIJ"
_REVIEW_PAGE_SIZE = 3
_GRAMMAR_CARD_EXAM_POINTS = {"文法形式の判断", "文章の文法", "文の組み立て"}
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


def _vocab_audio_enabled(card) -> bool:
    return bool((card.example_ja or "").strip())


def _send_vocab_audio(settings: AssistantSettings, *, card, chat_id: str | None, params=None) -> None:
    token = getattr(settings, "openclaw_telegram_bot_token", None)
    if not token:
        raise QuizVocabAudioError("telegram bot token missing")
    if not (chat_id or "").strip():
        raise QuizVocabAudioError("chat_id missing")
    cache_dir = build_vocab_audio_cache_dir(settings=settings)
    synth = build_vocab_synthesizer(settings, params)
    cache_id = (
        getattr(card, "vocab_id", None)
        or getattr(card, "card_id", None)
        or getattr(card, "headword", "")
    )
    audio = synth.synthesize_to_cache(
        text=card.example_ja,
        cache_dir=cache_dir,
        vocab_id=str(cache_id),
    )
    client = TelegramBotClient(token, ssl_context=build_ssl_context(settings))
    reading = (getattr(card, "reading_hiragana", None) or "").strip()
    heading = f"{card.headword}（{reading}）" if reading else str(card.headword)
    caption = f"{heading}\n音源：{audio.engine_label}\n例句：{card.example_ja}"
    client.send_document(
        chat_id=str(chat_id),
        document_path=audio.output_path,
        caption=caption[:1024],
    )


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


def _like_song(settings: AssistantSettings, db, youtube_url: str) -> str:
    from .quiz_favorite_songs import FavoriteSongError, FavoriteSongIngestor

    url = (youtube_url or "").strip()
    if not url:
        return "用法：/quizlikesong <youtube_url>"
    try:
        result = FavoriteSongIngestor(settings=settings, db=db).ingest_youtube_song(url)
    except FavoriteSongError as exc:
        return f"收藏歌曲失敗：{exc}"
    except Exception as exc:
        logger.exception("quiz like song failed url=%s", url)
        return f"收藏歌曲失敗：{exc}"
    reused = "（已存在，直接重用）" if result.reused_existing else ""
    return (
        f"❤️ 已加入最愛曲目{reused}\n"
        f"歌曲：{result.title}\n"
        f"歌手：{result.artist or '—'}\n"
        f"YouTube：{result.youtube_short_url}\n"
        f"歌詞：{result.lyrics_url or '無'}\n"
        f"狀態：{result.status}\n"
        f"句子數：{result.sentence_count}\n"
        f"詞元數：{result.token_count}\n"
        f"N1 詞元：{result.n1_token_count}"
    )


def build_like_song_confirmation(settings: AssistantSettings, youtube_url: str):
    from .quiz_favorite_songs import FavoriteSongError, fetch_youtube_song_metadata

    url = (youtube_url or "").strip()
    if not url:
        return None
    try:
        meta = fetch_youtube_song_metadata(settings=settings, youtube_url=url)
    except FavoriteSongError:
        return None
    except Exception:
        logger.exception("quiz like-song confirmation failed url=%s", url)
        return None
    text = (
        "🎵 偵測到 YouTube 歌曲連結\n\n"
        f"歌曲：{meta.title}\n"
        f"歌手：{meta.artist or '—'}\n"
        f"YouTube：{meta.youtube_short_url}\n\n"
        "要加入最愛曲目清單嗎？"
    )
    markup = {
        "inline_keyboard": [[
            {"text": "❤️ 加入最愛", "callback_data": f"quiz:ls:{meta.video_id}"},
            {"text": "先不要", "callback_data": f"quiz:lx:{meta.video_id}"},
        ]]
    }
    return text, markup


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
    starting with 'jlpt') is the level; the first remaining token is the theme.
    A bare ``random`` token is a serve-mode flag, not a theme — it's ignored here
    (the handler strips/detects it separately via _wants_random)."""
    level = _DEFAULT_LEVEL
    theme = _DEFAULT_THEME
    remaining: list[str] = []
    for part in (text or "").split():
        if part.lower().startswith("jlpt") or re.search(r"[nN][1-5]", part):
            level = _normalize_level(part)
        elif part.lower() in ("random", "byauthor"):
            continue  # serve-mode flags, not themes
        else:
            remaining.append(part)
    if remaining:
        theme = remaining[0].lower()
    return level, theme


def _wants_random(text: str) -> bool:
    """True if the args contain a bare ``random`` token → serve immediately from
    any 題型 (weighted), skipping the type-selection menu."""
    return any(p.lower() == "random" for p in (text or "").split())


def _wants_byauthor(text: str) -> bool:
    """True if the args contain a bare ``byauthor`` token → show the 出題者
    selection menu first; picking an author then leads to that author's 題型 menu."""
    return any(p.lower() == "byauthor" for p in (text or "").split())


def _author_code(author: str) -> str:
    """A colon-free, ASCII-only short token for an author, safe to embed in
    Telegram callback_data (which we split on ':'). ``qwen3:14b`` → ``qwen314b``."""
    return re.sub(r"[^A-Za-z0-9]", "", author or "")[:16] or "x"


def _author_from_code(db, level: str, code: str) -> str | None:
    """Reverse a callback author code back to the real author tag by matching
    against the live author list (no hardcoded map — survives new authors)."""
    for author, _ in db.author_counts(level=level):
        if _author_code(author) == code:
            return author
    return None


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
    if getattr(q, "author", None):
        parts.append(f"🖋️ 出題者：{q.author}")
    toast = "✅ 答對了！" if ok else "❌ 答錯了"
    return toast, "\n".join(parts)


def _grade_actions_markup(db, q) -> dict:
    buttons: list[list[dict]] = [[
        {"text": "🎲 下一題（隨機）", "callback_data": f"quiz:t:{q.level}:*"},
        {"text": "🧩 同類型下一題", "callback_data": f"quiz:t:{q.level}:{q.exam_point}"},
    ]]
    if q.tested_point:
        if q.exam_point in _GRAMMAR_CARD_EXAM_POINTS:
            try:
                gcard = db.get_grammar_card(headword=q.tested_point, level=q.level)
            except Exception:
                gcard = None
            if gcard is not None:
                buttons.append([{
                    "text": f"📗 查「{q.tested_point}」文法卡",
                    "callback_data": f"quiz:gc:{gcard.card_id}",
                }])
        else:
            try:
                vcard = db.get_vocab_card(
                    headword=q.tested_point, level=q.level
                )
            except Exception:
                vcard = None
            if vcard is not None:
                buttons.append([{
                    "text": f"📚 查「{q.tested_point}」單字卡",
                    "callback_data": f"quiz:vc:{vcard.vocab_id}",
                }])
    return {"inline_keyboard": buttons}


def _render_stats(db, chat_id: str | None) -> str:
    """Show the learner's per-題型 accuracy and weakest specific 考点 so they can
    see what the adaptive selector is now biasing toward."""
    s = db.mastery_stats(chat_id=str(chat_id or ""))
    if not s["total"]:
        return "你還沒作答過任何題目。先 /quiz 出一題作答，之後就會依你的弱項調整出題機率。"
    lines = [f"📊 你的答題統計（累計 {s['total']} 次作答）", "", "■ 各題型答對率（低→高，越前面越會被多出）："]
    for r in s["by_type"]:
        pct = round(r["accuracy"] * 100)
        lines.append(f"　{pct:3d}%  {r['key']}（{r['corrects']}/{r['attempts']}）")
    weak = [r for r in s["by_point"] if r["accuracy"] < 1.0][:12]
    if weak:
        lines.append("")
        lines.append("■ 最弱的具體考點（會被優先重練）：")
        for r in weak:
            pct = round(r["accuracy"] * 100)
            lines.append(f"　{pct:3d}%  {r['key']}（{r['corrects']}/{r['attempts']}）")
    pairs = db.confusion_pairs(chat_id=str(chat_id or ""))
    if pairs:
        lines.append("")
        lines.append("■ 你最常混淆的選項（正解 ← 你誤選）：")
        for p in pairs:
            times = f"×{p['count']}" if p["count"] > 1 else ""
            lines.append(f"　「{p['correct']}」← 你選了「{p['chosen']}」　[{p['exam_point']}]{times}")
    return "\n".join(lines)


def _parse_vocab_args(text: str) -> tuple[str, str, str]:
    """Return (level, mode, query) for `/quiz vocab ...`.

    Modes:
      weak   - default personalized ordering
      all    - full browse
      wrong  - only points whose latest tested_point attempt is wrong
      random - one random card
      lookup - exact/substring word lookup
      source - filter by source name
    """
    level = _DEFAULT_LEVEL
    tokens: list[str] = []
    for part in (text or "").split():
        if part.lower().startswith("jlpt") or re.search(r"[nN][1-5]", part):
            level = _normalize_level(part)
        else:
            tokens.append(part)
    if not tokens:
        return level, "weak", ""
    head = tokens[0].lower()
    if head in {"all", "wrong", "random"}:
        return level, head, " ".join(tokens[1:]).strip()
    if head == "source":
        return level, "source", " ".join(tokens[1:]).strip()
    return level, "lookup", " ".join(tokens).strip()


def _parse_grammar_args(text: str) -> tuple[str, str, str]:
    """Return (level, mode, query) for `/quiz grammar ...`."""
    level = _DEFAULT_LEVEL
    tokens: list[str] = []
    for part in (text or "").split():
        if part.lower().startswith("jlpt") or re.search(r"[nN][1-5]", part):
            level = _normalize_level(part)
        else:
            tokens.append(part)
    if not tokens:
        return level, "weak", ""
    head = tokens[0].lower()
    if head in {"all", "wrong", "random"}:
        return level, head, " ".join(tokens[1:]).strip()
    if head == "source":
        return level, "source", " ".join(tokens[1:]).strip()
    return level, "lookup", " ".join(tokens).strip()


def _render_vocab_card(card, *, mode: str, index: int, total: int) -> tuple[str, dict]:
    mode_label = {
        "weak": "弱點單字卡",
        "all": "全部單字卡",
        "wrong": "錯題單字卡",
        "random": "隨機單字卡",
        "recent": "最新加入單字卡",
        "lookup": "單字查詢",
    }.get(mode, "單字卡")
    # tested_jlpt_level is the item's true difficulty; NULL/blank means N1.
    difficulty = (getattr(card, "tested_jlpt_level", None) or "").strip().upper() or "N1"
    author_label = (getattr(card, "author", None) or "codex").strip() or "codex"
    head = f"📘 {card.level} {mode_label}　{index + 1}/{total}　〔難度 {difficulty}〕〔作者 {author_label}〕"
    lines = [
        head,
        "",
        f"{card.headword}（{card.reading_hiragana}）",
        f"中文：{card.zh_gloss_short}",
        f"例句：{card.example_ja}",
    ]
    if card.exam_points:
        lines.append(f"題型：{' / '.join(card.exam_points)}")
    lines.append(f"來源：{card.source_name or '—'}")
    if card.source_media_url:
        lines.append(f"歌曲：{card.source_media_url}")
    if card.source_text_url:
        lines.append(f"原文：{card.source_text_url}")
    buttons: list[list[dict]] = []
    if mode in {"weak", "all", "wrong", "recent"} and total > 1:
        nav: list[dict] = []
        if index > 0:
            nav.append(
                {"text": "⬅️ 上一張", "callback_data": f"quiz:vb:{card.level}:{mode}:{index - 1}"}
            )
        nav.append({"text": f"{index + 1}/{total}", "callback_data": "noop"})
        if index < total - 1:
            nav.append(
                {"text": "下一張 ➡️", "callback_data": f"quiz:vb:{card.level}:{mode}:{index + 1}"}
            )
        buttons.append(nav)
    if _vocab_audio_enabled(card):
        buttons.append([{"text": "🔊 播放例句", "callback_data": f"quiz:va:{card.vocab_id}"}])
    buttons.append([{"text": "📝 出相關題", "callback_data": f"quiz:vr:{card.vocab_id}"}])
    buttons.append([{"text": "🆕 最新加入", "callback_data": f"quiz:vb:{card.level}:recent:0"}])
    buttons.append([{"text": "🎲 下一張隨機", "callback_data": f"quiz:vrnd:{card.level}"}])
    return "\n".join(lines), {"inline_keyboard": buttons}


def _render_grammar_card(card, *, mode: str, index: int, total: int) -> tuple[str, dict]:
    mode_label = {
        "weak": "弱點文法卡",
        "all": "全部文法卡",
        "wrong": "錯題文法卡",
        "random": "隨機文法卡",
        "recent": "最新加入文法卡",
        "lookup": "文法查詢",
    }.get(mode, "文法卡")
    difficulty = (getattr(card, "tested_jlpt_level", None) or "").strip().upper() or "未標定"
    author_label = (getattr(card, "author", None) or "codex").strip() or "codex"
    head = f"📗 {card.level} {mode_label}　{index + 1}/{total}　〔難度 {difficulty}〕〔作者 {author_label}〕"
    lines = [
        head,
        "",
        f"句型：{card.headword}",
        f"說明：{card.explanation_zh}",
        f"例句：{card.example_ja}",
    ]
    if card.exam_points:
        lines.append(f"題型：{' / '.join(card.exam_points)}")
    lines.append(f"來源：{card.source_name or '—'}")
    if card.source_media_url:
        lines.append(f"歌曲：{card.source_media_url}")
    if card.source_text_url:
        lines.append(f"原文：{card.source_text_url}")
    buttons: list[list[dict]] = []
    if mode in {"weak", "all", "wrong", "recent"} and total > 1:
        nav: list[dict] = []
        if index > 0:
            nav.append(
                {"text": "⬅️ 上一張", "callback_data": f"quiz:gb:{card.level}:{mode}:{index - 1}"}
            )
        nav.append({"text": f"{index + 1}/{total}", "callback_data": "noop"})
        if index < total - 1:
            nav.append(
                {"text": "下一張 ➡️", "callback_data": f"quiz:gb:{card.level}:{mode}:{index + 1}"}
            )
        buttons.append(nav)
    if _vocab_audio_enabled(card):
        buttons.append([{"text": "🔊 播放例句", "callback_data": f"quiz:ga:{card.card_id}"}])
    buttons.append([{"text": "📝 出相關題", "callback_data": f"quiz:gr:{card.card_id}"}])
    buttons.append([{"text": "🆕 最新加入", "callback_data": f"quiz:gb:{card.level}:recent:0"}])
    buttons.append([{"text": "🎲 下一張隨機", "callback_data": f"quiz:grnd:{card.level}"}])
    return "\n".join(lines), {"inline_keyboard": buttons}


def _render_vocab_browser(db, *, level: str, mode: str, chat_id: str | None, index: int = 0):
    cards = db.list_vocab_cards(level=level, chat_id=str(chat_id or ""), mode=mode)
    if not cards:
        if mode == "wrong":
            return "📘 目前沒有可看的錯題單字卡。先去 /quiz 練題，答錯後這裡才會有內容。"
        return "📘 目前沒有可用的單字卡。"
    index = max(0, min(index, len(cards) - 1))
    return _render_vocab_card(cards[index], mode=mode, index=index, total=len(cards))


def _render_grammar_browser(db, *, level: str, mode: str, chat_id: str | None, index: int = 0):
    cards = db.list_grammar_cards(level=level, chat_id=str(chat_id or ""), mode=mode)
    if not cards:
        if mode == "wrong":
            return "📗 目前沒有可看的錯題文法卡。先去 /quiz 練文法題，答錯後這裡才會有內容。"
        return "📗 目前沒有可用的文法卡。"
    index = max(0, min(index, len(cards) - 1))
    return _render_grammar_card(cards[index], mode=mode, index=index, total=len(cards))


def _render_vocab_lookup(db, *, level: str, query: str):
    query = (query or "").strip()
    if not query:
        return "用法：/quiz vocab <單字>　或　/quiz vocab source <歌曲名>"
    exact = db.get_vocab_card(headword=query, level=level)
    if exact is not None:
        return _render_vocab_card(exact, mode="lookup", index=0, total=1)
    hits = db.find_vocab_cards(level=level, query=query)
    if not hits:
        return f"📘 找不到「{query}」的單字卡。"
    if len(hits) == 1:
        return _render_vocab_card(hits[0], mode="lookup", index=0, total=1)
    lines = [f"📘 找到 {len(hits)} 張相關單字卡：", ""]
    for card in hits[:12]:
        lines.append(f"・{card.headword}（{card.reading_hiragana}）— {card.zh_gloss_short}")
    lines.append("")
    lines.append("可直接用 /quiz vocab <單字> 查看其中一張。")
    return "\n".join(lines)


def _render_grammar_lookup(db, *, level: str, query: str):
    query = (query or "").strip()
    if not query:
        return "用法：/quiz grammar <句型>　或　/quiz grammar source <歌曲名>"
    exact = db.get_grammar_card(headword=query, level=level)
    if exact is not None:
        return _render_grammar_card(exact, mode="lookup", index=0, total=1)
    hits = db.find_grammar_cards(level=level, query=query)
    if not hits:
        return f"📗 找不到「{query}」的文法卡。"
    if len(hits) == 1:
        return _render_grammar_card(hits[0], mode="lookup", index=0, total=1)
    lines = [f"📗 找到 {len(hits)} 張相關文法卡：", ""]
    for card in hits[:12]:
        lines.append(f"・{card.headword} — {card.source_name or '—'}")
    lines.append("")
    lines.append("可直接用 /quiz grammar <句型> 查看其中一張。")
    return "\n".join(lines)


def _render_vocab_source_list(db, *, level: str, source_name: str):
    source_name = (source_name or "").strip()
    if not source_name:
        return "用法：/quiz vocab source <歌曲名>"
    cards = db.vocab_cards_for_source(level=level, source_name=source_name)
    if not cards:
        return f"📘 找不到來源包含「{source_name}」的單字卡。"
    lines = [f"📘 來源包含「{source_name}」的單字卡（{len(cards)}）", ""]
    for card in cards[:20]:
        lines.append(f"・{card.headword}（{card.reading_hiragana}）— {card.zh_gloss_short}")
    if len(cards) > 20:
        lines.append("…")
    lines.append("")
    lines.append("可直接用 /quiz vocab <單字> 查看某一張卡。")
    return "\n".join(lines)


def _render_grammar_source_list(db, *, level: str, source_name: str):
    source_name = (source_name or "").strip()
    if not source_name:
        return "用法：/quiz grammar source <歌曲名>"
    cards = db.grammar_cards_for_source(level=level, source_name=source_name)
    if not cards:
        return f"📗 找不到來源包含「{source_name}」的文法卡。"
    lines = [f"📗 來源包含「{source_name}」的文法卡（{len(cards)}）", ""]
    for card in cards[:20]:
        lines.append(f"・{card.headword} — {card.source_name or '—'}")
    if len(cards) > 20:
        lines.append("…")
    lines.append("")
    lines.append("可直接用 /quiz grammar <句型> 查看某一張卡。")
    return "\n".join(lines)


def _render_author_menu(db, level: str, theme: str) -> tuple[str, dict]:
    """Show one button per 出題者 (author) present in the pool. Picking one leads
    to that author's 題型 menu. callback_data: ``quiz:au:<level>:<author_code>``."""
    authors = db.author_counts(level=level)
    if not authors:
        return (
            f"目前 {level} 題庫是空的，無法列出出題者。先跑 /quiz gen20 產題。",
            {"inline_keyboard": []},
        )
    lines = [
        f"🎴 {level} 測驗 — 選擇出題者",
        "",
        "先選一位出題者，下一步再選題型；之後只會出這位出題者的題目。",
    ]
    buttons = [
        {
            "text": f"🖋️ {author}（{n}）",
            "callback_data": f"quiz:au:{level}:{_author_code(author)}",
        }
        for author, n in authors
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return "\n".join(lines), {"inline_keyboard": rows}


def _render_type_menu(
    db, level: str, theme: str, author: str | None = None
) -> tuple[str, dict]:
    """Show one button per official 題型 (exam_point) present in the pool, plus a
    🎲 random-all button. Picking one serves a weighted question from that 題型.
    callback_data: ``quiz:t:<level>:<exam_point>`` (``*`` = random across all).
    When ``author`` is set (the byauthor path), the tally and the served questions
    are restricted to that 出題者, and callbacks carry the author code:
    ``quiz:ta:<level>:<exam_point>:<author_code>``."""
    counts = db.exam_point_counts(level=level, author=author)
    if not counts:
        if author:
            return (
                f"出題者「{author}」在 {level} 沒有題目。回 /quiz {theme} byauthor 換一位。",
                {"inline_keyboard": []},
            )
        # No verified questions yet → fall back to immediate (weighted) serve path.
        return (
            f"目前 {level} 題庫是空的，無法列出題型。先跑 /quiz gen20 產題，"
            "或用 /quiz {0} {1} random 立即出一題。".format(level, theme),
            {"inline_keyboard": []},
        )
    if author:
        code = _author_code(author)
        lines = [
            f"🎴 {level} 測驗 — 出題者：{author} — 選擇題型",
            "",
            "點一個題型，只會出這位出題者該題型的題目；或選「🎲 隨機（全部）」。",
        ]
        buttons = [
            {"text": f"{ep}（{n}）", "callback_data": f"quiz:ta:{level}:{ep}:{code}"}
            for ep, n in counts
        ]
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        rows.append([
            {"text": "🎲 隨機（全部）", "callback_data": f"quiz:ta:{level}:*:{code}"},
            {"text": "📭 錯題本", "callback_data": f"quiz:ta:{level}:!:{code}"},
        ])
        return "\n".join(lines), {"inline_keyboard": rows}
    lines = [
        f"🎴 {level} 測驗 — 選擇題型",
        "",
        "點一個題型，我會從你該題型的弱點優先出題；或選「🎲 隨機（全部）」。",
        "（小技巧：直接打 /quiz {0} {1} random 可跳過此選單）".format(level, theme),
    ]
    buttons = [
        {
            "text": f"{ep}（{n}）",
            "callback_data": f"quiz:t:{level}:{ep}",
        }
        for ep, n in counts
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([
        {"text": "🎲 隨機（全部）", "callback_data": f"quiz:t:{level}:*"},
        {"text": "📭 錯題本", "callback_data": f"quiz:t:{level}:!"},
    ])
    return "\n".join(lines), {"inline_keyboard": rows}


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
            if action == "stats":
                return _render_stats(db, chat_id)
            if action == "vocab":
                level, mode, query = _parse_vocab_args(rest)
                if mode in {"weak", "all", "wrong"}:
                    return _render_vocab_browser(
                        db, level=level, mode=mode, chat_id=chat_id, index=0
                    )
                if mode == "random":
                    cards = db.list_vocab_cards(level=level, chat_id=str(chat_id or ""), mode="random")
                    if not cards:
                        return "📘 目前沒有可用的單字卡。"
                    return _render_vocab_card(cards[0], mode="random", index=0, total=1)
                if mode == "source":
                    return _render_vocab_source_list(db, level=level, source_name=query)
                return _render_vocab_lookup(db, level=level, query=query)
            if action == "grammar":
                level, mode, query = _parse_grammar_args(rest)
                if mode in {"weak", "all", "wrong"}:
                    return _render_grammar_browser(
                        db, level=level, mode=mode, chat_id=chat_id, index=0
                    )
                if mode == "random":
                    cards = db.list_grammar_cards(level=level, chat_id=str(chat_id or ""), mode="random")
                    if not cards:
                        return "📗 目前沒有可用的文法卡。"
                    return _render_grammar_card(cards[0], mode="random", index=0, total=1)
                if mode == "source":
                    return _render_grammar_source_list(db, level=level, source_name=query)
                return _render_grammar_lookup(db, level=level, query=query)
            if action == "like":
                kind, _, target = rest.partition(" ")
                if kind.lower().strip() != "song":
                    return "用法：/quizlikesong <youtube_url>"
                return _like_song(settings, db, target)
            if action in ("wrong", "錯題", "錯題本"):
                level, theme = _parse_serve_args(rest)
                return _serve_question(
                    settings, db, level, theme, None, chat_id, wrong_only=True
                )
            # default: `/quiz <level> <theme> random` → serve immediately (any
            # 題型, weighted); without `random` → show the 題型 selection menu.
            level, theme = _parse_serve_args(text)
            if _wants_byauthor(text):
                return _render_author_menu(db, level, theme)
            if _wants_random(text):
                return _serve_question(settings, db, level, theme, None, chat_id)
            return _render_type_menu(db, level, theme)
        except Exception as exc:
            logger.exception("quiz handler: action=%s failed", action)
            return f"測驗指令失敗：{exc}"

    return handler


def _serve_question(
    settings,
    db,
    level: str,
    theme: str,
    exam_point: str | None,
    chat_id: str | None,
    wrong_only: bool = False,
    author: str | None = None,
):
    """Serve one weighted question (optionally restricted to ``exam_point``, to a
    specific ``author``/出題者, or to the learner's previously-wrong 錯題 when
    ``wrong_only``). Returns a ``(text, markup)`` view tuple, or an error string."""
    _ensure_provider_registered(theme)
    question = db.weighted_question(
        level=level, chat_id=chat_id, exam_point=exam_point,
        wrong_only=wrong_only, author=author,
    )
    if question is None and wrong_only:
        return (
            "📭 目前沒有錯題可複習：你還沒答錯過，或先前答錯的都已經訂正答對了。\n"
            "去 /quiz 練幾題，答錯的會自動進錯題本；訂正答對後就會移出。"
        )
    if question is None and author:
        scope = f"「{exam_point}」" if exam_point else ""
        return f"出題者「{author}」的{scope}題目目前出不來，換個題型或換一位出題者試試。"
    if question is None and not exam_point:
        # Pool empty → generate one on-demand (runs in background via bot.py).
        # Only for the unrestricted path: a type-filtered miss shouldn't trigger
        # an unrelated 単語 generation that ignores the chosen 題型.
        gen = _build_generator(settings, db)
        question = gen.generate_one_question(level=level, theme=theme, question_type="単語")
    if question is None:
        if exam_point:
            return f"目前 {level} 的「{exam_point}」題型沒有可出的題目，換個題型或用 random 試試。"
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

    def handler(
        payload: str, original_text: str, chat_id: str | None = None
    ) -> "tuple[object, str, object]":
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
                try:
                    db.record_attempt(
                        question_id=question.question_id,
                        exam_point=question.exam_point,
                        tested_point=question.tested_point,
                        level=question.level,
                        chat_id=str(chat_id or ""),
                        chosen_index=chosen,
                        correct=(chosen == question.answer_index),
                    )
                except Exception:
                    logger.exception("quiz: record_attempt failed qid=%s", qid)
                db.mark_served(qid)
                return toast, new_text, _grade_actions_markup(db, question)
            if action == "t":
                # type-menu selection: rest = "<level>:<exam_point>"
                # ('*' = random/all, '!' = 錯題本/wrong-only, else a specific 題型).
                lvl, _, ep = rest.partition(":")
                wrong_only = ep == "!"
                exam_point = None if ep in ("", "*", "!") else ep
                served = _serve_question(
                    settings, db, lvl or _DEFAULT_LEVEL, _DEFAULT_THEME,
                    exam_point, str(chat_id or ""), wrong_only=wrong_only,
                )
                if isinstance(served, tuple):
                    text, markup = served
                    return None, text, markup
                return None, served, None  # error string → replace menu text
            if action == "au":
                # byauthor author-menu pick: rest = "<level>:<author_code>" → show
                # that author's 題型 menu.
                lvl, _, code = rest.partition(":")
                lvl = lvl or _DEFAULT_LEVEL
                author = _author_from_code(db, lvl, code)
                if author is None:
                    return "找不到該出題者（題庫可能已變動）", None, None
                text, markup = _render_type_menu(db, lvl, _DEFAULT_THEME, author=author)
                return None, text, markup
            if action == "ta":
                # author-scoped type pick: rest = "<level>:<exam_point>:<author_code>"
                # (peel the trailing code with rpartition — ep never contains ':').
                head, _, code = rest.rpartition(":")
                lvl, _, ep = head.partition(":")
                lvl = lvl or _DEFAULT_LEVEL
                author = _author_from_code(db, lvl, code)
                if author is None:
                    return "找不到該出題者（題庫可能已變動）", None, None
                wrong_only = ep == "!"
                exam_point = None if ep in ("", "*", "!") else ep
                served = _serve_question(
                    settings, db, lvl, _DEFAULT_THEME, exam_point,
                    str(chat_id or ""), wrong_only=wrong_only, author=author,
                )
                if isinstance(served, tuple):
                    text, markup = served
                    return None, text, markup
                return None, served, None
            if action == "vb":
                lvl, _, tail = rest.partition(":")
                mode, _, idx_str = tail.partition(":")
                lvl = lvl or _DEFAULT_LEVEL
                try:
                    index = max(0, int(idx_str))
                except ValueError:
                    index = 0
                rendered = _render_vocab_browser(
                    db, level=lvl, mode=mode or "weak", chat_id=chat_id, index=index
                )
                if isinstance(rendered, tuple):
                    text, markup = rendered
                    return None, text, markup
                return None, rendered, None
            if action == "gb":
                lvl, _, tail = rest.partition(":")
                mode, _, idx_str = tail.partition(":")
                lvl = lvl or _DEFAULT_LEVEL
                try:
                    index = max(0, int(idx_str))
                except ValueError:
                    index = 0
                rendered = _render_grammar_browser(
                    db, level=lvl, mode=mode or "weak", chat_id=chat_id, index=index
                )
                if isinstance(rendered, tuple):
                    text, markup = rendered
                    return None, text, markup
                return None, rendered, None
            if action == "vr":
                card = db.get_vocab_card(vocab_id=rest, level=_DEFAULT_LEVEL)
                if card is None:
                    return "找不到這張單字卡", None, None
                question = db.weighted_question(
                    level=card.level,
                    chat_id=str(chat_id or ""),
                    tested_point=card.headword,
                    author=card.author,
                )
                if question is None:
                    return "這張單字卡目前沒有可出的相關題目", None, None
                db.mark_served(question.question_id)
                text, markup = _question_view(question)
                return None, text, markup
            if action == "gr":
                card = db.get_grammar_card(card_id=rest, level=_DEFAULT_LEVEL)
                if card is None:
                    return "找不到這張文法卡", None, None
                question = db.weighted_question(
                    level=card.level,
                    chat_id=str(chat_id or ""),
                    tested_point=card.headword,
                    author=card.author,
                )
                if question is None:
                    return "這張文法卡目前沒有可出的相關題目", None, None
                db.mark_served(question.question_id)
                text, markup = _question_view(question)
                return None, text, markup
            if action == "vc":
                # Direct vocab-card view by vocab_id (jumped from grade result).
                card = db.get_vocab_card(vocab_id=rest, level=_DEFAULT_LEVEL)
                if card is None:
                    return "找不到這張單字卡", None, None
                text, markup = _render_vocab_card(card, mode="lookup", index=0, total=1)
                return None, text, markup
            if action == "gc":
                card = db.get_grammar_card(card_id=rest, level=_DEFAULT_LEVEL)
                if card is None:
                    return "找不到這張文法卡", None, None
                text, markup = _render_grammar_card(card, mode="lookup", index=0, total=1)
                return None, text, markup
            if action == "vrnd":
                level = rest or _DEFAULT_LEVEL
                cards = db.list_vocab_cards(
                    level=level, chat_id=str(chat_id or ""), mode="random"
                )
                if not cards:
                    return "目前沒有可用的單字卡", None, None
                text, markup = _render_vocab_card(cards[0], mode="random", index=0, total=1)
                return None, text, markup
            if action == "grnd":
                level = rest or _DEFAULT_LEVEL
                cards = db.list_grammar_cards(
                    level=level, chat_id=str(chat_id or ""), mode="random"
                )
                if not cards:
                    return "目前沒有可用的文法卡", None, None
                text, markup = _render_grammar_card(cards[0], mode="random", index=0, total=1)
                return None, text, markup
            if action == "ga":
                card = db.get_grammar_card(card_id=rest, level=_DEFAULT_LEVEL)
                if card is None:
                    return "找不到這張文法卡", None, None
                if not _vocab_audio_enabled(card):
                    return "這張文法卡目前沒有開放例句音檔", None, None
                try:
                    voice_params = db.get_voice_params(str(chat_id or ""))
                    _send_vocab_audio(settings, card=card, chat_id=chat_id, params=voice_params)
                except QuizVocabAudioError as exc:
                    logger.warning("quiz grammar audio failed card_id=%s: %s", rest, exc)
                    return "音檔生成失敗", None, None
                except Exception:
                    logger.exception("quiz grammar audio unexpected failure card_id=%s", rest)
                    return "音檔生成失敗", None, None
                return "已送出例句音檔", None, None
            if action == "va":
                card = db.get_vocab_card(vocab_id=rest, level=_DEFAULT_LEVEL)
                if card is None:
                    return "找不到這張單字卡", None, None
                if not _vocab_audio_enabled(card):
                    return "這張單字卡目前沒有開放例句音檔", None, None
                try:
                    voice_params = db.get_voice_params(str(chat_id or ""))
                    _send_vocab_audio(settings, card=card, chat_id=chat_id, params=voice_params)
                except QuizVocabAudioError as exc:
                    logger.warning("quiz vocab audio failed vocab_id=%s: %s", rest, exc)
                    return "音檔生成失敗", None, None
                except Exception:
                    logger.exception("quiz vocab audio unexpected failure vocab_id=%s", rest)
                    return "音檔生成失敗", None, None
                return "已送出例句音檔", None, None
            if action == "ls":
                if not rest.strip():
                    return "無法辨識歌曲", None, None
                text = _like_song(settings, db, f"https://youtu.be/{rest.strip()}")
                return "已加入最愛" if text.startswith("❤️") else "加入失敗", text, None
            if action == "lx":
                return "已取消", original_text + "\n\n（已取消加入最愛）", None
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
        from .miku_ranking import backfill_song_media
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
            media_backfill_fn=lambda: backfill_song_media(db),
        )
        scheduler.start()
        return scheduler
    except Exception:
        logger.exception("start_quiz_daily_scheduler: failed to start")
        return None
