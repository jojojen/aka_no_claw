# Plan: 代購委託系統（Proxy-Buying Commission System）

> 規劃草案 — 尚未開發。最後更新：2026-05-29。
> 對應 plan 檔：`~/.claude/plans/claw-price-monitor-bot-hunt-ua-ws-pokem-shiny-fountain.md`

---

## ⛔ 決議（2026-05-29）：不對外營運，退回個人用 — 本計畫不予實作

深入評估後**決定不把這套當「代購生意」做**，理由是找不到「可規模化 ＋ 有護城河」
的位置：

- **通用代購的規模/自動化** → Buyee/ZenMarket 已佔死，正面打必輸。
- **賣監看/提醒情報** → 程式碼太便宜，沒護城河，技術客戶會自己寫。
- **日本實體 access（抽選/店舗特典代搶）** → 在日中文代購已飽和，據點是入場券不是優勢。
- 剩下有護城河的（領域專業/策展/信任）**綁在操作員個人身上 → 本質不可規模化**。
- 「規模化」與「有護城河」對這賽道**直接衝突，只能選一個**。唯一能規模化專業型生意
  的路是做 fandom KOL／養受眾（規模化注意力而非履約），但需要做內容/經營社群的能力、
  且現有系統幫不上那層 → 決定不走。

**結論：整套委託流程（狀態機／報價／買家網頁表單／email／Tailscale Funnel／對外資安）
不予實作**——那九成是為「服務別人」而存在的。獵物智能**退回個人用**。

**唯一保留、會實作的個人用增強**：`/watch` 命中時附**公允價評語**（划算/合理/偏貴 ＋
二手均價，重用 `fetch_avg_sold_price`），讓自己買東西時一眼判斷該不該下手。

> 以下正文保留為**當時評估的完整思考紀錄**（差異化分析、狀態機、費率、正規化管線、
> DB 設計、資安分析…），不再是待辦藍圖。若未來重啟「接委託」念頭，先回顧本決議，
> 別從頭重推一遍。

---

## Context

使用者想用現有的 bot 系統承接**他人委託代購**：委託者送出想買的商品與預算 →
系統自動產生報價單給操作員（使用者）確認 → 定期在網路上搜尋符合的商品 →
找到後給操作員確認 → 寄下單確認文件（匯款資訊＋商品連結）給委託者 → 委託者匯款
→ 操作員購買 → 系統整理寄件資訊（可直接複製貼上）→ 寄出後通知委託者。

這是標準的代購（proxy-buying）流程。網路調查確認流程正確；唯一要決定的「兩段式
vs 單段付款」使用者已選**單段付款**（方便優先，運費落差他願意吸收）。

**已確認的關鍵決策：**
- **委託者管道**：自建網頁表單（GitHub Pages，免費靜態前端）+ Email 對外溝通。
  後端跑在 Mac mini 的「龍蝦」stack。
- **付款模式**：單段付款（找到實際商品、委託者確認後一次收齊；報價含運費 buffer）。
- **建置方式**：分階段 MVP，先能用再逐步自動化。
- **報價金額**：系統自動算（費率公式見下）。
- **核心賣點（使用者選定）**：**主動代尋 + 公允價**。

**前端是否免費：** 是，前端可 100% 免費——GitHub Pages 放靜態表單，表單 POST 到
Mac mini 上的後端，後端用 **Tailscale Funnel**（免費、固定 HTTPS 網址、不需網域、
不需開 port）對外曝光。資料完全留在 Mac mini，不經第三方。

---

## 服務定位與差異化：為什麼用你，而不是 Buyee／Zenmarket？

**殘酷現實**：Buyee / ZenMarket / FromJapan / Neokyo 這些既有業者規模大、便宜、高度
自動化、支援上百個日本站點。**正面打「通用代購」必輸**。差異化必須來自既有業者做不好
的**利基切角**——而這正好是本系統現有程式碼的強項。

**核心洞見：既有業者是「被動代購」（你貼 URL → 我幫你買）。本系統能做「主動代尋」
（你說想要什麼 → 我幫你獵、幫你判斷划不划算、幫你搶）。** 這不是 代購，是 **代尋＋
代購＋代搶**。

### 五個差異化切角（每個都對應現有能力，不是空談）

1. **垂直深耕 TCG／收藏品／炒作品**（不做通用代購）
   定位成「**日本卡牌／收藏品／熱炒品的專屬代購**」（One Piece、寶可夢、鏈鋸人 UA、
   Weiss Schwarz、球鞋）。既有業者橫向而淺；你縱向而深。
   → 重用：`market_monitor` 的 TCG 爬蟲、`tcg_tracker`、snkrdunk 公允價。

2. **主動獵物（Active sourcing）= 真正的產品核心**（← 使用者選定為核心賣點）
   買家只說「想要某張卡／某個 box／某個只在日本抽選的東西」，系統**定期掃** Mercari／
   Rakuma／官網 + **監看 SNS** 補貨/抽選訊號，找到就帶**公允價判斷**推給操作員。
   Buyee 不會幫你「找」也不會幫你「等補貨」。
   → 重用：`MarketplaceSearchClient` + SNS monitor。

