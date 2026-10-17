# MaaS evaluations with Tekton

This manual OpenShift Pipelines workflow triggers one EvalHub job per selected benchmark, waits for the remote jobs, collects their MLflow links, writes a dashboard-compatible summary bundle, and fails the PipelineRun when execution, required metrics, or MLflow linking is incomplete. Select `hkmmlu`, `tmmluplus`, `cmmlu`, and/or `qualbench`; `--datasets all` expands to all four before a PipelineRun is created.

The sample all-benchmark run uses the `smoke` profile. It samples up to five validated rows per selected benchmark. QualBench uses the already approved diagnostic subset with the 11 documented source anomalies excluded; full QualBench is intentionally disabled. A green run without configured accuracy thresholds means the selected jobs completed with clean data-quality metrics and MLflow links. It is not an accuracy acceptance decision.

## Prerequisites

- RHOAI 3.5 EvalHub and MLflow are healthy; a registered Chinese MCQ provider serves the stable benchmark IDs. Configure its actual provider UUID, not a display label.
- OpenShift Pipelines serves `tekton.dev/v1`. The runner image can reach EvalHub and the Kubernetes API using trusted TLS. EvalHub evaluation Jobs can reach the MaaS endpoint and the selected data source.
- The pipeline namespace is also the EvalHub tenant namespace (`evalhub-lab` in the examples), so EvalHub can resolve the model Secret and the selected dataset source. The runner ServiceAccount never reads Secrets.
- The selected JSONL dataset and its `manifest.json` are available through either a versioned S3 location or a tenant-local PVC. Do not use a mutable `latest` data prefix.
- A registry and Python 3.12 base image are available. Pin both the base image and the final runner image by digest.

The Tekton runner uses only the Python standard library. The EvalHub provider remains responsible for inference and scoring; Tekton does not start a serving stack or score model answers itself.

## Prepare the runtime profile

Prepare the approved data locally from the repository-pinned revisions and QualBench correction policy:

```sh
python3 adapters/chinese-mcq/prepare_datasets.py --output-dir /tmp/chinese-mcq-prepared
python3 tekton/maas-evaluation/scripts/make_dataset_profile.py \
  --manifest /tmp/chinese-mcq-prepared/manifest.json \
  --provider-id <registered-provider-uuid> \
  --output tekton/maas-evaluation/deployment/dataset-profile.json
```

For an MMLU-only environment, prepare with `--mmlu-only` and generate a partial data profile. Unselected datasets do not need prepared files, S3 references, or QualBench correction data. Selecting QualBench requires its approved diagnostic manifest and correction hash.

Publish the files under `/tmp/chinese-mcq-prepared` to a versioned S3 prefix and configure the separate S3 credential Secret, or use an existing tenant-local PVC. Copy `config/runtime.example.json` to the ignored `deployment/runtime.json`. Set the actual provider UUID, selected `test_data_refs`, cluster-local EvalHub service URL, service CA bundle path, approved MaaS endpoint prefix, and endpoint-to-model-Secret binding. Leave `quality_thresholds` empty unless the team has approved explicit per-benchmark thresholds on the adapter's 0–100 percentage scale.

The dashboard uses body-based routing at the MaaS gateway origin. The exact model ID returned by `/v1/models` is sent in the inference request body, including IDs with namespace/model path segments. Keep the gateway origin in the endpoint allowlist and its Secret binding; exact `/llm/{model-id}` paths remain supported for legacy PipelineRuns.

Create the namespace-local runtime ConfigMap after the profile and runtime file are ready:

```sh
oc create configmap maas-evaluation-runtime -n evalhub-lab \
  --from-file=runtime.json=tekton/maas-evaluation/deployment/runtime.json \
  --from-file=dataset-profile.json=tekton/maas-evaluation/deployment/dataset-profile.json
```

## Build and install

Build the runner image with a digest-pinned Python 3.12 base, push it to an approved registry, and resolve the pushed image digest. Do not use a mutable tag in the Task manifests.

```sh
cd tekton/maas-evaluation
podman build \
  --build-arg PYTHON_BASE_IMAGE='registry.access.redhat.com/ubi9/python-312@sha256:<verified-base-digest>' \
  -f Containerfile -t 'quay.io/<org>/maas-eval-runner:<version>' .
podman push 'quay.io/<org>/maas-eval-runner:<version>'
python3 scripts/render_tasks.py \
  --runner-image 'quay.io/<org>/maas-eval-runner@sha256:<verified-pushed-digest>'
```

After checking the installed CRDs and namespace, the install helper performs server-side dry runs before it applies the dedicated ServiceAccount, namespace Role/RoleBinding, Tasks, and Pipeline:

```sh
oc api-resources --api-group=tekton.dev
oc api-resources --api-group=trustyai.opendatahub.io
oc get tektonconfig
PIPELINE_NAMESPACE=evalhub-lab ./scripts/install.sh
oc create --dry-run=server -f ./pipelinerun.example.yaml -n evalhub-lab
```

