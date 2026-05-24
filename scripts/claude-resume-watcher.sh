#!/usr/bin/env bash
# claude-resume-watcher.sh
#
# Watch a tmux pane running Claude Code; when it shows a usage-limit /
# rate-limit message with a reset time, sleep until that time and
# automatically send "continue" (or a custom message) to the pane.
#
# Usage:
#   ./claude-resume-watcher.sh [-t <tmux_target>] [-i <secs>] [-m <text>]
#                              [-l <log_file>] [-d]
#
#   -t  tmux target (e.g. "claude:1.0"). Default: auto-detect the first
#       attached session's active pane.
#   -i  poll interval in seconds (default 30).
#   -m  message to send when reset time arrives (default "continue").
#   -l  append-log file path (default: stdout only).
#   -d  dry-run — print what we'd send instead of sending.
#   -h  show this help.
#
# Examples:
#   ./claude-resume-watcher.sh                       # auto-detect target
#   ./claude-resume-watcher.sh -t claude:1.0         # explicit target
#   ./claude-resume-watcher.sh -t claude:1.0 -m "go" # send "go" instead
#   ./claude-resume-watcher.sh -l ~/claude-watcher.log &  # background + log
#
# Detection heuristic (tight to avoid false positives):
#   The pane's last ~300 lines are scanned for a single LINE that contains
#   ALL THREE of: a clock time, a reset-announcement verb, and a limit
#   phrase. Lines that look like watcher logs, code comments, or diff /
#   editor output are stripped first so chatting about rate limits in the
#   same pane doesn't trigger us. After sending we sleep 5 min before
#   re-watching so we don't repeatedly trigger on the stale text still
#   visible in the pane.
#
#   Test phrases for this script's regex live in tests/ (not inline) so
#   that loading the script source into the pane doesn't trigger the
#   watcher recursively.

set -euo pipefail

TARGET=""
INTERVAL=30
MESSAGE="continue"
LOG_FILE=""
DRY_RUN=0
COOLDOWN_AFTER_SEND_SEC=300  # 5 min — long enough for the user/Claude to scroll past the limit msg

usage() {
    sed -n '1,32p' "$0" | grep -E '^#' | sed 's/^# \?//'
}

while getopts "t:i:m:l:dh" opt; do
    case "$opt" in
        t) TARGET="$OPTARG" ;;
        i) INTERVAL="$OPTARG" ;;
        m) MESSAGE="$OPTARG" ;;
        l) LOG_FILE="$OPTARG" ;;
        d) DRY_RUN=1 ;;
        h) usage; exit 0 ;;
        *) usage; exit 1 ;;
    esac
done

# ── Target auto-detection ──────────────────────────────────────────────────
if [ -z "$TARGET" ]; then
    # Pick the first attached session; use its currently active window/pane.
    session=$(tmux list-clients -F '#{client_session}' 2>/dev/null | head -1 || true)
    if [ -z "$session" ]; then
        echo "ERROR: no tmux client attached and -t not given" >&2
        echo "Hint: list panes with 'tmux list-panes -a' and pass -t session:window.pane" >&2
        exit 1
    fi
    TARGET=$(tmux display-message -t "$session" -p '#S:#I.#P')
fi

# Sanity-check the target exists
if ! tmux display-message -t "$TARGET" -p '' >/dev/null 2>&1; then
    echo "ERROR: tmux target '$TARGET' does not exist" >&2
    exit 1
fi

log() {
    local line="[claude-resume-watcher $(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$line"
    if [ -n "$LOG_FILE" ]; then
        echo "$line" >> "$LOG_FILE"
    fi
}

# ── Parse a reset time string ("3:00 PM" / "15:00") → epoch seconds ───────
# Rolls forward to tomorrow if the time has already passed today.
# Uses BSD `date -j -f` (macOS); the script aborts loudly elsewhere.
parse_reset_to_epoch() {
    local raw="$1"
    local today
    today=$(date '+%Y-%m-%d')
    local now_epoch
    now_epoch=$(date '+%s')

    # Normalise whitespace and uppercase AM/PM.
    raw=$(echo "$raw" | tr -d ',' | sed -E 's/[[:space:]]+/ /g' | sed -E 's/([aA])\.?[mM]\.?/AM/; s/([pP])\.?[mM]\.?/PM/')

    local target_epoch=""
    # Try 12-hour first, then 24-hour.
    if [[ "$raw" =~ [APap][Mm] ]]; then
        target_epoch=$(date -j -f "%I:%M %p %Y-%m-%d" "$raw $today" '+%s' 2>/dev/null || true)
    fi
    if [ -z "$target_epoch" ]; then
        target_epoch=$(date -j -f "%H:%M %Y-%m-%d" "$raw $today" '+%s' 2>/dev/null || true)
    fi
    if [ -z "$target_epoch" ]; then
        echo ""
        return
    fi
    if [ "$target_epoch" -le "$now_epoch" ]; then
        target_epoch=$((target_epoch + 86400))
    fi
    echo "$target_epoch"
}

