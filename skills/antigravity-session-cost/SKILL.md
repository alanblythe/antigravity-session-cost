---
name: antigravity-session-cost
description: Configures, calibrates, deploys, and reports USD spend for Antigravity CLI (agy), including live statusLine badges with recursive local subagent/Swarm rollups, single-session per-model breakdowns, historical spend reports (1d/7d/30d/all) with prompt-cache savings, custom discount/rate-card calibration, and enterprise GCS/HTTPS ETag-cached fleet distribution. Use when asked to enable session cost in Antigravity CLI, inspect or wrap an existing statusLine script, calibrate negotiated Vertex AI / enterprise discounts, deploy a shared rate card across a team, or answer spend queries like "what did I spend today/this week", "show 7-day cost", or "break down this session's cost by model".
---

# Antigravity CLI Session Cost (`antigravity-session-cost`)

Configures local, recursive USD session cost tracking for **Antigravity CLI (`agy`)** without clobbering existing `statusLine` setups or requiring external Python packages.

## Core Principles

- **Zero Destructive Edits**: Never overwrite a user's existing `statusLine` command or script without auditing it first and getting confirmation on the integration mode.
- **Zero Blocking Network I/O**: `statusLine` renders must never make network calls. Remote rate cards (`gs://...` or `https://...`) are refreshed out-of-band (via `--refresh-rates` or a `SessionStart` hook) with ETag validation and fail-open offline caching.
- **Dynamic Path Resolution**: This skill directory is 100% self-contained (`scripts/session_cost.py`, `scripts/statusline_wrapper.sh`, `scripts/statusline_standalone.sh`, `scripts/stop_hook.sh`, `config/rate_card.json`). Always resolve runtime scripts and config relative to this skill's own directory (`<skill_dir>`) — never hardcode paths, usernames, or assume an enclosing git repository checkout.

---

## Workflow 1: Smart Audit -> Propose -> Apply (`statusLine` Setup)

Always run this 3-phase flow before modifying `~/.gemini/antigravity-cli/settings.json`.

### Phase 1: Audit
1. Check `~/.gemini/antigravity-cli/settings.json` (or `<gemini_dir>/antigravity-cli/settings.json` if the user runs an isolated `--gemini_dir`) for an existing `statusLine` block and `hooks` (`SessionStart`, `Stop`).
2. If `statusLine.command` is already configured, inspect the target script to see:
   - What fields it renders (model, context %, git branch, etc.)
   - Whether it reads stdin JSON directly (`jq`, `python3`, `node`, etc.)
   - Whether it uses ANSI escape codes or Powerline/separator characters
3. Check whether `~/.config/antigravity-session-cost/config.json` or `/etc/antigravity-session-cost/config.json` already exists.

### Phase 2: Propose
Present the audit findings and recommend the least-invasive integration mode:

| Mode | When to Recommend | Trade-off |
| :--- | :--- | :--- |
| **1. Composable Wrapper (Recommended if `statusLine` exists)** | User already has a custom `statusLine` script they want to keep untouched. | `scripts/statusline_wrapper.sh` tees stdin JSON to the existing command and appends `│ $0.42 (sub $0.11)`. Zero edits to the original script. |
| **2. In-Place Diff Patch** | User prefers a single script with custom placement/colors inside their existing `statusLine`. | Show a minimal unified diff invoking `session_cost.py --format plain` (or `--format badge`) and inserting the badge at the user's chosen position. |
| **3. Standalone Turnkey (`statusline_standalone.sh`)** | User has no `statusLine` configured yet. | Renders `Model │ ctx XX% │ branch │ N sub │ $0.42` out of the box. |
| **4. Stop-Hook Pre-Compute Cache Mode** | User has very large transcripts or wants `<5ms` statusLine renders plus automatic rate-card refresh on `SessionStart`. | Wire `scripts/stop_hook.sh stop` to the `Stop` hook and `scripts/stop_hook.sh session-start` to `SessionStart` in `settings.json` (can be paired with modes 1–3). |

### Phase 3: Apply & Verify
1. Ensure the skill's runtime scripts are executable (`chmod +x <skill_dir>/scripts/*.sh <skill_dir>/scripts/session_cost.py`) and apply only the approved mode:
   - **Wrapper**: preserve any existing `statusLine.command` as `"wrapped_statusline_command"` in `~/.config/antigravity-session-cost/config.json`, then set `statusLine` in `<gemini_dir>/antigravity-cli/settings.json` to `{"type": "command", "command": "<skill_dir>/scripts/statusline_wrapper.sh"}`.
   - **Standalone**: set `statusLine` in `<gemini_dir>/antigravity-cli/settings.json` to `{"type": "command", "command": "<skill_dir>/scripts/statusline_standalone.sh"}`.
   - **In-place patch**: apply the approved diff to the user's existing script invoking `python3 <skill_dir>/scripts/session_cost.py --format plain` (or `--format badge`).
   - **Hook mode**: add `SessionStart` (`<skill_dir>/scripts/stop_hook.sh session-start`) and `Stop` (`<skill_dir>/scripts/stop_hook.sh stop`) hook entries in `<gemini_dir>/antigravity-cli/settings.json`.
   - *(Optional repo clone shortcut: if installed from a full git checkout, `./scripts/install.sh --mode wrapper|standalone` performs the wrapper/standalone wiring automatically.)*
