# SNS Monitor 故障排除指南

Last reviewed: 2026-06-20
Status: Current
Owner area: sns

## 問題: 沒有收到 X (Twitter) 通知

### 症狀
- `/snslist` 顯示監控規則已添加 ✓
- `telegram-poll` 正常運行 ✓  
- 但 X 帳號發文時沒有收到 Telegram 通知

### 根本原因

**Twikit 與 X 的加密機制不兼容**

從 2026-05-12 開始，X 改變了其內部加密算法，twikit 2.3.3 版本無法解析新的加密指標。

日誌錯誤:
```
Exception: Couldn't get KEY_BYTE indices
```

---

## 解決方案

### 方案 A: 使用 X 官方 API（推薦 - 長期方案）

需要：
1. 在 [developer.x.com](https://developer.x.com) 註冊開發者帳號
2. 創建 API 應用取得 `API_KEY`, `API_SECRET`, `ACCESS_TOKEN`, `ACCESS_TOKEN_SECRET`
3. 切換到 tweepy 或 official X SDK

**優點**:
- 官方支持，持久穩定
- 更快的 API 速率限制
- 功能更完整

**缺點**:
- 需要官方開發者批准
- 可能有 API 配額限制

### 方案 B: 等待 Twikit 更新（短期臨時方案）

監控 twikit GitHub 倉庫：
- [twikit GitHub](https://github.com/d60/twikit)
- 預期 twikit 會在近期發布補丁版本修復加密問題

```bash
# 定期檢查更新
pip install --upgrade twikit
```

### 方案 C: 使用 Selenium 瀏覽器自動化（備用方案）

如果 API 不可用，使用無頭瀏覽器：
- 安裝 Selenium + ChromeDriver
- 模擬人工瀏覽 X 網站

**缺點**: 非常慢，容易被檢測到，不推薦

---

## 當前狀態

| 功能 | 狀態 | 說明 |
|------|------|------|
| CLI 工具 | ✅ 完全正常 | 規則添加、管理、刪除都正常 |
| 規則存儲 | ✅ 完全正常 | SQLite 數據庫持久化運行 |
| Telegram 集成 | ✅ 完全正常 | 命令解析和規則查詢正常 |
| X 登錄 | ❌ 失敗 | Twikit 加密問題，無法登錄 |
| 推文抓取 | ⏸️ 被阻 | 因登錄失敗而無法進行 |
| 通知發送 | ⏸️ 被阻 | 等待推文數據 |

---

## 臨時工作方案

如果急需功能，可以使用以下方式：

### 1. 使用 CLI 工具驗證設置
```bash
python -m openclaw_adapter sns.list-rules
```

這證實所有規則已正確配置，只需要 X 連接修復。

### 2. 手動添加推文到通知隊列（開發人員）

編輯 `data/sns.sqlite3` 直接添加測試推文，驗證 Telegram 通知流程。

### 3. 使用替代 X 監控（臨時）

如果有其他 X API 訪問方式，可以修改 `sns_monitor_bot/src/sns_monitor/x_client.py` 使用替代後端。

---

## 推薦行動步驟

### 立即（今天）
1. ✅ 確認規則已正確添加: `/snslist`
2. ✅ 驗證 Telegram Bot Token 有效
3. 📝 記錄此票為"待修復"

### 短期（本週）
1. 檢查 twikit 是否發布新版本
2. 如果有新版本: `pip install --upgrade twikit` 後使用 `/restartall`
   或 web console restart，讓 Telegram、command bridge、web frontend 與背景服務
   一起用同一套 runtime 重啟。
3. 如果沒有新版本: 啟動申請 X 開發者 API

### 長期（本月）
1. 申請 X 官方 Developer API
2. 切換 `sns_monitor_bot` 使用官方 API
3. 享受穩定可靠的 X 監控

---

## 測試 Telegram 通知系統

即使 X 連接暫時不可用，你也可以測試 Telegram 通知流程：

```bash
# 進入 Python shell
python -c "
from price_monitor_bot.bot import TelegramClient
from assistant_runtime import get_settings

settings = get_settings()
client = TelegramClient(settings.openclaw_telegram_bot_token)

chat_ids = settings.openclaw_telegram_chat_id.split(',')
for chat_id in chat_ids:
    client.send_message(
        chat_id=chat_id,
        text='SNS Monitor 測試通知 - 系統正常 ✓'
    )
print('通知已發送')
"
```

---

## 日誌查看

### 查看最新錯誤
```bash
tail -100 logs/telegram-poll.log | grep -i "sns\|error\|exception"
```

### 完整日誌分析
```bash
grep "sns_monitor" logs/telegram-poll.log | tail -50
```

### 檢查 X 客戶端初始化
```bash
grep "XClient\|twikit\|KEY_BYTE" logs/telegram-poll.log
```

---

## 知道的限制

1. **X 加密變更** (2026-05-12)
   - X 更改了客戶端加密算法
   - Twikit 還未適配新算法
   - 預計 twikit 會在近期發布修復

2. **Rate Limiting**
   - 即使連接恢復，X 也有速率限制
   - SNS Monitor 已實現 600s/900s 冷卻機制
   - 人類瀏覽延遲 (1.5-4.0s) 已配置

3. **持久化 Cookies**
   - X Cookies 會定期過期
   - `X_COOKIES_FILE` 自動管理
   - 定期重新登錄是預期行為

---

## 聯繫支持

此問題的根本原因是 **Twikit 庫的外部依賴問題**，不是 aka_no_claw 集成的問題。

相關資源:
- [Twikit GitHub Issues](https://github.com/d60/twikit/issues)
- [X API Docs](https://developer.x.com/en/docs)
- [Twikit Documentation](https://github.com/d60/twikit)

---

## 更新履歷

| 日期 | 事件 | 狀態 |
|------|------|------|
| 2026-05-12 | 發現 twikit 加密不兼容 | 診斷完成 |
| - | 等待 twikit 更新 | ⏳ 進行中 |
