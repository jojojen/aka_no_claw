# 音樂播放故障排除指南（Mac mini afplay / CoreAudio）

Last reviewed: 2026-06-23
Status: Current
Owner area: music

涵蓋 `/music`（FLAC 播放、隨機、playbest）與 `/saynow`（AivisSpeech 在 Mac mini
本機播放）共用的音訊輸出問題。

---

## 問題：afplay 啟動就失敗，音樂／語音放不出來

### 症狀
- `/music`、`/saynow` 回報「播放失敗」。
- 日誌或 `/musicdiag` 看到：
  ```
  AudioQueueStart failed ('what')
  ```
  （即 CoreAudio 錯誤碼 `-66681` / `kAudioQueueErr_CannotStart`）
- `say` 指令仍能發聲，但 `afplay`（甚至 AVFoundation 播放）一律失敗。

### 根本原因
這是 macOS **CoreAudio 輸出 wedge**——系統層級的已知 bug，與 bot 程式無關。
`afplay` 走的 AudioQueue API 在這個狀態下無法啟動輸出佇列，即使輸出裝置本身正常。
重點：**重新選取輸出裝置沒有用**（已實測），因為這台 Mac mini 只有單一輸出裝置
「Mac mini的揚聲器」，重選等於對 HAL 做 no-op。**唯一可靠的清除方式是重啟
`coreaudiod` daemon**（launchd 約 1 秒內會自動重新拉起）。

### 程式如何自癒
`src/openclaw_adapter/audio_recovery.py` 的 `restart_coreaudiod()` 會用
**免密碼 sudo**（`sudo -n killall coreaudiod`）重啟 daemon。`/music`
（`music_command._spawn_with_retry`）與 `/saynow`（`voice_command.play_audio_file`）
在 afplay 啟動失敗時，會**重啟 coreaudiod 一次並重試**；若沒有授權則快速失敗、
照實回報錯誤，不會假裝成功（無 silent fallback）。

---

## 一次性手動設定（必要，否則自癒無法運作）

`coreaudiod` 屬於 root，需要在 Mac mini 上替使用者 `jen` 加一條**最小範圍**的
免密碼 sudo 授權。在真正的 Terminal 執行下列其中一種：

**做法 A（互動式）**
```
sudo visudo -f /etc/sudoers.d/openclaw-coreaudiod
```
在編輯器內輸入這一行後存檔離開（visudo 存檔會自動檢查語法）：
```
jen ALL=(root) NOPASSWD: /usr/bin/killall coreaudiod
```

**做法 B（一行搞定，不進編輯器）**
```
echo 'jen ALL=(root) NOPASSWD: /usr/bin/killall coreaudiod' | sudo tee /etc/sudoers.d/openclaw-coreaudiod
sudo chmod 0440 /etc/sudoers.d/openclaw-coreaudiod
sudo visudo -c
```
最後一行印出 `parsed OK` 代表語法正確。

### 驗證授權（應免密碼、立即返回）
```
sudo -n /usr/bin/killall coreaudiod; echo "exit=$?"     # exit=0 即成功（會順手重啟一次 coreaudiod）
sudo -n -l /usr/bin/killall coreaudiod                  # 應印出 /usr/bin/killall coreaudiod
```

### 安全注意
- 這條授權**只開放這一個指令、指定絕對路徑與單一 daemon**，沒有萬用字元，
  範圍最小化。
- `sudo -l` 清單裡的 `(ALL) ALL` 是 macOS 對 admin 使用者的**預設**權限
  （仍需密碼），不是這個檔案加的，不用擔心。
- 檔案權限應為 `-r--r-----  root:wheel`（0440）。

設定完成後按「重啟龍蝦」（`/restartall`）讓新程式上線，自癒才會生效。

---

## 相關指令
- `/musicdiag` — 列出 afplay / SwitchAudioSource 是否可用、目前輸出裝置、播放健康狀態。
- `/music now` — 回報目前是否真的有曲目在播（會偵測 stale / pid reuse）。
- `/music` 音源選單 — 切換 macOS 輸出裝置（見 `music_audio_device.py`，issue #35）。

## 仍然失敗時
- 確認免密碼授權真的生效（上面的驗證指令）。
- coreaudiod 重啟後若數分鐘內又 wedge，通常代表更底層的音訊驅動異常——
  最後手段是重開機。
- 燈控／Broadlink 是另一個獨立問題，見
  [BROADLINK_LOCAL_NETWORK_TROUBLESHOOTING.html](BROADLINK_LOCAL_NETWORK_TROUBLESHOOTING.html)。
