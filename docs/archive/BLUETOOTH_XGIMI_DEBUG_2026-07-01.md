# Bluetooth / XGIMI Z8X Debug Record

Last reviewed: 2026-07-03
Status: Historical
Owner area: operations

Date: 2026-07-01 12:03 JST

Context: aka_no_claw Web UI `生活 -> Bluetooth` can scan devices, but connecting
to `XGIMI Z8X` returned:

```text
連線「XGIMI Z8X」逾時，請確認裝置已開機且在範圍內。
```

## User-Visible Symptom

- Before reboot, the user reports Bluetooth control appeared to work.
- After reboot, Web UI initially reported:

```text
藍牙連線需要 blueutil，但目前尚未安裝。
請在 Mac mini 執行：brew install blueutil
```

- After installing/confirming `blueutil`, Web UI timed out when connecting
  `XGIMI Z8X`.
- Root cause found afterward: macOS required an on-screen privacy permission
  approval for Terminal / the app launching OpenClaw to use Bluetooth.

## Current Tool State

Commands run from:

```text
<workspace>/aka_no_claw
```

Results:

```bash
command -v blueutil
# /opt/homebrew/bin/blueutil

blueutil --version
# 2.13.0

brew list --versions blueutil
# blueutil 2.13.0

command -v SwitchAudioSource
# /opt/homebrew/bin/SwitchAudioSource

command -v afplay
# /usr/bin/afplay

command -v system_profiler
# /usr/sbin/system_profiler
```

Earlier, before `blueutil` was installed, checks showed:

```text
brew info blueutil -> Not installed
/opt/homebrew/bin/blueutil -> missing
/usr/local/bin/blueutil -> missing
/opt/homebrew/Cellar/blueutil -> missing
workspace/.venv search -> no blueutil
```

Conclusion: `blueutil` is not a Python/venv package. It is a Homebrew native
CLI. The "installed in venv" theory was checked and did not match the machine
state.

## Device Scan Result

`system_profiler SPBluetoothDataType -json` successfully sees `XGIMI Z8X`, but
lists it under `device_not_connected`:

```json
{
  "XGIMI Z8X": {
    "device_address": "<XGIMI_MAC>",
    "device_firmwareVersion": "20.3.6",
    "device_minorType": "Video Display",
    "device_productID": "0x1200",
    "device_vendorID": "0x000F"
  }
}
```

Bluetooth controller state is on:

```json
"controller_state": "attrib_on"
```

## blueutil Behavior

With `blueutil` 2.13.0 installed, these commands were tested through a Python
wrapper with an 8-second timeout:

```bash
blueutil --paired
blueutil --connected
blueutil --info <XGIMI_MAC>
```

All timed out.

Also tested:

```bash
BLUEUTIL_USE_SYSTEM_PROFILER=1 blueutil --paired
BLUEUTIL_USE_SYSTEM_PROFILER=1 blueutil --connected
BLUEUTIL_USE_SYSTEM_PROFILER=1 blueutil --info <XGIMI_MAC>
BLUEUTIL_USE_SYSTEM_PROFILER=1 blueutil --is-connected <XGIMI_MAC>
```

These also timed out in the observed run.

One command detail: `blueutil --is-on` is not valid for blueutil 2.13.0. The
correct power query is:

```bash
blueutil --power
```

## Archive Note

This incident is resolved and preserved only as a sanitized historical debug
record for similar macOS Bluetooth permission failures.

## Audio Output Check

The alternate "maybe previous behavior used audio output switching" path was
checked:

```bash
SwitchAudioSource -a -t output
# Mac mini的揚聲器

SwitchAudioSource -c -t output
# Mac mini的揚聲器
```

`XGIMI Z8X` is not currently visible as an audio output device. This suggests it
is not connected at the macOS audio layer right now.

## Current Code Path

Web UI Bluetooth route calls aka_no_claw backend endpoint:

```text
POST /api/command/bluetooth
```

The backend eventually calls:

```python
blueutil --connect <XGIMI_MAC>
```

Source file:

```text
src/openclaw_adapter/bluetooth_command.py
```

Current implementation already scans devices via:

```text
system_profiler SPBluetoothDataType -json
```

and connects via:

```text
blueutil --connect <MAC>
```

## Code Changes Made Locally

These changes are currently local and not yet committed/pushed:

1. Cold-start launcher now installs `blueutil`:

```bash
brew list blueutil >/dev/null 2>&1 || brew install blueutil
```

2. `/restartall` now refreshes Homebrew PATH using:

```bash
eval "$(/opt/homebrew/bin/brew shellenv)"
```

or `/usr/local/bin/brew shellenv`.

3. Command bridge restarted by `/restartall` now sources:

```text
run/mac-mini-stack.env
```

4. `bluetooth_command.py` now falls back to Homebrew fixed paths when PATH is
narrow:

```text
/opt/homebrew/bin/blueutil
/usr/local/bin/blueutil
```

5. Cold-start runtime env now persists important resolved values:

```text
PATH
REPUTATION_AGENT_SERVER_URL
REPUTATION_AGENT_ADMIN_TOKEN
OPENCLAW_TESSERACT_PATH
OPENCLAW_TESSDATA_DIR
OPENCLAW_LOCAL_TEXT_BACKEND / ENDPOINT / MODEL
OPENCLAW_LOCAL_VISION_BACKEND / ENDPOINT / MODEL
PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
```

Verification run:

```bash
PYTHONPATH=src:../price_monitor_bot/src:../telegram_nl/src \
uvx --python /opt/homebrew/bin/python3.12 \
  --with truststore --with beautifulsoup4 --with requests --with broadlink \
  pytest tests/test_bluetooth_command.py tests/test_service_restart.py -q

# 36 passed
```

## Current Best Hypothesis

This is no longer a simple missing-dependency problem.

Updated conclusion: the timeout was caused by macOS Bluetooth privacy permission
not yet being approved for the terminal/app context running `blueutil`. The
permission prompt had to be accepted on screen.

Other possible contributing factors considered during debugging:

1. `blueutil` / CoreBluetooth API is hanging on this Mac after reboot.
2. XGIMI is visible in Bluetooth cache / known devices but not currently
   accepting a connection.
3. Previous "working" behavior may not have used `blueutil --connect`; it may
   have been:
   - macOS already had XGIMI connected, so OpenClaw only observed it as connected
   - user/system selected XGIMI through the macOS audio output path
   - auto-reconnect happened outside OpenClaw
4. The XGIMI device may need to be awake / in pairing or reconnectable mode.

Evidence for this: `system_profiler` can read Bluetooth state, but `blueutil`
commands that query paired/connected/info all timeout.

The final clue was that an interactive macOS permission prompt was waiting for
approval. After granting Bluetooth permission to the terminal/app context,
`blueutil` should be retried.

## Suggested Next Checks

First, approve the macOS Bluetooth permission prompt for Terminal / the app that
launches OpenClaw. Then retry:

```bash
blueutil --power
blueutil --paired
blueutil --connected
blueutil --info <XGIMI_MAC>
blueutil --connect <XGIMI_MAC>
```

If permission is already granted but the commands still hang, try toggling
Bluetooth off/on:

```bash
blueutil --power 0
sleep 3
blueutil --power 1
```

Then retry:

```bash
blueutil --paired
blueutil --connected
blueutil --info <XGIMI_MAC>
blueutil --connect <XGIMI_MAC>
```

If `blueutil --paired` still hangs, restart the macOS Bluetooth daemon or reboot
again. Possible command to discuss before running:

```bash
sudo pkill bluetoothd
```

Also test whether macOS UI can connect XGIMI from System Settings. If macOS UI
connects it, then `SwitchAudioSource -a -t output` should start showing
`XGIMI Z8X`, and OpenClaw could prefer an audio-output switching path for this
device.

## Question For Another Reviewer

Given that:

- `system_profiler SPBluetoothDataType -json` sees `XGIMI Z8X`
- `blueutil --paired`, `--connected`, and `--info <MAC>` all timeout
- `SwitchAudioSource` does not list XGIMI as an output device
- OpenClaw connects using `blueutil --connect <MAC>`

What is the most reliable macOS automation path to reconnect an already paired
Bluetooth audio/video sink like XGIMI after reboot?

Options to evaluate:

1. Continue with `blueutil --connect`, but add bluetoothd reset guidance.
2. Use macOS UI / Shortcuts / AppleScript automation instead of blueutil.
3. Use audio output selection after the system auto-connects the device.
4. Remove/re-pair XGIMI and then use blueutil.
