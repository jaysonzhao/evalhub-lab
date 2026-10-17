---
name: evalhub-disconnected-deployment
description: Package and deploy this EvalHub lab in disconnected or air-gapped OpenShift environments, including pinned dataset staging, image builds and transfer, internal registry import, PVC data loading, and Tekton setup. Use when preparing or updating offline EvalHub deployment assets for this repository.
---

# EvalHub Disconnected Deployment

Use this skill to prepare this repository's EvalHub lab for clusters that cannot reach public registries, Hugging Face, or GitHub. The complete deployment bundle lives beside this file: `README.md`, `scripts/`, `images/`, `manifests/`, and `config/`.

## Workflow

1. Read [the deployment guide](README.md) and inspect repository instructions before changing deployment assets. Keep the connected staging host, transfer workstation, and target cluster steps distinct.
2. On a connected staging host, fetch only repository-pinned dataset sources, verify their hashes, normalize with `adapters/chinese-mcq/prepare_datasets.py --source-dir`, build the project images, and export the checksummed artifacts. Use approved digest-pinned base images for repeatable builds.
3. Transfer the OCI archive, source bundle, source cache, prepared data, and checksums through the site's approved process. Never package OpenShift tokens, MaaS keys, kubeconfigs, or database passwords.
4. On the disconnected registry workstation, verify `SHA256SUMS`, import and push the images to the internal registry, then use the emitted immutable image digests for deployment.
5. On the target cluster, provision namespace-local Secrets through the approved secret-management process. Install namespaces and core services, register the rendered EvalHub adapter through the control plane, load the verified benchmark bundle onto the PVC, and install the MaaS evaluation pipeline.
6. When asked to deploy or run the lab, verify actual cluster resources and service readiness. Report any unverified state; a successful image build, manifest apply, or job creation alone does not prove a working deployment.

## Data and safety constraints

- The cluster-side evaluation path must use the prepared PVC and internal services. Do not add Hugging Face, GitHub, or public-registry downloads to cluster startup or evaluation jobs.
- Keep the repository-pinned dataset revisions and checksums. `--source-dir` must verify cached source files before normalization; retain the normal online path for connected use.
- Preserve the approved QualBench diagnostic exclusions documented in `adapters/chinese-mcq/PREFLIGHT.md` and the versioned correction manifest: idx `4224`, `4229`, `4233`, `4234`, `4253`, `4354`, `8131`, `8132`, `8133`, `14861`, and `15175`. Do not silently add, remove, or reinterpret exclusions.
- Keep site-specific runtime configuration, generated profiles, and artifact/work directories ignored by Git. Do not commit credentials or secret values.
- Keep platform-managed services (OpenShift AI/EvalHub, MLflow, MaaS, and operators) separate from the six repository/sidecar images in the bundle. Follow the site's disconnected mirroring process for platform images.

## Resources

- [Deployment guide](README.md): prerequisites, commands, transfer contents, deployment sequence, and storage/platform notes.
- `scripts/`: source staging, archive creation, registry import, manifest rendering, and OpenShift/Tekton setup.
- `images/`: component and dataset-bundle Containerfiles.
- `manifests/`: namespace, database, EvalHub, dashboard, PVC, and dataset-loader resources.
- `config/runtime.example.json`: non-secret runtime template for the Tekton evaluation pipeline.