2. Smoke-test by piping a sample statusLine JSON payload into the configured `statusLine.command` and verifying clean single-line output and exit code `0`.

---

## Workflow 2: Interactive Rate-Card & Discount Calibration

Bundled rates in `config/rate_card.json` (inside this skill directory) reflect public Vertex AI list prices (per 1M tokens, USD, with >200K context tier pricing where applicable).

When a user wants to calibrate pricing to their contract or custom rates:

1. **Flat Enterprise / Committed Discount (`global_discount_pct`)**:
   - Ask for the percentage off list price (e.g., `15` for 15% off).
   - Write `"global_discount_pct": 15.0` to `~/.config/antigravity-session-cost/config.json` (or run `./scripts/install.sh --discount-pct 15` if working from a full repo clone).
2. **Per-Model Overrides (`overrides`)**:
   - Add targeted entries under `"overrides"` in `~/.config/antigravity-session-cost/config.json` or `~/.config/antigravity-session-cost/rate_card.json`:
     ```json
     {
       "global_discount_pct": 10.0,
       "overrides": {
         "gemini-4-argon": {
           "input": 1.60,
           "output": 8.00,
           "cache_read": 0.08,
           "discount_pct": 0.0
         }
       }
     }
     ```
   - Note: a model-level `discount_pct` takes precedence over `global_discount_pct` (use `0.0` if the override prices are already net of discount).
3. **Parsing a Pasted Pricing Table**:
   - If the user pastes a Markdown, CSV, or plain-text pricing table (per 1M tokens for input, output, cache read, and optional >200K tiers), map each row into the `rate_card.json` schema (`input`, `output`, `cache_read`, `gt_200k_input`, `gt_200k_output`, `gt_200k_cache_read`), show the generated JSON diff, and save to `~/.config/antigravity-session-cost/rate_card.json` upon approval.
4. **Verify**: Run `python3 <skill_dir>/scripts/session_cost.py --transcript <path> --format json` and confirm the resolved rates and `source` field.

---

## Workflow 3: Enterprise & Fleet Deployment (GCS / HTTPS + ETag)

For organizations managing negotiated rates centrally across developer workstations or devcontainers (the `deploy_gcs.sh`, `teardown_gcs.sh`, `install.sh`, and `terraform/` fleet helpers live at the root of the full repository checkout):

### 1. Provision & Publish Rate Card (Private GCS + ADC or HTTPS)
- **Private GCS + ADC (Recommended — DRS & Public Access Prevention compliant)**:
  Both paths create a private bucket with Uniform Bucket-Level Access, enforced Public Access Prevention, and object versioning, granting least-privilege `roles/storage.objectViewer` to explicit IAM readers (`user:`, `group:`, `serviceAccount:`) — never `allUsers` or `allAuthenticatedUsers`. Workstations read via Application Default Credentials (`gcloud auth application-default login` or `gcloud auth login`).
  - **30-second PoC (repo root `scripts/deploy_gcs.sh`)**:
    ```bash
    ./scripts/deploy_gcs.sh \
      --project <PROJECT_ID> \
      --bucket <BUCKET_NAME> \
      --region us-central1 \
      --reader group:eng@example.com
    ```
    Tear down versioned objects and the bucket with `./scripts/teardown_gcs.sh --project <PROJECT_ID> --bucket <BUCKET_NAME>`.
  - **Enterprise IaC (repo root `terraform/`)**:
    Copy `terraform/terraform.tfvars.example` to `terraform/terraform.tfvars`, populate `project_id`, `bucket_name`, and `reader_members`, and run `terraform init && terraform apply` (works with Terraform or OpenTofu).
- **HTTPS (`https://...`)**: Host `rate_card.json` behind an internal endpoint that returns standard `ETag` headers and honors `If-None-Match` (`304 Not Modified`). Supports ADC identity tokens (`--auth-mode adc`), bearer tokens (`AGY_COST_RATE_CARD_TOKEN`), or custom headers (`AGY_COST_RATE_CARD_AUTH_HEADER`).

### 2. Config Precedence (3-Tier)
1. **Environment variables** (`AGY_COST_RATE_CARD_URL`, `AGY_COST_RATE_CARD`, `AGY_COST_RATE_CARD_TTL`, `AGY_COST_DISCOUNT_PCT`, `AGY_COST_GEMINI_DIR`, `GEMINI_DIR`)
2. **System / MDM policy** (`/etc/antigravity-session-cost/config.json`)
3. **User config** (`~/.config/antigravity-session-cost/config.json`)

> **Isolated `--gemini-dir` persistence:** Setting `"gemini_dir"` in `~/.config/antigravity-session-cost/config.json` (or running the repo installer with `--gemini-dir <path>`) persists the default root for standalone shell runs outside `agy`. When invoked from inside an active `agy` session, `scripts/session_cost.py --report` always detects the running `agy` ancestor's `--gemini_dir` first so reports never bleed across isolated instances.