3. **抽選代行（代抽）—— 最痛、最難被取代的利基**
   日本 抽選（寶可夢中心、Premium Bandai、球鞋 drop）常需日本地址/帳號，海外根本
   無法參加。系統已能偵測 抽選 視窗 → 提供「**代抽**」：替買家參加抽選，中了再買、再
   轉寄。通用業者極少做好這塊。
   → 重用：Bandai/PokeCen 官網 crawler 的 `LOTTERY_OPEN` 偵測。

4. **價值情報透明（不讓你買貴）**
   下手前自動比對二手公允價，告訴買家「這個價合理／偏貴」。Buyee 不會。建立信任、
   也合理化你的服務費。
   → 重用：`fetch_avg_sold_price`、snkrdunk、opportunity 的 profit gate。

5. **賣家信譽查核 + 高互信 concierge**
   買前先驗 Mercari 賣家信譽（避免買到雷賣家/詐騙），高價卡/整箱貨尤其重要；
   Telegram/email 一對一、有人味，對高價收藏者比冷冰冰平台更安心。
   → 重用：`reputation_snapshot`（賣家評價快照/信任分數）。

### 切入策略（Go-to-market）
- **從你自己的社群下手**：你本來就在監看 TCG／鏈鋸人 UA／寶可夢 的 SNS 圈，先服務
  這群「想要但海外搶不到」的人。系統的獵物智能就是護城河。
- **低量、高值、高互信**：本質是 concierge（高接觸、低量、高單價），這正是差異化，
  不必跟大平台拚自動化規模。

### 誠實風險
- **信任與金流**：無名服務收錢比有平台背書難賣 → 用公允價佐證＋透明＋先做熟人社群。
- **法務/退換貨/詐騙/海關**：先小規模、社群內、非正式起步，別一開始就規模化。

---

## 現況可重用的基礎（探索結果）

- **Telegram 操作台**：`price_monitor_bot/src/price_monitor_bot/bot.py`
  - 原生 Telegram API + polling（單操作員，`allowed_chat_ids`）。
  - inline 按鈕「確認/取消」既有模式：callback_data 前綴分派（`handle_telegram_callback_query` bot.py:3540；SNS 👍/💰/👎 走 `snsfb:` bot.py:3646）。
  - 多步驟 5 分鐘 in-memory pending-state machine（bot.py:848–890）。
  - 主動推播：`TelegramBotClient.send_message()`（bot.py:663）。
- **儲存層**：SQLite + `bootstrap()` 冪等 migration（`PRAGMA table_info` 加欄位）；
  frozen dataclass model；UTC ISO 字串時間戳。範例 `sns_monitor_bot/src/sns_monitor/storage.py` + `models.py`。**目前沒有任何 order/quote/commission entity → 全新乾淨的表。**
- **搜尋基礎**：`price_monitor_bot/src/market_monitor/`
  - `MarketplaceSearchClient` protocol（`marketplace_search.py:43`）：`search(query, price_max, ...) -> list[MarketplaceListing]`。已有 Mercari / Rakuma / Yuyutei 實作。
  - 公允價：`fetch_avg_sold_price()`（`mercari_search.py:164`）、snkrdunk。
  - 背景輪詢迴圈：`watch_monitor.py:49`（每 watch 有 `schedule_minutes`）。
- **既有 Flask 服務先例**：`reputation_snapshot/app.py` — `create_app()` factory、
  `services/`+`utils/` 分層、跑在 `127.0.0.1:5000`，由 launchd label
  `local.openclaw.reputation` 啟動（launcher: `aka_no_claw/launchers/start-mac-mini-stack.command`，labels 含 reputation/telegram/opportunity/ollama）。

---

## 架構總覽

```
委託者(buyer)                    操作員(你)                  Mac mini「龍蝦」stack
─────────────                   ──────────                 ──────────────────────
GitHub Pages 靜態表單  ──POST──▶ (不直接互動)        ┌─▶ Flask commission_web
   (免費)                                             │     /intake  (寫 DB, status=request)
        ▲                                             │     /act?token=… (買家點連結改狀態)
        │ Email(SMTP 寄出, 內含                       │
        │ 報價/下單文件/寄送通知,                     │   commission core (新模組)
        │ 內嵌 tokenized 動作連結)                    │     models / storage(SQLite) / fee / state / templates
        │                                             │
        └──────────────────────────  Telegram bot ◀──┘   搜尋: 重用 MarketplaceSearchClient
                                     操作員按鈕審核        Tailscale Funnel: 對外固定 HTTPS 網址(免費)
```

**核心設計決策——買家動作全用「tokenized 連結」，不解析 email 回信：**
報價單/下單文件 email 內嵌「接受報價」「確認下單」「我已匯款」按鈕連結
（帶一次性 token），點下去打到 Flask `/act` 端點改狀態並通知操作員。
→ 完全不需要 IMAP 收信解析，可靠又簡單。Email 只負責**寄出**（SMTP）。

---

## 委託狀態機（單段付款，對齊使用者 7 步）