# ── Main loop ──────────────────────────────────────────────────────────────
log "Starting watcher target=$TARGET interval=${INTERVAL}s message='$MESSAGE' dry_run=$DRY_RUN log=${LOG_FILE:-stdout}"

last_fired_reset=""
WARMUP_SEC=60  # don't detect for the first 60s — avoids matching old text
              # still in the pane when the watcher is freshly deployed.
started_epoch=$(date '+%s')

while true; do
    # If the target pane no longer exists (user closed Claude / killed
    # the window / detached & killed the session), exit cleanly so the
    # watcher doesn't keep running in the background after Claude is gone.
    if ! tmux display-message -t "$TARGET" -p '' >/dev/null 2>&1; then
        log "Target pane $TARGET no longer exists — exiting watcher."
        exit 0
    fi

    now_epoch=$(date '+%s')
    if [ $((now_epoch - started_epoch)) -lt "$WARMUP_SEC" ]; then
        sleep "$INTERVAL"
        continue
    fi
    # Capture only the visible-region backlog (~50 lines). Wider windows
    # are more prone to picking up stale content (script source, previous
    # test output, etc.) that looks like a banner.
    raw_content=$(tmux capture-pane -t "$TARGET" -p -S -50 2>/dev/null || echo "")

    # Strip lines that ARE NOT Claude UI output:
    #   - this watcher's own log lines (we shouldn't trigger on ourselves)
    #   - shell-/code-style comment lines (`#`, `//`)
    #   - markdown bullets/quotes that often appear in chat about rate limits
    # The remaining text is what Claude itself rendered to the pane.
    content=$(printf '%s\n' "$raw_content" | grep -viE '^\[claude-resume-watcher|^[[:space:]]*#|^[[:space:]]*//|^[[:space:]]*[`>]|^[[:space:]]*\*[[:space:]]|^[[:space:]]*[+-][[:space:]]*#|^[[:space:]]*[0-9]+[[:space:]]+[+-]?[[:space:]]*#|^[[:space:]]*[+-][[:space:]]+"')

    # Tight detection: three cascading greps so the regex stays simple and
    # works under both BSD grep and ugrep. The line must:
    #   (a) contain a clock time HH:MM (with optional AM/PM)
    #   (b) also contain a "reset-announcement" verb near it
    #   (c) ALSO contain a "limit" phrase that names the kind of limit
    # All three must match the SAME line — that's what Claude's actual
    # banner looks like (single-line UI element). Discussion mentioning
    # the words across multiple lines won't trigger.
    detection_line=$(printf '%s\n' "$content" \
        | grep -iE '[0-9]{1,2}:[0-9]{2}' \
        | grep -iE '(reset|resume|available again|wait until|try again|come back|continue sending)' \
        | grep -iE '(5[- ]hour|usage|claude pro|claude max|monthly|daily) limit|limit (reached|reset|will reset)|hit (your |the )?(usage )?limit|reached (your |the )?(usage |5[- ]hour )?limit' \
        | tail -1)

    if [ -n "$detection_line" ]; then
        reset_time=$(printf '%s' "$detection_line" \
            | grep -oiE '[0-9]{1,2}:[0-9]{2}[[:space:]]*[APap][.]?[Mm][.]?|[0-9]{1,2}:[0-9]{2}' \
            | tail -1)

        if [ -n "$reset_time" ]; then
            # Dedup: if we already fired for this exact reset time, ignore.
            if [ "$reset_time" = "$last_fired_reset" ]; then
                sleep "$INTERVAL"
                continue
            fi

            target_epoch=$(parse_reset_to_epoch "$reset_time")
            if [ -n "$target_epoch" ]; then
                now_epoch=$(date '+%s')
                wait_sec=$((target_epoch - now_epoch + 30))  # 30s buffer past reset
                if [ "$wait_sec" -lt 0 ]; then wait_sec=0; fi
                hr=$((wait_sec / 3600))
                mn=$(((wait_sec % 3600) / 60))
                log "Limit detected. Reset at $reset_time (epoch=$target_epoch). Sleeping ${hr}h${mn}m (${wait_sec}s)..."

                # Sleep but allow interruption per minute so a Ctrl-C is responsive.
                slept=0
                while [ "$slept" -lt "$wait_sec" ]; do
                    sleep 60
                    slept=$((slept + 60))
                done

                if [ "$DRY_RUN" -eq 1 ]; then
                    log "[dry-run] would send '$MESSAGE' to $TARGET (skipped)"
                else
                    log "Sending '$MESSAGE' to $TARGET"
                    tmux send-keys -t "$TARGET" "$MESSAGE" Enter
                fi
                last_fired_reset="$reset_time"

                log "Cooling down ${COOLDOWN_AFTER_SEND_SEC}s before resuming watch..."
                sleep "$COOLDOWN_AFTER_SEND_SEC"
            else
                log "Could not parse reset time '$reset_time' to epoch — skipping this round."
            fi
        else
            # Limit-like phrase but no clock found yet — wait & retry.
            : noop
        fi
    fi

    sleep "$INTERVAL"
done
