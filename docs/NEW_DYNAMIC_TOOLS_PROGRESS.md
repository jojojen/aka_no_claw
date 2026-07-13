# `/new` 動態自寫工具 — 開發進度與接手指南

Last reviewed: 2026-06-20
Status: Current
Owner area: dynamic-tools

> 給接手者（人或 codex）：本檔是 single source of truth。每完成一段落會更新。
> 目標：Telegram `/new <需求>` → 本地 qwen3:14b 自己寫 Python 工具 → 護欄下執行 →
> 回答案，並把工具存進 gitignored `generated_tools/`（含 manifest）供重用。全程地端、零 API 費用。
> 完整設計見 plan：`/Users/jen/.claude/plans/3-ollama-modular-trinket.md`。
> 進行中遷移計畫（Planned）：把 `/new` + Chat 雲端模型脫離 OpenCode CLI、改走直接 HTTP，
> 並加 Mistral 切換 → 見 companion [NEW_OPENCODE_DECOUPLING_PLAN.md](NEW_OPENCODE_DECOUPLING_PLAN.md)。
>
> 2026-06 update：預設仍維持全地端 Ollama。若需要更穩定 codegen，可設定
> `OPENCLAW_CODEGEN_BACKEND=opencode` 讓 `/new` 的文字生成/修復/驗證走 OpenCode Big Pickle
> (`OPENCLAW_OPENCODE_BASE_URL=https://opencode.ai/zen/v1`,
> `OPENCLAW_OPENCODE_MODEL=big-pickle`)；生成後的 Python 工具仍由 OpenClaw 的 venv、
> sandbox、manifest/reuse 流程執行。實測 direct HTTP 被 Cloudflare 1010
> `browser_signature_banned` 擋住，所以 runtime 直接走
> `opencode run --pure -m opencode/big-pickle`。`opencode run -m opencode/big-pickle "hi"`
> 可作為機器 smoke test。CLI subprocess 會使用隔離的 `HOME` / `CLAUDE_CONFIG_DIR`，
> 避免讀到 `~/.claude/CLAUDE.md` 這類全域協作規則並污染 `/new` 答案格式。

## 驗收標準（使用者定義）
反覆「開發→測試→修正」直到兩個 benchmark 數字正確或誤差很小（幾 %）：
- **B1 0050**：今年以來到 5 月的年化報酬。正解（2026-05-31 抓 Yahoo 日線）：
  起點 2026-01-02 收 66.95、終點 2026-05-29 收 105.40、配息 1.0（01-22 除息）、147 天。
  YTD 價格報酬 +57.4%、含息總報酬 +59.7%；複利年化 ≈ +208.6%（價格）/ +219.5%（含息）。
- **B2 TSLA FY2025 財報簡單分析**：營收 $94.83B、淨利 $3.79B、稀釋EPS $1.08、
  毛利率 18.03%、營益率 5.11%、淨利率 4.00%；YoY 營收 -2.9%、淨利 -46.8%、EPS -47%
  （vs FY2024 營收 $97.69B/淨利 $7.13B/EPS $2.04）。方向性：營收小幅下滑、獲利大幅衰退。

## 環境事實（已驗證）
- ollama 在線 `http://127.0.0.1:11434`，有 `qwen3:14b`（最強 text model）。
- `.env`：`OPENCLAW_LOCAL_TEXT_MODEL=qwen3:14b`、`OPENCLAW_LOCAL_TEXT_TIMEOUT_SECONDS=75`。
- OpenCode Big Pickle 是 opt-in：`.env` 設 `OPENCLAW_CODEGEN_BACKEND=opencode` 才啟用；
  endpoint/model 預設 `https://opencode.ai/zen/v1` / `big-pickle`。
- Yahoo Finance chart API 可連（`query1.finance.yahoo.com/v8/finance/chart/0050.TW`）。
- knowledge DB 路徑：`settings.knowledge_db_path`（預設 `data/knowledge.sqlite3`）。
- aka venv：`aka_no_claw/.venv`。

## 架構接點（沿用、勿重造）
- 指令分派：`price_monitor_bot/src/price_monitor_bot/bot.py`
  - `*_COMMANDS` 常數區 line ~69-140；`_handle_command` 分派 line ~1107；
    重型指令用 `ack=...` + `reply_factory=lambda ...`（見 WEB_RESEARCH 分支 ~1153）。
  - 處理器 class `TelegramCommandProcessor.__init__` line ~802（注入式 handler 都在這）。
  - `run_telegram_polling(...)` line ~3244 把 handler 透傳給 processor。
  - `_help_text()` line ~2656。
- 注入：`aka_no_claw/src/openclaw_adapter/telegram_bot.py` line ~373 `_base_run_telegram_polling(...)`
  傳 `knowledge_handler=build_knowledge_handler(settings)`（line 394）。新 handler 比照。