| # | status | 觸發者 | 動作 |
|---|--------|--------|------|
| 1 | `request` | 買家送表單 | 寫 DB；Telegram 通知操作員 |
| 2 | `quote_review` | 系統 | 自動算報價 → 操作員 Telegram 看到草稿＋「核可寄出/改金額」按鈕 |
| 3 | `quote_sent` | 操作員核可 | (MVP) 產生報價單複製貼上文字 / (後期) SMTP 寄 email 給買家 |
| 4 | `quote_accepted` | 買家 | (MVP) 操作員手動標記 / (後期) 買家點 email 連結 → 開始搜尋 |
| 5 | `searching` | 系統 | 定期搜尋 |
| 6 | `candidate_review` | 系統/操作員 | 找到符合品 → 操作員 Telegram 確認「就買這個/再找」 |
| 7 | `order_confirm_sent` | 操作員確認候選 | 產生下單確認文件（匯款帳號＋金額＋商品連結）→ 給買家 |
| 8 | `awaiting_payment` | 買家 | 買家確認下單（連結/手動）→ 等匯款 |
| 9 | `paid_verified` | 操作員 | 操作員核對收到款項 → 標記 |
| 10 | `purchased` | 操作員 | 操作員實際下單後告訴系統「已購買」 |
| 11 | `shipping_prep` | 系統 | 整理寄件資訊（收件人/地址/品名/申報/重量欄）成可複製貼上文字 |
| 12 | `shipped` | 操作員 | 操作員標記已寄出（填單號）→ 通知買家 |
| 13 | `done` | 系統 | 結案 |

每次狀態變更：寫 `updated_at` + 一筆 event log；需要對外時產生對應文字模板或寄 email。

---

## 報價費率公式（採用預設值，**不含刷卡加成**）

```
報價(TWD) = ( 商品價(JPY) + 服務費(JPY) + 國際運費估(JPY) ) × 匯率(TWD/JPY)
```
- **服務費**：`max(min_service_fee, 商品價 × service_rate)`，分級距：
  - ≤¥5,000 → service_rate 15%；¥5,001–30,000 → 12%；>¥30,000 → 10%
  - `min_service_fee = ¥500`
- **國際運費估**：依重量級距表（~500g/1kg/2kg 對應金額），單段付款故加
  `shipping_buffer = +15%` 吸收落差。
- **匯率**：`fx_rate` 手動設定值 ＋ `fx_markup = +3%`（匯差）。
- **刷卡加成**：**不做**（使用者決定先不處理，太複雜）。

→ 全部放在 `fee.py` 的 `FeeConfig` dataclass（tunable 常數），用上述預設值。

---

## 需求正規化管線（核心：模糊需求 → 精準搜尋）

**問題**：買家只會說「鏈鋸人那盒」「最新彈一箱」，但 Mercari 是日文站，要的是精準
日文關鍵字（チェンソーマン、ユニオンアリーナ、BOX…）＋品項類型。命中率高度取決於此。

**做法**：結合既有三套件——**地端 LLM（Ollama）＋ `/search` 網路研究 ＋ 地端知識庫**
——做一條 `commission/normalize.py` 的正規化管線，沿用既有「`llm_fn` 注入 + 結構化
JSON」模式（`classify_sns_signal` / `extract_entities`），**LLM 猜、操作員確認**。

```
normalize_want(raw_want, target_price, *, knowledge_db, llm_fn, web_research_fn)
    -> NormalizedWant
```

**步驟（cheap→貴，可逐層降級）：**
1. **知識庫解析（免費、確定性）**：`extract_entities(raw_want, alias_source=知識庫,
   llm_fn=None)` 的子字串掃描，把「鏈鋸人」等別名 → canonical；`get_entry()` 取出
   該 entity 的官方名/系列/set code/市場脈絡摘要。重用 `KnowledgeDatabase`
   (`lookup_canonical`/`all_aliases`/`get_entry`)。
2. **LLM 結構化正規化（地端 Ollama）**：把 raw_want ＋ 步驟1的知識庫摘要 ＋ 目標價
   餵進 prompt，用 `_call_ollama_json`（format=json, temp=0）回傳：
   ```json
   {
     "search_queries": ["チェンソーマン ユニオンアリーナ BOX", "鏈鋸人 UA BOX"],
     "item_type": "sealed_box|booster_pack|single|other",
     "set_code": "UAxxBT/..." | null,
     "canonical": "chainsaw man union arena",
     "confidence": 0.0-1.0,
     "needs_clarification": bool,
     "clarification_question": "最新彈指哪一彈？" | null
   }
   ```
   **關鍵價值＝中文模糊詞 → 日文精準搜尋詞（含同義/變體）**，這是 Buyee 不會幫你做的。
3. **網路研究消歧（選用）**：遇到「最新彈/latest」這種相對詞，或 LLM 標
   `needs_clarification`，呼叫 `/search` 的 `build_web_research_answer`（Yahoo Japan Playwright 搜尋 +
   Ollama）查「目前最新一彈是哪個 set code」，再回填步驟2。
4. **操作員確認（人在迴圈）**：正規化結果（建議搜尋詞＋品項＋set code）推到 Telegram，
   操作員可**編輯/確認**後才開始搜尋（沿用既有 pending-state + inline 按鈕）。
5. **降級**：Ollama 掛掉（`llm_fn=None`）→ 只用知識庫子字串匹配＋原文當查詢，操作員手改。
   （與既有碼一致的 graceful degradation。）
