"""Quiz question generator with dual-LLM verification.

Priority #1 is answer correctness, so generation has two independent LLM passes:
  1. An *author* pass produces a question grounded in real source material, with
     retrieved authoring-knowledge injected into the prompt (the self-improving RAG
     loop — reviewer corrections accumulate there and steer future generations).
  2. A *grader* pass re-solves the question seeing ONLY the stem + options (never
     the intended answer). The question is accepted into the verified pool only if
     the grader independently lands on the same answer. Otherwise it's discarded and
     regenerated, up to a retry budget.

The generator is source-agnostic: it only talks to the ``SourceProvider`` interface,
so new themes (JPOP, essays, …) plug in with no change here.
"""

from __future__ import annotations

import json
import logging
import random
import re
import ssl
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from .quiz_db import (
    QuizDatabase,
    QuizQuestion,
    answer_leaks_into_stem,
    correct_option_is_verbatim_copy,
    derive_tested_point,
    format_authoring_knowledge_block,
    is_reading_exam_point,
    options_have_duplicates,
)
from .quiz_sources import QuizSource, SourceProvider, get_provider

logger = logging.getLogger(__name__)


def _strip_json_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    return text


def _parse_json(raw: str) -> dict | None:
    try:
        parsed = json.loads(_strip_json_fence(raw))
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


_VOCAB_RULES = (
    "本題型固定為「単語」（文字・語彙），不可改出文法／読解或其他題型。\n"
    "最重要：答案必須『客觀唯一』，獨立的解題者只看題幹+選項也必能選到同一個答案。\n"
    "因此一律採用 JLPT 官方文字・語彙的標準題型，三選一（不可自創主觀題型，"
    "嚴禁問『最も適切な意味』『この歌の…』這類沒有唯一解的主觀題）：\n"
    "\n"
    "【A 漢字読み】句中有一個加〈〉標記的 N1 漢字詞，問它的讀音。\n"
    "  例：stem「彼の発言で場が〈和〉んだ。『和んだ』の読み方は？」\n"
    "      options=[\"なごんだ\",\"やわらんだ\",\"あえんだ\",\"わらんだ\"] 正解=0。\n"
    "  四個選項皆為平假名讀音，只有一個是該詞真正讀法，其餘為似是而非的誤讀。\n"
    "  ★四個假名讀音選項必須『彼此完全相異』，不可有任何兩個重複；重複即無鑑別度。\n"
    "  ★鐵則：被考的〈〉詞『必須至少含一個漢字』。純假名／純片假名詞（如「メルト」"
    "「ありがとう」）絕對不可當考點——那沒有漢字可讀，不成題；若素材整句都沒有合適"
    "的含漢字 N1 詞，請改自選一個契合作品的含漢字 N1 詞，或改出其他兩種題型。\n"
    "\n"
    "【B 言い換え類義】句中有一個加〈〉標記的 N1 詞，問與它『意思最接近』的詞。\n"
    "  例：stem「彼は仕事に〈没頭〉している。〈没頭〉に最も近い意味は？」\n"
    "      options=[\"夢中になる\",\"飽きている\",\"困っている\",\"休んでいる\"] 正解=0。\n"
    "  正解須為公認同義／近義；其餘三個語意明顯不同（不可模稜兩可）。\n"
    "  ★鐵則：正解必須是『另一個不同的詞或片語』。被考的詞本身、或它換成純假名／"
    "漢字的同一個詞，都【絕對不可】當成任何一個選項——那是同一個詞，不算言い換え。\n"
    "  例（錯誤示範，禁止）：考〈躓いた〉卻把「つまずいた」當正解（那只是它的假名）。\n"
    "\n"
    "【C 文脈規定】給一個完整句子並挖空（用 ＿＿＿ 表示），問填入哪個 N1 詞最恰當。\n"
    "  例：stem「徹夜続きで＿＿＿が溜まっている。」\n"
    "      options=[\"疲労\",\"疲労感を解消\",\"健康\",\"睡眠\"] 正解=0。\n"
    "  只有一個詞在語法與語意上都正確，其餘三個放進空格會明顯不通。\n"
    "\n"
    "共同規則（務必遵守）：\n"
    "1. 只考一個 N1 等級的詞彙；考點詞請盡量取自下方素材片段中真實出現的詞，"
    "若素材沒有合適的 N1 詞，可自選一個契合作品風格的 N1 詞，但絕不可杜撰歌詞。\n"
    "2. 題幹必須『自足』——只看題幹與選項就能作答，絕不可要求讀過未提供的歌詞或文章，"
    "也不可把『理解整首歌』當成考點；歌詞只是取詞來源。\n"
    "3. 四個選項皆為同類（A 皆讀音／B 皆詞彙／C 皆詞彙），長度相近、看似合理，"
    "但只有一個客觀正確，其餘三個必須是『明確錯誤』而非『也說得通』。\n"
    "4. 解說(explanation)裡若提到選項，務必用與 options 相同的 0 起算順序，"
    "且與 answer_index 完全一致，不可出現自相矛盾的編號；請直接引用選項文字而非只寫編號。\n"
    "5. exam_point 請填該官方題型名稱：『漢字読み』或『言い換え類義』或『文脈規定』。\n"
    "6. explanation 結尾務必附一行『【読み】』，把題幹與所有選項中出現的每個漢字詞都標上"
    "正確平假名讀音，格式如：【読み】環状線（かんじょうせん）・東奔西走（とうほんせいそう）。"
    "助詞、純假名不用標；但凡含漢字的詞都要標，且讀音必須正確。\n"
)


