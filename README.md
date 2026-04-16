# OpenClaw Personal Assistant Workspace

這個專案把 OpenClaw 當成個人助理入口，價格追蹤只是其中一組工具，不是整個系統本體。

開始任何 substantial work 前，先讀 [Constitution.md](/C:/AI_Related/codex_work_space/Constitution.md)。

目前我們先把底層拆成三層：

- `assistant_runtime`
  - 通用 tool registry、`.env` 載入、assistant runtime 基礎元件。
- `market_monitor`
  - 可重用的追價核心，之後除了卡牌，也能擴充到色紙、二手 J-Pop CD、模型等品項。
- `tcg_tracker`
  - 卡牌專用模組，先支援 Pokemon / Weiss Schwarz 的卡片正規化與遊々亭查價。
- `openclaw_adapter`
  - OpenClaw / Telegram / CLI 的接點，負責把底層能力註冊成 assistant tools。

## 專案結構

```text
src/
  assistant_runtime/  # 通用 assistant tool runtime
  market_monitor/     # 通用追價核心
  tcg_tracker/        # TCG 專用模組
  openclaw_adapter/   # OpenClaw / CLI / Telegram 入口
tests/
config/
data/
```

## Python 開發流程

這個 repo 預設使用：

- `.venv` 管理虛擬環境
- `requirements.txt` / `requirements-dev.txt` 管理相依
- `.env` 管理敏感資訊與本機設定
- `.gitignore` 避免把虛擬環境、資料庫、快取和敏感資訊推上 git

建立虛擬環境：

```powershell
python -m venv .venv
```

PowerShell 啟用：

```powershell
.\.venv\Scripts\Activate.ps1
```

安裝相依：

```powershell
python -m pip install -r requirements-dev.txt
```

建立本機設定：

```powershell
Copy-Item .env.example .env
```

`.env` 目前可放：

```dotenv
MONITOR_ENV=development
LOG_LEVEL=INFO
MONITOR_DB_PATH=data/monitor.sqlite3
YUYUTEI_USER_AGENT=OpenClawPriceMonitor/0.1 (+https://local-dev)
OPENCLAW_TELEGRAM_CHAT_ID=
OPENCLAW_TELEGRAM_BOT_TOKEN=
OPENCLAW_CA_BUNDLE_PATH=
OPENCLAW_TLS_INSECURE_SKIP_VERIFY=0
```

注意：

- 程式執行時讀的是 `.env`，不是 `.env.example`。
- `.env.example` 只能留空白範例值，不能放真實 token 或 chat id。
- 如果 Telegram API 在你的網路環境下出現 TLS / 憑證錯誤，可以先嘗試 `OPENCLAW_CA_BUNDLE_PATH`。
- 只有在本機測試被公司憑證或代理擋住時，才最後手動改用 `OPENCLAW_TLS_INSECURE_SKIP_VERIFY=1`。

## Assistant Tools

列出目前所有已註冊工具：

```powershell
python -m openclaw_adapter list-tools
```

啟動本地 dashboard UI：

```powershell
python -m openclaw_adapter serve-dashboard --open-browser
```

如果不自動開瀏覽器，也可以手動打開 `http://127.0.0.1:8765`。

Dashboard 上方目前會顯示兩塊高流動性榜：

- Pokemon 高流動性前十
- Weiss Schwarz 高流動性前十

目前的判斷方式是流動性優先，而不是單純話題熱度：

- Pokemon 目前以 Card Rush Pokemon 高稀有單卡頁面的可見庫存深度為主，賣場排序只當次要 tie-breaker，並把同卡不同品況合併。
- WS 目前以 magi 的 Weiss Schwarz 商品頁 active 出品數為主，頁面排序只當次要 tie-breaker，並把同卡不同 grade 版本合併。
- 0 active listing / 0 在庫的項目不會再被當成高流動性主訊號，只會在資料不足時作為 fallback。
- graded 卡會被視為比 raw copy 更低流動性，因此同深度下會往後排。
- 每張卡都會附上排行榜來源頁與商品頁連結，方便直接回原站核對。
- `listing_count` 會當成主要流動性訊號，另外搭配來源頁排序與是否 graded 做輔助判斷。