6. **學習回饋（飛輪）**：操作員確認某正規化後，`add_alias()` 把「鏈鋸人那盒」→ canonical
   寫回知識庫，下次秒解析。呼應記憶中的「proactive hash DB philosophy」。

**新檔**：`commission/normalize.py`（＋ `NormalizedWant` dataclass）。
**重用**：`knowledge_db.py`、`entity_extractor.extract_entities`、
opportunity_agent 的 `_call_ollama_json`、telegram 的 `build_web_research_answer`。

> **模組相依（重要）**：LLM/知識庫/`/search` 都在 **aka_no_claw**，但 `commission/`
> 放在 **price_monitor_bot**；而 aka_no_claw 已相依 price_monitor_bot → **直接 import
> 會循環相依**。解法沿用既有反轉模式（`classify_sns_signal(llm_fn=…)`）：`commission/`
> 把 `llm_fn` / `knowledge_lookup` / `web_research_fn` 當**注入的 callable**，核心模組
> 不直接 import aka_no_claw；由 aka_no_claw（composition root，既有 telegram 組裝點）
> 在啟動時把真正的 Ollama/知識庫/網路研究函式接進去。`commission/` 本身保持 dep-free。

---

## 公允價與候選呈現（使用者決定）

- **判定門檻 → 改用買家目標價當閘**：不做自動「划算/合理/偏貴」門檻（太難、樣本少時
  不可靠）。**買家直接提供目標價**，即 `price_max`；搜尋只要 `listing_price ≤ 目標價`
  即為候選。`fetch_avg_sold_price` 的二手均價**降為輔助資訊**（「市場均價 ¥X（n 筆）」
  附在候選旁），不當判斷主軸。
- **多候選呈現 → top-N ＋ 龍蝦評語，操作員判斷**：找到多筆時，推**前 N 筆**（如 3–5），
  每筆由地端 LLM 產一句**評語**（品況/賣家/價格相對市場均價/是否真的符合 want），
  操作員自己拍板。評語走 `_call_ollama_json` 或文字呼叫；N 設小以控延遲。

---

## 報價語意、全包預算與其他決策

### 報價不確定性（#4，採建議）：報價單 = 條款確認，綁定價延到下單
二手市場價浮動，事前報固定價必錯。故：
- **「報價單」改性質為「委託確認單」**：確認 ① 全包預算上限、② 費率表、③ 附一段
  **市場參考價區間**（`fetch_avg_sold_price`，**明確標「非保證、僅供參考」**）。
  此階段 **不收錢、不綁價**。
- **真正全包金額在 `order_confirm_sent`（下單確認文件）才鎖定**：找到具體 listing 時
  算出**該商品的實際全包金額**（保證 ≤ 買家預算），買家此時才付實際金額（單段付款）。
- 狀態機不變；只是 `quote_*` 語意 = 條款＋預算確認，綁定價落在 `order_confirm_sent`。

### 全包目標價 → 反推商品價上限（#6）
買家給的目標價 = **「我最多付這麼多（全包）」**（TWD，含商品＋服務費＋運費）。
故搜尋閘不是直接拿目標價當 `price_max`，要**反推商品價上限**：

```
derive_item_price_ceiling(all_in_budget_twd, item_type, fee_config) -> price_max_jpy
```
- `B_jpy = all_in_budget_twd / (fx_rate × (1+fx_markup))`
- 減去**該品項預設重量**對應的運費估（如 sealed_box≈1kg），得 `B_after_ship`
- 解 `P + service_fee(P) ≤ B_after_ship`（service_fee 分級距，逐 tier 試算）→ `P` 即
  Mercari 搜尋的 `price_max_jpy`。
- 搜到的 listing 在 `order_confirm` 階段用**實際重量**重算全包；若實際運費把總額頂過
  預算 → 操作員決定（問買家加價／自行吸收）。小估誤差可接受，因買家付的是實際值。

### 超時/找不到（#5）：MVP 先無限掛著
委託搜不到符合的就**維持 `searching`，不自動超時、不自動通知買家**。等量大了再加
「N 天無果自動通知/降級」。

### 知識庫冷啟動（#7）：正規化只當輔助，操作員手填為主
MVP 階段 **操作員手動填/改搜尋詞為權威來源**；`normalize.py` 的結果只當**建議**顯示
（冷門 want 知識庫沒資料、Ollama 可能猜錯）。操作員確認/修正後才寫回知識庫養飛輪。

---

## `/watch` 三類來源 DB 設計（追蹤清單分流）

**需求**：`/watch` 清單要分三類來源——① **操作員自己**指定的追蹤關鍵字（現行行為）、
② **委託人**指定的（綁某筆委託）、③ **龍蝦系統建議**的（趨勢/榜單/SNS 訊號自動發掘）。
三類的搜尋/輪詢/去重邏輯**完全相同**，差別只在「來源」與「命中後推給誰、怎麼推」。

### 設計決策：擴充既有 `marketplace_watchlist`，不開新表

