# 語音控制前置閘門與自動個人化快路徑設計

狀態：設計提案  
追蹤 Issue：[#82](https://github.com/jojojen/aka_no_claw/issues/82)  
適用範圍：`aka_no_claw` command bridge、`aka_no_claw_web` 語音輸入、生活控制 action surfaces  
非目標：不導入付費雲端 STT；不以人工維護 hotwords、錯字 alias 或固定自然語句表作為核心方案。

---

## 1. 問題摘要

目前 Web 語音輸入的實際路徑是：

```text
MediaRecorder 完整錄音
  -> POST /api/command/transcribe
  -> LocalWhisperTranscriber / faster-whisper
  -> transcript
  -> frontend onSend(transcript)
  -> 通用 Chat tool router
  -> /search、/research、music、IR、Bluetooth 或其他工具
```

現行實作刻意讓語音 transcript 走和鍵盤文字相同的 `onSend()`，以共享 conversation、mode 與 NLP routing。這對自由問句合理，但對高頻短控制指令有兩個結構性問題：

1. **延遲過高**：即使只是「關電扇」或「下一首」，仍要完成整段錄音、上傳、完整自由語音轉錄、通用工具選擇與控制 dispatch。
2. **錯誤會被不可逆地消耗**：例如使用者說「關電扇」，STT 多次輸出「關鍵善」之類的近音文字；這個 transcript 直接進通用 router 後可能被判成 `/search`，系統根本沒有機會讓使用者選「關閉電扇」並建立個人化資料。

目前關聯程式位置：

- Web STT client：`aka_no_claw_web/frontend/src/api/commandClient.ts::transcribeAudio`
- Web 語音送出：`aka_no_claw_web/frontend/src/App.tsx::onTranscribe`
- Bridge STT endpoint：`src/openclaw_adapter/command_bridge_server.py::_handle_transcribe`
- 本機 STT：`src/openclaw_adapter/local_stt.py::LocalWhisperTranscriber`
- 通用 Chat routing／tool execution：`src/openclaw_adapter/command_bridge.py`
- 現有結構化控制 surface：music、Bluetooth、IR、workflow、schedule-home endpoints／actions

### 1.1 具體失敗案例

```text
使用者音訊：關電扇
STT transcript：關鍵善
目前行為：onSend("關鍵善") -> 通用 router -> /search
期待行為：voice gate 阻止 /search -> 顯示結構化 action 候選 -> 使用者選 fan.off -> 執行並學習
```

這不是單純的字詞修正問題。若用全域 replacement：

```text
關鍵善 -> 關電扇
```

會把正常聊天中的同音詞也改壞；若用手寫 hotwords／aliases，則會形成持續膨脹、需要人工維護且無法自然覆蓋個人說話方式的規則庫。

---

## 2. 設計目標

### 2.1 功能目標

1. 語音來源資訊在 STT 後仍保留，直到 routing 與 action execution。
2. 疑似未解析的短控制語音，在 `/search`、`/research` 或通用 LLM 工具真正執行前先被攔截。
3. 第一次沒有個人化資料時，系統可從現有 action registry 產生候選，讓使用者選擇，不需要手寫語句表。
4. 使用者確認並成功執行後，自動保存「音訊表示 -> action ID」的 prototype。
5. 同類指令越常成功使用，越可能在 Whisper 後段 Chat routing 之前完成；成熟後可直接走低延遲 action fast path。
6. 無法可靠辨認的輸入保留原本完整 STT + Chat routing fallback。
7. 所有功能本機執行，不依賴付費雲端語音 API。

### 2.2 安全目標

1. 只有低風險、可逆 action 可在高信心下自動執行。
2. 高風險 action 永遠需要顯式確認，不能只靠 prototype 相似度。
3. 使用頻率只能作 prior，不得壓過最低音訊相似度與候選 margin。
4. 未知語音必須能被 open-set rejection 擋下，不得硬塞進最接近 action。
5. 原始音訊預設不長期保存；產生 embedding 後即可刪除，除非使用者明確啟用除錯樣本留存。
6. 使用者可查看、刪除、停用或重置個人化資料。

### 2.3 效能目標

以本機、單使用者情境為基準：

- 成熟常用低風險 action：錄音停止後 p50 到 dispatch < 500 ms（不含裝置本身反應時間）。
- 需要 clarification：錄音停止後 p50 顯示候選 < 1 s。
- fallback：不得比現行完整 STT + router 路徑顯著變慢；gate 自身 p95 overhead < 100 ms。
- 自動快路徑 precision 優先於 recall；誤執行率目標低於 0.1%，未知語音 false accept 必須單獨量測。

---

## 3. 設計原則

### 3.1 不以 transcript 當唯一真相

短語音的自由轉錄容易因上下文不足產生近音字。語音控制應把 transcript 視為一個訊號，而不是唯一表示。系統同時保留：

- 音訊 embedding／prototype similarity
- transcript 與 STT metadata
- action registry
- 使用頻率／最近成功紀錄
- UI mode、目前裝置頁面與 active context
- 候選風險等級

### 3.2 不手寫自然語句詞表

候選 action 必須從結構化 registry 自動產生，而不是維護：

```python
{"關電扇": "fan.off", "關鍵善": "fan.off"}
```

可執行 action 應具有穩定 ID 與 metadata：

```json
{
  "action_id": "home.fan.living_room.off",
  "surface": "ir",
  "display_label": "關閉客廳電扇",
  "risk": "low",
  "reversible": true,
  "available": true,
  "context_tags": ["home", "living_room", "fan"]
}
```

`display_label` 是 UI 顯示與 accessibility 文案，不是 ASR hotword 或硬編碼語法。

### 3.3 確認要發生在工具執行前

錯 transcript 若先被 `/search` 消耗，就失去建立標註的機會。因此 gate 必須位於：

```text
transcription 完成後
但在通用 tool selection／execution 前
```

前端攔截可改善 UX，但後端仍必須具備同等防線，避免 Telegram、其他 client 或前端 regression 繞過。

### 3.4 Open-set，而不是封閉式強制分類

系統必須有 `unknown`／`fallback` 類別。即使某個 action 是最相近候選，只要：

- similarity 未達門檻，或
- 第一、第二候選差距不足，或
- speaker／環境偏離已知分布，

就不能直接執行。

---

## 4. 目標架構

```text
┌──────────────────────────┐
│ Browser / Telegram audio │
└─────────────┬────────────┘
              │ audio + provenance + duration
              v
┌──────────────────────────┐
│ Voice ingest / STT       │
│ - local faster-whisper   │
│ - audio embedding        │
│ - transcript metadata    │
└─────────────┬────────────┘
              v
┌──────────────────────────┐
│ VoiceIntentGate          │
│ 1. prototype lookup      │
│ 2. action availability   │
│ 3. risk policy           │
│ 4. confidence + margin   │
│ 5. unresolved-control    │
└──────┬────────┬──────────┘
       │        │
       │        └──────────── unknown / non-control
       │                         -> existing Chat router
       │
       ├─ high-confidence low-risk
       │     -> direct structured action dispatch
       │
       └─ medium-confidence / first-use suspicion
             -> clarification response
             -> user selects action or "當一般問題處理"
             -> execute selected action
             -> persist confirmed prototype
```

### 4.1 三段結果型別

```python
class VoiceResolutionKind(str, Enum):
    DIRECT_ACTION = "direct_action"
    CLARIFY = "clarify"
    FALLBACK = "fallback"
```

```python
@dataclass(frozen=True)
class VoiceResolution:
    kind: VoiceResolutionKind
    transcript: str
    action_id: str | None = None
    candidates: tuple[VoiceActionCandidate, ...] = ()
    confidence: float | None = None
    margin: float | None = None
    reason_code: str = ""
    learning_token: str | None = None
```

`reason_code` 必須是可量測的結構化值，例如：

- `prototype_high_confidence`
- `prototype_margin_too_small`
- `short_voice_search_guard`
- `no_prototype_first_use`
- `high_risk_requires_confirmation`
- `no_control_evidence`
- `prototype_store_disabled`

---

## 5. Voice provenance

### 5.1 Request contract

目前 STT 完成後只留下 transcript。需要保留來源與音訊識別：

```json
{
  "mode": "chat",
  "input": "關鍵善",
  "input_source": "voice",
  "voice": {
    "utterance_id": "uuid",
    "duration_ms": 1450,
    "stt_language": "zh",
    "stt_language_probability": 0.98,
    "audio_embedding_ref": "local:voice-embedding/uuid",
    "transcript_confidence": null
  }
}
```

注意：faster-whisper 不一定提供可直接當整句置信度的單一值，因此 contract 不應假造；可保存 segment log probability、no-speech probability 等原始 metadata，或保持 `null`。

### 5.2 前端行為

`onTranscribe(audio)` 不再直接：

```ts
await onSend(transcript)
```

改為：

```ts
const transcription = await transcribeAudio(audio, metadata)
const resolution = await resolveVoiceIntent({
  transcript: transcription.transcript,
  utteranceId: transcription.utterance_id,
  inputSource: "voice",
  durationMs,
})

switch (resolution.kind) {
  case "direct_action":
    await runStructuredAction(resolution.action)
    break
  case "clarify":
    showVoiceClarification(resolution)
    break
  case "fallback":
    await onSend(transcription.transcript, { inputSource: "voice" })
    break
}
```

後端仍要重做 gate，不能信任 client 宣稱的 `kind` 或 `action_id`。

---

## 6. Action registry

### 6.1 統一 action descriptor

現有 music／Bluetooth／IR／workflow／schedule-home surface 的按鈕與 callback 應投影到共同 descriptor：

```python
@dataclass(frozen=True)
class VoiceActionDescriptor:
    action_id: str
    surface: str
    display_label: str
    callback_data: str | None
    risk: ActionRisk
    reversible: bool
    available: bool
    context_tags: tuple[str, ...]
```

### 6.2 Registry 規則

- `action_id` 必須跨 restart 穩定。
- 不得把 transient callback token 當 action ID。
- 不可用 label 文字作唯一 identity。
- Registry 每次 resolution 時過濾 unavailable actions。
- 裝置移除後，既有 prototype 不刪除，但標為 dormant，不得執行。
- action semantics 變更時要 bump `action_version` 或 migration。

### 6.3 候選來源

第一次沒有 prototype 時，候選可依以下系統資料排序，而非手寫 utterance：

1. 當前 UI mode／life category 可用 actions。
2. 最近成功使用 actions。
3. 同一時段常用 actions。
4. 同一裝置頁面 actions。
5. 全域低風險 actions。

最多顯示 2–4 個，另加：

- `都不是，當一般問題處理`
- `取消`

不應顯示數十個 action。

---

## 7. Prototype learning

### 7.1 儲存模型

建議 SQLite：

```sql
CREATE TABLE voice_action_prototypes (
    prototype_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    action_version INTEGER NOT NULL DEFAULT 1,
    embedding_model TEXT NOT NULL,
    embedding_version INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    confirmed_count INTEGER NOT NULL DEFAULT 1,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_used_at REAL NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_voice_prototype_action
ON voice_action_prototypes(action_id, disabled, last_used_at);
```

另建 event audit：

```sql
CREATE TABLE voice_resolution_events (
    event_id TEXT PRIMARY KEY,
    utterance_id TEXT NOT NULL,
    resolution_kind TEXT NOT NULL,
    selected_action_id TEXT,
    predicted_action_id TEXT,
    confidence REAL,
    margin REAL,
    reason_code TEXT NOT NULL,
    user_confirmed INTEGER,
    execution_succeeded INTEGER,
    latency_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
```

預設不要存 transcript 全文與原始音訊；audit 只存必要 metadata。若保留 transcript，需有 retention 上限與設定開關。

### 7.2 Prototype 建立條件

只有下列情況可建立或強化 prototype：

1. 使用者在 clarification UI 明確選擇 action，且執行成功。
2. 使用者先拒絕錯誤候選，再選正確 action，且執行成功。
3. 已有高信心 direct action 執行後，使用者沒有在短時間內 undo／糾正，可增加 success count，但不得把每次錄音都永久新增為獨立 prototype。

不得從以下資料自動學習：

- router 推測但未經確認的 action
- `/search` 結果
- 執行失敗 action
- 高風險 action 的單次確認
- background noise／no-speech

### 7.3 聚合策略

第一版可採 nearest-prototype；之後按 action 聚合 centroid：

```text
prototype[action] = normalized mean of confirmed embeddings
```

為避免口音、距離、麥克風與環境差異被平均掉，可每個 action 保留小量 clusters，例如最多 5 個 medoids：

- 手機近講
- 房間遠講
- 夜間低聲
- 有風扇噪音

超過上限時，合併最相近 cluster 或淘汰低權重、久未使用 prototype。

### 7.4 Scoring

```text
base = cosine(audio_embedding, prototype)
frequency_prior = bounded_log(success_count)
recency_prior = exponential_decay(last_used_at)
context_prior = mode/device/time compatibility
penalty = rejection/failure history

final_score = base
            + w_frequency * frequency_prior
            + w_recency * recency_prior
            + w_context * context_prior
            - w_penalty * penalty
```

硬性條件：

```python
if base_similarity < MIN_AUDIO_SIMILARITY:
    fallback()
```

頻率不能讓低音訊相似度輸入通過。

### 7.5 Confidence policy

```python
if best.base_similarity >= direct_threshold \
   and best.final_score - second.final_score >= direct_margin \
   and best.action.risk == LOW \
   and best.confirmed_count >= min_confirmations:
    DIRECT_ACTION
elif best.base_similarity >= clarify_threshold:
    CLARIFY
else:
    evaluate_unresolved_control_gate()
```

門檻必須由本機收集的正負樣本校準，不能直接把論文數字照搬。

---

## 8. Unresolved-control gate

### 8.1 為什麼需要

完全沒有 prototype 的第一天，prototype matcher 不會產生候選；若直接 fallback，錯 transcript 仍會進 `/search`。因此需要一個**不依賴特定詞彙內容**的 suspicion gate，負責決定「是否先停下來問」。

### 8.2 可用訊號

Gate 可使用下列結構化訊號：

- `input_source == voice`
- 音訊時長短，例如 0.4–4 秒範圍（實際門檻可設定）
- transcript token／字數短
- transcript 無 URL、無附件、無長篇描述
- 通用 router 預計選 `/search` 或 `/research`
- search query 異常短、缺少明確資訊需求結構
- 當前 UI 在生活控制 mode
- 最近幾分鐘有控制 action context
- action registry 有可用低風險 actions
- ASR language probability 或 segment evidence 偏低

Gate 不應使用「電扇」「關」「開」等人工 keyword 作核心判斷。

### 8.3 執行位置

後端必須在 `_select_chat_tool_plan` 後、`_run_chat_tool` 前執行：

```python
plan = select_chat_tool_plan(req)
voice_resolution = voice_gate.intercept_before_tool(req, plan)

if voice_resolution.kind is CLARIFY:
    return voice_clarification_response(voice_resolution)
if voice_resolution.kind is DIRECT_ACTION:
    return dispatch_structured_action(voice_resolution.action_id)
return run_chat_tool(plan)
```

尤其：

```python
if req.input_source == "voice" \
   and plan.tool in {CHAT_TOOL_SEARCH, CHAT_TOOL_RESEARCH} \
   and gate.looks_like_unresolved_control(req, plan):
    # 禁止先執行工具
    return clarification
```

### 8.4 避免攔截正常短問句

「今天幾度」「現在幾點」也很短。不能只靠長度。避免誤攔方式：

- 若 router 對資訊 intent 有高置信且 query 完整，直接 fallback。
- 若 transcript 明確包含日期、天氣、人物、計算等結構，可降低 control suspicion；這些特徵應由現有 intent schema 或小型 classifier 產生，而不是散落 if/else keyword。
- 無 prototype 且 UI 不在控制 context 時，clarification 門檻提高。
- clarification UI 永遠提供「當一般問題處理」，避免死路。

第一版可以保守：只攔截 `voice + short + /search planned + life/control context`，再用 telemetry 調整。

---

## 9. Clarification contract

### 9.1 Response

```json
{
  "status": "needs_confirmation",
  "kind": "voice_action_clarification",
  "message": "我不確定你要執行哪個控制。",
  "learning_token": "signed-or-server-side-token",
  "candidates": [
    {
      "action_id": "home.fan.living_room.off",
      "label": "關閉客廳電扇",
      "risk": "low",
      "callback_data": "voice-confirm:token:0"
    }
  ],
  "fallback_action": {
    "label": "都不是，當一般問題處理",
    "callback_data": "voice-fallback:token"
  }
}
```

`learning_token` 在 server-side 綁定：

- utterance ID
- embedding reference
- transcript
- candidate snapshot
- expiry
- conversation ID

Client 不得自行提交任意 audio embedding + action ID 來污染 prototype store。

### 9.2 Confirmation

```json
{
  "learning_token": "...",
  "selected_action_id": "home.fan.living_room.off"
}
```

伺服器流程：

1. 驗證 token、expiry、conversation。
2. 確認 action 仍 available。
3. 依 risk policy 決定是否需要第二次確認。
4. 執行 action。
5. 只有成功後才 commit prototype／event。
6. 回傳結果與 undo（若 action 支援）。

### 9.3 Fallback

選「當一般問題處理」時：

- 使用 token 內原 transcript 重進既有 Chat router。
- 記錄 gate false positive。
- 不建立 action prototype。
- 不得再次被同一 gate 無限攔截；request 帶 `voice_gate_bypassed=true` 的 server-side continuation flag。

---

## 10. Fast path 與 STT 的關係

### 10.1 第一階段

仍先做現行 faster-whisper transcription，同時產生 embedding。主要收益來自：

- 阻止錯 transcript 進 `/search`
- 不再呼叫通用 Chat LLM/router
- 直接 action dispatch

### 10.2 第二階段

當 prototype 足夠成熟，可把 embedding matching 移到完整 transcription 前：

```text
audio -> embedding -> direct action / clarify
                    -> fallback only then faster-whisper
```

這才是完整的低延遲路徑。為降低風險，先 shadow mode：

- 系統照常跑 Whisper。
- prototype router 只記錄預測，不執行。
- 比較 prototype prediction 與最後確認 action。
- precision／false accept 達標後再啟用 direct execution。

### 10.3 Embedding model

第一版應封裝介面，不把系統綁死某模型：

```python
class VoiceEmbeddingEncoder(Protocol):
    model_id: str
    version: int
    def encode(self, audio: AudioRequest) -> Sequence[float]: ...
```

可評估：

- 從現有 Whisper encoder 取 pooled representation，減少額外模型下載，但要驗證短指令類別分離度。
- 使用小型、免費、本機 speech embedding／keyword encoder。
- 自行訓練只作後續選項，不是首版前置條件。

模型更換必須以 `embedding_model + embedding_version` 隔離；不同版本的向量不能直接比較，需 migration 或重新 enrollment。

---

## 11. 風險分類

### 11.1 低風險，可在成熟 prototype 下直接執行

- 音樂播放／暫停／下一首／音量小幅調整
- 燈、電扇等可逆家電開關
- 顯示狀態、掃描裝置

### 11.2 中風險，需要至少一次 UI confirmation

- 冷氣大幅溫度變更
- 執行長 workflow
- 發送非敏感通知
- 影響多個裝置的 scene

### 11.3 高風險，不允許 prototype 直接執行

- 購買、付款
- 發送訊息／公開貼文
- 刪除資料
- shell／部署／系統重啟
- 門鎖、安全系統
- 權限與安全設定

風險 metadata 應由 action registry 定義，不可在 voice gate 重複維護另一份清單。

---

## 12. 隱私與資料治理

1. 預設原始音訊只存在 request lifetime，embedding 完成後刪除。
2. Prototype DB 僅存向量與 action metadata；仍視為個人資料，檔案權限需限制。
3. 提供設定：
   - 啟用／停用語音個人化
   - 清除所有 prototypes
   - 清除單一 action prototypes
   - 匯出只含 metadata 的診斷摘要
4. 不將 embedding、transcript 或 audio 寫入一般 INFO log。
5. Debug audio retention 必須 opt-in、有明確期限、自動清理。
6. 若未來多使用者，prototype store 必須加入 owner／speaker scope；不可跨使用者共享。

---

## 13. Telemetry 與延遲量測

現有 STT 已記錄 `model_load_ms`、`transcribe_ms`。需擴充完整 trace：

```text
recording_ms
upload_ms
multipart_parse_ms
duration_probe_ms
embedding_ms
stt_ms
voice_gate_ms
clarification_render_ms
confirmation_wait_ms
chat_router_ms
action_dispatch_ms
device_ack_ms
total_after_recording_ms
```

每次 resolution 記錄：

- `kind`
- `reason_code`
- planned tool
- candidate count
- best similarity、margin
- 是否被使用者確認／拒絕
- action success
- 是否在 10 秒內 undo／糾正

核心指標：

- direct-action precision
- direct-action coverage
- unknown false acceptance rate
- clarification acceptance rate
- clarification fallback rate
- `/search` prevented count
- gate false-positive rate
- p50/p95 latency by path
- 每 action 的 prototype health

禁止只看整體 accuracy；對 open-set 系統，未知語音 false accept 是獨立且更重要的指標。

---

## 14. 實作切分

### PR 1：Voice provenance + pre-tool gate

- `WebCommandRequest` 增加 `input_source` 與 voice metadata。
- Web `onTranscribe` 保留 utterance ID／duration。
- Bridge 在 tool execution 前加入 `VoiceIntentGate`。
- 先實作 unresolved-control gate。
- `/search`／`/research` 若被 gate 攔截，不得先執行。
- clarification response 與「當一般問題處理」。
- 不做 prototype direct execution。

### PR 2：統一 action registry

- 將 music／Bluetooth／IR／workflow／schedule-home actions 投影為穩定 descriptor。
- 加 risk、reversible、availability、context metadata。
- clarification 候選只從 registry 產生。
- action ID migration 與 unavailable handling。

### PR 3：Embedding + prototype store

- `VoiceEmbeddingEncoder` abstraction。
- SQLite schema、retention、settings、清除操作。
- confirmation 成功後 enrollment。
- nearest prototype／centroid matching。
- shadow telemetry，不直接執行。

### PR 4：Direct low-risk fast path

- 校準 threshold／margin。
- 只開放低風險 action。
- undo／correction feedback。
- direct execution feature flag。
- 可選擇在成熟後將 embedding match 提前到完整 STT 前。

### PR 5：效能與 UX

- trusted duration，避免不必要的重複 duration decode。
- clarification UI、prototype 管理 UI。
- latency dashboard／benchmark fixture。
- 裝置 unavailable、模型版本 migration、資料清理。

---

## 15. 測試計畫

### 15.1 Unit tests

- voice provenance parse／serialization。
- action descriptor identity 穩定。
- risk policy。
- similarity threshold 與 margin。
- frequency prior 不能越過 base similarity floor。
- unknown rejection。
- prototype disabled／model-version mismatch。
- learning token expiry、conversation binding。
- fallback bypass 防止循環攔截。

### 15.2 Bridge integration tests

必測原始事故：

```text
input_source=voice
transcript="關鍵善"
router planned tool=/search
life/control context active
```

斷言：

- `/search` handler call count = 0
- response kind = `voice_action_clarification`
- candidates 來自 registry
- 選 `home.fan...off` 後 action call count = 1
- action 成功後 prototype count +1
- 再次相近 embedding 在 shadow／direct mode 命中同 action

另測：

- 選「當一般問題處理」後 `/search` call count = 1，且不再被重複攔截。
- 「今天幾度」不應被 gate 攔截。
- 高風險 action 即使 similarity 很高仍需 confirmation。
- unavailable action 不出現在候選且不能執行。
- action execution 失敗不 enrollment。
- malformed／replayed learning token 被拒絕。

### 15.3 Frontend tests

- `onTranscribe` 不再無條件呼叫一般 `onSend`。
- direct action、clarify、fallback 三種 UI path。
- clarification 選擇、取消與 fallback。
- accessibility labels。
- stream／network error 不丟失原 transcript。

### 15.4 Offline evaluation

蒐集本機測試集：

- 每個常用 action 至少多個距離、音量、背景噪音樣本。
- 相似但不同 action，例如 fan off／aircon off。
- 正常短問句。
- 無關語音與背景聲。
- 不同說話者負樣本。

報告：

- ROC／DET 或 threshold sweep
- FAR／FRR
- direct precision／coverage
- 每 action confusion matrix
- cold start vs 1／3／5 confirmed examples
- p50／p95 latency

### 15.5 Shadow rollout

1. Gate clarification 先上線，避免錯誤 `/search`。
2. Prototype matcher 只 shadow。
3. 達到 precision 門檻後，對單一低風險 action 開 canary。
4. 逐步擴大，保留 kill switch。

---

## 16. 驗收條件

- [ ] 語音 transcript 帶 `input_source=voice` 進 bridge，來源不在 `onSend` 前遺失。
- [ ] 「關電扇」被 STT 誤讀成「關鍵善」的 regression 中，若通用 router 選 `/search`，`/search` 不會先執行。
- [ ] 無 prototype 的 cold-start 情況能回傳 2–4 個由 action registry 產生的候選，以及「當一般問題處理」。
- [ ] 選擇候選後只執行一次對應 action；成功後才建立 prototype。
- [ ] 選擇「當一般問題處理」後原 transcript 只進一次既有 Chat router，不會被 gate 無限攔截。
- [ ] Prototype matcher 支援 unknown rejection；第一候選不足門檻或 margin 時不直接執行。
- [ ] 使用頻率 prior 無法讓低於 base similarity floor 的輸入通過。
- [ ] 高風險 action 永遠不能由 prototype 自動執行。
- [ ] 原始音訊預設在 embedding 完成後刪除；可清除個人化資料。
- [ ] action unavailable、action version／embedding version 不相容時 fail closed。
- [ ] Bridge、frontend 與 offline evaluation 測試覆蓋 cold start、clarify、direct、fallback、unknown、錯誤修正。
- [ ] 提供 p50／p95 latency、direct precision、coverage、unknown FAR 與 clarification fallback rate。
- [ ] 常用成熟低風險 action 在 direct mode 下不經通用 Chat LLM／`/search`。

---

## 17. 非目標

- 不以手寫 hotwords、固定句型 grammar 或錯字 replacement 作主要 personalization 機制。
- 不要求首版訓練新的 ASR 模型。
- 不移除 faster-whisper；它仍是自由語音與 fallback。
- 不讓所有短語音都進 clarification。
- 不允許只靠使用頻率直接執行 action。
- 不把 voice gate 變成第二套散落的家電指令 parser。

---

## 18. 文獻與技術依據

以下引用用來支持架構方向，不代表直接照搬其資料集門檻或模型。

### 18.1 Whisper：自由語音 fallback

Alec Radford et al., **Robust Speech Recognition via Large-Scale Weak Supervision**, 2022.  
https://arxiv.org/abs/2212.04356

Whisper 證明大規模多語弱監督訓練能提供強健的 zero-shot ASR，適合作為一般自由語音 fallback；但本設計不假設自由 transcript 對短、受限控制命令永遠可靠。

官方實作與 MIT License：  
https://github.com/openai/whisper

`faster-whisper`（CTranslate2 本機推論實作）：  
https://github.com/SYSTRAN/faster-whisper

### 18.2 Few-shot、prototype 與 open-set keyword customization

Manuele Rusci, Tinne Tuytelaars, **Few-Shot Open-Set Learning for On-Device Customization of Keyword Spotting Systems**, 2023.  
https://arxiv.org/abs/2306.02161

此研究直接支持「深度音訊特徵 encoder + prototype classifier + unknown rejection」的方向，並強調 on-device、少量使用者範例與 open-set false acceptance 控制。本文不直接採用其特定模型或 10-shot 門檻，但採用相同的系統原則：少量確認樣本建立 prototype，未知語音可拒絕。

Seunghan Yang et al., **Improving Small Footprint Few-shot Keyword Spotting with Supervision on Auxiliary Data**, 2023.  
https://arxiv.org/abs/2309.00647

支持小型 few-shot KWS representation 可透過適當輔助資料與 supervision 改善；可作為未來替換 embedding encoder 的研究基礎，而非首版必要條件。

### 18.3 Speaker personalization

Beltrán Labrador et al., **Personalizing Keyword Spotting with Speaker Information**, 2023.  
https://arxiv.org/abs/2311.03419

研究顯示把 speaker information 納入 KWS 可改善不同口音與族群表現，且額外參數與延遲可很小。這支持未來加入 speaker scope／speaker-conditioned scoring，但首版單使用者環境可先以 confirmed prototypes 達成個人化。

### 18.4 預測、prefetch 與語音助理延遲

Andreas Schwarz et al., **Personalized Predictive ASR for Latency Reduction in Voice Assistants**, 2023.  
https://arxiv.org/abs/2305.13794

研究使用部分 ASR hypothesis 預測完整 utterance 並預取下游結果，以隱藏 voice assistant latency；個人化提高預測價值。本設計的成熟 fast path 同樣利用高頻個人化歷史，提前於完整通用 routing 完成 action resolution，但採 fail-closed 與低風險限制，避免錯誤 prefetch 直接造成副作用。

### 18.5 本設計對文獻的取捨

- 採用：on-device、few-shot、prototype、open-set rejection、speaker／usage personalization、latency-aware pre-routing。
- 不採用：把固定 keyword vocabulary 當唯一產品介面。
- 不採用：從論文直接複製 threshold；所有門檻需以本機資料校準。
- 不採用：未知輸入強制歸類到最近 action。
- 保留：Whisper 作為自由語音 fallback，確保 open-domain 能力。

---

## 19. 決策摘要

核心決策不是「如何把『關鍵善』改回『關電扇』」，而是：

> 語音控制不能把自由 transcript 直接當成一般 Chat 輸入並立即執行工具。系統需要在工具執行前保留 voice provenance，以 open-set 個人化 prototype 與結構化 action registry 做解析；不確定時先讓使用者選，確認成功後才學習。成熟的低風險常用 action 才能走直接快路徑，其餘全部 fail closed 到 clarification 或原本 Whisper + Chat fallback。