如果你想直接雙擊或用一行命令啟動，也可以用 repo 根目錄的批次檔：

```powershell
.\start-dashboard.bat
```

要改 port 或 host 時，參數會直接透傳：

```powershell
.\start-dashboard.bat --host 127.0.0.1 --port 8766
```

如果 dashboard 已經開著舊版進程，改完程式後要先關掉再重開，否則頁面不會吃到新邏輯。

Telegram 測試 bot：

```powershell
python -m openclaw_adapter telegram-send-test
python -m openclaw_adapter telegram-poll --notify-startup
```

也可以直接用批次檔：

```powershell
.\start-telegram-bot.bat --notify-startup
```

目前 Telegram 測試指令：

```text
/start
/help
/ping
/status
/tools
/lookup pokemon ピカチュウex
/lookup pokemon | ピカチュウex | 132/106 | SAR | sv08
/lookup ws | “夏の思い出”蒼(サイン入り) | SMP/W60-051SP | SP
/liquidity pokemon
/liquidity ws 5
```

查卡價：

```powershell
python -m openclaw_adapter tcg.lookup-card `
  --game pokemon `
  --name "ピカチュウex" `
  --card-number "132/106" `
  --rarity "SAR" `
  --set-code "sv08"
```

Weiss Schwarz 範例：

```powershell
python -m openclaw_adapter tcg.lookup-card `
  --game ws `
  --name "15th Anniversary カレン(サイン入り)" `
  --card-number "KMS/W133-002SSP" `
  --rarity "SSP" `
  --set-code "kms"
```

灌入範例 watchlist：

```powershell
python -m openclaw_adapter tcg.seed-example-watchlist
```

列出目前已知的參考資料來源：

```powershell
python -m openclaw_adapter market.list-reference-sources
```

只看 Pokemon 可用來源：

```powershell
python -m openclaw_adapter market.list-reference-sources --game pokemon
```

只看 `listing_price` 類型來源：

```powershell
python -m openclaw_adapter market.list-reference-sources --role listing_price
```

舊 alias 仍保留：

```powershell
python -m openclaw_adapter lookup-card ...
python -m openclaw_adapter seed-example-watchlist
python -m openclaw_adapter list-reference-sources
```

## 參考資料來源策略

系統現在把資料來源分成三類，而不是把所有站都當成同一種價格來源：

- `official_metadata`
  - 用來核對正式卡名、卡號、彈別、標題，不直接當行情價。
  - 例：Pokemon 官方卡牌搜尋、Weiss Schwarz 官方卡表。
- `specialty_store`
  - 用來做高可信的販售價 / 買取價參考。
  - 例：Yuyu-Tei、Card Rush Pokemon、Hareruya 2。
- `marketplace` / `market_content`
  - 用來看流動性、刊登深度、熱門訊號和撿漏機會。
  - 例：Mercari、SNKRDUNK、Magi、Yahoo Flea Market、Rakuma。

這個來源目錄目前定義在 [config/reference_sources.json](/C:/AI_Related/codex_work_space/config/reference_sources.json)，之後不同模組都可以共用這份來源權重與角色設定。

## 目前已納入的可靠參考來源

- Pokemon 官方卡牌搜尋
- Weiss Schwarz 官方卡表
- Yuyu-Tei
- SNKRDUNK 主站
- Card Rush Pokemon
- Hareruya 2 buylist
- Magi
- Mercari
- Yahoo Flea Market
- Rakuma

不是每個來源都會在第一階段立刻寫 collector，但它們現在已經先被正式放進「來源目錄」，後續做定價、比價和異常低價判斷時可以直接沿用。

## 測試

執行單元測試：

```powershell
python -m pytest
```

跑 live card lookup 範例：

```powershell
python -m tcg_tracker.live_checks
```

## 下一步

接下來比較自然的延伸是：

- SNKRDUNK collector
- Mercari scanner
- 低於行情價 alert pipeline
- Telegram 指令與排程任務
- 更多非追價工具，例如 reminders、daily summary、notes capture
