#!/usr/bin/env bash
# Turnkey standalone statusLine script for Antigravity CLI.
# Renders model, context window percentage, git branch, and live session cost.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${BASH_SOURCE[0]}")")" && pwd)"
COST_SCRIPT="${SCRIPT_DIR}/session_cost.py"

INPUT_JSON="$(cat)"

IFS=$'\t' read -r MODEL_NAME USED_PCT VCS_BRANCH SUBAGENTS <<< "$(
  printf '%s' "${INPUT_JSON}" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
model = (d.get("model") or {}).get("display_name") or (d.get("model") or {}).get("id") or "unknown"
ctx = d.get("context_window") or {}
pct = ctx.get("used_percentage")
pct_str = f"{float(pct):.0f}%" if pct is not None else "0%"
branch = (d.get("vcs") or {}).get("branch") or ""
subs = len(d.get("subagents") or [])
print(f"{model}\t{pct_str}\t{branch}\t{subs}")
' 2>/dev/null || printf 'unknown\t0%%\t\t0\n'
)"

COST_BADGE="$(printf '%s' "${INPUT_JSON}" | python3 "${COST_SCRIPT}" --format badge 2>/dev/null || true)"

SEGMENTS=()
SEGMENTS+=("\033[96m${MODEL_NAME}\033[0m")
SEGMENTS+=("\033[97mctx ${USED_PCT}\033[0m")
if [[ -n "${VCS_BRANCH}" ]]; then
  SEGMENTS+=("\033[95m${VCS_BRANCH}\033[0m")
fi
if [[ "${SUBAGENTS}" != "0" ]]; then
  SEGMENTS+=("\033[93m${SUBAGENTS} sub\033[0m")
fi
if [[ -n "${COST_BADGE}" ]]; then
  SEGMENTS+=("${COST_BADGE}")
fi

OUT=""
for seg in "${SEGMENTS[@]}"; do
  if [[ -n "${OUT}" ]]; then
    OUT="${OUT} \033[90m│\033[0m ${seg}"
  else
    OUT="${seg}"
  fi
done
printf '%b\n' "${OUT}"
