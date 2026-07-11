# 語音管線延遲優化 — 設計依據與文獻

Last reviewed: 2026-07-11
Status: Current
Owner area: telegram

實作日期：2026-07-11。對象鏈路：Telegram 語音 OGG → 本機 faster-whisper STT
（`local_stt.py`）→ transcript 重派發 → NL 意圖 router（`natural_language.py`
`_PoolAwareRouter`，cloud LLM 優先、local Ollama 備援）→ 工具 handler。

## 落地項目與依據

### 1. 語意意圖快取（`intent_cache.py`）

transcript → 路由決策的 embedding 快取：exact-match（sha256）優先、
未中再以 bge-m3 embedding 做餘弦相似度比對（門檻預設 0.93）。命中即跳過
router LLM。快取內容是 **LLM 產出的路由決策**，不是關鍵字表，未命中一律
回到原 LLM 路徑（符合開放世界原則）。

- 模式來源：GPTCache / GPT Semantic Cache——query embedding + 向量相似搜尋 +
  門檻判斷，FAQ 型負載命中率 40–70%，命中回應 3–8ms vs LLM 500–2000ms。
  - GPT Semantic Cache: Reducing LLM Costs and Latency via Semantic
    Embedding Caching — https://arxiv.org/html/2411.05276v2
  - GPTCache (open source) — https://github.com/zilliztech/gptcache
  - Semantic Caching for LLM Inference (2026) —
    https://www.spheron.network/blog/semantic-cache-llm-inference-gpu-cloud/
- 門檻選定：業界建議 near-duplicate 0.88–0.95；本案用真實 bge-m3 實測
  （2026-07-11，/tmp/probe_intent_cache.py）：
  - 反義對「音量調高/音量調低」= 0.868、跨意圖對 = 0.680 → 門檻必須 > 0.87
  - 改寫同義「音量調高/幫我把音量調高一點」= 0.937、語序對 = 0.979 → 0.93 可命中
  - 結論：**0.93 預設有實測依據**；口語變形（0.864）會漏接走 LLM，
    符合「寧漏接勿錯接」的優先序。
- 數字守衛：embedding 對「調到50/調到70」給 0.871 仍偏高，參數不同絕不可
  互相命中 → 語意命中前比對兩文本的數字序列（`re.findall(r"\d+")`）必須
  完全一致。此為結構性守衛，非關鍵字表。
- 包含守衛：多步驟工作流指令絕不可被單步快取截斷（例：「開燈然後播放
  初音的歌」不可命中快取的「播放初音的歌」而只執行放歌）。實測 8 組
  複合 vs 單步對（2026-07-11，/tmp/probe_compound_guard.py）全部低於
  0.93（最壞 0.8968 正是「複合逐字包含快取文本」的情境），但 0.033
  邊際太薄 → 語意命中前檢查兩文本互為嚴格子字串者一律拒絕、交回 LLM
  （會路由成 create_workflow）。與數字守衛同為結構性守衛；代價是部分
  含包含關係的改寫（如「幫我把音量調高一點」⊃「音量調高」）改走 LLM，
  符合「寧漏接勿錯接」。exact-hash 命中不受影響（全文相等才會中）。
- namespace 綁 tool_spec + prompt suffix + allowed intents 的 sha256：
  spec 改版自動全體失效。TTL 7 天、每 namespace 上限 500 筆（LRU by last_hit）。
- embedding 模型選 bge-m3（本機已有、多語 CJK 強）而非 nomic-embed-text
  （偏英文）。實測單次 embed 約 95–250ms。

### 2. Ollama keep_alive + prompt 前綴穩定（KV cache 重用）

`OllamaTextClient` 新增 `keep_alive`（chat tool-plan 路徑設 30m），避免模型
被換出後冷載；router prompt 前綴（tool spec）本為靜態檔案，前綴一致時
Ollama 自動重用 KV cache。

- LLM prompt prefix caching 省 200–400ms TTFT；KV-cache 跨輪重用省 100–300ms —
  How to Optimize Voice Agent Latency: 12 Techniques for 2026 —
  https://futureagi.com/blog/how-to-optimize-voice-agent-latency-2026/

### 3. 音訊側：跳過重複 duration 解碼

`_probe_audio_duration` 會為驗長度完整解碼一次音訊；Telegram 路徑在下載前
已用 API 回傳的 `duration` 驗過上限 → `AudioRequest.trusted_duration_seconds`
讓 Telegram 路徑跳過 probe，省一整趟 PyAV 解碼。非 Telegram 來源（web 上傳）
不受影響，仍走 probe。

### 4. 音訊側：beam_size 可調（`OPENCLAW_STT_BEAM_SIZE`，預設 5）

短語音指令用 greedy（beam=1）幾乎不掉準度、速度可達數倍。預設維持 5
（行為不變），待以真實語音樣本 A/B 後再由 .env 切換——依「先量測再優化」。

- faster-whisper 官方文件與短指令低延遲建議（beam_size=1 per chunk）—
  https://github.com/SYSTRAN/faster-whisper
- Quantizing Whisper-small: design choices vs ASR performance —
  https://arxiv.org/pdf/2511.08093 （int8 量化：`OPENCLAW_STT_COMPUTE_TYPE`
  已支援，純設定調整）

### 5. 音訊側：STT 模型預熱

whisper 模型 lazy load → 重啟後第一句語音要付整個載入成本。
`run_telegram_polling` 建好 processor 後呼叫 `prewarm_stt()` 背景載入。
（刻意不放建構子：測試大量直接建構 processor，不可觸發真實模型載入。）

### 6. 計時儀表

`[voice-latency]` 結構化 log：`stage=stt model_load_ms= transcribe_ms= audio_s=`
與 `stage=router elapsed_ms= cache=hit-exact|hit-semantic|miss backend=`。
先量測、再判斷下一步（見「評估過未做」）。

- 「先量測再優化」：Semantic Caching for LLMs: How to Measure Latency, Cost,
  and Quality Before You Optimize —
  https://medium.com/@mohantaastha/semantic-caching-for-llms-how-to-measure-latency-cost-and-quality-before-you-optimize-64ff73b0f370

## 評估過、暫不做

- **投機式工具預取**（信心 >0.85 先併發打工具、講完驗證回滾）：省 200–400ms
  但需 async 管線+回滾機制，等 baseline 證明 router 之後仍是瓶頸再議。
  - Speculative Interaction Agents — https://arxiv.org/pdf/2605.13360
- **邊聽邊想（Listen-Think-Speak）**：Telegram 語音整包送達、無串流麥克風，
  不適用；web 介面可改分塊上傳，屬另一工程。
  - LTS-VoiceAgent — https://arxiv.org/html/2601.19952
- **mlx-whisper 後端**：Apple Silicon 上比 faster-whisper 快 30–40%
  （Metal GPU），屬換後端的中型改動，待 baseline 顯示 STT 為主瓶頸再做。
  - mac-whisper-speedtest — https://github.com/anvanvan/mac-whisper-speedtest
  - lightning-whisper-mlx — https://github.com/mustafaaljadery/lightning-whisper-mlx
- **音訊層快取（跳過 STT）**：同句話每次錄音波形不同，聲學相似門檻難調且
  聽錯會被黏住；transcript 層快取已吸收重複指令收益，不做。
- **telegram_nl 本地 router 的 keep_alive**：在另一 repo（telegram_nl），
  cloud router 為主路徑時收益有限，列為後續。
