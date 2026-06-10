# /quiz 出題品管與教學迴圈（給接手者 / Codex 的交接文件）

本文件說明 `/quiz`（JLPT 測驗）功能的**出題品管迴圈**：如何讓地端模型出題、
逐題檢查、刪除不合格題、把該教的知識寫進「出題技巧知識庫」讓它愈出愈好，直到累積
足量合格題目再請 user 最終確認。

## 為什麼有這個迴圈（user 定的優先順序，務必照順序）

1. **答案正確性**（最高）
2. 符合 **JLPT N1** 等級
3. 取材自真實歌曲的趣味性／變化性

只有前項達標，才往下深入後項。正確性靠兩道防線：
- **雙重 LLM 驗證**：出題後由獨立 grader（只看題幹+選項、不給正解）重新作答，
  與作者答案一致才入庫，否則丟棄重生（見 `quiz_generator.py`）。
- **自我改善的出題技巧知識庫** `quiz_authoring_knowledge`（鏡像 codegen 知識庫）：
  審題時發現的問題，抽象成通用規則寫入；generator 下次出題前會檢索注入 prompt。

## 接手者的職責（這是「教學」的核心，不要丟給 user）

**你（agent）負責檢查、刪除、教學**；user 只在最後做總驗收。流程：

1. 請 generator 出 1 題（目前**只出単語題**，見下）。
2. **逐題檢查**是否合格（標準見下節）。
3. 不合格 → `delete_question(id)` 直接刪除，並把「該教它的知識點」寫進 KB。
4. 合格 → 留著。
5. 重複，直到**累積 ≥ 20 題你認為合格的題目**，才回報 user 做最終確認。

> user 原話：「讓他先出20題，他每出一題妳再每一題檢查，檢查發現任何問題就把該教他的
> 知識點寫到出題技巧知識庫，讓他下次可以改善…直到合格題累積到20題以上才叫我確認。」

## 目前的範圍限制（user 指示）

- **只出単語（詞彙）題**。読解／文法等之後再開放。原因：読解需要附上文章，模型太
  容易出成「沒有文章卻問理解」的不可作答題。程式已用參數 `question_type="単語"` 限定
  （`quiz_generator.generate_one_question`、`QuizDailyScheduler`、`quiz_command` 都已帶入）。
  要改題型只需改這個參數，schema 不用動（`exam_point` 是自由欄位）。

## Favorite Songs 快取優先原則

新增 `/quiz like song <youtube_url>` 後，歌曲蒐藏與分析流程改成「先重分析一次，之後重用 SQLite」。

### 收藏時要做的事

1. 解析 YouTube URL，取 `title / artist / youtube_short_url`
2. 優先找可用歌詞全文來源：
   - `VocaDB`
   - `歌ネット`
   - `UtaTen`
3. 抓歌詞全文後切句
4. 用 `SudachiPy + SudachiDict Full` 做形態素分析
5. 寫入 `data/quiz.sqlite3`：
   - `favorite_songs`
   - `lyrics`
   - `sentences`
   - `vocabulary_tokens`
6. 完成後標記 `favorite_songs.status = 'ready'`

### 之後出題 / 單字卡的資料優先順序

1. **先查 favorite-song 快取**
   - 先從 `favorite_songs.status='ready'` 的歌曲找可用 token / sentence / full lyrics
   - 先以「歌曲清單優先」為原則輪流出題，不是直接跳到一般歌曲池
   - 在單一 favorite song 內，再優先選 `used_quiz_count = 0`、`used_flashcard_count = 0` 的詞
2. **不要重新 fetch 網頁**
3. **要讀整首歌詞時，讀 SQLite 快取裡的 `lyrics.full_text`**
4. **必要時也可讀已快取的背景介紹 / 賞析文**
5. **不要重新跑 Sudachi**
6. 只有在下列情況才回到 LLM 或外部抓取：
   - 使用者剛收藏一首新歌
   - SQLite 裡沒有符合條件的 token
   - JLPT 等級無法用現有規則資料判斷
   - 需要生成題幹文案或解說文案

### 出題實務規則

- 若 favorite-song cache 中已有可用素材，**優先從你的 Favorite Songs 清單出題**。
- 在轉向一般歌曲來源前，應先確認目前 `status='ready'` 的 favorite songs 都至少已經出過題。
- 只有在 favorite songs 全部都已有覆蓋、或當前 favorite-song cache 找不到可用素材時，才回到一般歌曲池。
- 若 favorite-song cache 中已有原句，出題時應直接用該句或該句抽出的 tested point，不要再重新抓歌詞頁。
- 若題目品質需要更大的語境，應先讀該歌在 SQLite 中快取的**整首歌詞**，而不是只看 token。
- 若題目需要背景、主題或賞析層級的支撐，應優先讀取已快取的背景介紹 / 賞析文，而不是臨時重新上網抓。
- `used_quiz_count` / `used_flashcard_count` 是後續重複利用節流的基礎；新流程應先消耗未用過的詞。
- Favorite Songs 是**來源優先權**，不是正確性豁免。所有題目仍要滿足本文件前述的「自足、唯一解、N1 難度、grounded」要求。

## 合格標準（審題檢查清單）

一題**單語題**要全部通過才算合格：