_KUMITATE_RULES = (
    "本題型固定為「文の組み立て」（文の並べ替え），不可改出其他題型。\n"
    "最重要：答案必須客觀唯一，獨立解題者只看題幹＋四個片段就能排出同一答案。\n"
    "\n"
    "【格式】從下方素材片段中挑『一整句真實存在的歌詞』L（務必逐字取自素材，不可杜撰）。\n"
    "把 L 自然地切成『四個連續詞組片段』，這四個片段拼回去剛好等於 L。\n"
    "題幹是四個空格的句子，其中一格標上 ★，例如：「＿＿　＿＿　＿★＿　＿＿。」；\n"
    "把四個片段『打亂順序』放進 options，問『★ 那一格應填入哪一個片段』。\n"
    "  - options＝那四個被打亂的片段本身（每個都是 L 的真實片段，不可加入 L 沒有的詞）。\n"
    "  - answer_index＝排好正確語順後，落在 ★ 那一格的片段在 options 中的索引。\n"
    "  - 範例：L=「君のことを　いつまでも　忘れない　と誓う」，四片段切為\n"
    "    ［君のことを］［いつまでも］［忘れない］［と誓う］。\n"
    "    題幹「＿＿　＿＿　＿★＿　＿＿。」（★在第三格），\n"
    "    options=[\"と誓う\",\"君のことを\",\"忘れない\",\"いつまでも\"]，\n"
    "    正確語順 君のことを→いつまでも→忘れない→と誓う，★(第三格)=「忘れない」→ answer_index=2。\n"
    "\n"
    "【鐵則】\n"
    "1. 題幹『絕對不可』出現組合後的整句 L，也不可把正解片段直接寫進題幹——否則沒有鑑別度。\n"
    "2. 四個選項必須『互不相同』；嚴禁出現兩個一模一樣的選項。\n"
    "3. 四個片段必須能且只能拼回 L 這一種自然語順；其餘排列在文法或語意上明顯不通。\n"
    "4. explanation 用繁體中文說明正確語順，並寫出組合後的完整句子 L（逐字）。\n"
    "5. exam_point 請填「文の組み立て」。\n"
    "6. explanation 結尾附一行【読み】，把題幹與選項中所有含漢字的詞標上正確平假名讀音。\n"
)


# The three official 文字・語彙 subtypes — all share _VOCAB_RULES; naming one as the
# question_type pins the generator to it (so we can fill an under-target subtype
# without spending tokens on a subtype that's already met its quota).
_VOCAB_SUBTYPES = ("漢字読み", "言い換え類義", "文脈規定")


def _select_type_block(question_type: str | None) -> str:
    qt = (question_type or "").strip()
    if qt == "単語" or qt in _VOCAB_SUBTYPES:
        block = _VOCAB_RULES
        if qt in _VOCAB_SUBTYPES:
            block += (
                f"\n★本回合務必固定只出「{qt}」這一種官方題型，"
                f"exam_point 請填「{qt}」，不可改出其他兩種 文字・語彙 題型。\n"
            )
        return block
    if "組み立て" in qt or "組立" in qt:
        return _KUMITATE_RULES
    if qt:
        return f"本題型固定為「{qt}」題型（考點），不可改出其他題型。\n"
    return "題型（考點）可自由選擇文法／單字／読解等任何單選題能表達的型別。\n"


