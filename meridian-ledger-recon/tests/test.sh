#!/usr/bin/env bash
# Verifier entrypoint for meridian-ledger-recon.
# Writes /logs/verifier/reward.json and /logs/verifier/reward.txt.
set -u

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-/app}"
LOGS_DIR="${VERIFIER_LOGS_DIR:-/logs/verifier}"
mkdir -p "$LOGS_DIR"

PY="$(command -v python3 || command -v python)"

"$PY" "$TEST_DIR/grade.py" --app-dir "$APP_DIR" --logs-dir "$LOGS_DIR"
status=$?

if [ ! -f "$LOGS_DIR/reward.json" ]; then
    printf '{"overall": 0.0, "functional_correctness": 0.0, "constraint_satisfaction": 0.0, "robustness": 0.0, "artifact_quality": 0.0}\n' > "$LOGS_DIR/reward.json"
    printf '0.0\n' > "$LOGS_DIR/reward.txt"
fi

exit $status
