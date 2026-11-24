#!/usr/bin/env bash
# Composable wrapper for Antigravity CLI statusLine.
# Tees stdin JSON to an existing statusLine command (if configured) and appends
# the local session cost badge without modifying the original script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${BASH_SOURCE[0]}")")" && pwd)"
COST_SCRIPT="${SCRIPT_DIR}/session_cost.py"
CONFIG_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/antigravity-session-cost/config.json"

INPUT_JSON="$(cat)"

WRAPPED_CMD="${AGY_COST_WRAPPED_STATUSLINE:-}"
if [[ -z "${WRAPPED_CMD}" && -f "${CONFIG_FILE}" ]]; then
  WRAPPED_CMD="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("wrapped_statusline_command",""))' "${CONFIG_FILE}" 2>/dev/null || true)"
fi

COST_BADGE="$(printf '%s' "${INPUT_JSON}" | python3 "${COST_SCRIPT}" --format badge 2>/dev/null || true)"

if [[ -n "${WRAPPED_CMD}" ]]; then
  BASE_OUT="$(printf '%s' "${INPUT_JSON}" | bash -c "${WRAPPED_CMD}" 2>/dev/null || true)"
  if [[ -n "${BASE_OUT}" && -n "${COST_BADGE}" ]]; then
    printf '%s \033[90m│\033[0m %s\n' "${BASE_OUT}" "${COST_BADGE}"
  elif [[ -n "${BASE_OUT}" ]]; then
    printf '%s\n' "${BASE_OUT}"
  elif [[ -n "${COST_BADGE}" ]]; then
    printf '%s\n' "${COST_BADGE}"
  fi
else
  if [[ -n "${COST_BADGE}" ]]; then
    printf '%s\n' "${COST_BADGE}"
  fi
fi
