#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
NAMESPACE="${NAMESPACE:-evalhub-lab}"
DATA_PVC_SIZE="${DATA_PVC_SIZE:-2Gi}"
DATA_PVC_ACCESS_MODE="${DATA_PVC_ACCESS_MODE:-ReadWriteMany}"
DATASET_IMAGE="${DATASET_IMAGE:?Set DATASET_IMAGE to the pushed internal dataset-bundle image}"
RENDERED="$(mktemp -d)"
trap 'rm -rf "$RENDERED"' EXIT
export NAMESPACE DATA_PVC_SIZE DATA_PVC_ACCESS_MODE DATASET_IMAGE

python3 "$SKILL_DIR/scripts/render-template.py" \
  "$SKILL_DIR/manifests/dataset-pvc.yaml" "$RENDERED/dataset-pvc.yaml"
python3 "$SKILL_DIR/scripts/render-template.py" \
  "$SKILL_DIR/manifests/dataset-loader-job.yaml" "$RENDERED/dataset-loader-job.yaml"
oc apply -f "$RENDERED/dataset-pvc.yaml"

if oc get job chinese-benchmark-data-loader -n "$NAMESPACE" >/dev/null 2>&1; then
  if ! oc get job chinese-benchmark-data-loader -n "$NAMESPACE" -o jsonpath='{.status.completionTime}' | rg -q .; then
    echo "A dataset-loader job already exists and has not completed; inspect it before retrying." >&2
    exit 2
  fi
  if [[ "${REFRESH_DATA:-false}" != "true" ]]; then
    echo "The dataset-loader job already completed. Set REFRESH_DATA=true only after confirming no evaluation is using this PVC." >&2
    exit 0
  fi
  oc delete job chinese-benchmark-data-loader -n "$NAMESPACE" --wait=true
fi
oc apply -f "$RENDERED/dataset-loader-job.yaml"
oc wait --for=condition=complete --timeout="${DATA_LOADER_TIMEOUT:-20m}" \
  job/chinese-benchmark-data-loader -n "$NAMESPACE"
oc logs job/chinese-benchmark-data-loader -n "$NAMESPACE"
echo "Canonical datasets are populated in PVC chinese-benchmark-data in $NAMESPACE."
