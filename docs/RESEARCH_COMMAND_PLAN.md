# /research 深度商品研究功能規劃與實作現況

> 狀態：已實作並上線。2026-06-12 起草，2026-06-19 已完成 `/research` 主流程、Mercari item adapter、`ItemData` / `ResearchSectionResult` contract、knowledge DB `research_command` origin、seller reputation snapshot、第一版合理市價分析、基於 sold/active 樣本的流動性分析，以及 `/research` appreciation 雲端 offload + stage 3/4/6 平行化。
> 注意：使用者輸入打成 `/resaerch` 也應容錯（dispatcher 加 alias）。

## 2026-06-19 最終落地狀態

- `/research` 已不再只是 `/search` 別名，而是由 aka_no_claw registry 接管的背景指令。
- Phase 1 已完成：`OPENCLAW_RESEARCH_CLOUD_ENRICHER=opencode` 會把 appreciation summarizer 下放到 OpenCode Big Pickle；雲端失敗只會單次回退到本地，不會觸發 `/new` 那套 bot restart failover。
- Phase 2 也已完成：stage 3（增值潛力）、4（合理市價）、6（賣家風險）會平行執行，stage 5（流動性）在 stage 4 完成後再跑。
- `ResearchBudget.consume()` 已加鎖，避免平行階段誤算 Yahoo 搜尋預算。
- cloud mode 會停用本地 relevance gate，避免 stage 3 的第二次地端 LLM 呼叫和 stage 4 price gate 擠同一顆 Ollama。
- 報告輸出順序已做固定排序，所以即使 3/4/6 完成順序不同，最終報告仍維持穩定。

### 與原 offload 討論稿的差異

- 外部 review 建議的「stage 回傳 result object、主執行緒再 apply 到 ctx」沒有原樣採用。
- 目前實作是讓 stage 3/4/6 直接寫入各自分離的 `ctx` 欄位，並對 `section_results` / `warnings` 追加加 lock，再於最終格式化前按 section name 固定排序。
- 這不是文件字面上的架構，但它已解決當初 review 想避免的兩個核心問題：共享 append 競態與最終輸出順序不穩。

## 目標

`/research <輸入>` 讓龍蝦動用**受控且可預算化的既有工具**對一件商品做長時間深度研究，
最後給出有依據、有信心度、有出處的結論。

兩種輸入模式：

| 輸入 | 範例 | 產出 |
|---|---|---|
| Mercari 商品 URL | `https://jp.mercari.com/item/m65806654179?afid=…` | ①增值潛力 ②合理市價 ③流動性 ④賣家風險 |
| 商品名稱（自由文字） | `初音ミク 15th フィギュア` | ①增值潛力 ②合理市價 ③流動性 |

URL 先去除 tracking query（`afid`/`utm_*`/`source_location`），只留 canonical item id（`m+數字`）。

## 現況與命名衝突（必處理）

- `price_monitor_bot/src/price_monitor_bot/bot.py:77`
  `WEB_RESEARCH_COMMANDS = {"/search", "/research", "/web"}` —
  **`/research` 目前只是 /search 的別名**（`_handle_web_research` → `ResearchRenderer`）。
- **遷移方式（codex review 修正，已驗證）**：不改 price_monitor_bot 上游常數。
  dispatcher 的 registry 分派（bot.py:1061 `self._command_registry.get(command)`）
  **先於** `WEB_RESEARCH_COMMANDS` 分支（bot.py:1102），因此只需在 aka_no_claw
  `telegram_bot.py` `_build_registries()` 註冊 `/research`（與容錯 alias `/resaerch`）
  為 `RegisteredCommand(..., background=True)`，即可遮蔽上游別名、零上游改動。
  `/search`、`/web` 與自然語言 intent `web_research`（bot.py:1657）維持原行為。
- registry handler 簽名為 `(remainder, chat_id)`；`/research` 走既有
  `RegisteredCommand(..., background=True)` 背景執行路徑，不再額外自開 daemon thread。
  pipeline 取得 chat_id 後，必須透過注入的 `ResearchNotifier` 推進度訊息，最終報告走 handler 回傳值。

## 可重用的既有元件（盤點）