既有 `/watch` 走 `market_monitor/storage.py` 的 `marketplace_watchlist`（v2 schema，
storage.py:181），`watch_monitor.py` 統一輪詢這張表、命中寫 `marketplace_watch_hits`
去重後推播。三類來源**共用同一套輪詢/去重**，故**只加欄位標記來源**，不複製輪詢迴圈。
（開三張表會逼你維護三份 poll loop，違反現況單一迴圈設計。）

### Schema 變更（4 個新欄位，照既有冪等 migration 慣例）

在 `MonitorDatabase` 新增一個 migration 方法，**完全比照** `_migrate_add_feedback_polarity`
（storage.py:315）的寫法——`PRAGMA table_info` 檢查欄位是否已存在，存在就 return：

```python
def _migrate_add_watch_origin(self, connection: sqlite3.Connection) -> None:
    """Add origin/commission_id/suggestion_state/origin_meta_json to
    marketplace_watchlist. Fresh DBs get these from SCHEMA_MARKETPLACE;
    this only runs on DBs created before /watch source-tagging shipped.
    Idempotent: skips any column already present."""
    if not _table_exists(connection, "marketplace_watchlist"):
        return
    cols = {row[1] for row in connection.execute(
        "PRAGMA table_info(marketplace_watchlist)"
    )}
    if "origin" not in cols:
        connection.execute(
            "ALTER TABLE marketplace_watchlist ADD COLUMN "
            "origin TEXT NOT NULL DEFAULT 'operator'"
        )
    if "commission_id" not in cols:
        connection.execute(
            "ALTER TABLE marketplace_watchlist ADD COLUMN commission_id TEXT"
        )
    if "suggestion_state" not in cols:
        connection.execute(
            "ALTER TABLE marketplace_watchlist ADD COLUMN suggestion_state TEXT"
        )
    if "origin_meta_json" not in cols:
        connection.execute(
            "ALTER TABLE marketplace_watchlist ADD COLUMN "
            "origin_meta_json TEXT NOT NULL DEFAULT '{}'"
        )
```

並在 `bootstrap()`（storage.py:302）裡，於 `connection.executescript(SCHEMA_MARKETPLACE)`
**之前**呼叫 `self._migrate_add_watch_origin(connection)`；同時把這四欄加進
`SCHEMA_MARKETPLACE` 的 `CREATE TABLE marketplace_watchlist`（讓全新 DB 直接帶欄位）：

```sql
    origin TEXT NOT NULL DEFAULT 'operator',           -- 'operator'|'commission'|'suggested'
    commission_id TEXT,                                 -- 軟連結；僅 origin='commission'
    suggestion_state TEXT,                              -- 'pending'|'accepted'|'dismissed'；僅 origin='suggested'
    origin_meta_json TEXT NOT NULL DEFAULT '{}',        -- 來源附帶資訊（見下）
```

加一個查詢索引（清單常依 origin 分組）：
```sql
CREATE INDEX IF NOT EXISTS idx_marketplace_watch_origin
    ON marketplace_watchlist(origin, suggestion_state);
```

### 欄位語意

| 欄位 | 型別 | 說明 |
|---|---|---|
| `origin` | TEXT NOT NULL | `'operator'`（自己）／`'commission'`（委託人）／`'suggested'`（龍蝦建議）。預設 `'operator'`，故既有資料列自動歸為操作員自己的 watch。 |
| `commission_id` | TEXT nullable | 僅 `origin='commission'` 時填，指向 `CommissionDatabase` 的委託 id。**軟連結**：委託實體在另一個 sqlite 檔，跨檔不設 FK，存純 TEXT。 |
| `suggestion_state` | TEXT nullable | 僅 `origin='suggested'`：`'pending'`（待操作員裁決，不輪詢）／`'accepted'`（已採納，照常輪詢）／`'dismissed'`（已婉拒，當墓碑保留以防重複建議）。 |
| `origin_meta_json` | TEXT NOT NULL `'{}'` | JSON。`suggested` 用：`{"reason": "...", "signal_source": "snkrdunk_rank"|"/trend"|"sns_buzz", "score": 0.0-1.0, "suggested_at": "ISO"}`。`commission` 可留空或放 `{"want_summary": "..."}`。 |

### 三類行為對照

| origin | 誰建立 | enabled / 是否輪詢 | 命中後路由（在 `watch_monitor` 分派） |
|---|---|---|---|
| `operator` | 操作員 `/watch <query> <price>`（現行） | 立即 enabled=1 輪詢 | 現有降價提醒推給操作員 |
| `commission` | 委託進件後自動建：`query` 來自 `normalize.py` 正規化結果、`price_threshold_jpy` 來自 `derive_item_price_ceiling`（全包預算反推） | 僅當該委託處於 `searching` 時 enabled=1；其餘狀態 enabled=0 | 命中 → 用 `commission_id` 回查委託 → 帶**公允價 verdict + top-N + LLM 評語** 推進該委託的 `candidate_review`（不是普通降價提醒） |
| `suggested` | 龍蝦背景任務（趨勢/snkrdunk 榜/SNS buzz 發掘新標的） | `pending`→enabled=0（**不輪詢**，只推一次「要不要追蹤？」）；`accepted`→enabled=1 | `pending`：發掘當下推一則含 inline 按鈕（受/拒）的建議訊息，不走 hit 流程；`accepted` 後命中比照 operator 降價提醒 |