1. **可作答 / 自足**：只看題幹+選項就能答，不需讀過未提供的歌詞或文章。
2. **正解唯一且正確**：客觀、可驗證；其餘三個確實錯誤。
3. **N1 難度**：考高階漢語複合詞、慣用句、細膩近義詞辨析；不是 N3 以下常見詞。
4. **選項同類**：四個皆詞彙／皆讀音／皆釋義，長度相近、看似合理。
5. **解說一致**：explanation 提到選項時，編號要與 `answer_index`（0 起算）一致，
   不可出現「選択肢3」卻說正解是 D 的矛盾；最好直接引用選項文字。
6. **連結齊全**：作答後會顯示 📖 歌詞原文連結與 🎵 YouTube 連結（來自 VocaDB，
   見下）。若 media_url 為 None 表示該歌在 VocaDB 沒有 YouTube PV，可換一首。

## 取材來源現況（重要：web 搜尋已改 Playwright + Yahoo Japan）

- DuckDuckGo 已淘汰；`/search` 現走 `search_yahoo_japan_playwright`（Playwright + Yahoo Japan）。
  但每日自動查詢量須壓在個位數（避免 IP 被封），故取材仍勿依賴 web 搜尋抓歌詞/YT。
- 改用 **VocaDB API** 一次取齊（`miku_ranking.py` `fetch_miku_song_sources`）：
  - `media_url` ← `pvs[].service == 'Youtube'` 的網址
  - `excerpt`（接地用的真實日文歌詞）← `lyrics[].translationType == 'Original'` 且
    `cultureCodes` 含 `ja` 的 `value`
  - `text_url` ← 該歌詞條目的 `url`（沒有就退回 VocaDB 歌曲頁）
- 取材失敗一律 graceful return `[]`，不報錯。

## 怎麼操作（具體指令）

題庫與知識庫路徑：`data/quiz.sqlite3`（env `OPENCLAW_QUIZ_DB_PATH`）。
在 `aka_no_claw` 目錄下用該 repo 的 venv 執行（`.venv/bin/python`）。

### 出一題並檢查（不經 Telegram，直接驅動 generator）

```python
from openclaw_adapter.quiz_db import QuizDatabase
from openclaw_adapter.quiz_command import _build_generator, _ensure_provider_registered
from assistant_runtime import get_settings  # 取得 settings（含 Ollama endpoint/model）

settings = get_settings()
db = QuizDatabase(settings.quiz_db_path)
_ensure_provider_registered("miku")
gen = _build_generator(settings, db)

q = gen.generate_one_question(level="JLPT N1", theme="miku", question_type="単語")
print(q)  # 檢查 stem / options / answer_index / explanation / media_url / text_url
```

> 地端模型是 qwen3:14b，雙重驗證 + 重試，單題可能要數十秒；不一致會自動丟棄重生。

### 檢查目前池子

```python
for x in db.recent_questions(limit=100):
    print(x.question_id[:10], x.exam_point, '| ans=', x.answer_index, '| yt=', x.source_media_url)
    print('   ', x.stem)
    for i,o in enumerate(x.options): print('     ', i, o)
    print('   解說:', x.explanation)
```

### 刪除不合格題

```python
db.delete_question("<question_id>")
```

### 教學：把知識點寫進出題技巧知識庫

兩種方式（擇一）：

- 程式直接寫（審題時用）：
  ```python
  db.upsert_authoring_knowledge(
      category="vocabulary",          # grammar|vocabulary|reading|distractor_design|level_calibration|source_grounding
      title="短標題",
      technique="一兩句、通用、可遷移的出題規則（不要綁特定歌曲/題目）",
      keywords=("關鍵字", ...),
      origin="distilled", confidence=0.6,
  )
  ```
- 經 Telegram（user 也能用）：`/quiz teach <要教它的知識點>` → 由 LLM 蒸餾成
  (category,title,technique,keywords) 寫入（見 `quiz_command._distill_authoring`）。

規則寫得**抽象、可遷移**（像 codegen 知識庫），keywords 要能被 `level/source_name`
等檢索字串命中（`retrieve_authoring_knowledge` 用 token overlap）。

### 已種下的初始規則（seed，confidence 0.8）

| category | title | 重點 |
|---|---|---|
| source_grounding | 題幹必須自足 | 嚴禁需要未提供歌詞/文章才能答的題 |
| distractor_design | 解說選項編號要與answer_index一致 | 不可自相矛盾的編號 |
| vocabulary | 単語題只考一個N1詞彙 | 四選項同類、僅一正確 |
| level_calibration | 確保N1難度 | 高階詞彙，非 N3 以下常見詞 |

## Telegram 端測試指令（給 user）

- `/quiz JLPTN1 miku` — 出一題＋A/B/C/D 按鈕（池空會即時生成，較慢）
- 點選項 → 顯示 ✅/❌＋詳解＋歌詞原文連結＋YouTube 連結
- `/quiz review` — 攤開正解的分頁檢視（含刪除鈕）
- `/quiz gen20` — 後台批次出 20 題
- `/quiz teach <知識點>` — 寫入出題技巧知識庫

## 收尾（達標後）

1. 累積 ≥20 題合格單語題後，回報 user 做最終確認。
2. 兩個 repo 跑 `.venv/bin/python -m pytest -q` 全綠。
3. 重啟龍蝦：`launchctl kickstart -k gui/$(id -u)/local.openclaw.telegram`，
   並用 `lsof -nP -p <pid> | grep ESTABLISHED` 確認連到 149.154.x.x:443。
4. 推送前先依 SKILL.md §A 摘要、等 user 說「推」。
