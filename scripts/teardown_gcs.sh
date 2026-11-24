#!/usr/bin/env bash
# Teardown script for the private GCS rate-card PoC bucket.
# Deletes all object versions and removes the bucket cleanly.
set -euo pipefail

PROJECT_ID=""
BUCKET_NAME=""
AUTO_APPROVE=false

usage() {
  cat <<EOF
Usage: $(basename "$0") --bucket <BUCKET_NAME> [options]

Delete all versioned objects and remove the GCS rate-card bucket.

Options:
  --project <PROJECT_ID>   GCP project ID (default: gcloud config get-value project)
  --bucket <BUCKET_NAME>   Target GCS bucket name (required)
  --yes                    Skip interactive confirmation prompt
  -h, --help               Show this help
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
    --yes)
      AUTO_APPROVE=true; shift ;;
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

BUCKET_NAME="${BUCKET_NAME#gs://}"
BUCKET_NAME="${BUCKET_NAME%/}"

if [[ -z "${BUCKET_NAME}" ]]; then
  echo "Error: --bucket <BUCKET_NAME> is required." >&2
  usage
  exit 1
fi

BUCKET_URI="gs://${BUCKET_NAME}"

if ! gcloud storage buckets describe "${BUCKET_URI}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "[*] Bucket ${BUCKET_URI} not found in project ${PROJECT_ID}; nothing to delete."
  exit 0
fi

if [[ "${AUTO_APPROVE}" != "true" ]]; then
  read -r -p "Permanently delete all versioned objects and bucket ${BUCKET_URI} in project ${PROJECT_ID}? [y/N] " reply
  case "${reply}" in
    y|Y|yes|YES) ;;
    *)
      echo "[*] Aborted."
      exit 0 ;;
  esac
fi

if [[ -n "$(gcloud storage ls --all-versions "${BUCKET_URI}" --project="${PROJECT_ID}" 2>/dev/null || true)" ]]; then
  echo "[*] Deleting all object versions in ${BUCKET_URI}..."
  gcloud storage rm --recursive --all-versions "${BUCKET_URI}/**" --project="${PROJECT_ID}" --quiet
fi

echo "[*] Removing bucket ${BUCKET_URI}..."
gcloud storage buckets delete "${BUCKET_URI}" --project="${PROJECT_ID}" --quiet

echo "[✓] Removed ${BUCKET_URI}."
