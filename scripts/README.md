# scripts/ — 開發工具與一次性腳本

這個資料夾放**不屬於 production stack**的腳本：smoke tests、本機開發輔助、跨平台跑測試的 wrapper、跟手動運維小工具。

Production stack 的啟動 / 停止指令在 [`../launchers/`](../launchers/)；正常 ops 不會碰這裡。

---

## Claude Code 工具

| 檔案 | 用途 |
|------|------|
| [`claude-resume-watcher.sh`](claude-resume-watcher.sh) | tmux 內監看 Claude Code，偵測到 usage limit + reset 時間時 sleep 到時間自動送 `continue` 給 pane。watcher 自己會跟著 target pane 一起結束 — 不需手動 kill。 |

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
