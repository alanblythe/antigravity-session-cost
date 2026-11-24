#!/usr/bin/env bash
# Fast-path PoC deployer for a private, DRS-compliant GCS rate-card bucket.
# - Idempotently creates a bucket with Uniform Bucket-Level Access & Public Access Prevention
# - Enables object versioning
# - Uploads skills/antigravity-session-cost/config/rate_card.json with private cache headers
# - Grants roles/storage.objectViewer to specified IAM readers (never allUsers/allAuthenticatedUsers)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${BASH_SOURCE[0]}")")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT_ID=""
BUCKET_NAME=""
REGION="us-central1"
RATE_CARD_PATH="${REPO_ROOT}/skills/antigravity-session-cost/config/rate_card.json"
OBJECT_NAME="rate_card.json"
READERS=()

usage() {
  cat <<EOF
Usage: $(basename "$0") --bucket <BUCKET_NAME> [options]

Deploy a private GCS bucket hosting rate_card.json for ADC-authenticated fleet reads.

Options:
  --project <PROJECT_ID>     GCP project ID (default: gcloud config get-value project)
  --bucket <BUCKET_NAME>     Target GCS bucket name (required)
  --region <REGION>          Bucket location/region (default: us-central1)
  --reader <IAM_MEMBER>      IAM principal granted roles/storage.objectViewer (repeatable,
                             e.g. user:dev@example.com, group:eng@example.com,
                             serviceAccount:sa@project.iam.gserviceaccount.com)
  --rate-card <PATH>         Local rate_card.json path (default: skills/antigravity-session-cost/config/rate_card.json)
  --object <NAME>            Object path inside bucket (default: rate_card.json)
  -h, --help                 Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      [[ $# -ge 2 ]] || { echo "Error: --project requires a value" >&2; exit 1; }
      PROJECT_ID="$2"; shift 2 ;;
    --bucket)
      [[ $# -ge 2 ]] || { echo "Error: --bucket requires a value" >&2; exit 1; }
      BUCKET_NAME="$2"; shift 2 ;;
    --region)
      [[ $# -ge 2 ]] || { echo "Error: --region requires a value" >&2; exit 1; }
      REGION="$2"; shift 2 ;;
    --reader)
      [[ $# -ge 2 ]] || { echo "Error: --reader requires a value" >&2; exit 1; }
      READERS+=("$2"); shift 2 ;;
    --rate-card)
      [[ $# -ge 2 ]] || { echo "Error: --rate-card requires a value" >&2; exit 1; }
      RATE_CARD_PATH="$2"; shift 2 ;;
    --object)
      [[ $# -ge 2 ]] || { echo "Error: --object requires a value" >&2; exit 1; }
      OBJECT_NAME="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if ! command -v gcloud >/dev/null 2>&1; then
  echo "Error: gcloud CLI not found in PATH." >&2
  exit 1
fi

if [[ -z "${PROJECT_ID}" ]]; then
  PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
fi
if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  echo "Error: GCP project not specified. Pass --project <PROJECT_ID> or set a default with 'gcloud config set project <PROJECT_ID>'." >&2
  exit 1
fi

# Normalize bucket and object names
BUCKET_NAME="${BUCKET_NAME#gs://}"
BUCKET_NAME="${BUCKET_NAME%/}"
OBJECT_NAME="${OBJECT_NAME#/}"

if [[ -z "${BUCKET_NAME}" ]]; then
  echo "Error: --bucket <BUCKET_NAME> is required." >&2
  usage
  exit 1
fi

if [[ ! -f "${RATE_CARD_PATH}" ]]; then
  echo "Error: rate card file not found: ${RATE_CARD_PATH}" >&2
  exit 1
fi

# Validate JSON before uploading
if ! python3 -m json.tool "${RATE_CARD_PATH}" >/dev/null 2>&1; then
  echo "Error: invalid JSON in rate card: ${RATE_CARD_PATH}" >&2
  exit 1
fi

# Enforce DRS / enterprise compliance: never allow public IAM principals
for member in "${READERS[@]}"; do
  if [[ "${member}" == "allUsers" || "${member}" == "allAuthenticatedUsers" ]]; then
    echo "Error: public IAM member '${member}' is prohibited. Use user:, group:, domain:, or serviceAccount: principals." >&2
    exit 1
  fi
done

BUCKET_URI="gs://${BUCKET_NAME}"
OBJECT_URI="${BUCKET_URI}/${OBJECT_NAME}"

# 1. Idempotently create bucket with UBLA + Public Access Prevention
if gcloud storage buckets describe "${BUCKET_URI}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "[*] Bucket ${BUCKET_URI} already exists; enforcing UBLA, Public Access Prevention, and versioning..."
  gcloud storage buckets update "${BUCKET_URI}" \
    --project="${PROJECT_ID}" \
    --uniform-bucket-level-access \
    --public-access-prevention \
    --versioning \
    --quiet
else
  echo "[+] Creating private bucket ${BUCKET_URI} in ${REGION} (project: ${PROJECT_ID})..."
  gcloud storage buckets create "${BUCKET_URI}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --uniform-bucket-level-access \
    --public-access-prevention \
    --quiet

  echo "[+] Enabling object versioning on ${BUCKET_URI}..."
  gcloud storage buckets update "${BUCKET_URI}" \
    --project="${PROJECT_ID}" \
    --versioning \
    --quiet
fi

# 2. Upload rate card with JSON content-type and private cache-control
echo "[+] Uploading ${RATE_CARD_PATH} -> ${OBJECT_URI}..."
gcloud storage cp "${RATE_CARD_PATH}" "${OBJECT_URI}" \
  --project="${PROJECT_ID}" \
  --content-type="application/json" \
  --cache-control="private, max-age=300" \
  --quiet

# 3. Grant roles/storage.objectViewer to requested readers
if [[ ${#READERS[@]} -gt 0 ]]; then
  for member in "${READERS[@]}"; do
    echo "[+] Granting roles/storage.objectViewer on ${BUCKET_URI} to ${member}..."
    gcloud storage buckets add-iam-policy-binding "${BUCKET_URI}" \
      --project="${PROJECT_ID}" \
      --member="${member}" \
      --role="roles/storage.objectViewer" \
      --quiet >/dev/null
  done
else
  echo "[*] No --reader flags supplied (existing project/bucket IAM bindings apply)."
fi

echo ""
echo "[✓] Rate card deployed."
echo "Rate card URI: ${OBJECT_URI}"
echo ""
echo "Install on developer workstations (ADC auth):"
echo "  ./scripts/install.sh --rate-card-url ${OBJECT_URI} --mode wrapper --non-interactive"