def _build_author_prompt(
    *, level: str, source: QuizSource, authoring_block: str, question_type: str | None = "単語"
) -> str:
    material = source.excerpt or "(無可用歌詞片段，請改出不依賴特定歌詞、契合該作品風格的通用 N1 単語題，切勿杜撰歌詞)"
    type_block = _select_type_block(question_type)
    exam_point_value = question_type or "文法|単語|読解|..."
    return (
        "你是嚴謹的日本語能力試驗（JLPT）出題老師。\n"
        f"請出一題「{level}」等級的單選題，取材自以下日文作品。\n"
        "最高原則：正解必須客觀、唯一、可驗證；干擾選項要看似合理但確實錯誤。\n"
        "語言規則：題目與選項用日文；解說用台灣繁體中文（zh-TW）。\n"
        f"{type_block}\n"
        f"作品名稱：{source.name}\n"
        f"素材片段：{material}\n\n"
        "出題技巧（務必遵守，這是歷次校正累積的規則）：\n"
        f"{authoring_block}\n\n"
        "tested_point 規則：填本題實際考的『具體知識點』，不是題型——\n"
        "  ・漢字読み／言い換え／用法 → 填被考的那個詞（如「咽返る」「操る」）。\n"
        "  ・文法系 → 填被考的那個句型／文法（如「〜ばかりに」「〜をよそに」）。\n"
        "  ・読解系 → 填一個概括主旨的短語即可。\n"
        "只輸出 JSON，格式如下（options 至少 4 個，answer_index 為 0 起算的正解索引）：\n"
        f'{{"exam_point": "{exam_point_value}", "tested_point": "具體考點", '
        '"stem": "題目文", '
        '"options": ["...", "...", "...", "..."], "answer_index": 0, '
        '"explanation": "為何此為正解、其他為何錯（繁體中文）"}'
    )


def _build_grader_prompt(
    *, level: str, stem: str, options: list[str], passage: str | None = None
) -> str:
    lines = [f"{i}. {opt}" for i, opt in enumerate(options)]
    # Reading questions are only answerable WITH 本文, so the correctness grader
    # must see it; self-contained types (vocab/grammar) pass passage=None.
    passage_block = f"本文：{passage}\n\n" if passage else ""
    return (
        f"你是嚴格的 JLPT {level} 解題者。下面是一題單選題，請獨立作答。\n"
        "只輸出 JSON：{\"answer_index\": <0起算的正解索引>, \"reason\": \"簡短理由\"}\n\n"
        f"{passage_block}題目：{stem}\n選項：\n" + "\n".join(lines)
    )


def _build_leak_probe_prompt(*, level: str, stem: str, options: list[str]) -> str:
    """Stem-leak probe: a reading question should be UNANSWERABLE without 本文.
    The grader is told it cannot see 本文 and to return -1 when the answer can't be
    pinned down from stem+options alone. If it still lands on the intended answer,
    the answer leaked into the stem → no discrimination → reject."""
    lines = [f"{i}. {opt}" for i, opt in enumerate(options)]
    return (
        f"你是嚴格的 JLPT {level} 解題者，但這次你【看不到本文】。\n"
        "請只憑下面的題幹與選項作答。如果『沒有本文就無法確定唯一答案』，請回 answer_index: -1；\n"
        "只有在不需本文、光看題幹與選項就能確定唯一答案時，才給出該索引。\n"
        "只輸出 JSON：{\"answer_index\": <0起算索引，或 -1 表示需要本文才能判斷>, \"reason\": \"簡短理由\"}\n\n"
        f"題目：{stem}\n選項：\n" + "\n".join(lines)
    )


