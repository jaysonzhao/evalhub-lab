#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
NAMESPACE="${NAMESPACE:-evalhub-lab}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:?Set POSTGRES_IMAGE to the pushed internal PostgreSQL image}"
DASHBOARD_IMAGE="${DASHBOARD_IMAGE:?Set DASHBOARD_IMAGE to the pushed dashboard image}"
OAUTH_PROXY_IMAGE="${OAUTH_PROXY_IMAGE:?Set OAUTH_PROXY_IMAGE to the pushed OAuth proxy image}"
MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:?Set the in-cluster MLflow tracking URI for EvalHub}"
DASHBOARD_MLFLOW_TRACKING_URI="${DASHBOARD_MLFLOW_TRACKING_URI:?Set the in-cluster MLflow tracking URI for the dashboard}"
EVALHUB_BASE_URL="${EVALHUB_BASE_URL:-https://evalhub.${NAMESPACE}.svc.cluster.local:8443}"
MAAS_ENDPOINT="${MAAS_ENDPOINT:?Set the internal or cluster-routable MaaS API root}"
RENDERED="$(mktemp -d)"
trap 'rm -rf "$RENDERED"' EXIT
export NAMESPACE POSTGRES_IMAGE DASHBOARD_IMAGE OAUTH_PROXY_IMAGE
export MLFLOW_TRACKING_URI DASHBOARD_MLFLOW_TRACKING_URI EVALHUB_BASE_URL MAAS_ENDPOINT

if [[ ! "$NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "NAMESPACE must be a valid Kubernetes namespace name." >&2
  exit 2
fi
if [[ "$NAMESPACE" != "evalhub-lab" ]]; then
  echo "The dashboard source-access RBAC currently targets evalhub-lab; update it before using another namespace." >&2
  exit 2
fi

python3 "$ROOT/disconnected/scripts/render-template.py" \
  "$ROOT/disconnected/manifests/evalhub-namespace.yaml" "$RENDERED/evalhub-namespace.yaml"
python3 "$ROOT/disconnected/scripts/render-template.py" \
  "$ROOT/disconnected/manifests/postgres.yaml" "$RENDERED/postgres.yaml"
python3 "$ROOT/disconnected/scripts/render-template.py" \
  "$ROOT/disconnected/manifests/evalhub.yaml" "$RENDERED/evalhub.yaml"
python3 "$ROOT/disconnected/scripts/render-template.py" \
  "$ROOT/disconnected/manifests/dashboard.yaml" "$RENDERED/dashboard.yaml"

oc apply -f "$RENDERED/evalhub-namespace.yaml"
oc apply -f "$ROOT/disconnected/manifests/dashboard-namespace.yaml"
for secret in evalhub-postgres-credentials evalhub-db-credentials; do
  if ! oc get secret "$secret" -n "$NAMESPACE" >/dev/null 2>&1; then
    echo "Create namespace-local Secret $secret before installing the database and EvalHub." >&2
    exit 2
  fi
done
oc apply -f "$RENDERED/postgres.yaml"
oc rollout status deployment/evalhub-postgres -n "$NAMESPACE" --timeout="${ROLLOUT_TIMEOUT:-10m}"
oc apply -f "$RENDERED/evalhub.yaml"

if ! oc get secret model-evaluation-dashboard-oauth -n evalhub-dashboard >/dev/null 2>&1; then
  echo "Create evalhub-dashboard/model-evaluation-dashboard-oauth before installing the dashboard." >&2
  exit 2
fi
sed "s/namespace: evalhub-lab/namespace: $NAMESPACE/g" \
  "$ROOT/evaluation-dashboard/deploy/source-access.yaml" > "$RENDERED/source-access.yaml"
oc apply -f "$RENDERED/source-access.yaml"
oc apply -f "$RENDERED/dashboard.yaml"
oc rollout status deployment/model-evaluation-dashboard -n evalhub-dashboard \
  --timeout="${ROLLOUT_TIMEOUT:-10m}"
echo "Applied EvalHub lab and dashboard manifests. Verify the EvalHub route and source access before adding the provider and pipeline."