### `watch_id` 防撞（重要，必改）

現行 `build_marketplace_watch_id = sha1(f"{chat_id}|{query}")`（storage.py:258）。
單操作員下 `chat_id` 固定，**兩筆不同委託想搜同一個 query 會算出相同 watch_id 互相覆蓋**。
故非 `operator` 的 watch 必須把來源併進 hash：

```python
def build_marketplace_watch_id(
    *, chat_id: str, query: str,
    origin: str = "operator", commission_id: str | None = None,
) -> str:
    if origin == "operator":
        payload = f"{chat_id}|{query}"            # 既有格式，向後相容不變
    elif origin == "commission":
        payload = f"commission|{commission_id}|{query}"
    else:  # suggested
        payload = f"suggested|{chat_id}|{query}"
    return sha1(payload.encode("utf-8")).hexdigest()
```
> operator 路徑的 payload 保持原樣 → 既有 watch 的 id 不變、不需資料搬遷。

### `MarketplaceWatch` dataclass 變更（storage.py:216）

新增 4 個欄位，給預設值以維持既有建構呼叫相容：
```python
    origin: str = "operator"
    commission_id: str | None = None
    suggestion_state: str | None = None
    origin_meta: dict[str, Any] = field(default_factory=dict)
```
連帶更新 `_row_to_marketplace_watch`（storage.py:1308）解析這四欄（`origin_meta_json`
照既有 `market_options_json` 的 try/except JSON 解析慣例），以及 `add_marketplace_watch`
／`update_marketplace_watch` 的 INSERT/UPDATE 欄位列表與 `ON CONFLICT` 子句。

### `list_marketplace_watchlist` 與 `/watch list` 顯示

- `list_marketplace_watchlist` 加選用參數 `origin: str | None = None`、
  `suggestion_state: str | None = None` 做過濾（比照既有 `market` 過濾的 in-memory filter
  或加 WHERE）。
- `/watch list` 輸出**依 origin 分組**，標頭用 emoji 一眼分辨：
  `🧍 我的追蹤` / `🎁 委託追蹤`（附委託 id/買家代稱）/ `🤖 龍蝦建議`（pending 的另列
  受/拒按鈕）。

### `watch_monitor.py` 唯一要改的分派點

輪詢命中（`record_marketplace_hits` 回傳 new/price_changed）後，依 `watch.origin` 分派：
```python
if watch.origin == "commission" and watch.commission_id:
    notify_commission_candidates(watch.commission_id, hits, fair_value, verdicts)
elif watch.origin == "suggested":
    pass  # accepted 才會輪詢到這；命中比照 operator
else:  # operator（含 accepted 的 suggested）
    notify_operator_price_alert(hits)
```
其餘輪詢/去重/`mark_watch_checked` 邏輯**完全不動**。pending 的 suggested 因 enabled=0
根本不會進輪詢迴圈，發掘建議是另一條背景流程產生的（後期增強，見下）。

### 建立來源（誰寫這些列）

- **operator**：現行 `/watch` 指令，不變。
- **commission**：`commission/search.py` 在委託進入 `searching` 時，呼叫
  `add_marketplace_watch(MarketplaceWatch(origin="commission", commission_id=…, query=正規化詞, price_threshold_jpy=反推上限, enabled=True, …))`；委託離開 `searching`（成交/取消）時 `toggle_marketplace_watch(enabled=False)` 或刪除。
- **suggested**：**屬後期增強**（對應「SNS 補貨/熱度監看」「趨勢發掘」）。背景任務發現熱
  標的 → 建 `origin='suggested', suggestion_state='pending', enabled=0` 列並推建議訊息；
  操作員按「受」→ `suggestion_state='accepted', enabled=1`；按「拒」→ `'dismissed',
  enabled=0`（保留當墓碑，發掘任務先查 dismissed 避免重複騷擾）。**MVP 可只先落地
  schema 欄位 + operator/commission 兩條路徑**，suggested 的發掘任務隨後期再接。

---

## 分階段範圍

> **已選定核心賣點：「主動代尋 + 公允價」。** 故把搜尋＋公允價判斷**拉進 MVP**——
> 它才是讓人願意用你的價值核心；「貼 URL 幫你買」最沒差異化、最後做。
> 委託狀態機/報價只是支撐骨架，不是賣點。

### MVP（價值核心）：委託 want → 正規化 → 自動代尋 → top-N＋評語 → 操作員確認 → 複製貼上
合併「最小骨架」＋「需求正規化」＋「主動搜尋」。**零對外曝光**（無 web/email/tunnel）。
- **委託進件（先簡單）**：操作員在 Telegram `/commission new` 貼上買家「想要的東西
  ＋**目標價**」即可建單；自助網頁表單留到後期。
- **需求正規化（賣點）**：跑 `commission/normalize.py`（地端 LLM＋知識庫＋/search）把
  模糊中文需求 → 日文精準搜尋詞＋品項類型，操作員確認後才搜。詳見上節。
