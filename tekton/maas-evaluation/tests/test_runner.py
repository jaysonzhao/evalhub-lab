from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import URLError

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner.core import (  # noqa: E402
    DATASET_ORDER,
    EvalHubClient,
    EvaluationError,
    Workflow,
    assess_dataset_result,
    command_results,
    normalize_datasets,
    parse_dataset_csv,
    profile_parameters,
    reconcile_by_correlation,
    validate_endpoint,
    validate_endpoint_secret_binding,
    validate_data_profile,
    QUALBENCH_CORRECTION_SHA256,
    QUALBENCH_EXCLUDED_IDS,
)
from runner.ledger import ConfigMapLedger, ledger_name, run_key_prefix  # noqa: E402


def make_data_profile() -> dict[str, Any]:
    manifest = "a" * 64
    datasets: dict[str, Any] = {}
    for index, dataset in enumerate(DATASET_ORDER):
        datasets[dataset] = {
            "row_count": {"hkmmlu": 26368, "tmmluplus": 20118, "cmmlu": 11582, "qualbench": 17305}[dataset],
            "coverage_unit_count": 17 if dataset == "qualbench" else {"hkmmlu": 66, "tmmluplus": 66, "cmmlu": 67}[dataset],
            "manifest_sha256": manifest,
            "canonical_jsonl_sha256": f"{index + 1:064x}",
            "source_revision": "a" * 40,
            "evaluation_label": "QualBench diagnostic subset" if dataset == "qualbench" else dataset,
        }
    datasets["qualbench"].update({
        "source_row_count": 17316,
        "correction_manifest_sha256": QUALBENCH_CORRECTION_SHA256,
        "audit": {"source_rows": 17316, "evaluated_rows": 17305, "excluded_ids": QUALBENCH_EXCLUDED_IDS},
    })
    return {
        "schema_version": "maas-eval-data-profile.v1",
        "provider_id": "chinese-mcq-v2",
        "protocol": "chinese-mcq-zero-shot-strict-v1",
        "manifest_sha256": manifest,
        "source_manifest": {"path": "manifest.json"},
        "datasets": datasets,
    }


def make_runtime() -> dict[str, Any]:
    endpoint = "https://maas.example.test/llm/gpt-oss-20b"
    refs = {
        dataset: {"s3": {"bucket": "eval-data", "key": "datasets/chinese-mcq/manifest-abc", "secret_ref": "evalhub-data-s3"}}
        for dataset in DATASET_ORDER
    }
    return {
        "evalhub_url": "https://evalhub.example.test",
        "tenant": "evalhub-lab",
        "provider_id": "chinese-mcq-v2",
        "approved_endpoint_prefixes": [endpoint],
        "endpoint_secret_bindings": [{
            "endpoint_prefix": endpoint,
            "secret_names": ["shared-maas-key"],
            "secret_name_patterns": ["maas-gpt-oss-20b-[0-9a-f]{12}"],
        }],
        "test_data_refs": refs,
        "wait_deadlines_seconds": {"smoke": 60},
        "poll_seconds": 1,
        "quality_thresholds": {},
    }


class FakeAPI:
    def __init__(self, behavior: str = "completed"):
        self.behavior = behavior
        self.requests: list[dict[str, Any]] = []
        self.submits = 0

    def get_provider(self, provider_id: str) -> dict[str, Any]:
        return {
            "resource": {"id": provider_id},
            "benchmarks": [{"id": dataset} for dataset in DATASET_ORDER],
        }

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.submits += 1
        self.requests.append(payload)
        if self.behavior == "unknown_post":
            raise EvaluationError("EvalHub POST request failed (URLError).")
        dataset = payload["benchmarks"][0]["id"]
        return {"resource": {"id": f"job-{dataset}"}, "status": {"state": "pending"}}

    def find_by_tag(self, tag: str) -> list[dict[str, Any]]:
        if self.behavior == "recover_one":
            return [{"resource": {"id": "job-recovered", "name": "maas-1234-hkmmlu"}, "name": "maas-1234-hkmmlu", "tags": [tag]}]
        return []

    def get_job(self, job_id: str) -> dict[str, Any]:
        dataset = job_id.replace("job-", "")
        metrics = [
            {"metric_name": "overall_accuracy", "metric_value": 80.0},
            {"metric_name": "attempted_count", "metric_value": 5},
            {"metric_name": "valid_count", "metric_value": 5},
            {"metric_name": "correct_count", "metric_value": 4},
            {"metric_name": "transport_failures", "metric_value": 0},
            {"metric_name": "parse_failures", "metric_value": 0},
            {"metric_name": "truncated_responses", "metric_value": 0},
        ]
        return {
            "resource": {"id": job_id},
            "name": f"maas-{dataset}",
            "model": {"name": "gpt-oss-20b"},
            "status": {"state": "completed"},
            "results": {
                "mlflow_experiment_url": "https://mlflow.example.test/#/experiments/1",
                "benchmarks": [{
                    "id": dataset,
                    "provider_id": "chinese-mcq-v2",
                    "status": "completed",
                    "metrics": metrics,
                    "mlflow_run_id": "0123456789abcdef0123456789abcdef",
                }],
            },
        }


