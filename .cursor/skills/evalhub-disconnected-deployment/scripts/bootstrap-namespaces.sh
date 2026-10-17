#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
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

python3 "$SKILL_DIR/scripts/render-template.py" \
  "$SKILL_DIR/manifests/evalhub-namespace.yaml" "$RENDERED/evalhub-namespace.yaml"
oc apply -f "$RENDERED/evalhub-namespace.yaml"
oc apply -f "$SKILL_DIR/manifests/dashboard-namespace.yaml"
echo "Created namespaces $NAMESPACE and evalhub-dashboard. Provision their required Secrets through your secret-management process."