- Ollama 呼叫範例：`price_monitor_bot/.../natural_language.py` `_post_generate`(~557) /
  `_resolve_generate_url`(~1034)，stdlib urllib POST `/api/generate`。

## 進度

### ✅ Task 1：codegen_knowledge RAG 表 + API（DONE）
檔：`aka_no_claw/src/openclaw_adapter/knowledge_db.py`
- 新表 `codegen_knowledge`（knowledge_id/category/title/technique/keywords_json/origin/
  confidence/times_applied/created_at/updated_at）+ index。
- `CodegenKnowledge` dataclass、`build_codegen_knowledge_id`、`CODEGEN_CATEGORIES/ORIGINS`。
- 方法：`upsert_codegen_knowledge`、`retrieve_codegen_knowledge(request,k)`（keyword/category/
  title 命中 + confidence tiebreak）、`mark_codegen_applied`、`all_codegen_knowledge`、
  `seed_codegen_knowledge`。
- 模組層 `format_codegen_knowledge_block`、`CODEGEN_SEED`（6 條抽象通則：年化簡單/複利、
  價格vs含息、API 先驗結構、JSON get 防呆、合理性檢查、ANSWER 輸出契約）。
- 已驗證：seed=6、retrieve 對 0050 query 正確排序、format/ mark 正常。

### ✅ Task 2：DynamicToolRunner（DONE）
檔（新）：`aka_no_claw/src/openclaw_adapter/dynamic_tools.py`。已 smoke test：
`/new 計算1到100總和` → qwen3:14b 生成→建 venv→護欄執行→回 5050（首試成功，69s 含建 venv）。
細節同下規格。`build_dynamic_tool_runner_from_settings` 已可用。

### ✅ Task 3：接線 /new（DONE）
- bot.py：`NEW_COMMANDS={"/new"}`、constructor + `run_telegram_polling` 加 `dynamic_tool_handler`、
  分派分支（ack「1-2 分鐘」+ reply_factory → `_handle_new_tool`）、`_help_text` 加一行。
- telegram_bot.py：`build_dynamic_tool_runner_from_settings(settings)` 建 runner，
  `dynamic_tool_handler=lambda req: runner.run(req)` 注入。
- `.gitignore` 加 `generated_tools/`；`TELEGRAM_TOOL_SPEC.md` 加 /new 條目。
- 回歸：aka 596 passed；price_monitor 386 passed（2 個 playwright 失敗為既有環境問題，與本次無關）。

### 🚧 Task 4：benchmarks + selftest + 迭代到正確（IN PROGRESS）
- 已建：`dynamic_tools_benchmarks.json`（B1/B2/B3）、selftest CLI（`python -m
  openclaw_adapter.dynamic_tools selftest`）、`tests/test_dynamic_tools.py`（mock，10/10 通過）。
- 跑法：`.venv/bin/python -m openclaw_adapter.dynamic_tools selftest`（會清 `generated_tools/`
  前請手動 `rm -rf`，否則會走重用分支）。實跑用真 qwen3:14b + 真 Yahoo API。

#### 迭代紀錄
- **run1（FAIL/FAIL）**：
  - B1：gens=1 但數字錯。模型(1)複利年化公式指數寫反 → 算出 2.36%（應 ~219%）；
    (2)只輸出年化、沒輸出「今年以來原始報酬」那個數字 → numeric check 掃不到 59.7。
  - B2：qwen3:14b 生成在 client timeout(225s) 逾時 → urlopen TimeoutError 整個 crash。
  - 修法（皆為抽象方法論，非教答案）：
    1. seed「年化要分簡單與複利」加強：明說指數是 365/天數（未滿一年 >1 會放大），
       附『非 benchmark 數字』的示例（+20%/90天→~108%）錨定指數方向；並要求**同時**輸出
       期間原始報酬與年化報酬。已 re-seed 進 `data/knowledge.sqlite3`（upsert 覆寫同 id）。
    2. `codegen_timeout` 由 `max(180, t*3)` 改 `max(420, t*5)`（給 14b thinking 更多時間）。
    3. `_check_numeric` 的 `label` 改 optional（單元測試用）。
- **B3（主判定指標）TSLA 選擇權定價**：`request` 把全部參數寫死（S=430,K=450,T=60天,r=4%,
  σ=60%,無息），用 Black-Scholes 算歐式買權。正解 CALL=$34.47（math.erf 算常態 CDF），
  容差 8%。全參數自含 → 可重現、不依賴即時行情 → **主要能力指標**。
  B3 排在 benchmarks.json 第一位。
