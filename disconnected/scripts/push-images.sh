#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${OUTPUT_DIR:-$ROOT/disconnected/artifacts}"
REGISTRY="${IMAGE_REGISTRY:?Set IMAGE_REGISTRY, for example registry.internal.example:5000}"
NAMESPACE="${IMAGE_NAMESPACE:?Set IMAGE_NAMESPACE to the registry project/repository prefix}"
TAG="${IMAGE_TAG:?Set IMAGE_TAG to the value used by build-and-export.sh}"

command -v podman >/dev/null || { echo "podman is required on the registry workstation" >&2; exit 2; }
command -v skopeo >/dev/null || { echo "skopeo is required to resolve pushed digests" >&2; exit 2; }
(
  cd "$OUT"
  if command -v sha256sum >/dev/null; then
    sha256sum -c SHA256SUMS
  else
    shasum -a 256 -c SHA256SUMS
  fi
)
podman load --input "$OUT/images.oci.tar"

DEST="$REGISTRY/$NAMESPACE"
mkdir -p "$OUT/pushed"
: > "$OUT/pushed/images.env"
while read -r key name source_ref; do
  [[ -n "$key" ]] || continue
  destination="$DEST/$name:$TAG"
  podman tag "$source_ref" "$destination"
  podman push "$destination"
  printf '%s_IMAGE=%s\n' "$key" "$destination" >> "$OUT/pushed/images.env"
  digest="$(skopeo inspect --format '{{.Digest}}' "docker://$destination")"
  printf '%s_DIGEST_REF=%s\n' "$key" "$DEST/$name@$digest" >> "$OUT/pushed/images.env"
done <<EOF
CHINESE_MCQ_ADAPTER chinese-mcq-adapter localhost/evalhub-offline/chinese-mcq-adapter:$TAG
CHINESE_DATASET_BUNDLE chinese-dataset-bundle localhost/evalhub-offline/chinese-dataset-bundle:$TAG
MAAS_EVAL_RUNNER maas-eval-runner localhost/evalhub-offline/maas-eval-runner:$TAG
MODEL_EVALUATION_DASHBOARD model-evaluation-dashboard localhost/evalhub-offline/model-evaluation-dashboard:$TAG
EVALHUB_POSTGRES evalhub-postgres localhost/evalhub-offline/evalhub-postgres:$TAG
OSE_OAUTH_PROXY ose-oauth-proxy localhost/evalhub-offline/ose-oauth-proxy:$TAG
EOF
sort -o "$OUT/pushed/images.env" "$OUT/pushed/images.env"
echo "Pushed internal image references and digests to $OUT/pushed/images.env"