- **自動代尋（賣點）**：`commission/search.py` 用正規化後的查詢，定期呼叫
  `search_mercari/search_rakuma(query, price_max=目標價)`，掛進 `watch_monitor.py`
  既有輪詢迴圈（每委託一個 `schedule_minutes`）。閘＝`listing_price ≤ 目標價`。
- **候選呈現（賣點）**：推**前 N 筆**，每筆附二手均價（`fetch_avg_sold_price` 輔助）
  ＋地端 LLM 一句**評語**，推給操作員 Telegram（沿用 SNS alert 推播）；操作員判斷。
- **骨架**：`models/storage/state/fee/templates` + `bot.py` 的 `/commission`、`cm:`
  callback；操作員確認候選 → 產生下單確認文件（匯款＋金額＋連結）複製貼上文字。
- **新檔**：`commission/{models,storage,fee,state,templates,normalize,search}.py`、
  `bot.py` 修改、`watch_monitor.py` 掛入、`tests/test_commission_*.py`（tmp_path DB）。
- **依賴**：地端 Ollama（`OPENCLAW_LOCAL_TEXT_ENDPOINT/MODEL`）；掛掉時正規化/評語降級。

### 後期增強（依序、非 MVP）
1. **抽選偵測整合（代抽）**：把 Bandai/PokeCen crawler 的 `LOTTERY_OPEN` 訊號接進
   委託搜尋，命中抽選窗時特別標示。
2. **SNS 補貨/熱度監看**：把 sns_monitor 訊號接進委託（want 對應帳號/關鍵字補貨提醒）。
3. **賣家信譽查核**：候選帶 `reputation_snapshot` 賣家信任分數。
4. **買家自助 web + Email 自動化**：GitHub Pages 表單 + Flask `/intake`+`/act`
   + SMTP + Tailscale Funnel。**對外曝光 → 套用「資安分析」全部改善。**

#### 後期 web/email 細節
- **新** GitHub Pages repo：靜態委託表單（純 HTML/JS，免費），POST 到後端。
- **新** `commission_web/app.py`：Flask（仿 `reputation_snapshot/app.py` 的 `create_app()`）：
  - `POST /intake`：驗證 → 寫 `request` → Telegram 通知操作員。
  - `GET /act?token=…`：買家點 email 內連結 → 改狀態 → 通知操作員。需 CORS（限該 Pages 網域）。
- **Email 寄出**：`commission/email.py` 用 stdlib `smtplib`，**Gmail + app password，
  登入資訊寫在 `.env`（已被 .gitignore，不上 git）**，寄報價單/下單文件/寄送通知，
  內嵌 tokenized 動作連結。**不收信、不解析 IMAP。**
- **部署**：
  - launcher 加第 5 個 launchd service `local.openclaw.commission`（Flask app）。
  - **Tailscale Funnel** 對外曝光該 port（免費、固定 `https://<host>.ts.net`、無需網域）。
    一次性手動設定（裝 Tailscale + `tailscale funnel`）——需使用者執行（涉及帳號）。
    GitHub Pages 表單的後端網址填這個固定 URL。
    - 備案：Cloudflare Tunnel（免費但 named tunnel 需網域）。

---

## 不改動 / 沿用
- Telegram 仍是單操作員（`allowed_chat_ids`）；買家**不進** Telegram。
- 沿用 SQLite + frozen dataclass + UTC ISO 時間戳慣例，新表獨立、不動既有表。
- 搜尋複用既有 `MarketplaceSearchClient`，不寫新爬蟲。

---

## 已確認的細節
1. **費率**：採用上述預設值；**不做刷卡加成**。
2. **Email 帳號**：Gmail + app password，寫在 `.env`（不上 git）。
3. **對外溝通**：MVP 純複製貼上（操作員自己貼到 email/LINE）；自動寄 email 留到後期。

---

## 資安分析與改善（認真檢查）

**現況基線（既有系統，探索確認）：**
- 4 個 repo 的 `.env` 都已被 `.gitignore`。本機存在 `.env` 檔屬正常。**實作前先驗證**：
  `git ls-files | grep -i env` 確認沒被追蹤；若已追蹤則 `git rm --cached`（不刪本機檔）。
- 既有 reputation Flask：綁可設定 host（launcher 預設 `127.0.0.1`）、`debug=False`、
  已有 `ADMIN_TOKEN` 驗證模式（query/header/api_key 三擇一）＋ URL 驗證工具
  (`is_valid_*` / `normalize_*`)。→ **新服務直接沿用這兩個既有模式。**

**本次新增的主要攻擊面 = 對外公開的 web 端點**（MVP/內部階段無對外曝光，風險低）。