- **seed rule 新增（共 7 條）**：`Black-Scholes 選擇權定價與標準常態 CDF 實作`
  （confidence=0.95）：含完整 d1/d2/call/put 公式、N(x)=math.erf 實作、常見 5 種錯誤。
  B3 相關 request 時這條排第一優先注入。
- **run2（B1 PASS / B2 FAIL）**：
  - B1：gens=1，YTD 60.67% ✅、複利年化 219.50% ✅（加強版 seed 有效）。
  - B2：gens=3 全失敗。模型在 f-string 裡寫 `{天數}` 當變數名但未定義（NameError），
    且錯誤印進 ANSWER block（違反 rule 5）。B2 需外部財報 API，暫為次要。
- **run3（B3 PASS / B1 FAIL / B2 FAIL）**：
  - B3 主指標：gens=1，**$34.47 完全命中** ✅。Black-Scholes seed rule 有效。
  - B1：FAIL（模型把「今年以來」抓成 2023 年資料，正確年是 2026）。
  - B2：gens=3 全失敗（用 chart API 抓股價報酬而非財報 API）。
  - **修法（run4）**：`_build_codegen_prompt` + `_repair_code` 注入 `今天日期: date.today().isoformat()`。
- **run4（B3 PASS / B1 FAIL / B2 逾時）**：
  - B3：gens=1，$34.47 ✅ 再次命中（主指標穩定）。
  - B1：gens=3 失敗。新錯誤：`price_return * 100` NoneType → adjclose 陣列有 None，
    model 取 start/end price 前沒過濾，直接 × 100 炸掉。日期注入有效（知道 2026 年）。
  - B2：逾時（thinking mode + 複雜財報，420s 不夠）。
- **改動（run5 起生效）**：
  1. `think=False` Phase 1（3 次）+ `think=True` Phase 2（1 次）escalation。
  2. `OllamaTextClient.generate()` 加 `think` kwarg。
  3. polling loop threading fix（ack+reply_factory 改 daemon thread，bot 不再凍結）。
  4. 強化 static rules：None 過濾。
- run5 進行中，確認 B3 在 think=False 下依然 PASS。

### （原 Task 2 規格保留如下）
檔（新）：`aka_no_claw/src/openclaw_adapter/dynamic_tools.py`
規格：`DynamicToolRunner.run(request)` →
1. 讀 manifest 判斷可重用（qwen 回 id 或 NONE）
2. NONE→ retrieve_codegen_knowledge 注入 + 靜態硬規則 + 強制 PLAN→code 兩段式，qwen 寫單檔
3. 解析 `# requires:` 裝進 `generated_tools/.venv`
4. 護欄 subprocess（shell=False, timeout, cwd, CLEAN_ENV 剝 OPENCLAW_*），用 venv python
5. 自我修復迴圈 ≤3；ModuleNotFoundError 特例直接補裝重跑
6. 成功擷取 `===ANSWER===`…`===END===`、登錄 manifest
7. 失敗蒸餾：≥2 次才成功時，要 qwen 抽象成通則 upsert(origin='distilled')
- `build_dynamic_tool_runner_from_settings(settings)`：無 text model/非 ollama → None。

### ⏳ Task 3：接線 /new（PENDING）
- bot.py：`NEW_COMMANDS={"/new"}`、constructor 加 `dynamic_tool_handler`、分派分支（ack+reply_factory）、
  `_help_text` 加一行；`run_telegram_polling` 透傳。
- telegram_bot.py：建 runner 注入。
- `TELEGRAM_TOOL_SPEC.md` 加 /new、`.gitignore` 加 `generated_tools/`。

### ⏳ Task 4：benchmarks + selftest + 反覆修到正確（PENDING）
- `generated_tools/benchmarks.json`（B1/B2）、selftest CLI、`tests/test_dynamic_tools.py`。
- 實跑 qwen 生成→執行→比對正解，迭代修 prompt/seed 直到誤差幾 %。

## 已知取捨 / 注意
- 專屬 venv：plan 原想用 aka requirements.txt 初始化，但那是 `-e ../sibling` 相對路徑，
  在 generated_tools/.venv 會解析失敗。**改為**：建乾淨 venv，只靠 script 的 `# requires:`
  按需裝（benchmark 會用 yfinance / 直接 urllib + Yahoo）。此為對 plan 的務實偏離。
- 14b 真實任務成功率有限，靠自我修復拉高；`/new` 會比固定指令慢（每輪 30–90s+）。
- 護欄非沙箱：仍可刪檔/連外網（開放 pip 的代價，使用者已同意）。

## R4 — 管線分解 refactor（issue #76，接手指南）

> 目標：把 3340 行的單體 `dynamic_tools.py` 拆成 `dynamic_tools/` 套件（9 個模組），
> 讓 generation / safety / execution / repair / evaluation 各自獨立、generator 無法
> 繞過 verifier。**完整責任→模組對照、威脅/資源、公開介面契約、分解不變式**都在
> `docs/R4_DYNAMIC_TOOLS_INVENTORY.md`（single source of truth，接手先讀它）。