### 3. Non-Interactive Fleet Install
Write `/etc/antigravity-session-cost/config.json` (or `~/.config/antigravity-session-cost/config.json`) with `"rate_card_url"` and `"auth_mode": "adc"`, or from a full repo clone run:

```bash
./scripts/install.sh \
  --non-interactive \
  --mode wrapper \
  --rate-card-url gs://my-org-finops/antigravity/rate_card.json \
  --auth-mode adc
```

### 4. Caching & Fail-Open Offline Behavior
- `python3 <skill_dir>/scripts/session_cost.py --refresh-rates` sends `If-None-Match: <cached_etag>`.
  - `200 OK`: atomically updates `~/.cache/antigravity-session-cost/rate_card.json` and `rate_card.meta.json`.
  - `304 Not Modified`: refreshes `fetched_at` timestamp without rewriting the rate card.
  - Network/auth error: returns non-zero on `--refresh-rates`, while normal `statusLine` runs silently fail open to the last cached remote rate card, then `~/.config/antigravity-session-cost/rate_card.json`, then bundled `config/rate_card.json` list prices.

---

## Workflow 4: Session & Historical Spend Reports (`--breakdown` / `--report 1d|7d|30d`)

Use `scripts/session_cost.py` (relative to this skill directory) to answer current-session breakdown or historical spend queries without double-counting subagent / Swarm worker transcripts:

1. **Current-session per-model breakdown** (Main vs. Subagents, tokens, prompt-cache savings, model share):
   ```bash
   python3 <skill_dir>/scripts/session_cost.py \
     --transcript <path/to/transcript.jsonl> \
     --breakdown
   ```
   Pass `--format json` to retrieve structured JSON (`cache_savings_usd`, per-model `steps`, `cost_usd`, `rate_card_key`).

2. **Historical daily & model spend reports** (`1d`, `7d`, `30d`, or `all`):
   ```bash
   # Today's spend (local timezone)
   python3 <skill_dir>/scripts/session_cost.py --report 1d

   # Last 7 days (default when --report is passed without an argument)
   python3 <skill_dir>/scripts/session_cost.py --report 7d

   # Last 30 days or full local history
   python3 <skill_dir>/scripts/session_cost.py --report 30d
   python3 <skill_dir>/scripts/session_cost.py --report all
   ```
   - **Deterministic active-instance directory resolution**: `scripts/session_cost.py --report` strictly resolves **one** config root (never silently merging `~/.gemini` and `~/.antigravity`) in this exact order:
     1. Explicit `--brain-dir`
     2. Explicit `--transcript` parent `brain/` (when `--all-surfaces` is not set)
     3. Explicit `--gemini-dir` CLI flag or `AGY_COST_GEMINI_DIR` / `GEMINI_DIR` env var
     4. Active ancestor process (`detect_active_gemini_dir()`): walks up the parent PID chain for `agy` or `language_server*`, using its `--gemini_dir` (or `--config_dir` parent if named `config`), or `~/.gemini` when launched with no custom dir flag
     5. Configured `"gemini_dir"` in `~/.config/antigravity-session-cost/config.json` (used for standalone shell runs outside `agy`)
     6. `~/.gemini` single deterministic fallback
   - **Custom / isolated directory (e.g. `~/.antigravity`)**: set `"gemini_dir": "~/.antigravity"` in `~/.config/antigravity-session-cost/config.json` for standalone shells, set `AGY_COST_GEMINI_DIR` / `GEMINI_DIR`, or pass `--gemini-dir ~/.antigravity` / `--brain-dir ~/.antigravity/antigravity-cli/brain`. Inside an `agy --gemini_dir ~/.antigravity` session, ancestor detection automatically resolves `~/.antigravity`.
   - **All local surfaces**: add `--all-surfaces` to scan `antigravity-cli/brain`, `antigravity/brain`, and `antigravity-ide/brain` strictly within the single resolved config root (each transcript file is scanned flat once, so subagents are never double-counted).
   - **Machine-readable output**: append `--format json`.

---

## Troubleshooting & CLI Flags

- **Force local calculation** (ignore native `cost.total_usd` from stdin): `session_cost.py --force-local`
- **Current-session breakdown table**: `session_cost.py --transcript <path> --breakdown`
- **Historical spend report**: `session_cost.py --report 7d [--gemini-dir <dir> | --brain-dir <dir>] [--all-surfaces]`
- **Inspect per-model JSON**: `session_cost.py --transcript <path> --format json`
- **Plain text (no ANSI)**: `session_cost.py --transcript <path> --format plain`
- **Spend badge color thresholds (ANSI)**: set `AGY_COST_WARN_USD` (default `1.00`, yellow) and `AGY_COST_HIGH_USD` (default `5.00`, red); spend below warn renders bright green.
- **Estimated badge (`~$0.42`)**: Appears when one or more transcript steps used an unrecognized model ID and fell back to `default_fallback`. Add the model under `overrides` or `models` to clear the `~` indicator.