| 能力 | 模組 / 服務 | 用途 |
|---|---|---|
| 網頁搜尋 | `openclaw_adapter/web_search.py` `search_yahoo_japan_playwright` | 比價、IP/作者背景（**注意每日個位數預算，防 IP ban**） |
| 網頁抓取 | `/fetch`（telegram_bot 既有） | 抓單頁內容 |
| 知識庫 | `knowledge_db.py` `KnowledgeDatabase`（`data/knowledge.sqlite3`） | 查 IP/作者/商品既有 grounded 知識 |
| 背景補知識 | `entity_researcher.py`（web search → LLM 濃縮 → upsert `origin='web_research'`） | 遇到不認識的 entity 時補課 |
| IP 熱度 | `ip_heat_store.py`、`google_trends_tracker.py`、`/snsbuzz`（4chan JSON + IP catalog） | 增值潛力的熱度面 |
| 行情/流動性 | `/trend` `/hot` `/liquidity` 板（`cross_signal_aggregator.py`、snkrdunk rank、yuyutei） | 市價與流動性（沿用 `docs/LIQUIDITY_METHODOLOGY.md`） |
| 賣家信譽 | `reputation_agent.py`（`_MERCARI_HOST="jp.mercari.com"`，snapshot 服務 `127.0.0.1:5000`）＋ `/snapshot` `/repcheck` | 賣家評價快照、差評內容 |
| LLM | 本機 Ollama——**一律用當下最聰明的模型**（現役＝qwen3:14b；模型名走 config，升級即換） | 實體辨識、摘要、綜合判斷（Rule G：不寫關鍵字表） |

明確不自動使用（2026-06-12 使用者拍板折衷案）：

- `/new` / `DynamicToolRunner` **不可作為 pipeline 的自動 fallback**。
  研究流程需要固定 timeout、搜尋預算、資料 contract 與可測試性；若 Mercari adapter 或單一資料源失敗，該節降級為「無法取得 / 信心低」，不得臨時生成工具。
- **但缺口要回報為可行動建議**：最終報告的 warnings 區須列出每個降級缺口，
  並附一條建議的 `/new <具體任務描述>` 跟進指令（例：`/new 抓取 mercari 商品 mXXX 的
  已售同款清單`），由使用者手動觸發。pipeline 保持確定性，/new 的彈性保留在人工迴路。

## Pipeline（每階段完成即回報進度）

```
[0/6] 解析輸入 → URL or 名稱；URL 去 tracking 參數
[1/6] 取得商品資料（URL 模式）：標題/價格/說明/狀態/賣家id/圖
       來源優先序：Mercari item adapter spike → reputation_snapshot 可用欄位 → /fetch 單頁輔助
       若仍取不到，該節降級；不自動跑 /new，但最終報告附建議的 /new 跟進指令
[2/6] 實體辨識（LLM）：IP / 角色 / 作者(原型師/繪師) / 系列 / 品類
       → 查 knowledge DB；缺的丟 entity_researcher 背景補
[3/6] 增值潛力：IP 熱度(ip_heat_store + trends + snsbuzz) + 作者軌跡(web search)
       + 限定/絕版/再販訊號 + 週年/動畫化等催化劑 → LLM 綜合，附出處
[4/6] 合理市價：已售比價（mercari sold、TCG 走 snkrdunk/yuyutei）
       → 價格分佈 → 區間 + 中位數 + 與賣家開價的差
[5/6] 流動性：沿用 LIQUIDITY_METHODOLOGY（售出/在售比、售出速度訊號）
[6/6] 賣家風險（僅 URL 模式）：/snapshot 快照 + 評價分佈 + 差評 LLM 摘要
       + 商品說明紅旗（LLM 判讀，不用 regex/關鍵字表）
最後：綜合結論（每節附信心度與來源）＋ 蒸餾出的 entity 知識寫回 knowledge DB
```

### 進度回報（核心需求：使用者要知道沒當機）

- 每階段開始/結束各發一則短訊：`⏳ [3/6] 增值潛力分析中…` / `✅ [3/6] 完成（IP熱度：高）`。
- 單一階段超過 ~90 秒再發一次 heartbeat。
- `/research` 由既有 `RegisteredCommand(..., background=True)` 路徑背景執行；
  `research_command.py` 本身不再額外自開 daemon thread。主 poll loop 不被阻塞。
- 同一 chat 同時只允許一個 /research job（後到的排隊或拒絕）。
- 最終報告若超過 Telegram 4096 字上限就分段。

實作要求：

- `research_command.py` 不直接碰 Telegram bot internals。
- 定義 `ResearchNotifier` 介面：`send(text: str) -> None`。
- Telegram adapter 建立 notifier，包住 `send_message(chat_id, text)`。
- 測試用 fake notifier 驗證每階段開始、完成、heartbeat 與最終分段。

### 知識庫寫回

- 研究過程確認的事實（IP 熱度結論、作者背景、該品類流動性特徵）upsert 進
  `knowledge.sqlite3`，`origin='research_command'`，confidence 依來源定。
- 遵守「No formulas in knowledge DB」：只存 entity 事實與 `參考: <url>`，不存公式。
- M2 必須同步更新 `knowledge_db.py` 的 `ORIGINS` 白名單，加入 `research_command`，
  並補測試確認該 origin 不再只是 warning。

### 搜尋預算 contract

單次 `/research` 必須建立 `ResearchBudget(max_searches=5)`。