class QuizGenerator:
    def __init__(
        self,
        *,
        db: QuizDatabase,
        endpoint: str,
        model: str,
        timeout_seconds: int = 90,
        ssl_context: ssl.SSLContext | None = None,
        max_retries: int = 3,
        json_call_fn: Callable | None = None,
    ) -> None:
        self._db = db
        self._endpoint = endpoint
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._ssl_context = ssl_context
        self._max_retries = max(1, max_retries)
        if json_call_fn is not None:
            self._json_call_fn = json_call_fn
        else:
            from .opportunity_agent import _call_ollama_json
            self._json_call_fn = _call_ollama_json

    def _llm_json(self, prompt: str, *, temperature: float = 0) -> dict | None:
        try:
            raw = self._json_call_fn(
                endpoint=self._endpoint, model=self._model, prompt=prompt,
                timeout_seconds=self._timeout_seconds, ssl_context=self._ssl_context,
                temperature=temperature,
            )
        except TypeError:
            # Custom json_call_fn (e.g. tests) may not accept temperature.
            raw = self._json_call_fn(
                endpoint=self._endpoint, model=self._model, prompt=prompt,
                timeout_seconds=self._timeout_seconds, ssl_context=self._ssl_context,
            )
        except Exception:
            logger.exception("quiz: LLM call failed")
            return None
        return _parse_json(raw)

    def generate_one_question(
        self,
        *,
        level: str,
        theme: str,
        provider: SourceProvider | None = None,
        question_type: str | None = "単語",
    ) -> QuizQuestion | None:
        """Generate, verify, and store one question. Returns the stored
        ``QuizQuestion`` or ``None`` if every attempt failed verification."""
        provider = provider or get_provider(theme)
        if provider is None:
            logger.warning("quiz: no source provider for theme=%r", theme)
            return None
        try:
            candidates = provider.fetch_candidates(limit=5)
        except Exception:
            logger.exception("quiz: provider.fetch_candidates failed theme=%r", theme)
            return None
        if not candidates:
            logger.info("quiz: no source material for theme=%r", theme)
            return None
        # Shuffle so repeated calls don't all build questions from the single
        # top-ranked song; attempt rotation then walks distinct sources.
        random.shuffle(candidates)

        for attempt in range(1, self._max_retries + 1):
            source = candidates[(attempt - 1) % len(candidates)]
            authoring = self._db.retrieve_authoring_knowledge(
                f"{level} {question_type or ''} {source.name} {source.source_type}"
            )
            self._db.mark_authoring_applied(tuple(k.knowledge_id for k in authoring))
            # First attempt deterministic; later attempts warm up so a rejected
            # question isn't reproduced verbatim (the underlying call pins temp=0).
            author_temperature = 0.0 if attempt == 1 else min(0.3 * (attempt - 1), 0.8)
            author = self._llm_json(
                _build_author_prompt(
                    level=level,
                    source=source,
                    authoring_block=format_authoring_knowledge_block(authoring),
                    question_type=question_type,
                ),
                temperature=author_temperature,
            )
            question = self._validate_and_verify(level=level, source=source, author=author)
            if question is not None:
                logger.info(
                    "quiz: generated+verified question source=%s exam_point=%s (attempt %d)",
                    source.name, question.exam_point, attempt,
                )
                return question
            logger.info("quiz: attempt %d/%d rejected (verification)", attempt, self._max_retries)
        return None

    def _validate_and_verify(
        self, *, level: str, source: QuizSource, author: dict | None
    ) -> QuizQuestion | None:
        if not author:
            return None
        stem = str(author.get("stem") or "").strip()
        options = [str(o).strip() for o in (author.get("options") or []) if str(o).strip()]
        explanation = str(author.get("explanation") or "").strip()
        exam_point = str(author.get("exam_point") or "").strip()
        tested_point = str(author.get("tested_point") or "").strip() or None
        try:
            answer_index = int(author.get("answer_index"))
        except (TypeError, ValueError):
            return None
        if not stem or len(options) < 4 or not (0 <= answer_index < len(options)):
            return None

        # Universal structural guards (every question type): a duplicated option or
        # an answer copied verbatim into the stem means zero discrimination — the
        # grader would 'agree' trivially. Reject before spending a grader call.
        if options_have_duplicates(tuple(options)):
            logger.info("quiz: rejected — duplicate options (no discrimination)")
            return None
        if answer_leaks_into_stem(stem=stem, options=tuple(options), answer_index=answer_index):
            logger.info("quiz: rejected — correct option appears verbatim in stem (answer leak)")
            return None

        is_reading = is_reading_exam_point(exam_point)
        # Correctness grader — never sees the intended answer. Reading questions are
        # only answerable WITH 本文 (the excerpt rendered to the user), so the grader
        # must see it; self-contained vocab/grammar types carry everything in the stem.
        passage = source.excerpt if is_reading else None
        grader = self._llm_json(
            _build_grader_prompt(level=level, stem=stem, options=options, passage=passage)
        )
        if not grader:
            return None
        try:
            grader_index = int(grader.get("answer_index"))
        except (TypeError, ValueError):
            return None
        if grader_index != answer_index:
            logger.info(
                "quiz: grader disagreed (author=%d grader=%d) — discarding",
                answer_index, grader_index,
            )
            return None

        if is_reading and not self._passes_reading_discrimination(
            level=level, stem=stem, options=options,
            answer_index=answer_index, excerpt=source.excerpt,
        ):
            return None

        if not tested_point:
            tested_point = derive_tested_point(
                exam_point=exam_point, stem=stem,
                options=options, answer_index=answer_index,
            )
        try:
            return self._db.insert_question(
                level=level,
                exam_point=exam_point or "unknown",
                stem=stem,
                options=tuple(options),
                answer_index=answer_index,
                explanation=explanation,
                source_type=source.source_type,
                source_name=source.name,
                source_text_url=source.text_url,
                source_media_url=source.media_url,
                source_excerpt=source.excerpt,
                tested_point=tested_point,
                verified=True,
            )
        except ValueError as exc:
            logger.info("quiz: rejected generation (%s) — discarding", exc)
            return None

    def _passes_reading_discrimination(
        self, *, level: str, stem: str, options: list[str],
        answer_index: int, excerpt: str | None,
    ) -> bool:
        """Two complementary anti-leak guards for reading types, each catching a
        different leak topology (a passing correctness grader can't see either):

          1. verbatim-copy — the correct option is a 本文 sentence lifted verbatim,
             so the question is 'spot the copied line', not comprehension. Caught
             deterministically by string overlap.
          2. stem-leak — the answer is derivable from stem+options WITHOUT 本文, so
             a reading question that should require 本文 has none. Caught by an
             inverted grader that is denied 本文 and must NOT land on the answer.
        """
        if correct_option_is_verbatim_copy(
            options=tuple(options), answer_index=answer_index, source_excerpt=excerpt,
        ):
            logger.info("quiz: reading question rejected — correct option is a verbatim 本文 copy")
            return False

        leak = self._llm_json(
            _build_leak_probe_prompt(level=level, stem=stem, options=options)
        )
        # Fail-open on a probe error: correctness is already verified, so don't
        # discard a good question over a transient LLM hiccup.
        if leak is not None:
            try:
                leak_index = int(leak.get("answer_index"))
            except (TypeError, ValueError):
                leak_index = -1
            if leak_index == answer_index:
                logger.info(
                    "quiz: reading question rejected — answer derivable without 本文 (stem-leak)"
                )
                return False
        return True


