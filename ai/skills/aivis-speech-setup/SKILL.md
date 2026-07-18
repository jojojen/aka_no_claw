---
name: aivis-speech-setup
description: Install and verify AivisSpeech on the Mac mini so quiz vocab audio uses the AivisSpeech engine instead of the macOS Kyoko fallback. Use when bootstrapping a new machine, repairing a broken local TTS runtime, or documenting the exact proven install flow for OpenClaw.
---

# AivisSpeech Setup

## First Principle — General Correctness

1. **Correctness first means general correctness, not correctness for one case.**
   Never make the current example pass with hardcoded keywords, values, output
   text, or exception branches. Prefer a structural solution that removes
   special cases; a sound fix should normally reduce total code and branch
   count. If a proposed fix adds case-specific code, stop and redesign it.

2. **Research uncertainty before coding.** When the correct general solution
   is unclear, consult current primary sources and proven implementations before
   changing code. Use that evidence to define a general contract or design;
   never replace uncertainty with a case-specific hardcode.

Use this skill when the goal is to make `/quiz vocab` audio playback generate `--aivis.wav` files on macOS.

## Proven Path

This repo already supports two local TTS paths:

- preferred: `AivisSpeech` on `http://127.0.0.1:10101`
- fallback: macOS `say` voice `Kyoko`

When AivisSpeech is healthy, `src/openclaw_adapter/quiz_vocab_audio.py` will prefer it automatically. No bot code change is needed after install.

The proven install path on Apple Silicon is:

1. Download the official macOS arm64 **zip** release.
2. Unzip it into a temp directory.
3. Install the app bundle with `ditto` into `~/Applications/AivisSpeech.app`.
4. Remove quarantine attributes.
5. Launch the app once and wait for first-boot model downloads.
6. Verify both endpoints:
   - `curl http://127.0.0.1:10101/version`
   - `curl http://127.0.0.1:10101/speakers`

Do not install Python packages into the repo `.venv` for this path. The AivisSpeech runtime lives outside the repo.

## Script

Use:

```bash
launchers/install-aivis-speech.command
```

Environment overrides:

- `AIVIS_VERSION`
  - default: `1.1.0-preview.4`
- `AIVIS_PORT`
  - default: `10101`
- `AIVIS_READY_TIMEOUT_SECONDS`
  - default: `3600`
- `AIVIS_FORCE_REINSTALL`
  - default: `0`

## Verification

Run these checks after install:

```bash
curl -fsS http://127.0.0.1:10101/version
curl -fsS http://127.0.0.1:10101/speakers | head
```

Then verify OpenClaw is actually using AivisSpeech:

```bash
ls -lt .openclaw_tmp/quiz_vocab_audio | head
```

You want fresh files ending in `--aivis.wav`.

User-visible check:

1. Open any `/quiz vocab <word>` card.
2. Tap `播放例句`.
3. Confirm the delivered filename ends with `--aivis.wav`.
4. Confirm the caption contains `音源：AivisSpeech`.

## Troubleshooting

- If `/version` works but `/speakers` does not, first-boot model downloads are still running.
- If the app logs mention an invalid `default.aivmx`, remove:
  - `~/Library/Application Support/AivisSpeech-Engine/Models/default.aivmx`
- If the app is open but `10101` never comes up, inspect:
  - `~/Library/Logs/AivisSpeech/`
  - `~/Library/Application Support/AivisSpeech-Engine/Logs/`
- If AivisSpeech is unavailable, OpenClaw will still work through the `macOS Kyoko` fallback. That is acceptable as a temporary state, not the desired final state.