class MemoryLedger:
    def __init__(self):
        self.state = None
        self.files: dict[str, str] = {}
        self.write_count = 0

    def write(self, state: dict[str, Any], files: dict[str, Path] | None = None) -> None:
        self.state = json.loads(json.dumps(state))
        self.files = {key: path.read_text(encoding="utf-8") for key, path in (files or {}).items() if path.is_file()}
        self.write_count += 1


class DatasetSelectionTests(unittest.TestCase):
    def test_array_is_canonical_and_all_expands_only_in_cli_convenience(self) -> None:
        self.assertEqual(normalize_datasets(["cmmlu", "hkmmlu"]), ["hkmmlu", "cmmlu"])
        self.assertEqual(parse_dataset_csv("all"), list(DATASET_ORDER))
        self.assertEqual(parse_dataset_csv("hkmmlu,cmmlu"), ["hkmmlu", "cmmlu"])

    def test_rejects_empty_duplicate_unknown_and_mixed_all(self) -> None:
        for value in ([], [""], ["hkmmlu", "hkmmlu"], ["typo"], ["all", "cmmlu"]):
            with self.subTest(value=value), self.assertRaises(EvaluationError):
                normalize_datasets(value, expand_all=True)
        with self.assertRaises(EvaluationError):
            normalize_datasets(["all"])

    def test_profiles_keep_full_qualbench_disabled_and_full_has_no_limit(self) -> None:
        info = make_data_profile()["datasets"]["hkmmlu"]
        self.assertEqual(profile_parameters("smoke", "hkmmlu", info)["limit"], 5)
        self.assertEqual(profile_parameters("coverage", "hkmmlu", info)["per_subject_limit"], 1)
        self.assertNotIn("limit", profile_parameters("full", "hkmmlu", info))
        with self.assertRaises(EvaluationError):
            profile_parameters("full", "qualbench", make_data_profile()["datasets"]["qualbench"])

    def test_endpoint_and_secret_pairing_are_scoped(self) -> None:
        endpoint = "https://maas.example.test/llm/gpt-oss-20b"
        self.assertEqual(validate_endpoint(endpoint, [endpoint]), endpoint)
        for bad in (
            "http://maas.example.test/llm/gpt-oss-20b",
            "https://user:pass@maas.example.test/llm/gpt-oss-20b",
            endpoint + "?api_key=secret",
            endpoint + "-other",
        ):
            with self.subTest(bad=bad), self.assertRaises(EvaluationError):
                validate_endpoint(bad, [endpoint])
        binding = [{"endpoint_prefix": endpoint, "secret_names": ["shared-maas-key"], "secret_name_patterns": ["maas-[a-f0-9]{12}"]}]
        validate_endpoint_secret_binding(endpoint, "maas-012345abcdef", binding)
        with self.assertRaises(EvaluationError):
            validate_endpoint_secret_binding(endpoint, "unrelated-secret", binding)

    def test_qualbench_profile_requires_exact_approved_diagnostic_policy(self) -> None:
        profile = make_data_profile()
        validate_data_profile(profile, "chinese-mcq-v2", ["cmmlu", "qualbench"])
        profile["datasets"]["qualbench"]["audit"]["evaluated_rows"] = 17316
        with self.assertRaises(EvaluationError):
            validate_data_profile(profile, "chinese-mcq-v2", ["qualbench"])


