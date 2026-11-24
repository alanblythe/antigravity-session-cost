#!/usr/bin/env bash
# Optional Antigravity CLI lifecycle hook (SessionStart / Stop).
# - On SessionStart: refreshes remote GCS/HTTPS rate card (if TTL expired, via ETag).
# - On Stop: pre-computes incremental session cost cache so statusLine renders in <5ms.
# Never blocks or fails the agent turn.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${BASH_SOURCE[0]}")")" && pwd)"
COST_SCRIPT="${SCRIPT_DIR}/session_cost.py"
MODE="${1:-stop}"

INPUT_JSON="$(cat || true)"

if [[ "${MODE}" == "session-start" ]]; then
  python3 "${COST_SCRIPT}" --refresh-rates >/dev/null 2>&1 || true
  exit 0
fi

TRANSCRIPT_PATH="$(printf '%s' "${INPUT_JSON}" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("transcriptPath") or d.get("transcript_path") or "")
except Exception:
    print("")
' 2>/dev/null || true)"

MODEL_ID="$(printf '%s' "${INPUT_JSON}" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("modelName") or (d.get("model") or {}).get("id") or "")
except Exception:
    print("")
' 2>/dev/null || true)"

if [[ -n "${TRANSCRIPT_PATH}" && -f "${TRANSCRIPT_PATH}" ]]; then
  python3 "${COST_SCRIPT}" --transcript "${TRANSCRIPT_PATH}" --model "${MODEL_ID}" --format json >/dev/null 2>&1 || true
fi

exit 0
