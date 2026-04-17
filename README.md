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

Dashboard 上方目前會顯示兩塊高流動性榜，並支援切換要顯示前幾名：

- Pokemon 高流動性榜
- Weiss Schwarz 高流動性榜

完整邏輯寫在 [LIQUIDITY_METHODOLOGY.md](/C:/AI_Related/codex_work_space/LIQUIDITY_METHODOLOGY.md)。

目前的判斷方式重新調整成「主流動性 + 次級注意力」兩層，而不是把熱門度直接混進流動性：

- Pokemon 候選卡先來自 Card Rush Pokemon 頁；如果 Card Rush 在目前環境下阻擋抓取，會改用 magi Pokemon 頁面做候選池。
- WS 候選卡目前來自 magi Weiss Schwarz 頁面。
- `liquidity_score` 主體改成買方承接力：遊々亭買取價是否存在、買取價和賣價的接近程度，再加上 raw / graded 的可替代性微調。
- `buy_support_score` 會直接顯示 best bid / best ask / bid-ask ratio，這比單純看出品數更接近「能不能快速成交」。
- 如果店家頁面明確顯示「買取價上調」這種硬訊號，目前會當成輔助參考並做小幅加成；現階段已接進的是遊々亭 buy page 的 `priceup + 舊價` 組合，不是單純吃行銷文案，也不會單獨主導排序。
- `attention_score` 現在只保留給 SNS 公開訊號，不再混入來源頁出品排序。
- 同卡不同品況或不同 grade 會先合併，避免同一張卡占掉多個名次。
- graded 卡會被視為比 raw copy 更低可替代，因此會吃到流通性扣分。
- `listing_count` / `在庫数` / `出品数` 只留在備註裡做背景資訊，不參與分數。
- SNS 目前使用 Yahoo!リアルタイム検索 的公開搜尋頁當作輔助參考，只用來補需求加速度，不直接當成流動性本體。
- 最近一個月成交量目前還沒有對 Pokemon / WS 都一致且穩定的公開來源，因此暫時沒有把「月成交件數」直接做成主分數；在這個版本中，買方承接訊號比出品數更可信。
- 每張卡都會附上排行榜來源頁、商品頁連結和卡圖縮圖，方便直接回原站核對。

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

### Telegram Token Rotation

如果你懷疑 bot token 外流，或只是想定期換新 token，可以在 Telegram 的 `@BotFather` 完成：

1. 打開 `@BotFather`
2. 輸入 `/mybots`
3. 選你的 bot
4. 進入 `API Token` 或 `Bot Settings`
5. 選 `Revoke current token`
6. 確認後取得新的 token
7. 把新的 token 更新到本機 `.env`

`.env` 只需要更新這一行：

```dotenv
OPENCLAW_TELEGRAM_BOT_TOKEN=你的新token
```

更新後重啟 bot：

```powershell
.\start-telegram-bot.bat --notify-startup
```

注意：

- 舊 token 一旦 revoke，舊 bot 進程就不能再發訊息。
- 不要把真實 token 放進 `.env.example`。
- 這個專案會讀 `.env`，不是 `.env.example`。

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
