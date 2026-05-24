#!/bin/bash
# watch-claude-ratelimit.sh
# Monitors a Claude Code tmux pane and sends "continue" when a rate limit resets.
# Handles both formats:
#   "limit reached ∙ resets 2pm"
#   "You've hit your limit · resets 1:30am (Asia/Tokyo)"
#
# Usage: ./watch-claude-ratelimit.sh [tmux-pane-target]
# Default target: claude:0.0
#
# Run in a separate pane (e.g., replace autoclaude):
#   tmux send-keys -t claude:1.0 "watch-claude-ratelimit.sh" Enter

TARGET_PANE="${1:-claude:0.0}"
POLL_SECS=30
BUFFER_SECS=10   # extra seconds after reset before sending "continue"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

parse_reset_epoch() {
    local reset_str="$1"
    python3 - "$reset_str" <<'EOF'
import sys, re, datetime

try:
    import zoneinfo
    TZ = zoneinfo.ZoneInfo("Asia/Tokyo")
except Exception:
    import pytz
    TZ = pytz.timezone("Asia/Tokyo")

s = sys.argv[1].strip().lower()
m = re.match(r'(\d{1,2})(?::(\d{2}))?\s*([ap]m)', s)
if not m:
    sys.exit(1)

h  = int(m.group(1))
mi = int(m.group(2)) if m.group(2) else 0
ap = m.group(3)

if ap == 'pm' and h != 12:
    h += 12
elif ap == 'am' and h == 12:
    h = 0

now    = datetime.datetime.now(tz=TZ)
target = now.replace(hour=h, minute=mi, second=0, microsecond=0)
if target <= now:
    target += datetime.timedelta(days=1)

print(int(target.timestamp()))
EOF
}

log "Watching $TARGET_PANE for rate-limit messages (poll every ${POLL_SECS}s)..."

while true; do
    content=$(tmux capture-pane -t "$TARGET_PANE" -p 2>/dev/null)

    if echo "$content" | grep -qiE "hit your limit|limit reached|rate.?limited|out of extra usage"; then
        log "Rate-limit message detected."

        reset_str=$(echo "$content" | grep -oiE "resets? [0-9]{1,2}(:[0-9]{2})?\s*[ap]m" | head -1 | sed -E 's/[Rr]esets? //')

        if [ -n "$reset_str" ]; then
            reset_epoch=$(parse_reset_epoch "$reset_str")

            if [ -n "$reset_epoch" ]; then
                now_epoch=$(date +%s)
                wait_secs=$(( reset_epoch - now_epoch + BUFFER_SECS ))

                if [ "$wait_secs" -gt 0 ]; then
                    log "Reset at $reset_str — sleeping ${wait_secs}s..."
                    sleep "$wait_secs"
                fi
            else
                log "Could not parse reset time '$reset_str' — waiting 5 minutes as fallback."
                sleep 300
            fi
        else
            log "No reset time found in message — waiting 5 minutes as fallback."
            sleep 300
        fi

        log "Sending 'continue' to $TARGET_PANE."
        tmux send-keys -t "$TARGET_PANE" "continue" Enter
        sleep 90  # debounce: don't re-trigger immediately
    fi

    sleep "$POLL_SECS"
done
