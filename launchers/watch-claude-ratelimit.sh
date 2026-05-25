#!/bin/bash
# watch-claude-ratelimit.sh
# Thin wrapper around watch-claude-ratelimit.py.
#
# Detection moved from `tmux capture-pane + grep` to tailing claude-code's
# JSONL session log under ~/.claude/projects/<workspace>/*.jsonl, which carries
# structured rate-limit events ("error":"rate_limit", apiErrorStatus 429).
# Injection of "continue" still uses `tmux send-keys` to a configurable pane.
#
# Usage:
#   watch-claude-ratelimit.sh           # uses CLAUDE_WATCH_PANE or "claude:0.0"
# Env overrides (see watch-claude-ratelimit.py docstring):
#   CLAUDE_WATCH_PANE
#   CLAUDE_WATCH_PROJECT_DIR
#   CLAUDE_WATCH_BUFFER
#   CLAUDE_WATCH_DEBOUNCE
#   CLAUDE_WATCH_DEFAULT_TZ
#   CLAUDE_WATCH_LOG

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/env python3 "$SCRIPT_DIR/watch-claude-ratelimit.py" "$@"