| 風險 | 說明 | 改善（實作時內建） |
|---|---|---|
| **公開 /intake 被濫用/DoS** | Funnel 把端點曝到公網、未驗證 | 每 IP rate limit；honeypot 隱藏欄位擋 bot；`MAX_CONTENT_LENGTH` 限制 body；伺服器端逐欄驗證＋長度上限；丟棄控制字元 |
| **CORS 過寬** | GitHub Pages 跨網域 | `Access-Control-Allow-Origin` **只允許**該 Pages 網域，不用 `*` |
| **tokenized 連結被猜/重放** | 買家動作連結 `/act?token=` | `secrets.token_urlsafe(32)`；DB 只存其 **SHA-256 hash**；**單次有效**＋7 天到期；綁定 `(commission_id, action)`；比對用 `secrets.compare_digest` |
| **Funnel 曝光範圍過大** | Funnel 是 per-port 對全網 | commission_web 跑**獨立 port**，只開 `/intake`、`/act` 兩條公開路由；操作員/管理路由走 `ADMIN_TOKEN` 或不經 Funnel；Flask 綁 `127.0.0.1`，只由 Funnel 代理該 port |
| **買家自由文字注入** | 文字會進 Telegram 與 email | Telegram：escape MarkdownV2 特殊字元（或純文字送）；Email：**純文字**寄送（不用 HTML）；**header injection** → Subject/收件人欄位拒絕換行字元 |
| **SQL injection** | — | 維持既有 parameterized query 慣例，新碼一律 `?` 綁定 |
| **PII 落地** | 買家姓名/地址/email/電話存 SQLite | 資料最小化：**寄件地址等敏感欄位延到 `shipping_prep` 階段才收**，intake 只收必要欄位；DB 檔權限 600；DB 與 `.bak` 已 gitignore；結案後可排程清除 PII |
| **「我已匯款」被冒認** | 買家點連結宣稱已付 | **不自動信任**：永遠由操作員人工核對銀行入帳才進 `paid_verified`（刻意的安全控制） |
| **Secrets 外洩** | Gmail app password / token 簽章金鑰 / ADMIN_TOKEN | 全放 `.env`（gitignore ✓）；用 app password 非主密碼；SMTP 走 TLS；log 不印 secrets |

**結論**：內部/MVP 階段幾乎無新資安風險（純內部 + Telegram 單操作員）。風險集中在
對外端點，上述改善**在實作 web 階段一併內建**即可，不需事先大改既有系統。
唯一建議**現在就做的小事**：驗證 `.env` 未被 git 追蹤（一行指令）。

---

## 驗證計畫
- **MVP**：`cd price_monitor_bot && .venv/bin/python -m pytest tests/ -q --ignore=tests/test_mercari_search.py` 全綠；手動跑一筆委託從 `request` 一路推到 `done`，每步檢查狀態轉移合法、報價數字正確、複製貼上模板內容完整；對一筆委託跑搜尋，確認候選有照目標價過濾、帶公允價 verdict、推播到 Telegram。
- **web 階段**：本機 `curl POST /intake` 建單；點 `/act?token=` 確認改狀態並通知操作員；
  從 GitHub Pages 表單經 Tailscale Funnel 實際送一筆；收到一封含可點連結的測試 email。

---

## 修改/新增檔案清單

| 檔案 | 階段 | 類型 | 說明 |
|---|---|---|---|
| `price_monitor_bot/src/commission/models.py` | MVP | 新增 | Commission dataclass + Status 列舉 |
| `price_monitor_bot/src/commission/storage.py` | MVP | 新增 | CommissionDatabase + bootstrap + event log |
| `price_monitor_bot/src/commission/fee.py` | MVP | 新增 | FeeConfig + compute_quote |
| `price_monitor_bot/src/commission/state.py` | MVP | 新增 | 狀態轉移表 + advance() |
| `price_monitor_bot/src/commission/templates.py` | MVP | 新增 | 報價/下單/寄件/通知文字模板 |
| `price_monitor_bot/src/commission/normalize.py` | MVP | 新增 | 模糊需求 → 日文精準搜尋詞（地端 LLM＋知識庫＋/search），重用 `extract_entities`/`KnowledgeDatabase`/`_call_ollama_json`/`build_web_research_answer` |
| `price_monitor_bot/src/commission/search.py` | MVP | 新增 | 正規化查詢 → 搜尋客戶端 + 目標價閘 + top-N + LLM 評語 + 推播 |
| `price_monitor_bot/src/price_monitor_bot/bot.py` | MVP | 修改 | `/commission` 指令 + `cm:` callback + 通知 |
| `price_monitor_bot/src/market_monitor/storage.py` | MVP | 修改 | `marketplace_watchlist` 加 `origin`/`commission_id`/`suggestion_state`/`origin_meta_json` 四欄＋冪等 migration＋索引；`MarketplaceWatch` dataclass、`_row_to_marketplace_watch`、`add/update/list_marketplace_watch*`、`build_marketplace_watch_id` 連帶更新（見「/watch 三類來源 DB 設計」） |
| `price_monitor_bot/src/price_monitor_bot/watch_monitor.py` | MVP | 修改 | 掛入委託定期搜尋；命中後依 `watch.origin` 分派（commission→候選審核 / operator→降價提醒 / suggested→略過） |
| `price_monitor_bot/tests/test_commission_*.py` | MVP | 新增 | tmp_path DB 測試 |
| `commission_web/app.py`（新位置） | 後期 | 新增 | Flask intake + /act token 端點 |
| GitHub Pages repo（新） | 後期 | 新增 | 靜態委託表單前端 |
| `price_monitor_bot/src/commission/email.py` | 後期 | 新增 | SMTP 寄出 + tokenized 連結 |
| `aka_no_claw/launchers/start-mac-mini-stack.command` | 後期 | 修改 | 加 commission launchd service |
