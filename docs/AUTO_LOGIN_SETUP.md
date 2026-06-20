# ✨ 完全自動化啟動指南

Last reviewed: 2026-06-20
Status: Needs review
Owner area: operations

**無需任何手動操作** - 所有 Cookies 獲取和登錄已整合到服務啟動過程中。

---

## 🚀 一鍵啟動

```bash
cd /path/to/aka_no_claw
.venv/bin/python -m openclaw_adapter telegram-poll --with-reputation-agent
```

**就這樣！** 服務會自動：
1. ✅ 啟動 Playwright 瀏覽器
2. ✅ 自動登錄 X
3. ✅ 保存 Cookies
4. ✅ 啟動 SNS Monitor
5. ✅ 開始監控推文

---

## 📊 啟動流程

```
telegram-poll 啟動
    ↓
SNS Monitor 初始化
    ↓
X 客戶端檢查 Cookies
    ├─ 有效 Cookies? → 直接使用 ✅
    └─ 無效/不存在? → 自動登錄 🔐
        ↓
    啟動 Playwright 瀏覽器
        ↓
    訪問 https://x.com/login
        ↓
    自動填充:
        • 郵箱: akanoclaw@gamil.com
        • 用戶名: akanoclaw
        • 密碼: (已配置)
        ↓
    等待登錄完成
        ↓
    保存 Cookies 到 data/x_cookies.json
        ↓
    關閉 Playwright 瀏覽器
        ↓
SNS Monitor 開始監控 🎉
    ├─ @aka_claw 推文
    ├─ @elonmusk 推文
    ├─ 機動戰士 關鍵字
    └─ 通過 Telegram 發送通知
```

---

## ⚙️ 自動化實現

### 1. 改進的自動登錄 (`x_client_web.py`)

```python
class XClientWeb:
    async def _login():
        # 步驟 1: 檢查已有 Cookies
        if cookies_valid:
            return ✅
        
        # 步驟 2: 啟動 Playwright
        browser = launch(headless=True)  # 無頭模式
        
        # 步驟 3: 自動填充登錄表單
        fill_email("akanoclaw@gamil.com")
        fill_username("akanoclaw")
        fill_password("***")
        
        # 步驟 4: 等待登錄完成
        wait_for_home_timeline()
        
        # 步驟 5: 保存 Cookies
        save_cookies("data/x_cookies.json")
```

### 2. 智能重試機制 (`monitor.py`)

```python
async def _async_loop():
    # 最多重試 3 次，間隔逐漸延長
    for attempt in range(3):
        try:
            await x_client.ensure_logged_in()  # 自動登錄
            break
        except Exception as e:
            wait_time = 5 * (attempt + 1)  # 5s, 10s, 15s
            await asyncio.sleep(wait_time)
            retry()
```

### 3. 自動 Cookies 更新

```python
# 每次成功登錄後自動保存
cookies = await context.cookies()
with open("data/x_cookies.json", "w") as f:
    json.dump(cookies, f)
```

---

## 📱 使用示例

### 啟動（完全自動）

```bash
# 啟動
$ .venv/bin/python -m openclaw_adapter telegram-poll

# 日誌輸出
[sns-monitor] ✅ X/Twitter monitor started (interval=60s)
[sns-monitor] 📱 Monitoring: @aka_claw, @elonmusk, and more

# 看到這些消息表示一切正常！
```

### 後臺運行

```bash
# 後臺啟動
.venv/bin/python -m openclaw_adapter telegram-poll &

# 查看日誌
tail -f logs/openclaw.log | grep sns

# 停止
pkill -f telegram-poll
```

### 監控日誌

```bash
# 查看 SNS Monitor 相關日誌
grep "sns_monitor\|x_client_web\|XClient" logs/openclaw.log

# 查看登錄進度
grep "🔐\|📧\|👤\|🔑\|✅\|❌" logs/openclaw.log

# 查看推文抓取
grep "tweets\|notification\|Saved" logs/openclaw.log
```

---

## ✅ 正常情況下的日誌

