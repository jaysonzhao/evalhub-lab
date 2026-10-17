#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
NAMESPACE="${NAMESPACE:-evalhub-lab}"
RENDERED="$(mktemp -d)"
trap 'rm -rf "$RENDERED"' EXIT
export NAMESPACE

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
oc apply -f "$RENDERED/evalhub-namespace.yaml"
oc apply -f "$ROOT/disconnected/manifests/dashboard-namespace.yaml"
echo "Created namespaces $NAMESPACE and evalhub-dashboard. Provision their required Secrets through your secret-management process."