def _seconds_until_next(hour: int) -> float:
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class QuizDailyScheduler:
    """Daemon that generates a few verified questions per day into the pool."""

    def __init__(
        self,
        *,
        generator: QuizGenerator,
        level: str = "JLPT N1",
        theme: str = "miku",
        per_day: int = 2,
        hour: int = 4,
        question_type: str | None = "単語",
        media_backfill_fn: Callable[[], object] | None = None,
    ) -> None:
        self._generator = generator
        self._level = level
        self._theme = theme
        self._per_day = max(1, per_day)
        self._hour = hour
        self._question_type = question_type
        # Optional post-batch hook that heals any song question missing its 音檔 URL
        # (injected in production; left None in tests so they stay offline).
        self._media_backfill_fn = media_backfill_fn
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="quiz-daily-gen", daemon=True)
        self._thread.start()
        logger.info(
            "QuizDailyScheduler started — generates %d question(s)/day at %02d:00",
            self._per_day, self._hour,
        )

    def _loop(self) -> None:
        while True:
            time.sleep(_seconds_until_next(self._hour))
            try:
                self.generate_batch()
            except Exception:
                logger.exception("QuizDailyScheduler: batch failed")
            time.sleep(23 * 3600)

    def generate_batch(self) -> int:
        made = 0
        for _ in range(self._per_day):
            if self._generator.generate_one_question(
                level=self._level, theme=self._theme, question_type=self._question_type
            ):
                made += 1
        logger.info("QuizDailyScheduler: produced %d/%d question(s)", made, self._per_day)
        if self._media_backfill_fn is not None:
            try:
                self._media_backfill_fn()
            except Exception:
                logger.exception("QuizDailyScheduler: media backfill failed")
        return made
