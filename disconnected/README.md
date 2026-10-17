# Disconnected EvalHub Lab deployment

This folder packages the Chinese MCQ EvalHub lab for a cluster that cannot pull from public registries or download benchmark files. The connected staging machine fetches the repository-pinned public datasets and builds the images once. The target cluster receives an OCI image archive, loads those images into an approved internal registry, and reads benchmark data from a tenant PVC. The evaluation pods make no Hugging Face or GitHub requests.

The bundle includes the Chinese MCQ adapter, the pipeline runner, the dashboard, a dataset image containing the prepared JSONL files and checksums, and the PostgreSQL and OAuth proxy images used by this lab. EvalHub, OpenShift AI, OpenShift Pipelines, MLflow, MaaS, and the served models remain platform services. A cluster administrator must have those operators and any operator-managed images available through the site's disconnected installation process.

## Before staging

Choose one immutable bundle tag and approved Python 3.11/Python 3.12/Node 22 base images. For repeatable release builds, provide digest-pinned values for `PYTHON_311_BASE_IMAGE`, `PYTHON_312_BASE_IMAGE`, and `NODE_22_BASE_IMAGE`; their defaults are only a convenience for a connected trial. The component Containerfile is in this folder; it builds the adapter, pipeline runner, and dashboard as separate targets. It installs dependencies during image build, so run this build on a connected staging host or a build host with approved mirrors and caches. No package manager runs in the cluster at deployment time.

The build script targets `linux/amd64` by default, which matches the current lab cluster. If the target cluster architecture differs, set `TARGET_PLATFORM` to the correct OCI platform. An Apple Silicon staging workstation can build the target architecture through Podman's emulation support; a native matching-architecture staging host is preferable for faster builds.

The connected staging host needs Podman, Python 3.11+, `huggingface_hub`, and `pyarrow`:

```sh
python3 -m pip install huggingface_hub pyarrow
IMAGE_TAG="$(git rev-parse --short=12 HEAD)" \
PYTHON_311_BASE_IMAGE='registry.access.redhat.com/ubi9/python-311@sha256:<approved-digest>' \
PYTHON_312_BASE_IMAGE='registry.access.redhat.com/ubi9/python-312@sha256:<approved-digest>' \
NODE_22_BASE_IMAGE='registry.access.redhat.com/ubi9/nodejs-22@sha256:<approved-digest>' \
./disconnected/scripts/build-and-export.sh
```

`build-and-export.sh` fetches only the pinned test files selected by the dataset prep code. It writes a source cache with `source-manifest.json`, runs the existing normalization and QualBench diagnostic corrections using `--source-dir`, builds the four project images, pulls and packages the PostgreSQL/OAuth images, and writes checksums to `disconnected/artifacts/`. Set `POSTGRES_SOURCE_IMAGE` and `OAUTH_SOURCE_IMAGE` to the site's approved, preferably digest-pinned source images when building the archive. To prepare data from a cache already transferred to an offline workstation, install `pyarrow` from the site's wheel mirror and run:

```sh
python3 adapters/chinese-mcq/prepare_datasets.py \
  --source-dir /path/to/source-cache \
  --output-dir /path/to/prepared-data
```

The offline source cache uses `owner__repo/<pinned-revision>/<repository-path>` plus a SHA-256 manifest. `prepare_datasets.py --source-dir ...` verifies each source file before reading it and makes no outbound request. Without `--source-dir`, the existing online download behavior remains the default.

Copy the following from `disconnected/artifacts/` to the disconnected registry workstation using your approved transfer process:

- `images.oci.tar`
- `source-code.tar.gz` (contains the exact committed repository source)
- `source-cache.tar.gz`
- `prepared-data.tar.gz`
- `dataset-manifest.json` and `dataset-manifest.sha256`
- `images.txt`
- `SHA256SUMS`

`source-cache.tar.gz` is useful when the disconnected environment must re-prepare the normalized data. Do not place API keys, OpenShift tokens, kubeconfigs, or database passwords in the transfer bundle.

Extract the committed source bundle on the disconnected workstation, place the other transferred files in its ignored `disconnected/artifacts/` folder, and run setup commands from `evalhub-lab/`:

```sh
tar -xzf source-code.tar.gz
mkdir -p evalhub-lab/disconnected/artifacts
cp images.oci.tar source-cache.tar.gz prepared-data.tar.gz dataset-manifest.json dataset-manifest.sha256 images.txt SHA256SUMS \
  evalhub-lab/disconnected/artifacts/
cd evalhub-lab
```

The build script refuses a dirty working tree so the source archive, image tags, and deployment instructions all refer to the same commit.

## Import images and create cluster deployments

On the registry workstation, verify transfer checksums and log in to the approved registry. For the OpenShift integrated registry, use the site's supported registry route and credentials, such as `oc registry login`, before pushing.

```sh
IMAGE_TAG='<the tag selected on the staging host>' \
IMAGE_REGISTRY='registry.internal.example:5000' \
IMAGE_NAMESPACE='evalhub-lab' \
./disconnected/scripts/push-images.sh
```

The script loads the OCI archive, retags and pushes all six images, then records pushed tags and immutable digests in `disconnected/artifacts/pushed/images.env`. Make sure the OpenShift service accounts can pull from this registry, or create the site's image-pull Secrets before deploying. Load the generated values into the shell for the install scripts:

```sh
set -a
source disconnected/artifacts/pushed/images.env
set +a
export POSTGRES_IMAGE="$EVALHUB_POSTGRES_IMAGE"
export DASHBOARD_IMAGE="$MODEL_EVALUATION_DASHBOARD_IMAGE"
export OAUTH_PROXY_IMAGE="$OSE_OAUTH_PROXY_IMAGE"
export DATASET_IMAGE="$CHINESE_DATASET_BUNDLE_IMAGE"
export PIPELINE_RUNNER_IMAGE="$MAAS_EVAL_RUNNER_DIGEST_REF"
```

