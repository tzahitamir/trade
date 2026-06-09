#!/usr/bin/env bash
# One-shot midnight check: verifies fetch resumed after UTC midnight reset.
# Sends result to Telegram. Run at 00:15 UTC (03:15 IDT).

set -euo pipefail

REPO=/home/tzahi/repo/trade
DB=$REPO/src/data/trade.db
LOG=$REPO/logs/service.err

set -a
source "$REPO/secrets/credentials.env"
set +a

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" \
        -d text="$1" \
        -d parse_mode="HTML" > /dev/null
}

# 1. Service status via pgrep (works in cron — no dbus needed)
if pgrep -f "python.*-m main" > /dev/null 2>&1; then
    SVC_STATUS="active"
else
    SVC_STATUS="not running"
fi

# 2. Recent fetch log lines (last 5 lines mentioning fetch activity)
FETCH_LINES=$(grep -E "Fetch:|daily limit|API calls" "$LOG" 2>/dev/null | tail -5 || echo "(none)")

# 3. API calls for today UTC date
TODAY=$(date -u +%Y-%m-%d)
API_CALLS=$(cd "$REPO/src" && ../.venv/bin/python -c "
from db.local_db import LocalDB
db = LocalDB('data/trade.db')
print(db.get_api_calls_today())
" 2>/dev/null || echo "error")

# 4. Errors in last 30 minutes — compare log timestamps (local time) vs local now-30m
CUTOFF=$(date -d '30 minutes ago' '+%Y-%m-%d %H:%M:%S')
ERRORS=$(grep "\[ERROR\]" "$LOG" 2>/dev/null | awk -v c="$CUTOFF" '
    {
        ts = substr($1,1,10) " " substr($2,1,8)
        if (ts >= c) count++
    }
    END { print count+0 }
')

# Build message
if [ "$SVC_STATUS" = "active" ] && [ "$API_CALLS" -gt 0 ] 2>/dev/null; then
    ICON="✅"
    STATUS="Fetching normally"
elif [ "$SVC_STATUS" = "active" ] && [ "$API_CALLS" = "0" ] 2>/dev/null; then
    ICON="⚠️"
    STATUS="Service running but 0 calls — budget may not have reset"
else
    ICON="🔴"
    STATUS="Problem detected"
fi

MSG="${ICON} <b>[trade] Midnight check — ${TODAY} UTC</b>
Service: <code>${SVC_STATUS}</code>
API calls today: <code>${API_CALLS}/800</code>
Errors (last 30m): <code>${ERRORS}</code>

Recent fetch log:
<pre>${FETCH_LINES}</pre>"

send_telegram "$MSG"
echo "Sent. status=$SVC_STATUS calls=$API_CALLS errors=$ERRORS"
