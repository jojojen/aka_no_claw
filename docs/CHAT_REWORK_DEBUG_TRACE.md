# Chat 重工（rework）除錯全紀錄 — 為什麼一輪對話跑了 3 次 /research

Last reviewed: 2026-07-05
Status: Current — fix shipped; kept as the debugging record and design rationale.
Owner area: dynamic-tools

這份文件記錄 2026-07-05 chat 重工 bug 的完整除錯思路：症狀 → 現場證據 →
三個根因 → 為什麼不用快取 → 最終設計（seed variables + tool ledger）→
驗證方式。目的是讓後續接手的人不用重新推導一遍。

抽象泛化後的教訓已寫入 codegen RAG（`knowledge_db.py` `CODEGEN_SEED`
category=orchestration「多步/重試流程要把已完成的子結果帶著走，不要重取」），
/new 與 /fix 產生／修復工具時會自動檢索到。

## 1. 症狀（使用者觀察）

測試指令（web chat，investment 視角）：

> https://jp.mercari.com/item/m36091474765?afid=… 這張卡投資角度多少值得買？你可以看他的外觀加入分析

使用者看到的問題：

1. bot 有用 /visionlook 看圖，但下一輪問「你剛才用了什麼」時，它忘記自己
   用過，**又重跑一次** /visionlook。
2. /research 失敗後**整組作廢**——其實已拿到部分有用資訊，理論上只要再補
   一次 /visionlook 就能統整出結論，卻從頭重來。

## 2. 現場證據（live bridge tmux log）

`tmux -L openclaw_codex capture-pane -t bridge` 還原出比使用者看到的更糟的
實況（15:15–15:28，同一輪對話）：

- **/research 執行了 3 次**（約 10 分鐘），三次的 query 文字都不一樣：
  一次帶完整 afid 參數的 URL、一次裸 URL＋補充問句、一次只有裸 URL。
- **/visionlook 執行了 2 次**，下一輪使用者追問時又跑了**第 3 次**。

這個「query 每次都長得不一樣」的細節後來直接否決了 exact-match 快取方案
（見 §4）。

## 3. 根因分析（三個獨立缺陷疊加）

Chat 的工具架構是三層（`command_bridge.py`）：

1. 單工具快速路徑：`_select_chat_tool_plan` → `_run_chat_tool` → 滿意度判斷
2. `__goal__` 路徑：GoalLoop draft → run → replan（`goal_loop.py` +
   `goal_planner.py` + `task_workspace.py` WorkflowRunner）
3. 滿意度升級：`_maybe_upgrade_tool_result_to_goal_loop` —— 單工具答案被判
   「不足以回答」時，升級成 goal loop 重新規劃

三個根因分別對應三層：

### 根因 A：升級時丟棄剛完成的工具結果

`_maybe_upgrade_tool_result_to_goal_loop` 升級成 goal loop 時，**只把原始
goal 文字**交給 planner，剛剛已經跑完的工具答案（例如 /research 的部分結
果）完全沒有帶過去。planner 看到 goal 裡有商品 URL、又看到工具清單裡有
/research，自然規劃出「再跑一次 /research」。第一次重工由此而來。

### 根因 B：replan 會重跑已成功的步驟

GoalLoop 失敗後 replan 產生**全新** workflow，`WorkflowRunner.run()` 對新
workflow 的每一步都是全新執行。前一輪已成功步驟的輸出（綁在舊 trace 的
變數表裡）沒有任何機制流進新一輪。replan prompt 只告訴 LLM「上次在哪步失
敗」，沒告訴它「上次哪些東西已經拿到了」。第二、三次重工由此而來。

### 根因 C：router 對「已執行過什麼工具」零記憶

跨輪的 chat history 是**前端回傳的可見文字**（`command_bridge_models.py`
`_sanitize_history`，只收 user/assistant 純文字）。工具執行這件事從頭到尾
不存在於任何 prompt 可見的地方——所以「你剛才用了 visionlook 嗎？」這種
問題，router 只能靠猜，而最像正解的動作就是再跑一次 visionlook。

## 4. 為什麼不用快取（被否決的方案）

第一直覺是「對 (tool, query) 做 memoization」。被 §2 的現場證據否決：
三次 /research 的 query 文字互不相同（帶參數 URL／裸 URL＋問句／裸 URL），
exact-match 必然 miss；而做語意相似度快取又等於再養一個判斷器，且商品頁
內容會變，快取失效條件說不清楚。