```
2026-05-12 09:03:44 | INFO | sns_monitor.monitor | Login attempt 1/3...
2026-05-12 09:03:44 | INFO | sns_monitor.x_client_web | 🔐 Starting automated X login...
2026-05-12 09:03:45 | INFO | sns_monitor.x_client_web | Browser initialized successfully
2026-05-12 09:03:48 | INFO | sns_monitor.x_client_web | 📧 Step 1: Entering email...
2026-05-12 09:03:50 | INFO | sns_monitor.x_client_web | 👤 Step 2: Entering username...
2026-05-12 09:03:52 | INFO | sns_monitor.x_client_web | 🔑 Step 3: Entering password...
2026-05-12 09:03:56 | INFO | sns_monitor.x_client_web | ⏳ Step 4: Waiting for login completion...
2026-05-12 09:04:00 | INFO | sns_monitor.x_client_web | ✅ Successfully logged into X
2026-05-12 09:04:02 | INFO | sns_monitor.x_client_web | 💾 Saved 15 cookies to data/x_cookies.json
2026-05-12 09:04:02 | INFO | sns_monitor.monitor | ✅ Successfully logged in to X

[sns-monitor] ✅ X/Twitter monitor started (interval=60s)
[sns-monitor] 📱 Monitoring: @aka_claw, @elonmusk, and more
```

---

## ❌ 故障排除

### 登錄超時

如果日誌顯示：
```
⏳ Step 4: Waiting for login completion...
⚠️ Login may not be complete. Current URL: https://x.com/login
```

**可能原因**：
1. X 網頁改變了結構（選擇器失效）
2. 驗證碼或 MFA（但代碼會自動處理）
3. IP 被限制

**解決**：
```bash
# 1. 檢查日誌
tail logs/openclaw.log | grep "Error\|Failed"

# 2. 刪除舊 Cookies 重試
rm -f data/x_cookies.json

# 3. 重新啟動
.venv/bin/python -m openclaw_adapter telegram-poll
```

### 登錄全部失敗

```
❌ All login attempts failed after 3 tries
```

**可能原因**：
1. 帳戶憑證錯誤
2. 帳戶被鎖定
3. X 伺服器問題

**解決**：
```bash
# 1. 驗證 .env 文件
cat .env | grep X_

# 2. 測試帳號在瀏覽器中是否可用
# 3. 檢查帳戶狀態：https://x.com/login

# 4. 檢查詳細錯誤
tail -100 logs/openclaw.log | grep -E "Error|Exception|Failed"
```

### Playwright 初始化失敗

```
Failed to initialize browser
```

**解決**：
```bash
# 重新安裝 Chromium
.venv/bin/playwright install chromium

# 重試
.venv/bin/python -m openclaw_adapter telegram-poll
```

---

## 🔒 安全設置

### Cookies 隱私

```bash
# Cookies 文件包含認證令牌，需要保護
chmod 600 data/x_cookies.json

# 不要上傳到 Git
echo "data/x_cookies.json" >> .gitignore
```

### 帳號安全

❌ **不要在代碼中存儲密碼**

✅ **使用 .env 文件**
```
X_USERNAME=akanoclaw
X_USER_MAIL=akanoclaw@gamil.com
X_USER_PASSWORD=***  # 從 .env 讀取
```

❌ **不要分享 Cookies 文件**

---

## 📈 性能考慮

### 資源使用

- **內存**: ~200-300 MB（Playwright 瀏覽器）
- **CPU**: 低（無頭模式）
- **磁盤**: ~5 MB（Cookies + 日誌）

### 速度

- **首次啟動**: 30-60 秒（包括瀏覽器初始化和登錄）
- **後續啟動**: 5-10 秒（使用已有 Cookies）
- **推文檢索**: 2-3 秒/帳號

### 優化建議

1. **復用瀏覽器會話**：已實現（每次重新使用 Cookies）
2. **並行檢查**：已實現（多個監控規則並行）
3. **緩存**: Cookies 自動保存（避免重複登錄）

---

## 🔄 自動更新和維護

### Cookies 自動刷新

系統會在以下情況自動更新 Cookies：
- 每次成功登錄後
- 長期會話時自動重新驗證

### 自動故障恢復

```python
# 自動重試機制
Max Retries: 3
Retry Delay: 5s, 10s, 15s

# 會話錯誤恢復
catch Exception:
    close_browser()
    clear_invalid_cookies()
    retry_login()
```

---

## ✨ 完全自動化的好處

✅ **零手動操作** - 只需一個命令

✅ **自動故障恢復** - 網絡問題自動重試

✅ **智能 Cookies 管理** - 自動保存和加載

✅ **完整日誌** - 看日誌了解發生了什麼

✅ **生產就緒** - 可以作為系統服務運行

---

## 🎯 下一步

1. **啟動服務**
   ```bash
   .venv/bin/python -m openclaw_adapter telegram-poll &
   ```

2. **驗證運行**
   ```bash
   sleep 30 && grep "Successfully logged\|monitor started" logs/openclaw.log
   ```

3. **測試 Telegram 命令**
   ```
   /snslist        # 查看監控規則
   /snsadd @user   # 添加新監控
   ```

4. **等待推文通知** 🎉

---

**完成！服務已完全自動化，無需任何手動操作。** ✨
