#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${OUTPUT_DIR:-$ROOT/disconnected/artifacts}"
WORK="${WORK_DIR:-$ROOT/disconnected/work}"
TAG="${IMAGE_TAG:-$(git -C "$ROOT" rev-parse --short=12 HEAD)}"
PYTHON_311_BASE_IMAGE="${PYTHON_311_BASE_IMAGE:-registry.access.redhat.com/ubi9/python-311:latest}"
PYTHON_312_BASE_IMAGE="${PYTHON_312_BASE_IMAGE:-registry.access.redhat.com/ubi9/python-312:latest}"
NODE_22_BASE_IMAGE="${NODE_22_BASE_IMAGE:-registry.access.redhat.com/ubi9/nodejs-22:latest}"
TARGET_PLATFORM="${TARGET_PLATFORM:-linux/amd64}"

command -v podman >/dev/null || { echo "podman is required on the connected staging host" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 2; }
if [[ -n "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ]]; then
  echo "Commit or remove working-tree changes before producing the disconnected release bundle." >&2
  exit 2
fi
mkdir -p "$OUT" "$WORK"

SOURCE_CACHE="$WORK/source-cache"
PREPARED_DATA="$WORK/prepared-data"
DATA_CONTEXT="$WORK/dataset-context"
ADAPTER_IMAGE="localhost/evalhub-offline/chinese-mcq-adapter:$TAG"
DATASET_IMAGE="localhost/evalhub-offline/chinese-dataset-bundle:$TAG"
RUNNER_IMAGE="localhost/evalhub-offline/maas-eval-runner:$TAG"
DASHBOARD_IMAGE="localhost/evalhub-offline/model-evaluation-dashboard:$TAG"
POSTGRES_IMAGE="localhost/evalhub-offline/evalhub-postgres:$TAG"
OAUTH_IMAGE="localhost/evalhub-offline/ose-oauth-proxy:$TAG"

python3 "$ROOT/disconnected/scripts/fetch_dataset_sources.py" --output-dir "$SOURCE_CACHE"
python3 "$ROOT/adapters/chinese-mcq/prepare_datasets.py" \
  --source-dir "$SOURCE_CACHE" --output-dir "$PREPARED_DATA"

rm -rf "$DATA_CONTEXT"
mkdir -p "$DATA_CONTEXT/data"
cp "$PREPARED_DATA"/*.jsonl "$PREPARED_DATA"/manifest.json "$PREPARED_DATA"/manifest.sha256 "$DATA_CONTEXT/data/"
cp "$ROOT/disconnected/images/dataset-bundle/loader.py" "$DATA_CONTEXT/loader.py"

podman build \
  --build-arg "PYTHON_311_BASE_IMAGE=$PYTHON_311_BASE_IMAGE" \
  --build-arg "PYTHON_312_BASE_IMAGE=$PYTHON_312_BASE_IMAGE" \
  --build-arg "NODE_22_BASE_IMAGE=$NODE_22_BASE_IMAGE" \
  --platform "$TARGET_PLATFORM" \
  --target chinese-mcq-adapter \
  -f "$ROOT/disconnected/images/Containerfile.components" \
  -t "$ADAPTER_IMAGE" "$ROOT"
podman build \
  --build-arg "PYTHON_311_BASE_IMAGE=$PYTHON_311_BASE_IMAGE" \
  --build-arg "PYTHON_312_BASE_IMAGE=$PYTHON_312_BASE_IMAGE" \
  --build-arg "NODE_22_BASE_IMAGE=$NODE_22_BASE_IMAGE" \
  --platform "$TARGET_PLATFORM" \
  --target maas-eval-runner \
  -f "$ROOT/disconnected/images/Containerfile.components" \
  -t "$RUNNER_IMAGE" "$ROOT"
podman build \
  --build-arg "PYTHON_BASE_IMAGE=$PYTHON_311_BASE_IMAGE" \
  --platform "$TARGET_PLATFORM" \
  -f "$ROOT/disconnected/images/dataset-bundle/Containerfile" \
  -t "$DATASET_IMAGE" "$DATA_CONTEXT"
podman build \
  --build-arg "PYTHON_311_BASE_IMAGE=$PYTHON_311_BASE_IMAGE" \
  --build-arg "PYTHON_312_BASE_IMAGE=$PYTHON_312_BASE_IMAGE" \
  --build-arg "NODE_22_BASE_IMAGE=$NODE_22_BASE_IMAGE" \
  --platform "$TARGET_PLATFORM" \
  --target model-evaluation-dashboard \
  -f "$ROOT/disconnected/images/Containerfile.components" \
  -t "$DASHBOARD_IMAGE" "$ROOT"

# These platform-side images are not built by this repository. Pull them on
# the connected host so the air-gapped target does not need public registry access.
POSTGRES_SOURCE_IMAGE="${POSTGRES_SOURCE_IMAGE:-registry.redhat.io/rhel9/postgresql-16:latest}"
OAUTH_SOURCE_IMAGE="${OAUTH_SOURCE_IMAGE:-registry.redhat.io/openshift4/ose-oauth-proxy-rhel9:v4.20}"
podman pull --platform "$TARGET_PLATFORM" "$POSTGRES_SOURCE_IMAGE"
podman tag "$POSTGRES_SOURCE_IMAGE" "$POSTGRES_IMAGE"
podman pull --platform "$TARGET_PLATFORM" "$OAUTH_SOURCE_IMAGE"
podman tag "$OAUTH_SOURCE_IMAGE" "$OAUTH_IMAGE"

podman save --format oci-archive --output "$OUT/images.oci.tar" \
  "$ADAPTER_IMAGE" "$DATASET_IMAGE" "$RUNNER_IMAGE" "$DASHBOARD_IMAGE" "$POSTGRES_IMAGE" "$OAUTH_IMAGE"
git -C "$ROOT" archive --format=tar --prefix=evalhub-lab/ HEAD | gzip > "$OUT/source-code.tar.gz"
tar -C "$WORK" -czf "$OUT/source-cache.tar.gz" source-cache
tar -C "$WORK" -czf "$OUT/prepared-data.tar.gz" prepared-data
cp "$PREPARED_DATA/manifest.json" "$OUT/dataset-manifest.json"
cp "$PREPARED_DATA/manifest.sha256" "$OUT/dataset-manifest.sha256"
cat > "$OUT/images.txt" <<EOF
$ADAPTER_IMAGE
$DATASET_IMAGE
$RUNNER_IMAGE
$DASHBOARD_IMAGE
$POSTGRES_IMAGE
$OAUTH_IMAGE
EOF
(
  cd "$OUT"
  if command -v sha256sum >/dev/null; then
    sha256sum images.oci.tar source-code.tar.gz source-cache.tar.gz prepared-data.tar.gz dataset-manifest.json dataset-manifest.sha256 images.txt > SHA256SUMS
  else
    shasum -a 256 images.oci.tar source-code.tar.gz source-cache.tar.gz prepared-data.tar.gz dataset-manifest.json dataset-manifest.sha256 images.txt > SHA256SUMS
  fi
)
echo "Offline artifacts written to $OUT"
echo "Transfer all checksummed artifacts to the disconnected registry workstation."