進度：
- [x] R4.0（2026-07-13）：inventory 文件 + boundary 測試。
      新增 `docs/R4_DYNAMIC_TOOLS_INVENTORY.md`（責任地圖 §1、公開介面 §2、
      分解不變式 §3、slice 清單 §4）與 `tests/test_dynamic_tools_boundaries.py`
      （釘住 24 個外部消費者依賴的公開 symbol + safety 判定與 generator 無關 +
      safety/spec helper 是 module-level free function 而非 runner method）。
      No intended semantic change。`tests/test_dynamic_tools.py` 既有 ~90 測試已釘住
      行為契約，R4.0 只補「模組邊界」釘子。測試：142 passed。
- [x] R4.1（2026-07-13）：把單體 `dynamic_tools.py` 轉成 `dynamic_tools/` 套件。
      `git mv dynamic_tools.py → dynamic_tools/__init__.py`（**目前 `__init__.py`
      就是過渡期單體，程式碼都在裡面**；之後每個 slice 把一組 symbol 抽到獨立
      模組再 import 回來，`__init__` 逐步變薄）。調整：6 個相對 import `.X`→`..X`
      （深一層）、`_resolve_tools_dir` parents[2]→[3]、`_BENCHMARKS_PATH`
      `.parent`→`.parent.parent`（benchmark json 仍在 openclaw_adapter/ 下）。
      **關鍵設計決定**：`__init__` 當單體 = 讓 `mock.patch("...dynamic_tools.urlopen"
      / probe_*")` 這類 module-global patch 維持有效，直到該 symbol 真的被抽走時，
      對應測試才 retarget 到新模組（單次 churn、與搬移的 slice 同步）。曾試過
      `__init__` 當 facade + `_core` 當單體，導致 10 個 HTTP/probe 測試因 patch 打不到
      `_core` 命名空間而 fail，已放棄該做法。No intended semantic change。
      測試：dynamic_tools+boundary 142 passed；consumer 套件（command_bridge/
      telegram_bot/ir/catalog_planner/sns_buzz/fix/research）537 passed。
- [x] R4.2a（2026-07-13）：抽出 `dynamic_tools/specification.py`（stdlib-only leaf）。
      內容：output-protocol markers（`===ANSWER===`/`===CODE===`/`===PLAN===`/
      `===META===`/`===API_STRUCT===`、`_THINK_RE`、`_FENCE_RE`）、value objects
      （`AttemptTrace`/`TaskTrace`/`DynamicToolResult`/`SearchGroundingResult`/
      `SearchGroundingBudgetExhausted`/`ReusePlan`）、pure helpers（`_coerce_nonneg_int`/
      `_utc_now_iso`）、pure parsers（`_normalize_request`/`_extract_code`/`_extract_meta`/
      `_extract_answer`/`_extract_api_struct`/`_defaults_schema_from_code`/
      `_load_json_object`）。`__init__` 用 `from .specification import (...)` 全部
      import 回來，bare-name 引用照舊解析；順手刪掉 `__init__` 變 dead 的
      `dataclasses`/`datetime.datetime`/`timezone` import。這些 symbol 都沒有被
      任何測試 `mock.patch`，所以無需 retarget。No intended semantic change。
      測試：dynamic_tools+boundary 142 passed；consumer 套件（command_bridge/
      telegram_bot/ir/catalog_planner/sns_buzz/fix/research）537 passed。已 commit。
      **未完**：`knowledge_context.py`（RAG grounding）留待 R4.2b。
- [ ] R4.2b：抽 `knowledge_context.py`（reference/rule/search grounding + budget）。
- [ ] R4.3：抽 `providers.py`（protocol + 決定性失敗 fake）。
- [x] R4.4：抽 `safety.py`（靜態政策；既有拒絕字串維持相容）。
- [ ] R4.5：抽 `sandbox.py`（資源上限 + 每個終態都清乾淨）。
- [ ] R4.6：抽 `repair.py`（有界修復 + 重複嘗試偵測）。
- [ ] R4.7：抽 `evaluation.py`（generator-independent 驗證）。
- [ ] R4.8：抽 `catalog.py` + 收尾成 thin `service.py` facade。

接手要點：每個 slice 都是 code-motion，`dynamic_tools.py` 要持續 re-export §2 的
公開介面（`test_dynamic_tools_boundaries.py` 會擋回歸）；跑
`.venv/bin/python -m pytest tests/test_dynamic_tools.py tests/test_dynamic_tools_boundaries.py -q`
綠了才進下一片；任何行為改變都停下來另開 issue/PR。