改採的原則：**不攔截執行，改讓「規劃端」看得到已完成的結果，由 LLM 自己
決定重用**——結構性復原迴圈，不做關鍵字／等值判斷（符合 no-hardcode 原
則）。

## 5. 最終設計

### 5.1 Seed variables（解根因 A、B）

`seed_variables: dict[str, str]`（變數名 → 已取得的完整內容）貫穿整條鏈：

- `WorkflowRunner`（`task_workspace.py`）：執行前把 seeds 預綁進
  `VariableStore`（provenance="carried over from earlier result"）；
  `Workflow.validate_references()` 接受 seed 名字作為合法引用。
- `goal_planner.py`：draft 與 replan 的 prompt 各插入一段
  「已完成的前置結果」（每個變數 500 字預覽＋通用指示「這些內容已經取得，
  不要為了重新取得它們而再執行指令」）；產出的 workflow 允許步驟直接
  `inputs` 引用 seed 變數。
- `GoalLoop`（`goal_loop.py`）：`scratch["seeds"]` 起始於建構參數，**每次
  workflow 跑完（不論成敗）就把 trace 綁到的變數 merge 進 seeds**；resume
  時也把舊 trace 的變數 merge 進來。於是 replan 拿到的永遠是「至今所有已
  成功步驟的輸出」。
- `command_bridge.py`：滿意度升級時
  `seeds = {_seed_variable_name_for_tool(plan.tool): tool_result.answer}`
  （`/research` → `prior_research_result`，純機械 slug，無領域詞）；
  goal 路徑帶圖時 `{"image_observation": observation}`。

### 5.2 Tool ledger（解根因 C）

`command_bridge.py` 每個 conversation 一份有界 deque（maxlen 8）：

- `_run_chat_tool` 成功記 `status="ok"`＋答案摘要（400 字），**失敗也記**
  `status="error"`＋例外訊息——下一輪 router 必須知道「試過且失敗」，
  才不會原樣重試。goal loop 整趟跑完也記一筆（步驟清單＋結果）。
- `_build_chat_tool_plan_prompt` 把 ledger 注入 router prompt，附通用指示：
  既有結果足以回答（包括使用者問「你做過什麼」）就用 no_tool 統整；先前
  失敗就改道，不要原樣重試。判斷本身仍由 LLM 做。

### 5.3 這不是硬編碼

Rule G 禁的是「開放世界分類用關鍵字清單」。ledger 記的是**已發生的事實**
（資料），seed 命名是機械 slug 化，prompt 指示是無領域詞的通用行為原則；
「要不要重用／改道」全部留給 LLM 判斷。

## 6. 驗證

- 單元／回歸：`tests/test_task_workspace.py`（seed 預綁＋validate）、
  `tests/test_goal_planner.py`（prompt 列出前置結果＋截斷）、
  `tests/test_goal_loop.py`（draft/replan/resume 收到 seeds）、
  `tests/test_command_bridge.py`（ledger 記錄、router prompt 注入、升級帶
  seed）。全套 2307 passed。
- E2E：丟棄式 8799 bridge（**不動 8781**），用原始 Mercari 案例兩輪驗證：
  turn 1 不得重跑同一工具；turn 2 問「你剛才用了什麼」必須走 no_tool 用
  ledger 回答，不得再跑 /visionlook。

## 7. 陷阱備忘（改這條鏈時要知道的事）

- `_ScriptedPlanner`（`eval_runner.py`）等所有 planner 實作的 draft/replan
  簽名要跟著加 `seed_variables=None`——GoalLoop 現在一律傳這個 kwarg，
  漏改會讓整批 eval case 掛掉（本次就踩過）。
- 測試裡手工組 `GoalLoopContinuation` 時注意 decider 邏輯：ok 的 trace ＋
  `next_action="draft"` 會在 draft 後直接判定完成、不再跑 workflow；要模
  擬「中斷後續跑」必須給 failed trace ＋ `completed=["draft: …"]`。
- ledger 是 server 端 per-conversation 狀態（`_conversation_key`），前端
  history 靠不住（有損、只有可見文字），不要試圖把工具紀錄搬回前端。
