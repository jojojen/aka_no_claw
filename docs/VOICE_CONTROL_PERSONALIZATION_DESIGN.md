# 語音控制前置閘門與自動個人化快路徑設計

Last reviewed: 2026-07-12
Status: Planned
Owner area: voice / command-bridge / web
Tracking issue: [#82](https://github.com/jojojen/aka_no_claw/issues/82)
Target repositories: `aka_no_claw`, `aka_no_claw_web`
Canonical role: 語音控制個人化、前置確認與低延遲快路徑的設計真相來源。

> 核心限制：全部本機執行，不依賴付費雲端 STT；不以人工維護 hotwords、誤讀 alias 或固定自然語句表作為主要方案。

---

## 0. 摘要

目前 Web 語音輸入在本機 STT 完成後，會把 transcript 直接送進和鍵盤輸入相同的 Chat tool router。這種「語音只是一種文字輸入法」的設計對自由問答合理，但對短、頻繁、低風險的控制指令有兩個問題：

1. **延遲偏高**：即使只是「關電扇」或「下一首」，仍要等待完整錄音、上傳、自由語音轉錄、通用意圖判斷與工具路由。
2. **誤辨識會先被工具消耗**：例如使用者說「關電扇」，STT 多次輸出「關鍵善」。錯 transcript 直接進通用 router 後可能觸發 `/search`，系統沒有機會在工具執行前讓使用者選擇正確 action，也無法建立個人化資料。

本設計新增四個能力：

- **Voice provenance**：語音來源、utterance ID、時長與音訊表示在 STT 後仍保留。
- **VoiceIntentGate**：位於通用工具執行前，輸出 `direct_action | clarify | fallback`。
- **自動 action 候選**：候選由既有 action registry 產生，不手寫自然語句。
- **Prototype learning**：使用者確認並成功執行後，保存「音訊 embedding → action ID」的個人化原型；常用指令累積足夠可靠證據後可直接走快路徑。

核心流程：

```text
audio
  -> local STT + audio embedding + provenance
  -> VoiceIntentGate
       -> high-confidence + low-risk: direct structured action
       -> medium-confidence / first-use suspicion: clarification
       -> unknown / non-control: existing Chat router
```

最重要的安全規則是：

> 對語音來源，疑似未解析的短控制命令不得直接執行 `/search`、`/research` 或其他開放式工具；必須先 clarification，或明確判定為 fallback。

---

## 1. 問題與現況

### 1.1 現行 Web 語音路徑

```text
MediaRecorder 完整錄音
  -> POST /api/command/transcribe
  -> LocalWhisperTranscriber / faster-whisper
  -> transcript
  -> frontend onSend(transcript)
  -> 通用 Chat tool router
  -> /search、/research、music、IR、Bluetooth 或其他工具
```

關聯程式位置：

- Web STT client：`aka_no_claw_web/frontend/src/api/commandClient.ts::transcribeAudio`
- Web 語音送出：`aka_no_claw_web/frontend/src/App.tsx::onTranscribe`
- Bridge STT endpoint：`src/openclaw_adapter/command_bridge_server.py::_handle_transcribe`
- 本機 STT：`src/openclaw_adapter/local_stt.py::LocalWhisperTranscriber`
- 通用 Chat routing／tool execution：`src/openclaw_adapter/command_bridge.py`
- 現有控制 surfaces：music、Bluetooth、IR、workflow、schedule-home endpoints／actions

### 1.2 可重現失敗

```text
使用者音訊：關電扇
STT transcript：關鍵善
目前行為：onSend("關鍵善") -> 通用 router -> /search
期待行為：voice gate 阻止 /search
          -> 顯示結構化 action 候選
          -> 使用者選 home.fan.*.off
          -> 執行成功
          -> 建立個人化 prototype
```

### 1.3 為什麼不能只做字串修正

以下作法不接受作為核心方案：

```python
MISREAD_ALIASES = {
    "關鍵善": "關電扇",
}
```

原因：

- 正常聊天中可能真的出現相同或相近文字。
- 不同設備、房間、口音、噪音會持續產生新變體。
- 新增 action 時還要同步維護自然語句。
- 規則無法自然適應使用者真正的發音。
- 別的 client 或語言入口可能繞過前端替換。

### 1.4 為什麼不採人工 hotwords

本設計不禁止底層 ASR 未來使用自動生成的 contextual bias，但不把人工 hotword 表當主幹。人工 hotwords 仍具有：

- 維護成本；
- action registry 與語音詞表漂移；
- 對個人口音與實際說法覆蓋有限；
- 容易把辨識問題藏在設定中，而非建立可驗證的意圖契約。

---

## 2. 目標、非目標與成功定義

### 2.1 功能目標

1. 語音來源資訊在 STT 後仍保留到 routing、clarification 與 action execution。
2. 疑似未解析的短控制語音在通用工具執行前被攔截。
3. 第一次沒有 prototype 時，候選由現有 action registry、可用性與 UI context 自動產生。
4. 使用者確認 action 且執行成功後，自動保存音訊 prototype。
5. 同類低風險指令越常成功使用，越可能在通用 Chat router 前完成。
6. 不確定或非控制輸入保留完整 STT + Chat router fallback。
7. 全部本機運作；原始音訊預設不長期保存。
8. 所有決策具有可量測的 `reason_code`，不可只留下自由文字 log。

### 2.2 安全目標

1. 只有低風險、可逆 action 可自動執行。
2. 高風險 action 永遠顯式確認。
3. 使用頻率只能作 prior，不能取代最低相似度與候選 margin。
4. 必須支援 open-set rejection；「最接近」不等於「足夠可信」。
5. action 已下架、裝置離線或 context 不符時，不得使用舊 prototype 自動 dispatch。
6. 學習只在「使用者確認 + action 成功」後提交。
7. 取消、失敗或使用者更正時不得建立正向 prototype。
8. 使用者可以檢視、停用、刪除與重置個人化資料。

### 2.3 效能目標

以下為設計目標，須由 benchmark 校準，不是未量測即宣稱已達成：

- 成熟常用低風險 action：錄音停止到 dispatch，p50 < 500 ms。
- Clarification：錄音停止到顯示候選，p50 < 1 s。
- Gate 自身：p95 overhead < 100 ms。
- Fallback 不得顯著慢於現行路徑。
- 自動執行 precision 優先於 recall。
- 未知語音 false accept rate 必須獨立量測。
- 高風險 action 的無確認自動執行率必須為 0。

### 2.4 非目標

- 不取代現有自由語音 STT。
- 不在第一階段訓練或 fine-tune Whisper。
- 不把所有語音都視為控制命令。
- 不使用雲端付費語音 API。
- 不用單純字串長度規則決定控制意圖。
- 不把 UI 顯示 label 當成固定 ASR grammar。
- 不在第一版支援多使用者聲紋權限控制；資料模型需預留 profile namespace。

---

## 3. 設計原則

### 3.1 Transcript 只是其中一個訊號

短語音上下文少，近音與同音錯誤常見。意圖判斷應同時考慮：

- audio embedding / prototype similarity；
- transcript 與 STT metadata；
- action registry；
- action availability；
- 最近成功紀錄；
- UI mode / active surface / room context；
- action risk；
- 候選 margin；
- utterance duration；
- 是否存在 URL、長篇問句或明顯資訊查詢訊號。

### 3.2 候選來自 registry，不來自手寫句型

標準 action metadata：

```json
{
  "action_id": "home.fan.living_room.off",
  "surface": "ir",
  "display_label": "關閉客廳電扇",
  "risk": "low",
  "reversible": true,
  "available": true,
  "context_tags": ["home", "living_room", "fan"],
  "dispatch": {
    "kind": "ir_action",
    "callback_data": "..."
  }
}
```

`display_label` 用於 UI、accessibility 與確認，不作為手寫 hotword 契約。

### 3.3 確認發生在工具執行前

必要順序：

```text
transcription / embedding
  -> voice resolution
  -> clarification 或 direct action
  -> 才能執行 open-ended tools
```

禁止順序：

```text
transcription
  -> /search 已執行
  -> 才問是不是要關電扇
```

### 3.4 Open-set，而非強制閉集分類

即使存在第一候選，只要任一條件不滿足就不能 direct action：

- `best_score < direct_threshold`
- `best_score - second_score < direct_margin`
- action 非 low-risk
- prototype 樣本數不足
- action 不可用
- embedding model/version 不相容
- context 明顯衝突
- utterance 超出控制指令分布

### 3.5 前端 UX 與後端安全雙層防線

前端負責：

- 快速呈現候選；
- 保留原始 transcript；
- 提供「都不是／當一般問題處理」；
- 顯示即將執行的 action。

後端負責：

- 不信任 client 提供的 risk；
- 驗證 learning token；
- 驗證 action availability；
- 阻止 unresolved voice 直接執行 open-ended tool；
- 原子化 learning commit；
- 審計與資料版本管理。

---

## 4. 目標架構

```text
┌──────────────────────────┐
│ Browser / Telegram audio │
└─────────────┬────────────┘
              │ audio + source + duration + context
              v
┌──────────────────────────┐
│ Voice ingest             │
│ - validation             │
│ - local STT              │
│ - audio embedding        │
│ - utterance record       │
└─────────────┬────────────┘
              v
┌──────────────────────────┐
│ VoiceIntentGate          │
│ - prototype lookup       │
│ - action registry        │
│ - availability           │
│ - risk policy            │
│ - score + margin         │
│ - unresolved guard       │
└──────┬────────┬──────────┘
       │        │
       │        └── FALLBACK
       │             -> existing Chat router
       │
       ├── DIRECT_ACTION
       │     -> structured dispatcher
       │     -> result
       │
       └── CLARIFY
             -> candidates + learning_token
             -> user selects action or fallback
             -> dispatcher
             -> on success: commit prototype
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
    candidates: tuple["VoiceActionCandidate", ...] = ()
    confidence: float | None = None
    margin: float | None = None
    reason_code: str = ""
    learning_token: str | None = None
```

建議 `reason_code`：

- `prototype_high_confidence`
- `prototype_sample_count_low`
- `prototype_margin_too_small`
- `prototype_model_mismatch`
- `action_unavailable`
- `high_risk_requires_confirmation`
- `first_use_control_suspicion`
- `short_voice_search_guard`
- `no_control_evidence`
- `explicit_fallback_selected`
- `prototype_store_disabled`

---

## 5. Voice provenance 與 API contract

### 5.1 STT 回應

目前只回 transcript 不足以支持後續學習。建議：

```json
{
  "status": "ok",
  "utterance_id": "018f...",
  "transcript": "關鍵善",
  "language": "zh",
  "language_probability": 0.98,
  "duration_seconds": 1.45,
  "embedding_status": "ready",
  "embedding_model_version": "voice-embed-v1",
  "message": ""
}
```

不在 client 回傳 embedding 向量；只回 opaque `utterance_id`，避免：

- payload 過大；
- client 篡改；
- embedding 成為不必要的外洩面；
- model migration 難以控制。

### 5.2 Command request

```json
{
  "mode": "chat",
  "input": "關鍵善",
  "input_source": "voice",
  "voice": {
    "utterance_id": "018f...",
    "duration_ms": 1450,
    "stt_language": "zh",
    "stt_language_probability": 0.98
  },
  "conversation_id": "..."
}
```

### 5.3 Resolve endpoint

可選擇獨立 endpoint：

```http
POST /api/command/voice/resolve
```

Request：

```json
{
  "utterance_id": "018f...",
  "transcript": "關鍵善",
  "conversation_id": "...",
  "ui_context": {
    "mode": "life",
    "surface": "ir",
    "room": "living_room"
  }
}
```

Response：direct action

```json
{
  "status": "ok",
  "kind": "direct_action",
  "action": {
    "action_id": "home.fan.living_room.off",
    "display_label": "關閉客廳電扇",
    "risk": "low"
  },
  "confidence": 0.94,
  "margin": 0.18,
  "reason_code": "prototype_high_confidence"
}
```

Response：clarify

```json
{
  "status": "ok",
  "kind": "clarify",
  "transcript": "關鍵善",
  "learning_token": "signed-or-server-side-token",
  "reason_code": "first_use_control_suspicion",
  "candidates": [
    {
      "action_id": "home.fan.living_room.off",
      "display_label": "關閉客廳電扇",
      "risk": "low",
      "score": 0.61
    },
    {
      "action_id": "home.ac.living_room.off",
      "display_label": "關閉客廳冷氣",
      "risk": "low",
      "score": 0.49
    }
  ],
  "fallback": {
    "label": "當一般問題處理"
  }
}
```

Response：fallback

```json
{
  "status": "ok",
  "kind": "fallback",
  "reason_code": "no_control_evidence"
}
```

### 5.4 Clarification commit endpoint

```http
POST /api/command/voice/confirm
```

```json
{
  "learning_token": "...",
  "selection": {
    "kind": "action",
    "action_id": "home.fan.living_room.off"
  }
}
```

或：

```json
{
  "learning_token": "...",
  "selection": {
    "kind": "fallback"
  }
}
```

後端流程：

1. 驗證 token 尚未使用、未過期、屬於該 utterance/session。
2. 重新讀取 action registry。
3. 驗證 action 可用性與 risk。
4. 執行 action。
5. 只有成功後才 commit prototype。
6. 回傳結構化 action result。
7. token 單次使用。

### 5.5 為何不讓 client 直接提交 action label

Client 只能提交 `action_id`，後端必須重新解析 registry，不得信任：

- `display_label`
- `risk`
- dispatch payload
- callback data
- prototype score

---

## 6. Action registry

### 6.1 統一介面

```python
@dataclass(frozen=True)
class VoiceActionDescriptor:
    action_id: str
    display_label: str
    surface: str
    risk: str
    reversible: bool
    available: bool
    context_tags: tuple[str, ...]
    dispatch_kind: str
    dispatch_payload: dict[str, object]
```

```python
class VoiceActionRegistry(Protocol):
    def list_actions(
        self,
        *,
        user_context: VoiceUserContext,
    ) -> Sequence[VoiceActionDescriptor]:
        ...
```

### 6.2 Action ID 穩定性

Action ID 是學習資料的外鍵，必須：

- 不含暫時 UI 排序；
- 不依 display label；
- 裝置重新啟動後穩定；
- 對同一語意不可隨意改名；
- 若 action 被替代，提供 migration mapping；
- 若 action 永久刪除，prototype 標記 orphaned，不可自動執行。

### 6.3 第一階段接入 surfaces

建議順序：

1. music：pause / resume / next / previous / volume；
2. IR：明確低風險開關與模式；
3. Bluetooth：已配對裝置的 connect/disconnect；
4. schedule-home 內的低風險既有 action；
5. workflow 只納入明確標示 `voice_safe=true` 的 action。

不建議第一階段加入：

- `/search`
- `/research`
- `/fix`
- 任意 shell
- 發送訊息
- 購買
- 刪除
- 解鎖
- 帳號、安全或網路設定

---

## 7. Audio embedding 與 prototype learning

### 7.1 抽象介面先行

第一版不應把架構綁死在單一模型：

```python
class VoiceEmbeddingBackend(Protocol):
    @property
    def model_version(self) -> str:
        ...

    def embed(self, audio: AudioRequest) -> list[float]:
        ...
```

選型評估條件：

- 本機免費；
- 中文與跨語言短詞有合理表現；
- CPU / Apple Silicon 延遲可接受；
- 可輸出固定維度向量；
- license 相容；
- 支援 batch/offline benchmark；
- 模型版本可 pin；
- 不要求保存原始音訊。

可評估方向：

- 自監督 speech encoder 的 pooled representation；
- query-by-example keyword spotting encoder；
- 輕量 speaker-independent acoustic encoder；
- ONNX / CoreML 可部署模型。

### 7.2 Prototype record

```python
@dataclass(frozen=True)
class VoicePrototype:
    prototype_id: str
    profile_id: str
    action_id: str
    embedding: tuple[float, ...]
    embedding_model_version: str
    confirmed_count: int
    rejected_count: int
    created_at: float
    updated_at: float
    last_used_at: float
    source: str              # clarification / explicit-enroll
    status: str              # active / disabled / orphaned
    context_tags: tuple[str, ...]
```

### 7.3 學習提交條件

建立或更新 prototype 必須同時滿足：

- 使用者在 clarification 中選了 action；
- learning token 合法；
- action 在執行時仍存在；
- action 執行成功；
- utterance embedding 可讀且 model version 相容；
- utterance 沒被標示為 no-speech、過長或品質不合格；
- 使用者未關閉個人化。

### 7.4 不應提交的情況

- 使用者選 fallback；
- action 執行失敗；
- action 被取消；
- client 重送已使用 token；
- 選擇與候選集合不一致；
- utterance 已過保存期限；
- embedding backend 失敗；
- 高風險 action 僅確認執行，但政策設定不允許學習；
- 使用者隨後立即更正「不是這個」。

### 7.5 聚合策略

先採多 prototype，而非只存單一 centroid：

- 同一 action 保留最近 N 筆可靠樣本；
- 保留不同環境或說法的 cluster；
- retrieval 以 nearest prototype 與 cluster score 結合；
- 定期合併過度相近樣本；
- 淘汰長期未用且低成功率樣本；
- 禁止單次樣本直接進 direct action。

建議成熟條件初始值：

```text
confirmed_count >= 3
且至少跨 2 個不同 session
且近 N 次無 negative correction
```

實際值須 benchmark 校準。

### 7.6 Negative feedback

錯誤執行後應提供「不是這個」：

- 增加 `rejected_count`；
- 降低 prototype trust；
- 可加入 hard negative pair；
- 連續錯誤達門檻時停用該 prototype；
- 不自動把錯誤轉移到另一 action，仍需重新確認。

---

## 8. VoiceIntentGate

### 8.1 輸入

```python
@dataclass(frozen=True)
class VoiceIntentInput:
    utterance_id: str
    transcript: str
    duration_ms: int
    language: str | None
    language_probability: float | None
    profile_id: str
    conversation_id: str | None
    ui_context: dict[str, object]
```

### 8.2 候選評分

建議分數不是單一來源：

```text
final_score =
    w_audio   * audio_similarity
  + w_usage   * bounded_usage_prior
  + w_recency * bounded_recency_prior
  + w_context * context_compatibility
  + w_avail   * availability_gate
```

硬性規則：

- `audio_similarity` 未達最低值時，其他 prior 不得救回。
- action unavailable 時直接排除。
- 高風險 action 不得 direct。
- 使用頻率須 bounded，避免熱門 action 吞掉未知語音。
- direct 判斷同時需要 absolute threshold 與 top-1/top-2 margin。

### 8.3 Direct action

```python
if (
    candidate.action.risk == "low"
    and candidate.action.reversible
    and candidate.prototype_confirmed_count >= min_confirmed
    and candidate.audio_similarity >= direct_threshold
    and candidate.margin >= direct_margin
    and candidate.action.available
):
    return DIRECT_ACTION
```

### 8.4 Clarification

以下情境進 clarification：

- 有控制證據，但未達 direct threshold；
- 第一與第二候選太近；
- prototype 樣本數不足；
- action 中風險；
- 首次使用且 voice suspicion 成立；
- router 傾向 `/search`，但輸入形態像未解析控制；
- action context 不唯一，例如多個房間都有電扇。

Clarification 最多顯示 3–5 個候選，且一定有：

```text
都不是／當一般問題處理
```

### 8.5 Fallback

以下情境直接 fallback：

- transcript 含 URL；
- 明顯長篇資訊問題；
- 無任何 prototype 或 context 證據；
- 音訊長度遠超控制分布；
- 目前 UI context 明確是 research；
- 使用者明確說「搜尋」「查一下」且 router/tool evidence 一致；
- prototype store 關閉且沒有 first-use control suspicion。

---

## 9. First-use unresolved-control gate

這是避免「方法 A 永遠學不到」的必要部分。

### 9.1 問題

若沒有 prototype 時一律 fallback：

```text
第一次說關電扇
  -> 關鍵善
  -> /search
```

系統永遠不會拿到正確 action label，無法啟動學習循環。

### 9.2 First-use suspicion 不應靠設備詞彙

第一階段可使用非語意與結構訊號：

- `input_source == voice`
- 音訊短；
- transcript 短；
- 沒有 URL；
- 沒有長篇問句結構；
- router 選擇 open-ended `/search` 或 `/research`；
- search query 極短且低資訊；
- UI 正在 life/control surface；
- 最近使用過低風險 action；
- action registry 有可用候選。

這些訊號只決定「是否先問」，不直接決定 action。

### 9.3 後端 search guard

```python
plan = select_chat_tool_plan(req)

if (
    req.input_source == "voice"
    and plan.tool in {CHAT_TOOL_SEARCH, CHAT_TOOL_RESEARCH}
    and voice_gate.should_clarify_before_open_tool(req, plan)
):
    return voice_gate.build_first_use_clarification(req)
```

此 guard 必須在 `_run_chat_tool` 前。

### 9.4 候選來源

首次沒有 audio prototype 時，候選排序可用：

1. 當前 UI surface actions；
2. 最近成功 actions；
3. 同房間或同裝置群 actions；
4. 全域常用低風險 actions；
5. 若仍無候選，fallback。

這不是自然語句硬編碼；是從可執行系統狀態產生的 action shortlist。

### 9.5 避免過度攔截

不得只因「短」就 clarification。至少需要：

```text
voice source
AND short-form signal
AND open-tool plan or control context
AND available low-risk candidates
```

資訊問句如「今天幾度」若 router 有明確 weather/tool evidence，應正常 fallback 到 Chat router，不顯示家電候選。

---

## 10. 前端設計

### 10.1 錄音生命週期

前端保存：

- `utteranceStartAt`
- `utteranceStopAt`
- MIME type
- active UI mode
- active control surface
- conversation ID

`duration_ms` 可由 recorder 時間計算並送後端；後端仍可執行上限驗證。

### 10.2 onTranscribe

現行：

```ts
const res = await transcribeAudio(audio)
await onSend(res.transcript)
```

目標：

```ts
const transcription = await transcribeAudio(audio, {
  durationMs,
  inputSource: "voice",
})

const resolution = await resolveVoiceIntent({
  utteranceId: transcription.utterance_id,
  transcript: transcription.transcript,
  conversationId,
  uiContext: getVoiceUiContext(),
})

switch (resolution.kind) {
  case "direct_action":
    await dispatchResolvedVoiceAction(resolution)
    return

  case "clarify":
    showVoiceClarification(resolution)
    return

  case "fallback":
    await onSend(transcription.transcript, {
      inputSource: "voice",
      utteranceId: transcription.utterance_id,
      durationMs,
    })
    return
}
```

### 10.3 Clarification UI

顯示：

- 「我聽到：關鍵善」
- 「你是要執行哪一個？」
- action buttons
- 「都不是，當一般問題處理」
- 可選「重新錄音」

限制：

- 不顯示內部 score；
- 可在 debug mode 顯示 reason code；
- 候選必須包含 risk/availability 已驗證結果；
- 按鈕點擊後 disable，防止重複提交；
- action failure 不建立 prototype；
- fallback 保留原 transcript。

### 10.4 Direct action UX

為避免無感誤執行：

- 顯示短暫狀態：「已辨識：關閉客廳電扇」
- 執行後顯示成功或失敗；
- 提供短時間「不是這個」或 undo（action 支援時）；
- direct action 不建立新的 prototype，只更新既有 prototype 使用統計。

---

## 11. 後端元件

建議新增：

```text
src/openclaw_adapter/voice/
  __init__.py
  models.py
  action_registry.py
  embedding.py
  prototype_store.py
  intent_gate.py
  learning.py
  policy.py
  metrics.py
```

責任：

- `models.py`：typed contracts。
- `action_registry.py`：聚合控制 surfaces。
- `embedding.py`：model abstraction。
- `prototype_store.py`：版本化持久化。
- `intent_gate.py`：pure decision logic。
- `learning.py`：token lifecycle 與 atomic commit。
- `policy.py`：risk、threshold、availability。
- `metrics.py`：latency 與 outcome counters。

Command bridge 只協調，不應再吸收大量 voice domain logic。

---

## 12. 儲存設計

### 12.1 SQLite schema

```sql
PRAGMA user_version = 1;

CREATE TABLE voice_utterances (
    utterance_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    transcript TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    language TEXT,
    language_probability REAL,
    embedding BLOB,
    embedding_dim INTEGER,
    embedding_model_version TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE voice_prototypes (
    prototype_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    embedding BLOB NOT NULL,
    embedding_dim INTEGER NOT NULL,
    embedding_model_version TEXT NOT NULL,
    confirmed_count INTEGER NOT NULL DEFAULT 1,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_used_at REAL NOT NULL
);

CREATE INDEX idx_voice_prototypes_profile_action
ON voice_prototypes(profile_id, action_id, status);

CREATE TABLE voice_learning_tokens (
    token_hash TEXT PRIMARY KEY,
    utterance_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    candidate_action_ids_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL
);

CREATE TABLE voice_action_stats (
    profile_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_success_at REAL,
    PRIMARY KEY (profile_id, action_id)
);
```

### 12.2 保存期限

建議：

- unresolved utterance embedding：10–30 分鐘；
- consumed utterance：prototype commit 後立即刪除或標記可 GC；
- 原始 audio：預設請求結束即刪；
- prototypes：直到使用者刪除或 policy 淘汰；
- debug audio retention：明確 opt-in，另有期限與 UI 提示。

### 12.3 Model migration

每筆 embedding 保存 model version。若新 model 上線：

- 舊、新 embedding 不直接比較；
- 舊 prototypes 標記 `model_mismatch`；
- 可要求重新 enrollment；
- 若保留原始音訊是 opt-in，才能離線重算；
- 不可靜默把不同維度向量混用。

---

## 13. 安全與隱私

### 13.1 Risk policy

```python
class VoiceActionRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
```

- Low：可逆家電、媒體播放。
- Medium：可能造成較大干擾，但可恢復。
- High：金錢、訊息、刪除、解鎖、安全設定、任意程式執行。

規則：

```text
LOW + high confidence -> direct allowed
LOW + medium confidence -> clarify
MEDIUM -> always clarify
HIGH -> explicit confirmation; no prototype-only auto execution
```

### 13.2 Replay protection

`learning_token`：

- server-side random；
- 儲存 hash；
- 綁定 utterance、profile、candidate set；
- 短 TTL；
- single-use；
- commit 時重新驗證 action。

### 13.3 Embedding privacy

音訊 embedding 仍可能含說話者與聲學資訊，不能當匿名資料。要求：

- 僅本機；
- 存放權限限制；
- 不寫入一般 analytics；
- export 需明確操作；
- reset 功能刪除 prototypes、utterances 與 stats；
- log 不輸出完整 embedding。

### 13.4 容錯

- prototype store 壞掉：fallback，不阻塞一般 Chat。
- embedding model 載入失敗：STT 仍可工作，reason=`prototype_store_disabled`。
- action registry 失敗：不得 direct；fallback 或顯示不可用。
- clarification commit action failure：保留錯誤結果，不學習。
- DB corrupt：使用既有 failure vocabulary，明確回報 `corrupt`，不可當空資料庫重建而遺失證據。

---

## 14. 效能與量測

### 14.1 Stage timing

記錄：

```text
recording_ms
upload_ms
multipart_parse_ms
duration_probe_ms
stt_model_load_ms
stt_decode_ms
embedding_ms
prototype_lookup_ms
gate_ms
clarification_render_ms
dispatch_ms
device_response_ms
end_to_end_ms
```

### 14.2 Outcome counters

```text
voice_resolution_total{kind,reason_code}
voice_direct_action_total{action_id,result}
voice_clarification_total{selected_kind}
voice_fallback_total{target_tool}
voice_false_accept_total
voice_negative_correction_total{action_id}
voice_prototype_total{status,model_version}
voice_learning_commit_total{result}
```

高基數 action ID 若不適合 metrics backend，改以 surface/risk 聚合，細節留 structured log。

### 14.3 Benchmark corpus

建立本機、不提交真實個資的 benchmark manifest：

```json
{
  "sample_id": "fan-off-001",
  "audio_path": "private/...",
  "expected": {
    "kind": "clarify",
    "selected_action_id": "home.fan.living_room.off"
  },
  "environment": "quiet",
  "session": "s1"
}
```

Corpus 至少包含：

- 同一 action 多次說法；
- 同音／近音困難樣本；
- 不同距離與噪音；
- 未知短句；
- 短資訊問句；
- URL / 長問句；
- action 不可用；
- 多房間同類裝置；
- 高風險 action；
- speaker mismatch（若未來多使用者）。

### 14.4 核心評估

- Direct action precision / recall；
- false accept rate；
- clarification top-k accuracy；
- fallback correctness；
- action success rate；
- p50/p95 latency；
- 首次學習到成熟所需確認次數；
- prototype drift；
- negative correction recovery；
- 舊 model version 行為。

---

## 15. 測試計畫

### 15.1 Unit tests

`VoiceIntentGate`：

- high score + margin + low risk -> direct；
- score 高但 margin 小 -> clarify；
- sample count 不足 -> clarify；
- high risk -> clarify；
- unavailable -> 排除；
- no evidence -> fallback；
- usage prior 不得救回低 acoustic score；
- model version mismatch -> 不比較；
- unknown speech -> fallback。

`PrototypeStore`：

- CRUD；
- schema version；
- TTL；
- model version isolation；
- orphaned action；
- corruption handling；
- concurrent update；
- reset。

`LearningToken`：

- TTL；
- single-use；
- wrong profile；
- action 不在 candidate set；
- duplicate commit；
- action failure 不 commit。

### 15.2 Bridge integration tests

1. `input_source=voice` + short ambiguous transcript + `/search` plan：
   - `/search` handler call count = 0；
   - response kind = clarify。
2. 使用者選 action：
   - action call count = 1；
   - success 後 prototype count 增加。
3. action failure：
   - prototype count 不變。
4. fallback：
   - 原 transcript 進 Chat router；
   - `/search` 此時才允許執行。
5. non-voice 同文字：
   - 保持現有 routing，不被 voice gate 攔截。
6. mature prototype：
   - Chat router call count = 0；
   - action call count = 1。
7. high-risk：
   - 不 direct。

### 15.3 Web tests

- `onTranscribe` 不再無條件 `onSend`；
- resolve direct -> action dispatch；
- resolve clarify -> 顯示候選；
- fallback -> 原 transcript `onSend`；
- 「都不是」只 fallback 一次；
- duplicate click 防護；
- action failure 不顯示學習成功；
- accessibility labels；
- recorder cleanup；
- STT error 不進 router。

### 15.4 Regression test：關電扇

```text
Given:
  voice transcript = 關鍵善
  input_source = voice
  no mature prototype
  chat planner would select /search
  registry contains available low-risk fan.off

Expect:
  /search call count = 0
  response = clarify
  candidate contains fan.off
  user selects fan.off
  fan.off call count = 1
  prototype committed after success
```

第二輪：

```text
Given:
  similar audio embedding
  mature fan.off prototype
Expect:
  direct action
  STT後通用 Chat router call count = 0
  fan.off call count = 1
```

### 15.5 Unknown speech regression

```text
Given:
  unrelated short voice
  no sufficiently similar prototype
Expect:
  no direct action
  fallback or clarification with no automatic execution
```

---

## 16. PR 拆分

### PR 1：Voice provenance 與 unresolved-control gate

範圍：

- request model 增加 `input_source` / voice metadata；
- STT 回傳 `utterance_id`；
- action registry 最小介面；
- `/search` / `/research` 前置 voice guard；
- clarification response contract；
- Web clarification UI；
- 不做 embedding direct path。

驗收：

- 「關鍵善」語音不再直接 `/search`；
- 非語音不受影響；
- 「都不是」可 fallback。

### PR 2：Embedding backend 與 prototype store

範圍：

- embedding abstraction；
- 本機 backend；
- SQLite schema/version；
- utterance TTL；
- prototype CRUD/reset；
- benchmark harness。

驗收：

- 模型版本隔離；
- 原始音訊刪除；
- store failure fail-soft。

### PR 3：Learning transaction

範圍：

- learning token；
- clarification confirm endpoint；
- action-success 後 atomic commit；
- negative feedback；
- prototype management UI。

驗收：

- action failure 不學習；
- duplicate token 不重複執行；
- 可刪除/reset。

### PR 4：Direct fast path

範圍：

- score + margin policy；
- mature prototype direct action；
- risk enforcement；
- latency metrics；
- end-to-end benchmark。

驗收：

- 成熟低風險 action 不進 Chat router；
- high-risk 永遠確認；
- unknown false accept 達標。

### PR 5：Streaming / pre-ASR optimization（延後）

完成前四個 PR 並取得真實量測後才評估：

- 錄音期間 streaming embedding；
- partial audio early match；
- STT 與 embedding 並行；
- 前端停止錄音前預熱；
- 小型 KWS / query-by-example encoder。

---

## 17. Acceptance criteria

### 功能

- [ ] 語音 request 保留 `input_source=voice` 與 `utterance_id`。
- [ ] 「關電扇 → 關鍵善」不會在 clarification 前執行 `/search`。
- [ ] First-use 候選來自 action registry，不依手寫 hotwords/aliases。
- [ ] Clarification 一定有「都不是／當一般問題處理」。
- [ ] 選 action 且執行成功後才建立 prototype。
- [ ] action 失敗、取消、fallback 不建立正向 prototype。
- [ ] 成熟低風險 prototype 可直接 dispatch，不進通用 Chat router。
- [ ] 中風險與高風險 action 不得 prototype-only direct。
- [ ] action unavailable 時舊 prototype 不得執行。
- [ ] 使用者可檢視、停用、刪除與重置個人化資料。
- [ ] 一般文字輸入行為不變。
- [ ] 一般語音資訊問句仍可 fallback 到原路徑。

### 安全與資料

- [ ] learning token 短效、single-use、綁定候選集合。
- [ ] 後端重新驗證 action ID、risk、availability。
- [ ] 原始音訊預設不長期保存。
- [ ] embedding model version 持久化並隔離。
- [ ] prototype store corruption 明確回報，不靜默重建。
- [ ] 高風險無確認自動執行測試為 0。

### 測試

- [ ] Unit tests 覆蓋 direct/clarify/fallback。
- [ ] Bridge E2E 斷言 ambiguous voice 的 `/search` call count = 0。
- [ ] E2E 斷言 clarification action success 後 prototype 增加。
- [ ] E2E 斷言成熟 prototype 的 Chat router call count = 0。
- [ ] Unknown/open-set corpus 有 false accept 測試。
- [ ] Web tests 覆蓋三種 resolution。
- [ ] DB schema migration、TTL、reset 測試。
- [ ] CI 加入 deterministic synthetic embedding backend，避免測試依賴大型模型。

### 效能

- [ ] 建立 stage latency log。
- [ ] 報告 baseline 與實作後 p50/p95。
- [ ] 報告 direct precision、false accept、clarification top-k accuracy。
- [ ] 未達 threshold 前不得宣稱「越常用越快」已完成。

---

## 18. Rollout 與 feature flags

建議設定：

```text
OPENCLAW_VOICE_GATE_ENABLED=false
OPENCLAW_VOICE_PERSONALIZATION_ENABLED=false
OPENCLAW_VOICE_DIRECT_ACTION_ENABLED=false
OPENCLAW_VOICE_DEBUG_RETAIN_AUDIO=false
```

Rollout：

1. Shadow mode：只計算 resolution，不攔截、不執行。
2. Clarification-only：攔截 unresolved voice，但不 direct。
3. Prototype learning：建立資料，仍不 direct。
4. Direct action：僅 allowlist 的 low-risk surfaces。
5. 擴大 surfaces 前先審查 false accept 與 negative correction。

Shadow log 不得保存完整 embedding 或原始音訊。

---

## 19. 替代方案分析

### 19.1 只換更大的 Whisper

不採為主方案：

- 增加延遲與資源；
- 仍可能誤辨識短句；
- 無法解決錯 transcript 直接進 `/search`；
- 不建立可重用的 action-level 個人化。

### 19.2 人工 hotwords

不採為主方案：

- 維護與 drift；
- 仍依賴文字 decoder；
- 不直接學習使用者聲學模式。

### 19.3 Vosk grammar / 固定 grammar

適合封閉式固定命令，但本系統 action 動態、跨 surfaces，且使用者不希望手寫語句。可作研究對照，不作 canonical 架構。

### 19.4 全域模糊文字匹配

風險高：

- 同音誤改；
- 熱門 action 吞掉一般聊天；
- 不具 speaker/acoustic evidence。

### 19.5 直接 fine-tune ASR

延後：

- 資料與訓練管線複雜；
- 容易影響自由語音；
- rollback 與模型版本管理成本高；
- prototype router 更容易局部啟用、刪除與審計。

### 19.6 只做前端攔截

不足：

- Telegram/其他 client 可繞過；
- 前端 regression 可能直接送 router；
- risk 與 action availability 必須後端決定。

---

## 20. 研究依據與文獻

以下文獻用來支持「通用 ASR 作 fallback、受限／個人化 KWS 作快路徑、使用 open-set rejection、以少量範例建立 query-by-example prototypes」的方向。論文結果不可直接等同本專案效能；實作仍須用本機中文控制 corpus 驗證。

### 20.1 通用 ASR：Whisper

1. Alec Radford, Jong Wook Kim, Tao Xu, Greg Brockman, Christine McLeavey, Ilya Sutskever. **Robust Speech Recognition via Large-Scale Weak Supervision.** 2022.
   https://arxiv.org/abs/2212.04356

   關聯：Whisper 是強健的多語通用 ASR，適合作為自由語音 fallback；但其目標是轉錄，不是針對少量個人化控制 action 的低延遲 open-set 分類器。

2. OpenAI. **Whisper open-source repository.** MIT License.
   https://github.com/openai/whisper

   關聯：本機免費部署與模型授權來源。

3. SYSTRAN. **faster-whisper: Faster Whisper transcription with CTranslate2.**
   https://github.com/SYSTRAN/faster-whisper

   關聯：目前專案的本機 STT runtime；模型快取與推論優化不等於 action routing 個人化。

### 20.2 Few-shot 與 query-by-example keyword spotting

4. Paul M. Reuter, Christian Rollwage, Bernd T. Meyer. **Multilingual Query-by-Example Keyword Spotting with Metric Learning and Phoneme-to-Embedding Mapping.** 2023.
   https://arxiv.org/abs/2304.09585

   關聯：以少量語音範例建立新 keyword embedding、使用 metric learning 與 query-by-example 比對；支持 prototype-based 方向及跨語言評估需求。

5. Alican Gok, Oguzhan Buyuksolak, Osman Erman Okman, Murat Saraclar. **Enhancing Few-shot Keyword Spotting Performance through Pre-Trained Self-supervised Speech Models.** 2025.
   https://arxiv.org/abs/2506.17686

   關聯：自監督 speech representations 可用於 few-shot KWS，並可蒸餾到較小 edge 模型；支持先抽象 embedding backend、後續再依 benchmark 選型。

6. Kesavaraj V, Anil Kumar Vuppala. **Open vocabulary keyword spotting through transfer learning from speech synthesis.** 2024.
   https://arxiv.org/abs/2404.03914

   關聯：open-vocabulary KWS 透過共享或轉移表示處理未見關鍵詞，顯示固定手寫 grammar 不是唯一方案。

### 20.3 個人化與說話者差異

7. Beltrán Labrador, Pai Zhu, Guanlong Zhao, Angelo Scorza Scarpati, Quan Wang, Alicia Lozano-Diez, Alex Park, Ignacio López Moreno. **Personalizing Keyword Spotting with Speaker Information.** 2023.
   https://arxiv.org/abs/2311.03419

   關聯：納入 speaker information 可改善不同口音與族群的 keyword detection，且額外參數與延遲可控制。此設計不把 speaker embedding 當第一版必要條件，但資料模型應避免阻礙後續加入。

8. Zhiqi Ai, Han Cheng, Shiyi Mu, Xinnuo Li, Yongjin Zhou, Shugong Xu. **Effective User-defined Keyword Spotting with Dual-stage Matching, Multi-modal Enrollment, and Continual Adaptation.** 2026.
   https://arxiv.org/abs/2605.22120

   關聯：使用者自訂 KWS、confusable word 雙階段驗證、多模態 enrollment 與 continual adaptation；支持「第一階段候選、第二階段確認」及持續個人化，但屬新研究，需獨立重現。

### 20.4 Open-set 與 false accept

9. Keyword spotting systems generally require explicit unknown/negative handling rather than forced nearest-class assignment. 本專案以 absolute threshold、top-1/top-2 margin、risk policy、unknown corpus 與 false-accept 指標實作 open-set 行為。

   具體研究線索可由 query-by-example 與 open-vocabulary KWS 文獻延伸；工程驗收不得只報 top-1 accuracy，必須報 false accept / false alarm。

### 20.5 對本設計的直接推論

從上述文獻可合理推論，但仍須實驗驗證：

- 通用 ASR 與少量 action 分類是不同任務。
- 使用者已確認的語音範例可形成 query-by-example prototypes。
- 少量範例可改善個人化命令辨識，但不能取消 unknown rejection。
- Confusable short phrases 需要 margin／第二階段驗證，而非只取最近鄰。
- Edge/on-device 模型可行，但延遲與準確率高度依賴硬體、語言與資料。
- 使用頻率可提升排序，但不得取代 acoustic threshold。
- 真正的系統成功指標是 action precision、false accept、clarification 成功率與端到端延遲，不只是 WER。

---

## 21. 實作決策紀錄

### 已決定

- 免費、本機為硬限制。
- 不使用人工 hotwords/aliases 作核心方案。
- Gate 在 open-ended tool execution 前。
- 候選從 action registry 產生。
- 第一次使用必須有 clarification bootstrapping。
- 成功 action 才 commit prototype。
- direct action 僅限低風險。
- open-set rejection 為必要條件。
- issue #82 只追蹤交付；本文件是 canonical design。

### 尚待 benchmark 決定

- embedding backend；
- embedding dimension；
- direct / clarify threshold；
- margin；
- 每 action prototype 上限；
- mature sample count；
- utterance TTL；
- context prior 權重；
- 是否加入 speaker information；
- 是否做 streaming/pre-ASR fast path。

---

## 22. 實作完成後需更新的 truth docs

依 repository 文件治理，完成各階段時至少檢查：

- `docs/DOCS_INDEX.md`
- `docs/DOC_AUDIT.md`
- `docs/SYSTEM_MAP.md`
- `docs/CURRENT_STATE.md`
- `docs/TASK_ROUTING.md`
- `docs/VERIFICATION_MATRIX.md`
- `SYSTEM_MANIFEST.yaml`
- `docs/voice-latency-optimization-references.md`

若 action registry 或 bridge envelope 成為跨 repo contract，還要更新：

- `docs/CROSS_REPO_CONTRACTS.md`

---

## 23. Issue #82 建議摘要

Issue body 應保持精簡並指向本文件：

```markdown
## 問題

語音 STT 結果目前直接進通用 Chat tool router；短控制指令誤讀時可能直接執行 `/search`，沒有 clarification 與個人化學習節點。

## Canonical design

完整架構、API、資料模型、風險政策、測試矩陣、PR 切分與文獻：

- [`docs/VOICE_CONTROL_PERSONALIZATION_DESIGN.md`](https://github.com/jojojen/aka_no_claw/blob/main/docs/VOICE_CONTROL_PERSONALIZATION_DESIGN.md)

## 核心驗收

- [ ] 「關電扇 → 關鍵善」不在 clarification 前執行 `/search`
- [ ] 候選由 action registry 產生
- [ ] action 成功後才學習
- [ ] 成熟低風險 prototype 可繞過 Chat router
- [ ] high-risk 永遠確認
- [ ] open-set false accept 有量測
```
