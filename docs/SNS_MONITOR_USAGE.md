# SNS Monitor 使用指南

SNS Monitor 讓你在 X (Twitter) 上追蹤帳號、關鍵字和熱門話題，並在 Telegram 上接收通知。

## 快速開始

### 1. 啟動 Telegram Bot (自動啟動 SNS Monitor)

```bash
cd /Users/jen/ai_work_space/related_to_claw/aka_no_claw

# 啟動 Telegram 輪詢 (自動啟動 SNS Monitor)
.venv/bin/python -m openclaw_adapter telegram-poll

# 或使用儀表板
.venv/bin/python -m openclaw_adapter telegram-poll --dashboard
```

SNS Monitor 將在後臺自動啟動，每隔 15-60 分鐘檢查一次新推文。

### 2. 通過 Telegram 命令添加監控

在 Telegram 聊天室中發送命令：

#### 追蹤帳號
```
/snsadd @aka_claw
/snsadd @elonmusk
/snsadd @OpenAI
```

只在帳號推文提到指定詞時通知：
```
/snsadd @elonmusk ["buy", "sell"]
/snsadd @realDonaldTrump buy sell
```

#### 追蹤關鍵字
```
/snsadd keyword:機動戰士
/snsadd keyword:生成式AI
/snsadd keyword:Python開發
```

#### 追蹤熱門話題
```
/snsadd trend:trending
/snsadd trend:for-you
/snsadd trend:news
```

### 3. 管理監控規則

#### 查看所有監控規則
```
/snslist
```

輸出範例：
```
SNS 監控規則 (3 個):

  ✓ @aka_claw | aka_claw
     ID: account_1252b478581c
     間隔: 15 分鐘

  ✓ @elonmusk | Elon Musk
     ID: account_d0e3942ba427
     間隔: 15 分鐘

  ✓ Keyword: "機動戰士" | Gundam
     ID: keyword_1ef668326c55
     間隔: 30 分鐘
```

#### 刪除監控規則
```
/snsdelete account_1252b478581c
```

## CLI 工具使用

### 使用命令行添加監控

#### 添加帳號監控
```bash
python -m openclaw_adapter sns.add-account elonmusk \
  --label "Elon Musk" \
  --chat-id <YOUR_CHAT_ID> \
  --interval 15
```

只通知包含任一指定詞的帳號推文：
```bash
python -m openclaw_adapter sns.add-account elonmusk \
  --chat-id <YOUR_CHAT_ID> \
  --keywords buy sell
```

#### 添加關鍵字監控
```bash
python -m openclaw_adapter sns.add-keyword "機動戰士" \
  --label "Gundam" \
  --chat-id <YOUR_CHAT_ID> \
  --interval 30
```

#### 添加趨勢監控
```bash
python -m openclaw_adapter sns.add-trend trending \
  --label "Trending Topics" \
  --chat-id <YOUR_CHAT_ID> \
  --interval 60
```

#### 查看所有規則
```bash
python -m openclaw_adapter sns.list-rules
```

#### 啟用/禁用規則
```bash
# 禁用規則
python -m openclaw_adapter sns.toggle-rule account_1252b478581c --disabled

# 啟用規則
python -m openclaw_adapter sns.toggle-rule account_1252b478581c --enabled
```

#### 刪除規則
```bash
python -m openclaw_adapter sns.delete-rule account_1252b478581c
```

## 監控間隔

每種監控類型有不同的推薦間隔：

| 類型 | 推薦間隔 | 說明 |
|------|--------|------|
| 帳號 | 15 分鐘 | 跟蹤活躍用戶的最新推文 |
| 關鍵字 | 30 分鐘 | 追蹤特定主題的討論 |
| 趨勢 | 60 分鐘 | 監控整體熱門話題 |

## 監控規則 ID

每個監控規則都有唯一的 ID，用於管理：

```
account_1252b478581c   # 帳號監控 (@aka_claw)
keyword_1ef668326c55   # 關鍵字監控 (機動戰士)
trend_4433392b6baa     # 趨勢監控 (trending)
```

你可以在 `/snslist` 的輸出中找到這些 ID。

## 故障排除

### SNS 監控未啟動

確保：
1. X 憑證已在 .env 中配置
2. `telegram-poll` 已啟動
3. 查看日誌: `tail -f logs/openclaw.log | grep sns`

### 沒有收到通知

1. 檢查規則是否已啟用: `/snslist`
2. 檢查聊天 ID 是否正確: `.env` 中的 `OPENCLAW_TELEGRAM_CHAT_ID`
3. 查看日誌中的錯誤信息

### 規則未保存

確保你有寫入權限到 `data/sns.sqlite3` 所在的目錄。

## 配置文件

### .env 中的 SNS 設置

```dotenv
# SNS / X (Twitter) Monitor
SNS_DB_PATH=data/sns.sqlite3
X_USERNAME=akanoclaw
X_USER_MAIL=akanoclaw@gamil.com
X_USER_PASSWORD=akanoclawpassX
X_COOKIES_FILE=data/x_cookies.json
```

## 趨勢類別

支持的趨勢類別：
- `trending` - 目前在 X 上的熱門話題
- `for-you` - 為你推薦的熱門話題
- `news` - 新聞相關熱門話題
- `sports` - 體育相關熱門話題
- `entertainment` - 娛樂相關熱門話題

## 注意事項

⚠️ **服務器禮儀**
- SNS Monitor 使用隨機延遲 (1.5-4.0 秒) 以避免伺服器過載
- 自動實施速率限制迴避機制
- 首次掃描將所有現有推文標記為已通知（避免轟炸）

## 進階功能

### 數據庫查詢

直接查詢 SQLite 數據庫：

```bash
sqlite3 data/sns.sqlite3

# 列出所有監控規則
SELECT * FROM watch_rules;

# 查看推文歷史
SELECT * FROM tweets ORDER BY published_at DESC LIMIT 10;

# 統計監控規則
SELECT kind, COUNT(*) FROM watch_rules WHERE enabled=1 GROUP BY kind;
```

### 日誌分析

查看 SNS Monitor 活動日誌：

```bash
grep "sns" logs/openclaw.log
grep "XClient" logs/openclaw.log
```

## 示例場景

### 場景 1: 追蹤特定開發者的推文

```bash
# 通過 Telegram
/snsadd @OpenAI
/snsadd @AnthropicAI

# 或通過 CLI
python -m openclaw_adapter sns.add-account OpenAI --chat-id <YOUR_CHAT_ID>
```

### 場景 2: 監控產品發布公告

```bash
# 通過 Telegram
/snsadd keyword:新產品發布
/snsadd keyword:beta版本

# 或通過 CLI
python -m openclaw_adapter sns.add-keyword "新產品發布" --chat-id <YOUR_CHAT_ID>
```

### 場景 3: 追蹤行業趨勢

```bash
# 通過 Telegram
/snsadd trend:trending
/snsadd trend:news

# 或通過 CLI
python -m openclaw_adapter sns.add-trend trending --chat-id <YOUR_CHAT_ID>
```

## 支援

如遇問題，請查看：
1. [SNS 集成測試報告](archive/SNS_INTEGRATION_TEST_REPORT.md)（歷史快照）
2. [Telegram 工具規範](TELEGRAM_TOOL_SPEC.md)
3. 系統日誌: `logs/openclaw.log`
4. 數據庫: `data/sns.sqlite3` (SQLite)
