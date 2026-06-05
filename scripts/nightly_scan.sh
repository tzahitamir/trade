#!/bin/bash
# Nightly BOS scan + param sweep — runs full 365-day rescan then sweeps all param sets.
# Cron usage (2 AM UTC daily):
#   0 2 * * * /home/tzahi/repo/trade/scripts/nightly_scan.sh >> /home/tzahi/repo/trade/logs/cron.log 2>&1

set -euo pipefail
REPO=/home/tzahi/repo/trade

cd "$REPO"
set -a && source secrets/credentials.env && set +a

echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] Starting nightly BOS scan..."
.venv/bin/python -m src.main --experiment-bos
echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] Done."
