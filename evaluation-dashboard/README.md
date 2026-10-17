# Model Evaluation Dashboard

A React and FastAPI dashboard for the `evalhub-lab` MLflow workspace and EvalHub jobs. It reads live source APIs and can start the preconfigured `maas-evaluation` PipelineRun. The browser receives dashboard data only; Kubernetes credentials and inference credentials stay server-side.

## What it shows

- Overview and run history with filters for benchmark, model, status, protocol, experiment, and date.
- Side-by-side comparisons. Ranking and score deltas stay disabled unless the benchmark, scoring protocol, selected sample, denominator, and available run settings match.
- Subject/domain and question-type metrics, plus transport, parser, truncation, latency, and answer error views when the run contains authorized per-question artifacts.
- Performance data only when MLflow saved the corresponding measurements. Missing throughput or latency is not inferred from wall-clock duration.
- Architecture walkthrough for the EvalHub, MaaS adapter, Tekton, MLflow, and durable run ledger flow.
- Authenticated PipelineRun launcher with MaaS model discovery, run-scoped API-key input, benchmark/profile selection, and recent run status.
- CSV, JSON, printable HTML, and chart exports. CSV cells that could be interpreted as formulas are escaped.

Source timestamps remain UTC; the initial display timezone is `Asia/Hong_Kong`. Question text and model responses are retrieved only when a viewer opens a question row. The launcher creates the preconfigured Tekton PipelineRun in `evalhub-lab`. Users load the MaaS model catalog using their API key, then choose a model, profile, and benchmark IDs. The model endpoint is derived from the selected model ID; Pipeline name, MaaS gateway, and one Kubernetes Secret reference remain fixed by dashboard deployment configuration. The key is sent over HTTPS, is not stored in the browser after submission, and is never a PipelineRun parameter. The pipeline clears its Secret data after all remote EvalHub jobs are terminal. If an EvalHub job remains active or its state is unknown, the key is retained for that job and another dashboard run is blocked until reconciliation.

## Metric interpretation

The metric dictionary is served by `/api/metrics/dictionary`. For `chinese-mcq-zero-shot-strict-v1`, `overall_accuracy` is the source value in percent and uses all attempted questions as its denominator, including transport and parsing failures. `valid_accuracy` is shown separately. Count metrics retain count units. Unknown metrics retain their source key and raw value; the dashboard does not guess scales or units. Latency metrics with no source-defined measurement method are marked unknown.

EvalHub job results and MLflow runs are joined only where the source gives an explicit run ID. If both sources provide a value for the same linked metric, differences are shown as source conflicts rather than silently reconciled. The live catalog is cached for 30 seconds; question artifacts are cached for 60 seconds after the first request.

## Build and run locally

From this directory, build the web assets with the checked-in npm lock:

```sh
cd frontend
npm ci
npm test
npm run build
```

Run backend checks with Python 3.12 and the hash-locked development requirements:

```sh
cd ../backend
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
PYTHONPATH=. .venv/bin/python -m pytest -q tests
```

The API process is intended to run in the OpenShift pod with projected service-account credentials and cluster service CA. It binds to loopback so the OAuth proxy sidecar is the only network entry point. The frontend development server is not an authenticated data source.

## OpenShift deployment

See [`deploy/walkthrough.md`](deploy/walkthrough.md). It creates an isolated `evalhub-dashboard` namespace, binds source reads plus narrowly scoped PipelineRun create/list access to `evalhub-lab`, protects the route with the OpenShift OAuth proxy, and builds the image in the cluster. The build uses `Containerfile`, the frontend npm lock, and the Python hash lock.

The app's authorization is intentionally visible in the UI: viewers access sources through the dashboard service identity, while the OAuth proxy requires the viewer to have `get pods` in `evalhub-lab`. The service account receives no create/update/delete permission on EvalHub evaluation records. Its workload permissions are PipelineRun create/list, patch access to only `dashboard-maas-api-key`, and read access to only `maas-eval-ledger`. The MaaS gateway, Pipeline, and Secret reference are server-owned settings; the selected model ID is checked against the MaaS catalog for the submitted key.
