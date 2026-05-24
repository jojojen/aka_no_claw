# scripts/ — 開發工具與一次性腳本

這個資料夾放**不屬於 production stack**的腳本：smoke tests、本機開發輔助、跨平台跑測試的 wrapper、跟手動運維小工具。

Production stack 的啟動 / 停止指令在 [`../launchers/`](../launchers/)；正常 ops 不會碰這裡。

---

## Claude Code 工具

### ⚠️ 目前用 [autoclaude](https://github.com/henryaj/autoclaude)，不要再用本目錄的自製 watcher

```bash
brew install henryaj/tap/autoclaude
# 在現有 tmux session 裡開一個新 window 跑：
tmux new-window -t claude: -n autoclaude -d 'autoclaude'
# TUI 出來後按 a（enable auto-continue for all Claude panes）即可離開
```

autoclaude 偵測 Claude Code 的 banner（`"limit reached ∙ resets Xpm"` / `"You've hit your limit"`），到 reset 時間後自動送 `Escape → continue → Enter`。Go binary，由社群維護，不需要我們追 Claude Code UI 變動。

| 檔案 | 狀態 |
|------|------|
| [`claude-resume-watcher.sh`](claude-resume-watcher.sh) | **⚠️ DEPRECATED（2026-05-24）**。我們自製的 bash watcher 已三度失敗：(1) 誤抓 chat 中的 `Reset at 10:50pm`（false positive），(2) `pipefail` + grep no-match silent death，(3) Claude Code 2026-05 後 banner 改用「8pm」無 HH:MM 格式，regex 永遠抓不到 + 對話中提到 reset time 又會誤觸發 sleep。**不要再 patch 這個檔案**，改用上面 autoclaude。檔案留存供歷史參考。 |

## 跨平台 Docker smoke 測試

| 檔案 | 用途 |
|------|------|
| `mac_mini_docker_smoke.sh` / `rpi5_docker_smoke.sh` | mac mini / rpi5 各自的 Docker stack smoke test |
| `mac_mini_realistic_docker_test.sh` / `rpi5_realistic_docker_test.sh` | realistic e2e 跑法（含真實服務調用） |
| `run-mac-mini-docker-test.{sh,ps1}` / `run-rpi5-docker-test.{sh,ps1}` | bash + PowerShell wrapper（給 Windows / macOS 開發機 invoke） |
| `run-mac-mini-realistic-docker-test.{sh,ps1}` / `run-rpi5-realistic-docker-test.{sh,ps1}` | realistic 版的 wrapper |

## 影像查價 benchmark / smoke

| 檔案 | 用途 |
|------|------|
| `run_image_lookup_smoke.py` | image-lookup 服務本機 smoke |
| `run_live_auction_image_benchmark.py` | 對線上 auction 圖片跑 benchmark（含真實網路 / VLM 呼叫） |
| `verify_image_lookup_live_fixtures.py` | 對 live fixtures 跑回歸驗證 |

## Windows / 雜項

| 檔案 | 用途 |
|------|------|
| `ensure-dashboard-port.ps1` | Windows：確保 dashboard port 已釋放（裝錯機器時的修復） |
| `install-codex-skills.ps1` | Windows：安裝 codex skills |
| `get_x_cookies.py` | 從本機 Chrome / Firefox 撈 X cookies 做 SNS 抓取登入 |

---

## 新增腳本的規則

- **production stack 啟停** → 放 [`../launchers/`](../launchers/)
- **開發 / 測試 / 一次性工具** → 放這裡，加一行到上面表格
- 不要 hardcode 個人 user path（用 `$HOME` / 參數）
- 不要寫死 secrets（用 `.env` / args）
- shell script 加 `set -euo pipefail` 開頭、加 `chmod +x`
