#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if ! oc whoami >/dev/null 2>&1; then
  echo "oc is not authenticated. Log in to the target cluster, then retry." >&2
  exit 2
fi
if ! oc api-resources --api-group=tekton.dev -o name 2>/dev/null | rg -q '^pipelines\.tekton\.dev$'; then
  echo "The Tekton Pipeline API is not served by this cluster." >&2
  exit 2
fi
if ! oc api-resources --api-group=trustyai.opendatahub.io -o name 2>/dev/null | rg -q '^evaluations\.trustyai\.opendatahub\.io$'; then
  echo "The EvalHub evaluations API is not served by this cluster." >&2
  exit 2
fi
if [[ ! -f "$ROOT/dist/tasks.yaml" ]]; then
  echo "Render Tasks with scripts/render_tasks.py and a pinned runner image first." >&2
  exit 2
fi
if ! oc get configmap maas-evaluation-runtime -n "${PIPELINE_NAMESPACE:-evalhub-lab}" >/dev/null 2>&1; then
  echo "Create ConfigMap maas-evaluation-runtime from reviewed runtime.json and dataset-profile.json first." >&2
  exit 2
fi
NAMESPACE="${PIPELINE_NAMESPACE:-evalhub-lab}"
if [[ ! "$NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "PIPELINE_NAMESPACE must be a valid Kubernetes namespace name." >&2
  exit 2
fi
RBAC_FILE="$(mktemp)"
trap 'rm -f "$RBAC_FILE"' EXIT
sed "s/__PIPELINE_NAMESPACE__/$NAMESPACE/g" "$ROOT/rbac.yaml" > "$RBAC_FILE"
oc apply --server-side --dry-run=server -n "$NAMESPACE" -f "$RBAC_FILE"
oc apply --server-side --dry-run=server -n "$NAMESPACE" -f "$ROOT/ledger.yaml"
oc apply --server-side --dry-run=server -n "$NAMESPACE" -f "$ROOT/dist/tasks.yaml"
oc apply --server-side --dry-run=server -n "$NAMESPACE" -f "$ROOT/dist/pipeline.yaml"
oc apply --server-side -n "$NAMESPACE" -f "$RBAC_FILE"
oc apply --server-side -n "$NAMESPACE" -f "$ROOT/ledger.yaml"
oc apply --server-side -n "$NAMESPACE" -f "$ROOT/dist/tasks.yaml"
oc apply --server-side -n "$NAMESPACE" -f "$ROOT/dist/pipeline.yaml"
echo "Installed maas-evaluation Tasks and Pipeline in $NAMESPACE. No PipelineRun was started."
