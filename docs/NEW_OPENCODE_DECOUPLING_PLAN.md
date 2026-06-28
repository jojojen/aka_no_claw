# `/new` + Chat 脫離 OpenCode CLI、改走直接 HTTP — 研究結論與計畫

Last reviewed: 2026-06-28
Status: Planned
Owner area: dynamic-tools

> Companion of canonical [NEW_DYNAMIC_TOOLS_PROGRESS.md](NEW_DYNAMIC_TOOLS_PROGRESS.md).
> 本檔描述「尚未 ship」的遷移計畫（Planned）。實作完成、行為穩定後，把結論
> 併回 canonical 進度檔並把本檔標記 Historical 移入 `archive/`。
> 相關 issue：aka_no_claw #51（generalize /new loop）、#59（OpenCode runtime 隔離）。

---

## 1. 緣由

在 musubi-for-tenkyoku（Phase 6D）研究中，發現可以**不透過 OpenCode CLI**、用純 HTTP
直接呼叫 Big Pickle / Mistral 雲端模型，而且能穩定跑完真實的 generate→execute→repair loop。
這引出一個跨 repo 的問題：aka_no_claw 的 `/new`（dynamic_tools）與未來的 Chat 雲端模型，
是否也應該脫離 OpenCode CLI、改走直接 HTTP？

使用者額外想要：**Chat 的雲端模型能加入「切換 Mistral」的選項。**

## 2. musubi 端的實證結論（已驗證）

Phase 6D.0 三模型基線（instance `sympy__sympy-12419`，seed 42）全部 5 個 AC 通過、
`patch_accepted=true`：

| 模型 | Provider | 傳輸方式 | LLM 呼叫 |
|------|----------|----------|----------|
| Big Pickle (DeepSeek V4) | cloud | **Node `fetch` 直接 HTTP** | 18 |
| Mistral Nemo | cloud | Node `fetch` 直接 HTTP | 16 |
| qwen2.5-coder:7b | local/Ollama | HTTP | 20 |

musubi 的 client 抽象（`packages/server/src/phase6d/llm-client.ts`）：

- `P6DLLMClient` interface（`call(messages)`）+ `createLLMClient` factory
- `BigPickleClient`：`POST https://opencode.ai/zen/v1/chat/completions`，**無 auth**，
  body 走 **chat 格式（`messages`）**，`max_tokens` 必須 ≥ 4096（DeepSeek V4 是 CoT 模型，
  reasoning_content 先吃 token，太小會讓 `content` 空掉 → parse_fail）
- `MistralClient`：`https://api.mistral.ai/v1`，需 `MISTRAL_API_KEY`，內建 4 RPM 節流
- `OllamaClient`：本地 `http://localhost:11434`

## 3. aka_no_claw 現況（已查證 `dynamic_tools.py`）

關鍵發現：**直接 HTTP 的 client 早就寫好了，但 `/new` 實際路徑沒在用它。**

| Client | 形式 | 現況 |
|--------|------|------|
| `OpenCodeTextClient` | 直接 HTTP（OpenAI-compat，含 retry/abort/think-strip） | 已實作，但只在 `probe_opencode` 用到，**未進 /new runner build** |
| `OpenCodeCliTextClient` | `opencode run --pure` 子行程 | ← **/new 的 opencode backend 實際在用這個** |
| `OllamaTextClient` | Ollama HTTP | 預設 backend |

- `build_dynamic_tool_runner_from_settings`（dynamic_tools.py:2315-2327）：當
  `OPENCLAW_CODEGEN_BACKEND=opencode` 時，先 `probe_opencode_cli`、用 `OpenCodeCliTextClient`。
- `base_url` 設定**已存在**：`openclaw_opencode_base_url = "https://opencode.ai/zen/v1"`
  （settings.py:38）。
- `TextGenerationClient` 是 **Protocol**（dynamic_tools.py:192）——任何有 `.generate()`
  的物件都能插入。Mistral 只要再寫一個 conforming client。
- **OpenCode 在 /new 裡只是純文字產生器**（`client.generate(prompt)`）。所有 loop
  （執行 / 驗證 / 修復 / distill / sandbox / manifest reuse）都是 aka_no_claw 自己的 Python。
  → 拿掉 CLI 不會失去任何 agent 能力。

## 4. ⚠️ 核心矛盾（實作前必須先解決）

canonical 進度檔（NEW_DYNAMIC_TOOLS_PROGRESS.md:16-20）記載：

> 實測 direct HTTP 被 Cloudflare 1010 `browser_signature_banned` 擋住，所以 runtime
> 直接走 `opencode run --pure`。

但 musubi 的 Node `fetch` 直接 HTTP **成功**。兩者差異（待查證的假設）：

| 維度 | aka_no_claw（被擋） | musubi（成功） |
|------|--------------------|----------------|
| HTTP client | Python `urllib`（`urlopen`/`Request`） | Node `fetch`（undici） |
| 端點 | `/completions`（legacy text） | `/chat/completions`（chat） |
| body | `{"prompt": ...}` | `{"messages": [...]}` |
| User-Agent / TLS 指紋 | Python urllib 預設（非瀏覽器） | undici 預設 |

Cloudflare 1010 是針對 **client signature** 的封鎖。最可能原因是 Python urllib 的
TLS/JA3 指紋 + 預設 header 被判定為 bot；undici 的指紋剛好過關。次要可能是端點格式差異。

