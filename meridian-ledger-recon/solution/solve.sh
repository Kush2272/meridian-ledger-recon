#!/usr/bin/env bash
# Oracle solution for meridian-ledger-recon.
#
# 1. Replace the spec-violating rebuilder with a conforming implementation
#    (fixes: R1 ordering, R2 dedup, R3/R5 integer fee math + minimum fee,
#     R4 fee-inclusive sufficiency check, R6 adjustment rejection).
# 2. Regenerate /app/state/ledger.db from the full event log.
# 3. Produce /app/report.json by reconciling the corrected snapshot against
#    the read-only stale baseline.
set -euo pipefail

SOL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-/app}"
PY="$(command -v python3 || command -v python)"

cp "$SOL_DIR/ledger_fixed.py" "$APP_DIR/app/ledger.py"

"$PY" "$APP_DIR/app/ledger.py" rebuild \
    --events "$APP_DIR/data/events.ndjson" \
    --db "$APP_DIR/state/ledger.db"

"$PY" "$SOL_DIR/make_report.py" --app-dir "$APP_DIR"

echo "oracle: snapshot regenerated and report.json written"