The runner Role is namespace-scoped. It grants only the EvalHub virtual `evaluations` create/get/list rights, provider discovery reads, and MLflow `experiments` create/get rights required by EvalHub's job-create proxy policy. Its ConfigMap read/update permission is constrained to the single `maas-eval-ledger` object. It can patch only `dashboard-maas-api-key` to clear its data in the finally diagnostics task; it cannot read Secrets, create or edit the runtime ConfigMap, or administer providers. The authenticated dashboard launcher can patch only that same Secret to stage the user's entered MaaS key, and can read only the run ledger to ensure that key is not replaced while a remote EvalHub job may still use it. Limit who may edit Tasks, Pipelines, and PipelineRuns that use `maas-eval-runner`.

The dashboard model picker queries the MaaS OpenAI-compatible `GET /v1/models` catalog over HTTPS. A submitted model is checked against that catalog again. Inference uses the configured MaaS gateway origin at `/v1/chat/completions`, with the exact model ID in the request body. The API key is stored in the fixed Secret only for the run; the finally task clears it once all EvalHub jobs are terminal. A timeout or cancelled PipelineRun does not prove the remote jobs stopped, so the key is retained and a new dashboard run is blocked until reconciliation confirms terminal job state.

## Trigger all four smoke evaluations

The helper asks for the MaaS key privately, creates a unique immutable tenant Secret through stdin, then creates a PipelineRun. The key is not placed in PipelineRun parameters, Task status, or the durable ledger.

```sh
python3 tekton/maas-evaluation/scripts/launch_eval.py \
  --endpoint 'https://maas.apps.cluster-v4vsc.v4vsc.sandbox287.opentlc.com' \
  --model gpt-oss-20b \
  --profile smoke \
  --datasets all
```

The resolved dataset list is shown before submission. To reuse a separately provisioned Secret, pass `--secret-ref NAME`; model discovery is not attempted because the launcher does not read existing Secret contents. For a first inexpensive check, pass `--datasets hkmmlu`; selection is independent of the profile.

Follow the PipelineRun with the printed `tkn pipelinerun logs ...` command. Tekton timeouts are profile-specific: smoke defaults to 2h15m total, coverage to 7h, and full MMLU to 43h. Full QualBench is rejected. Override the timeout flags only when the corresponding EvalHub deadline and cluster quota have been reviewed.

## Durable status, reconciliation, and exports

Installation creates one non-secret ConfigMap, `maas-eval-ledger`. Each run stores its frozen selection, correlation tags, known remote job IDs and states, provenance, MLflow links, gate outcome, and small dashboard bundle/summary under keys derived from the PipelineRun UID. The ConfigMap has no PipelineRun owner reference, so PipelineRun cleanup does not erase recovery records. The runner only has read/update access to this named ConfigMap, not the runtime ConfigMap or any Secret. The ledger enforces a 700 KB data limit; prune old per-run keys under the tenant retention policy after all their remote jobs are terminal and exports are no longer needed.

Read the saved record and export the bundle after the PipelineRun completes:

```sh
python3 tekton/maas-evaluation/scripts/reconcile_eval.py \
  --namespace evalhub-lab --pipelinerun <PIPELINERUN_NAME> \
  --export-dir ./maas-evaluation-export
```

Add `--refresh` to query EvalHub and reconcile a timed-out POST by its exact PipelineRun/dataset correlation tag. That option uses the current `oc` login for EvalHub API access; the token is held in a temporary mode-0600 file and removed on exit. Persisting the refreshed status requires permission to update `maas-eval-ledger`. Multiple exact matches are reported as ambiguous and are never silently selected. The command does not cancel remote jobs or delete model Secrets. Tekton cancellation/timeouts do not cancel remote EvalHub jobs; inspect the record and reconcile the owned jobs before retiring their credentials.

The exported `evaluation-bundle.json` conforms to the dashboard's `evaluation-dashboard.v1` evaluation record shape and clearly marks the bundle summary-only. The pipeline does not copy per-question rows or verify whether they exist in source MLflow artifacts. It exports a bundle; it does not write to or claim to refresh the live dashboard.

The bundle and prepared-data profile contracts are recorded in [`config/evaluation-bundle.schema.json`](config/evaluation-bundle.schema.json) and [`config/data-profile.schema.json`](config/data-profile.schema.json).

## Local verification and live boundary

Run the mocked unit suite before installation:

```sh
python3 -m py_compile runner/*.py scripts/*.py tests/*.py
python3 -m unittest discover -s tests -v
```

The repository contains the versioned pipeline implementation and example launch flow. Applying the resources, confirming the installed Tekton schema through server-side dry run, and running an actual MaaS smoke evaluation require a currently authenticated cluster and a reachable inference endpoint. Do not infer a live deployment from a local manifest or test pass.
