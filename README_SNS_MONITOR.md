# 🚀 SNS Monitor - 完全自動化快速開始

**無需任何額外操作！一切都已自動集成到 aka_no_claw 服務中。**

---

## ⚡ 一鍵啟動

```bash
cd /path/to/aka_no_claw
.venv/bin/python -m openclaw_adapter telegram-poll
```

**完成！** 服務自動執行：
- ✅ 啟動 Playwright 瀏覽器
- ✅ 自動登錄 X (akanoclaw 帳戶)
- ✅ 保存 Cookies
- ✅ 啟動 SNS Monitor
- ✅ 監控 @aka_claw, @elonmusk, 機動戰士

---

## 📱 在 Telegram 中使用

### 查看監控規則
```
/snslist
```

### 添加新監控
```
/snsadd @username           # 追蹤帳號
/snsadd keyword:keyword     # 追蹤關鍵字
/snsadd trend:trending      # 追蹤熱門話題
```

### 刪除監控
```
/snsdelete rule_id
```

### 查看 4chan IP 熱門討論
```
/snsbuzz pjsk
```

`/snsbuzz` 不是 X 熱門討論，也不是 Reddit；它走 4chan public JSON API，
再交給 LLM 萃取收藏訊號。若只有 general thread 閒聊，回覆應明確說沒有
具體收藏催化或入手標的，且不寫入 RAG。

IP/角色/活動/商品正規化資料在：

```
data/sns_ip_catalog.json
```

可用 `OPENCLAW_SNS_IP_CATALOG_PATH=/path/to/catalog.json` 覆蓋。catalog 是多 IP
設計，新增 IP 時加一個 `ips[]` entry，不要把 IP 專名硬寫進 prompt 或程式碼。

最小格式：

```json
{
  "version": 1,
  "ips": [
    {
      "ip_id": "project_sekai",
      "canonical": "Project SEKAI",
      "aliases": ["pjsk", "プロセカ"],
      "entities": [
        {"category": "group", "name": "25時、ナイトコードで。", "aliases": ["Niigo"]},
        {"category": "character", "name": "巡音ルカ", "aliases": ["Luka"]},
        {"category": "gacha", "name": "限定ガチャ", "aliases": ["Limited Gacha"]}
      ]
    }
  ]
}
```

允許的 `category`：`group`, `character`, `event`, `gacha`, `card`,
`card_box`, `product`, `other`。

---

## 📊 預期輸出

### 啟動日誌
```
[sns-monitor] ✅ X/Twitter monitor started (interval=60s)
[sns-monitor] 📱 Monitoring: @aka_claw, @elonmusk, and more
```

### Telegram 通知示例
```
🐦 X/Twitter 更新 (@aka_claw)

新推文 (1):

@aka_claw
我最新的推文...

---
🔗 查看: https://x.com/aka_claw/status/...
```

---

## 🔄 後臺運行

### 啟動後臺
```bash
.venv/bin/python -m openclaw_adapter telegram-poll &
```

### 查看日誌
```bash
tail -f logs/openclaw.log | grep sns-monitor
```

### 停止服務
```bash
pkill -f telegram-poll
```

---

## 📁 文件位置

| 文件 | 說明 |
|------|------|
| `data/x_cookies.json` | 自動保存的 Cookies |
| `data/sns.sqlite3` | 監控規則數據庫 |
| `logs/openclaw.log` | 完整日誌 |

---

## ✅ 故障檢查

### 確認 SNS Monitor 運行中
```bash
grep "sns-monitor.*started" logs/openclaw.log
```

### 檢查登錄狀態
```bash
grep "Successfully logged\|Saved.*cookies" logs/openclaw.log
```

### 查看推文抓取
```bash
grep "tweet\|new tweet" logs/openclaw.log
```

---

## 🎯 下一步

1. **啟動服務**（已完成自動化）
2. **在 Telegram 中添加監控**：`/snsadd @handle`
3. **接收推文通知** 🎉

---

## 📚 詳細文檔

- **[自動登錄指南](docs/AUTO_LOGIN_SETUP.md)** - 完全自動化流程
- **[Cookies 設置](docs/X_COOKIES_SETUP.md)** - 應急手動操作（通常不需要）
- **[故障排除](docs/SNS_MONITOR_TROUBLESHOOTING.md)** - 問題診斷
- **[使用指南](docs/SNS_MONITOR_USAGE.md)** - 完整功能說明
- **[實現狀態](docs/WEB_SCRAPING_IMPLEMENTATION_STATUS.md)** - 技術詳情

---

## 🎊 已完成的功能

✅ **完全自動化登錄** - 無需手動干預
✅ **Cookies 自動管理** - 自動保存和更新
✅ **智能重試機制** - 自動故障恢復
✅ **3 種監控類型** - 帳號 / 關鍵字 / 趨勢
✅ **Telegram 集成** - 推文自動通知
✅ **6 個 CLI 工具** - 細粒度規則管理
✅ **完整測試** - 36/36 測試通過

---

## 💡 常見問題

### Q: 為什麼需要自動登錄？
A: X 現在有複雜的反爬蟲機制。Playwright 自動化瀏覽器可以繞過這些限制。

### Q: Cookies 會過期嗎？
A: 是的，通常 1-2 年後。系統會自動重新登錄並更新。

### Q: 可以監控多個帳戶嗎？
A: 可以！在 Telegram 中使用 `/snsadd @username` 添加多個。

### Q: 為什麼不使用官方 API？
A: 用戶要求不使用，本方案使用網頁爬蟲替代。

---

**享受自動化推文監控！🎉**
