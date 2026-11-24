#!/usr/bin/env bash
# Installer for antigravity-session-cost (interactive or --non-interactive fleet mode).
# - Symlinks skills/antigravity-session-cost into <gemini_dir>/config/skills/
# - Writes ~/.config/antigravity-session-cost/config.json (optional GCS/HTTPS rate card + discount)
# - Optionally wires Antigravity CLI statusLine (wrapper, standalone, or skill-only)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${BASH_SOURCE[0]}")")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SKILL_SCRIPTS_DIR="${REPO_ROOT}/skills/antigravity-session-cost/scripts"

GEMINI_DIR="${HOME}/.gemini"
GEMINI_DIR_SET=false
RATE_CARD_URL=""
RATE_CARD_PATH=""
DISCOUNT_PCT=""
AUTH_MODE="auto"
MODE="skill-only"   # skill-only | wrapper | standalone
NON_INTERACTIVE=false

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Options:
  --mode <skill-only|wrapper|standalone>  StatusLine wiring mode (default: skill-only)
  --rate-card-url <gs://...|https://...>  Remote rate card endpoint (ETag-cached)
  --rate-card-path <path>                 Local rate_card.json path
  --discount-pct <number>                 Global discount percentage off list price (0-100)
  --auth-mode <auto|adc|bearer|none>      Remote endpoint auth mode (default: auto)
  --gemini-dir <path>                     Antigravity config root (default: ~/.gemini)
  --non-interactive                       Do not prompt; ideal for MDM / devcontainer images
  -h, --help                              Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --rate-card-url) RATE_CARD_URL="$2"; shift 2 ;;
    --rate-card-path) RATE_CARD_PATH="$2"; shift 2 ;;
    --discount-pct) DISCOUNT_PCT="$2"; shift 2 ;;
    --auth-mode) AUTH_MODE="$2"; shift 2 ;;
    --gemini-dir) GEMINI_DIR="$2"; GEMINI_DIR_SET=true; shift 2 ;;
    --non-interactive) NON_INTERACTIVE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

GEMINI_DIR="${GEMINI_DIR/#\~/$HOME}"

chmod +x "${SKILL_SCRIPTS_DIR}/session_cost.py" \
  "${SKILL_SCRIPTS_DIR}/statusline_wrapper.sh" \
  "${SKILL_SCRIPTS_DIR}/statusline_standalone.sh" \
  "${SKILL_SCRIPTS_DIR}/stop_hook.sh"

# 1. Symlink skill into <gemini_dir>/config/skills/antigravity-session-cost
SKILLS_DIR="${GEMINI_DIR}/config/skills"
mkdir -p "${SKILLS_DIR}"
ln -sfn "${REPO_ROOT}/skills/antigravity-session-cost" "${SKILLS_DIR}/antigravity-session-cost"
echo "[+] Linked skill -> ${SKILLS_DIR}/antigravity-session-cost"

# 2. Write user config (~/.config/antigravity-session-cost/config.json)
USER_CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/antigravity-session-cost"
mkdir -p "${USER_CFG_DIR}"
USER_CFG_FILE="${USER_CFG_DIR}/config.json"

CLI_SETTINGS="${GEMINI_DIR}/antigravity-cli/settings.json"

python3 - "${USER_CFG_FILE}" "${CLI_SETTINGS}" "${MODE}" "${SKILL_SCRIPTS_DIR}" "${RATE_CARD_URL}" "${RATE_CARD_PATH}" "${DISCOUNT_PCT}" "${AUTH_MODE}" "${GEMINI_DIR}" "${GEMINI_DIR_SET}" <<'PY'
import json, os, sys
from pathlib import Path

cfg_path = Path(sys.argv[1])
settings_path = Path(sys.argv[2])
mode = sys.argv[3]
script_dir = sys.argv[4]
rate_url = sys.argv[5]
rate_path = sys.argv[6]
discount_raw = sys.argv[7]
auth_mode = sys.argv[8]
gemini_dir = os.path.abspath(os.path.expanduser(sys.argv[9]))
gemini_dir_set = sys.argv[10] == "true"

cfg = {}
if cfg_path.is_file():
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}

if rate_url:
    cfg["rate_card_url"] = rate_url
if rate_path:
    cfg["rate_card_path"] = rate_path
if discount_raw:
    cfg["global_discount_pct"] = float(discount_raw)
if gemini_dir_set:
    cfg["gemini_dir"] = gemini_dir
cfg["auth_mode"] = auth_mode

wrapper_cmd = os.path.join(script_dir, "statusline_wrapper.sh")
standalone_cmd = os.path.join(script_dir, "statusline_standalone.sh")

if mode in ("wrapper", "standalone"):
    settings = {}
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            settings = {}

    existing_sl = settings.get("statusLine")
    existing_cmd = ""
    if isinstance(existing_sl, dict):
        existing_cmd = str(existing_sl.get("command") or "")

    if mode == "wrapper":
        if existing_cmd and existing_cmd != wrapper_cmd:
            cfg["wrapped_statusline_command"] = existing_cmd
        new_sl = existing_sl if isinstance(existing_sl, dict) else {"type": "command"}
        new_sl["type"] = "command"
        new_sl["command"] = wrapper_cmd
        settings["statusLine"] = new_sl
        print(f"[+] Configured composable statusLine wrapper -> {wrapper_cmd}")
        if cfg.get("wrapped_statusline_command"):
            print(f"    Preserved existing command: {cfg['wrapped_statusline_command']}")
    elif mode == "standalone":
        settings["statusLine"] = {
            "type": "command",
            "command": standalone_cmd,
        }
        print(f"[+] Configured standalone statusLine -> {standalone_cmd}")

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
print(f"[+] Saved config -> {cfg_path}")
PY

# 3. Prime remote rate card cache if URL provided
if [[ -n "${RATE_CARD_URL}" ]]; then
  echo "[*] Priming remote rate card cache from ${RATE_CARD_URL}..."
  python3 "${SKILL_SCRIPTS_DIR}/session_cost.py" --refresh-rates --rate-card-url "${RATE_CARD_URL}" || \
    echo "[!] Remote refresh failed (will fall back to bundled list prices until reachable)."
fi

echo "[✓] Done."