**這是實作的第一個未知數，必須先用實驗釐清，不可直接假設「翻轉成 HTTP 就好」。**

### Phase 0 結果（2026-06-28，已釐清）

從 aka_no_claw `.venv` 用 `urllib` 直連 zen 的探針結果：

| 測試 | 結果 |
|------|------|
| `/chat/completions` + Python 預設 UA | ❌ 403 CF 1010（重現文件記載的封鎖） |
| `/chat/completions` + 瀏覽器 UA | ✅ 200, `content='ok'` |
| `/completions` + 預設 UA | ❌ 403 CF 1010 |
| `/completions` + 瀏覽器 UA | ❌ 404（zen 沒有這個端點） |

**根因確定**：CF 1010 是被 **Python urllib 預設 User-Agent**（`Python-urllib/3.x`）判定為 bot
擋下的，與 TLS/JA3 指紋無關。musubi 的 Node fetch（undici UA）剛好過關。**解法：帶一個瀏覽器
`User-Agent` header 即可直連。** 另外 zen **只服務 `/chat/completions`**（chat 格式 `messages`），
沒有 legacy `/completions`——現有 `OpenCodeTextClient` 打 `/completions` 是錯端點（就算加 UA 也 404）。

→ Python 端可直連，無需保留 CLI 當主要 transport。Phase 1 確定可做。

## 5. 計畫（分階段）

### Phase 0 — 釐清矛盾（先做，會決定後面走向）
1. 從 aka_no_claw 的 Python 環境，用 `urllib` 打 `https://opencode.ai/zen/v1/chat/completions`
   （chat 格式、無 auth），看是否仍被 CF 1010 擋。
2. 若被擋：試加瀏覽器型 `User-Agent`、改 `Accept`/`Accept-Encoding`；或評估換 `requests`/
   `httpx`（不同 TLS 指紋）。
3. 若 Python 怎樣都過不了、但 Node 可以：保留一個極小 Node HTTP shim 當 transport，
   或評估把 zen 呼叫獨立成一個 broker subprocess（仍比 `opencode run` 輕、且不讀 repo 規則）。
4. 結論寫回本檔；決定 Phase 1 的 transport 形態。

### Phase 1 — 把 /new 的 opencode backend 切到直接 HTTP
- 修 `OpenCodeTextClient`：對齊 musubi 驗證過的 `/chat/completions` + `messages` + `max_tokens≥4096`。
- 翻轉 `build_dynamic_tool_runner_from_settings`：偏好 `OpenCodeTextClient`（HTTP），
  `OpenCodeCliTextClient` 只當「HTTP 不通」的 fallback（line 474 註解原本就是這個設計，
  只是實作做反了）。
- 保留既有 sandbox / manifest reuse / validation / repair / distillation / cross-validation 不動。

### Phase 2 — 加 Mistral 雲端切換（Chat + /new）
- 新增 `MistralTextClient(TextGenerationClient)`，照 musubi `MistralClient`（4 RPM 節流、
  `MISTRAL_API_KEY`、`/v1/chat/completions`）。
- 新增設定：雲端 provider 選擇（big-pickle / mistral），給 Chat 與 `/new` 共用。

### Phase 3 — 收斂 #59
- 一旦 runtime 不再起 `opencode run` 子行程，#59 的污染根因（讀 `CLAUDE.md`/`AGENTS.md`、
  吐 "my lord"、需要隔離 `HOME`/`XDG`/`CLAUDE_CONFIG_DIR`）**消失**，Phase 1 hotfix 與
  runtime workspace 隔離大半不需要做。
- #59 仍值得保留的部分：**tool-execution broker**（OpenCode/模型只能提出工具呼叫意圖，
  真正執行由 OpenClaw 做權限/side-effect 檢查）。那是獨立的安全議題，與 context 污染無關，
  之後若讓雲端模型觸發真實工具再做。

## 6. 為何先脫離再做 #51

#51（把 /new 一般化成 budget-aware troubleshoot-reflect-continue loop）需要完全掌控
message 構造、模型階梯、continuation state 注入。自己拿著 HTTP transport，比夾在
`opencode run` 子行程中間容易得多。先脫離 = 後面 #51 與 Mistral 切換都更好做。

## 7. 風險 / 注意

- **CF 1010**：Phase 0 若顯示 Python 端無法穩定直連，需誠實保留 CLI fallback，不可硬切。
- **zen 無 auth、非官方**：可能被限流或下架；HTTP 與 CLI 共擔此風險，非新增風險。
- **不可破壞既有 benchmark**：`/new` 既有 B1/B2 benchmark 與 sandbox/manifest 行為必須維持。
- **degrade 開放**：HTTP 不通要 fail-open 到 CLI 或 Ollama，不可讓 `/new` 整個死掉。

## 8. 驗收條件

- [ ] Phase 0 結論明確：Python 直連 zen 是否可行、用什麼 transport
- [ ] `/new` 在 opencode backend 下走直接 HTTP（無 `opencode run` 子行程）仍能生成/修復/驗證
- [ ] smoke test：prompt「只輸出 exactly: ok」不再出現 "my lord"/oath（因為不再讀全域 CLAUDE.md）
- [ ] Chat + `/new` 可切換 big-pickle ↔ mistral 雲端模型
- [ ] 既有 `/new` benchmark 仍通過
- [ ] HTTP 不通時 fail-open 到 fallback，不中斷服務
