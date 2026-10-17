#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
NAMESPACE="${PIPELINE_NAMESPACE:-evalhub-lab}"
PROVIDER_ID="${PROVIDER_ID:?Set PROVIDER_ID to the registered EvalHub Chinese MCQ provider UUID}"
PIPELINE_RUNNER_IMAGE="${PIPELINE_RUNNER_IMAGE:?Set PIPELINE_RUNNER_IMAGE to the runner image pinned by sha256 digest}"
RUNTIME_JSON="${RUNTIME_JSON:-$ROOT/disconnected/config/runtime.json}"
PREPARED_DATA_DIR="${PREPARED_DATA_DIR:-$ROOT/disconnected/work/prepared-data}"
PIPELINE_ROOT="$ROOT/tekton/maas-evaluation"

if [[ ! -f "$RUNTIME_JSON" ]]; then
  echo "Runtime file not found: $RUNTIME_JSON (copy and edit disconnected/config/runtime.example.json first)." >&2
  exit 2
fi
if [[ ! -f "$PREPARED_DATA_DIR/manifest.json" ]]; then
  echo "Prepared manifest not found: $PREPARED_DATA_DIR/manifest.json (extract prepared-data.tar.gz first)." >&2
  exit 2
fi

python3 "$PIPELINE_ROOT/scripts/make_dataset_profile.py" \
  --manifest "$PREPARED_DATA_DIR/manifest.json" \
  --provider-id "$PROVIDER_ID" \
  --output "$ROOT/disconnected/config/dataset-profile.json"
oc create configmap maas-evaluation-runtime -n "$NAMESPACE" \
  --from-file=runtime.json="$RUNTIME_JSON" \
  --from-file=dataset-profile.json="$ROOT/disconnected/config/dataset-profile.json" \
  --dry-run=client -o yaml | oc apply -f -
python3 "$PIPELINE_ROOT/scripts/render_tasks.py" \
  --runner-image "$PIPELINE_RUNNER_IMAGE" \
  --namespace "$NAMESPACE" \
  --output-dir "$PIPELINE_ROOT/dist"
PIPELINE_NAMESPACE="$NAMESPACE" "$PIPELINE_ROOT/scripts/install.sh"