所有會觸發 Yahoo 搜尋的 dependency 都必須使用同一個 `budgeted_search_fn`：

- 作者 / IP 背景搜尋
- entity researcher 補知識
- 合理市價補查
- 任何後續新增的 web enrichment

實作要求：

- research job 在啟動時建立唯一一個 `budgeted_search_fn`。
- 所有 research 用到的 adapter / researcher constructor 都必須接受可注入的
  `search_fn`，不得在內部偷偷直接呼叫原始 Yahoo backend。
- 若某元件目前不支援注入 `search_fn`，M1/M2 必須先改成可注入，否則不得納入 `/research` pipeline。

規則：

- 每次 Yahoo 搜尋前先呼叫 budget。
- budget 用完後，該資料來源回傳「資料不足 / 信心低」，不得繞路另開 search。
- `/fetch` 單頁讀取不計入 Yahoo 搜尋次數，但必須有 timeout。
- 測試必須覆蓋 budget 耗盡後不再呼叫 search backend。

### Data contracts

M2 前先定義資料 contract，避免各階段用 dict 傳不穩定欄位。

`ItemData`：

```text
- source_site
- item_url
- item_id
- title
- listed_price_jpy
- description
- condition_label
- seller_id
- seller_url
- image_urls
- fetched_at
- source_confidence
```

`PriceEvidence`：

```text
- source_site
- source_url
- title
- price_jpy
- sold_status        # sold | active | unknown
- condition_label
- shipping_note
- excluded_reason
- observed_at
```

`ResearchSectionResult`：

```text
- section_name
- status             # ok | partial | unavailable
- confidence
- sample_count
- evidence_count
- summary
- evidence_urls
- warnings
```

合理市價至少要回報樣本數、排除理由與資料不足狀態；不得只給單一價格結論。

## 風險與約束（優先序：①正確 ②不被封鎖 ③省token ④速度）

1. **Yahoo 搜尋預算**：單次 /research 上限 **5 次**搜尋（已拍板）；超過就降級為「資料不足、信心度低」。
2. **Mercari 抓取**：重用 reputation_snapshot 服務的節流；不可新開無節流爬蟲。
3. **長時間運行**：預期 3–10 分鐘；所有外部呼叫都要 timeout + 單階段失敗不毀全局
   （該節標記「無法取得」繼續走）。
4. **Rule G**：實體辨識、紅旗判讀一律 LLM+RAG，不維護關鍵字清單。
5. 名稱模式不做賣家風險（無賣家可查），報告只含 1–3 節。

## 實作里程碑（已完成）

- **M1 骨架**：已完成。`_build_registries()` 已註冊 `/research`（+`/resaerch` alias），`research_command.py` 已接上 URL/名稱解析、`ResearchNotifier`、`ResearchBudget`、單 chat 鎖與 progress notifier。
- **M2 資料層**：已完成。`ItemData` / `PriceEvidence` / `ResearchSectionResult` contract、Mercari item adapter、實體辨識、knowledge DB 查/寫、`research_command` origin 白名單、`search_fn` 注入都已落地。
- **M3 四大分析**：已完成首版。增值潛力 / 市價 / 流動性 / 賣家風險均可獨立降級，不會單節失敗就毀全局。
- **M4 整合驗證**：已完成。真實 Telegram smoke test、全量 pytest 回歸、以及 `/research` cloud offload / parallel stages 的專項測試都已跑過。

### `/research` offload / parallelization 驗證

- `tests/test_telegram_bot.py`
  - 驗證 cloud summary success
  - 驗證 `CloudBackendUnavailable` 時單次本地 fallback
  - 驗證 cloud disabled 時保留原本 local relevance gate
- `tests/test_research_command.py`
  - 驗證 stage 3/4/6 會平行執行
  - 驗證最終報告順序穩定
  - 驗證 `ResearchBudget.consume()` thread-safe

## 已拍板決議（2026-06-12）

1. **模型**：一律用當下最聰明的本機模型（現役 qwen3:14b）；模型名放 config，
   未來換更強模型只改設定不改碼。
2. **非 mercari URL**：首版只支援 mercari，**但要預留擴充空間**——URL 解析與
   商品頁抓取走 site-adapter 介面（`parse(url) -> ItemRef`、`fetch(ItemRef) -> ItemData`），
   mercari 是第一個 adapter，下一版加站只需新增 adapter。
3. **報告語言**：繁體中文（進度訊息與最終報告皆是）。
4. **搜尋上限**：單次 /research 上限 5 次 Yahoo 搜尋。
5. **/new 取捨（折衷案）**：pipeline 不自動呼叫 /new（採 codex 的確定性論點），
   但降級缺口必須在最終報告附上建議的 `/new` 跟進指令，由使用者手動觸發。