class QualityGateTests(unittest.TestCase):
    def test_data_and_model_quality_are_independent(self) -> None:
        metrics = {
            "overall_accuracy": 42,
            "attempted_count": 5,
            "valid_count": 5,
            "correct_count": 2,
            "transport_failures": 0,
            "parse_failures": 0,
            "truncated_responses": 0,
        }
        passed = assess_dataset_result(
            profile="smoke", dataset="hkmmlu", dataset_info=make_data_profile()["datasets"]["hkmmlu"],
            state="completed", raw_metrics=metrics, quality_thresholds={"overall_accuracy_min": 50},
        )
        self.assertEqual(passed["data_quality_outcome"], "passed")
        self.assertEqual(passed["model_quality_outcome"], "failed")

    def test_missing_counts_and_transport_failures_fail_data_quality(self) -> None:
        info = make_data_profile()["datasets"]["hkmmlu"]
        missing = assess_dataset_result(profile="smoke", dataset="hkmmlu", dataset_info=info, state="completed", raw_metrics={})
        self.assertEqual(missing["data_quality_outcome"], "failed")
        metrics = {"overall_accuracy": 20, "attempted_count": 5, "valid_count": 4, "correct_count": 1,
                   "transport_failures": 1, "parse_failures": 0, "truncated_responses": 0}
        failed = assess_dataset_result(profile="smoke", dataset="hkmmlu", dataset_info=info, state="completed", raw_metrics=metrics)
        self.assertEqual(failed["data_quality_outcome"], "failed")


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        runtime = make_runtime()
        profile = make_data_profile()
        (self.root / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
        (self.root / "data-profile.json").write_text(json.dumps(profile), encoding="utf-8")
        self.env = {
            "RESULTS_DIR": str(self.root / "results"),
            "RUNTIME_CONFIG": str(self.root / "runtime.json"),
            "DATA_PROFILE_PATH": str(self.root / "data-profile.json"),
            "PIPELINE_RUN_UID": "12345678-1234-1234-1234-123456789abc",
            "PIPELINE_RUN_NAME": "maas-eval-abc12",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_preflight_command_dispatch_writes_checkpoint_and_result(self) -> None:
        workflow = Workflow(self.env, FakeAPI())
        result_path = self.root / "profile-hash"
        args = SimpleNamespace(
            endpoint="https://maas.example.test/llm/gpt-oss-20b",
            model_secret_name="shared-maas-key", model_name="gpt-oss-20b",
            profile="smoke", dataset=["hkmmlu"], result_path=str(result_path),
        )
        command_results(workflow, "preflight", args)
        state = json.loads((self.root / "results" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["selection"]["selected_datasets"], ["hkmmlu"])
        self.assertEqual(result_path.read_text(encoding="utf-8"), state["selection"]["profile_hash"])

    def test_all_four_submit_independent_jobs_without_key_material(self) -> None:
        api = FakeAPI()
        workflow = Workflow(self.env, api)
        ledger = MemoryLedger()
        workflow.ledger = ledger
        state = workflow.preflight(
            endpoint="https://maas.example.test/llm/gpt-oss-20b",
            model_secret_name="shared-maas-key", model_name="gpt-oss-20b",
            profile="smoke", datasets=list(DATASET_ORDER),
        )
        self.assertEqual(state["selection"]["selected_datasets"], list(DATASET_ORDER))
        submitted = workflow.submit(datasets=list(DATASET_ORDER))
        self.assertEqual(len(api.requests), 4)
        self.assertEqual(submitted["submission_outcome"], "complete")
        serialized = json.dumps(api.requests)
        self.assertNotIn("SENTINEL_API_KEY", serialized)
        self.assertEqual(api.requests[0]["model"]["auth"], {"secret_ref": "shared-maas-key"})
        workflow.wait()
        collected = workflow.collect()
        self.assertEqual(len(collected["results"]), 4)
        self.assertTrue(all(item["mlflow_url"] for item in collected["results"]))
        gated = workflow.gate()
        self.assertEqual(gated["gate"]["pipeline_gate"], "passed")
        bundle = json.loads((Path(self.env["RESULTS_DIR"]) / "evaluation-bundle.json").read_text())
        self.assertEqual(bundle["selected_datasets"], list(DATASET_ORDER))
        self.assertEqual(bundle["not_selected"], [])
        self.assertTrue(bundle["summary_only"])
        self.assertEqual(bundle["schema_version"], "model-evaluation-dashboard-bundle.v1")
        self.assertEqual(len(bundle["evaluations"]), 4)
        self.assertTrue(all(item["schema_version"] == "evaluation-dashboard.v1" for item in bundle["evaluations"]))
        self.assertTrue(all(item["completeness"]["question_artifact"] == "not_exported_by_pipeline" for item in bundle["evaluations"]))
        self.assertGreaterEqual(ledger.write_count, 5)
        self.assertEqual(ledger.state["gate"]["pipeline_gate"], "passed")
        self.assertIn("evaluation-bundle.json", ledger.files)
        self.assertIn("| Attempted | Valid | Data quality | Model quality |", ledger.files["summary.md"])

    def test_uncertain_post_is_not_retried_and_does_not_mark_unselected_jobs_failed(self) -> None:
        api = FakeAPI("unknown_post")
        workflow = Workflow(self.env, api)
        workflow.preflight(endpoint="https://maas.example.test/llm/gpt-oss-20b", model_secret_name="shared-maas-key",
                           model_name="gpt-oss-20b", profile="smoke", datasets=["hkmmlu", "cmmlu"])
        state = workflow.submit(datasets=["hkmmlu", "cmmlu"])
        self.assertEqual(api.submits, 1)
        self.assertEqual(state["jobs"][0]["submission_state"], "unknown")
        self.assertEqual(state["jobs"][1]["remote_state"], "not_submitted")

    def test_single_dataset_does_not_require_unselected_qualbench_data(self) -> None:
        profile = make_data_profile()
        del profile["datasets"]["qualbench"]
        (self.root / "data-profile.json").write_text(json.dumps(profile), encoding="utf-8")
        api = FakeAPI()
        workflow = Workflow(self.env, api)
        state = workflow.preflight(
            endpoint="https://maas.example.test/llm/gpt-oss-20b", model_secret_name="shared-maas-key",
            model_name="gpt-oss-20b", profile="smoke", datasets=["cmmlu"],
        )
        submitted = workflow.submit(datasets=["cmmlu"])
        self.assertEqual(state["selection"]["selected_datasets"], ["cmmlu"])
        self.assertEqual([item["dataset"] for item in submitted["jobs"]], ["cmmlu"])
        self.assertEqual(api.submits, 1)

    def test_tenant_local_pvc_reference_is_forwarded_for_selected_dataset(self) -> None:
        runtime = make_runtime()
        runtime["test_data_refs"]["hkmmlu"] = {"pvc": {"claim_name": "chinese-benchmark-data"}}
        (self.root / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
        api = FakeAPI()
        workflow = Workflow(self.env, api)
        workflow.preflight(
            endpoint="https://maas.example.test/llm/gpt-oss-20b",
            model_secret_name="shared-maas-key", model_name="gpt-oss-20b",
            profile="smoke", datasets=["hkmmlu"],
        )
        workflow.submit(datasets=["hkmmlu"])
        self.assertEqual(api.requests[0]["benchmarks"][0]["id"], "hkmmlu")
        self.assertNotIn("benchmark_id", api.requests[0]["benchmarks"][0])
        self.assertEqual(
            api.requests[0]["benchmarks"][0]["test_data_ref"],
            {"pvc": {"claim_name": "chinese-benchmark-data"}},
        )

    def test_pvc_reference_rejects_invalid_or_extra_fields(self) -> None:
        for data_ref in (
            {"pvc": {"claim_name": "Bad_Name"}},
            {"pvc": {"claim_name": "chinese-benchmark-data", "secret_ref": "unexpected"}},
            {"pvc": {"claim_name": "chinese-benchmark-data"}, "s3": {}},
        ):
            with self.subTest(data_ref=data_ref), self.assertRaises(EvaluationError):
                Workflow._validate_test_data_ref(data_ref, "shared-maas-key")

    def test_unique_correlation_reconciliation_only_adopts_one_exact_job(self) -> None:
        entry = {"correlation_tag": "tekton-correlation:uid:hkmmlu", "name": "maas-1234-hkmmlu"}
        recovered, status = reconcile_by_correlation(FakeAPI("recover_one"), entry)
        self.assertEqual((recovered, status), ("job-recovered", "reconciled"))


class EvalHubClientTests(unittest.TestCase):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    class Opener:
        def __init__(self):
            self.requests = []

        def open(self, request, timeout):
            self.requests.append(request)
            return EvalHubClientTests.Response()

    def test_token_file_is_reread_per_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("rotated-token-a", encoding="utf-8")
            opener = self.Opener()
            api = EvalHubClient("https://evalhub.example.test", "evalhub-lab", token, opener=opener)
            api.request("GET", "/api/v1/health")
            token.write_text("rotated-token-b", encoding="utf-8")
            api.request("GET", "/api/v1/health")
            self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer rotated-token-a")
            self.assertEqual(opener.requests[1].get_header("Authorization"), "Bearer rotated-token-b")

    def test_network_exception_text_is_not_echoed(self) -> None:
        class BrokenOpener:
            def open(self, request, timeout):
                raise URLError("https://evalhub.example.test/?token=SENTINEL_API_KEY")

        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("service-account-token", encoding="utf-8")
            api = EvalHubClient("https://evalhub.example.test", "evalhub-lab", token, opener=BrokenOpener())
            with self.assertRaises(EvaluationError) as caught:
                api.request("GET", "/api/v1/health", retries=1)
            self.assertNotIn("SENTINEL_API_KEY", str(caught.exception))


class LedgerTests(unittest.TestCase):
    class Response:
        status = 200

        def __init__(self, value: dict[str, Any]):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self.value).encode("utf-8")

    class FakeKubeAPI:
        def __init__(self):
            self.object = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "maas-eval-ledger", "namespace": "evalhub-lab", "resourceVersion": "1", "labels": {}},
                "data": {},
            }
            self.requests = []

        def open(self, request, timeout):
            self.requests.append(request)
            method = request.get_method()
            if method == "GET":
                return LedgerTests.Response(self.object)
            body = json.loads(request.data)
            if method == "POST":
                self.object = {**body, "metadata": {**body["metadata"], "resourceVersion": "1"}}
            else:
                next_version = str(int(self.object["metadata"]["resourceVersion"]) + 1)
                self.object = {**body, "metadata": {**body["metadata"], "resourceVersion": next_version}}
            return LedgerTests.Response(self.object)

    def test_configmap_ledger_survives_task_local_workspace_and_stores_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_file = root / "token"
            token_file.write_text("projected-token", encoding="utf-8")
            opener = self.FakeKubeAPI()
            ledger = object.__new__(ConfigMapLedger)
            ledger.namespace = "evalhub-lab"
            ledger.name = ledger_name()
            ledger.run_prefix = run_key_prefix("run-uid")
            ledger.collection_url = "https://kubernetes.default.svc/api/v1/namespaces/evalhub-lab/configmaps"
            ledger.object_url = ledger.collection_url + "/" + ledger.name
            ledger.opener = opener
            ledger.token_file = token_file
            export = root / "evaluation-bundle.json"
            export.write_text('{"summary_only":true}\n', encoding="utf-8")
            state = {"schema_version": "maas-eval-checkpoint.v1", "jobs": [{"dataset": "hkmmlu", "job_id": "job-1"}]}
            ledger.write(state, {"evaluation-bundle.json": export})
            self.assertEqual(ledger.read(), state)
            self.assertEqual(opener.object["data"][ledger.run_prefix + "-bundle.json"], '{"summary_only":true}\n')
            self.assertEqual(opener.object["metadata"]["name"], "maas-eval-ledger")
            self.assertNotIn("projected-token", json.dumps(opener.object))
            other = object.__new__(ConfigMapLedger)
            other.namespace = "evalhub-lab"
            other.name = ledger_name()
            other.run_prefix = run_key_prefix("other-run-uid")
            other.object_url = ledger.object_url
            other.opener = opener
            other.token_file = token_file
            other.write({"schema_version": "maas-eval-checkpoint.v1", "jobs": [{"dataset": "cmmlu"}]})
            self.assertEqual(ledger.read(), state)
            self.assertEqual(other.read()["jobs"][0]["dataset"], "cmmlu")

    def test_dashboard_metric_rows_keep_known_percent_scale(self):
        from runner.core import dashboard_metric_rows

        rows = dashboard_metric_rows({"overall_accuracy": 83.5, "attempted_count": 5, "new_vendor_metric": 0.75})
        by_key = {row["key"]: row for row in rows}
        self.assertEqual((by_key["overall_accuracy"]["value"], by_key["overall_accuracy"]["unit"]), (83.5, "%"))
        self.assertEqual((by_key["attempted_count"]["value"], by_key["attempted_count"]["unit"]), (5, "questions"))
        self.assertEqual((by_key["new_vendor_metric"]["value"], by_key["new_vendor_metric"]["unit"]), (0.75, "raw"))


if __name__ == "__main__":
    unittest.main()