Create both namespaces before provisioning Secrets:

```sh
./disconnected/scripts/bootstrap-namespaces.sh
```

Create these namespace-local Secrets through the site's secret-management process:

- In `evalhub-lab`: `evalhub-postgres-credentials` with `POSTGRESQL_DATABASE`, `POSTGRESQL_USER`, and `POSTGRESQL_PASSWORD`; `evalhub-db-credentials` with a `db-url` value for the local `evalhub-postgres` Service.
- In `evalhub-dashboard`: `model-evaluation-dashboard-oauth` with a random `cookie-secret` value.

Set the site-specific service URLs and image references, then install the workloads:

```sh
export MLFLOW_TRACKING_URI='https://<internal-mlflow-service>:8443/mlflow'
export DASHBOARD_MLFLOW_TRACKING_URI="$MLFLOW_TRACKING_URI"
export EVALHUB_BASE_URL='https://evalhub.evalhub-lab.svc.cluster.local:8443'
export MAAS_ENDPOINT='https://<internal-maas-gateway>'
./disconnected/scripts/install-core.sh
```

`install-core.sh` applies the namespace, PostgreSQL, EvalHub custom resource, dashboard, and dashboard source-access RBAC. It expects the EvalHub CRD and MLflow authorization ClusterRole to exist already. It checks for the three named Secrets; it does not create or print secret values. The database URL must reference the local PostgreSQL Service. `MLFLOW_TRACKING_URI` and `DASHBOARD_MLFLOW_TRACKING_URI` must use internal addresses and the site's trusted CA chain.

Register the rendered adapter definition with the site's EvalHub provider registration procedure, using the internal image reference. Record the provider UUID for the next step:

```sh
export ADAPTER_IMAGE="$CHINESE_MCQ_ADAPTER_IMAGE"
python3 disconnected/scripts/render-provider.py \
  --output disconnected/work/chinese-mcq-provider.yaml
```

The provider definition is generated from `adapters/chinese-mcq/provider.yaml`; its image is changed to the internal registry image. The provider registration itself is intentionally performed through the installed EvalHub control plane, not by applying the provider definition as a Kubernetes Deployment.

## Load benchmark files onto the PVC

The dataset bundle image carries canonical JSONL files plus `manifest.json` and `manifest.sha256`. A one-shot Job verifies the detached manifest hash and each canonical file hash, then copies the files to `chinese-benchmark-data`. The PVC must support `ReadWriteMany` if EvalHub can run evaluations on multiple nodes at the same time. If the cluster only provides `ReadWriteOnce`, set `DATA_PVC_ACCESS_MODE=ReadWriteOnce` and ensure EvalHub schedules jobs so the claim is not mounted concurrently from different nodes. Confirm the volume root is writable by the restricted pod's project-assigned UID/GID; configure the storage class or namespace-approved supplemental group instead of granting the loader privileged access.

```sh
export NAMESPACE=evalhub-lab
export DATA_PVC_ACCESS_MODE=ReadWriteMany
export DATA_PVC_SIZE=2Gi
./disconnected/scripts/install-data.sh
oc get pvc chinese-benchmark-data -n "$NAMESPACE"
oc logs job/chinese-benchmark-data-loader -n "$NAMESPACE"
```

The setup script leaves a failed loader Job in place for inspection. After diagnosing it, delete that specific failed Job and rerun the script. It will not rewrite a PVC after a successful load unless `REFRESH_DATA=true` is set. Only refresh while no evaluation is using the PVC.

## Install the MaaS evaluation pipeline

Copy `config/runtime.example.json` to the ignored `config/runtime.json`. Set the registered provider UUID, internal MaaS host allowlist, EvalHub service URL, and the `chinese-benchmark-data` PVC references. Do not place a MaaS API key in this file. The dashboard collects the key at run time and places it in the fixed tenant Secret.

Extract the staged profile input, then install the pipeline with the pushed runner image's digest reference:

```sh
mkdir -p disconnected/work
tar -xzf disconnected/artifacts/prepared-data.tar.gz -C disconnected/work
cp disconnected/config/runtime.example.json disconnected/config/runtime.json
# Edit config/runtime.json for the target provider, EvalHub, MaaS and tenant.
export PROVIDER_ID='<registered-provider-uuid>'
export PIPELINE_RUNNER_IMAGE="$MAAS_EVAL_RUNNER_DIGEST_REF"
export PREPARED_DATA_DIR="$PWD/disconnected/work/prepared-data"
./disconnected/scripts/install-pipeline.sh
```

The helper creates the runtime/profile ConfigMap, renders the Tasks with the immutable internal runner digest, and delegates installation to the existing namespace-scoped Tekton installer. Verify the Pipeline and Tasks are installed before using the dashboard's trigger page.

## Artifact and platform notes

- The source-cache manifest and prepared-data manifest are integrity checks. Preserve a separate trusted copy of their SHA-256 values or sign the transfer bundle if the threat model requires authenticity as well as corruption detection.
- The site must mirror OpenShift AI/EvalHub, MLflow, Pipelines, MaaS, and operator-managed images using its supported platform mirroring workflow. The six OCI images built or collected here cover only this repository's deployment plus PostgreSQL and OAuth proxy.
- If the registry requires a custom CA, install it in the registry workstation's Podman trust store and the cluster's image-pull trust configuration before importing images.
- If the storage class does not provision `ReadWriteMany`, use an approved shared volume or reduce evaluation concurrency before using `ReadWriteOnce`.
- The generated runtime, provider ID, route hosts, database secrets, registry path, and CA configuration are site-specific. Keep credentials out of source control.
