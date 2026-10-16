import csv
import io

from fastapi.testclient import TestClient

from app.main import create_app
from app.metrics import CHINESE_PROTOCOL


RUN_A = "0123456789abcdef0123456789abcdef"
RUN_B = "abcdef0123456789abcdef0123456789"


def make_rows():
    rows = []
    for i in range(80):
        rows.append({"id": f"q-{i}", "category": "經濟金融", "question_type": "single-choice", "correct": True, "transport_ok": True, "parse_ok": True, "response": "A"})
    for i in range(5):
        rows.append({"id": f"wrong-{i}", "category": "經濟金融", "question_type": "single-choice", "correct": False, "transport_ok": True, "parse_ok": True, "response": "B"})
    for i in range(10):
        rows.append({"id": f"transport-{i}", "category": "銀行保險", "correct": False, "transport_ok": False, "parse_ok": False, "error_type": "timeout"})
    for i in range(5):
        rows.append({"id": f"parse-{i}", "category": "銀行保險", "correct": False, "transport_ok": True, "parse_ok": False, "error_type": "invalid_answer"})
    return rows


def make_run(run_id, model_name="gpt-oss-20b"):
    return {
        "schema_version": "evaluation-dashboard.v1",
        "source_id": f"mlflow:3:{run_id}",
        "source": "mlflow",
        "workspace": "evalhub-lab",
        "experiment_id": "3",
        "experiment_name": "chinese-mcq",
        "run_id": run_id,
        "job_id": None,
        "run_name": f"run-{run_id[:4]}",
        "state": "completed",
        "start_time_ms": 1000,
        "end_time_ms": 2000,
        "model": {"name": model_name, "revision": None},
        "benchmark": "qualbench",
        "provider_id": "chinese-mcq-v2",
        "protocol": CHINESE_PROTOCOL,
        "metrics_raw": {"overall_accuracy": 80, "valid_accuracy": 80 / 85 * 100, "attempted_count": 100, "valid_count": 85, "correct_count": 80, "transport_failures": 10, "parse_failures": 5, "category_accuracy.經濟金融": 94.1176},
        "params": {"benchmark_id": "qualbench", "model_name": model_name},
        "tags": {"protocol": CHINESE_PROTOCOL},
        "source_refs": {"mlflow_run_id": run_id},
        "completeness": {"summary_metrics": True, "question_artifact": "available"},
        "warnings": [],
    }


class FakeMLflow:
    def __init__(self):
        self.rows = make_rows()
        self.runs = [make_run(RUN_A), make_run(RUN_B, "gpt-oss-20b-r2")]

    def list_runs(self):
        return self.runs, {"complete": True, "loaded_runs": len(self.runs), "limit": 5000, "experiment_count": 1}

    def get_run(self, run_id):
        return next(x for x in self.runs if x["run_id"] == run_id)

    def load_summary(self, run_id):
        return ({"job_id": "job-1", "benchmark": "qualbench", "attempted": 100, "selected_ids": [r["id"] for r in self.rows], "run_metadata": {"prompt_protocol": CHINESE_PROTOCOL, "data_manifest_sha256": "manifest-123", "data_source_revision": "revision-123", "temperature": 0.0, "seed": 42}}, None)

    def list_artifacts(self, run_id):
        return [{"path": "summary.json", "is_dir": False, "size_bytes": 100}, {"path": "detailed_results.jsonl", "is_dir": False, "size_bytes": 1000}]

    def load_questions(self, run_id):
        return {"rows": self.rows, "available": True, "complete": True, "loaded_count": len(self.rows), "path": "detailed_results.jsonl", "warning": None}


class FakeEvalHub:
    def list_jobs(self):
        return [], {"complete": True, "loaded_jobs": 0, "total_jobs": 0, "pages_read": 1}


def client():
    return TestClient(create_app(FakeMLflow(), FakeEvalHub()))


def test_summary_and_error_filter_keep_original_denominator_and_score():
    response = client().get("/api/questions", params={"run_id": RUN_A, "outcome": "wrong"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert body["filtered_subset_accuracy"] is None
    assert body["expected_count"] == 100


def test_dimension_subset_accuracy_uses_all_subject_rows_even_when_inspecting_errors():
    c = client()
    response = c.get("/api/questions", params={"run_id": RUN_A, "subject": "經濟金融", "outcome": "wrong"})
    assert response.status_code == 200
    assert response.json()["total"] == 5
    assert response.json()["filtered_subset_accuracy"] == 94.117647


def test_failure_partition_and_valid_accuracy_denominator():
    rows = make_rows()
    outcomes = [
        "transport_failed" if not r["transport_ok"] else "parse_failed" if not r["parse_ok"] else "correct" if r["correct"] else "wrong"
        for r in rows
    ]
    assert outcomes.count("correct") == 80
    assert outcomes.count("wrong") == 5
    assert outcomes.count("transport_failed") == 10
    assert outcomes.count("parse_failed") == 5
    assert round(80 / 85 * 100, 2) == 94.12


def test_dimension_view_retains_chinese_labels_and_counts():
    response = client().get(f"/api/runs/{RUN_A}/dimensions", params={"dimension": "subject"})
    assert response.status_code == 200
    body = response.json()
    assert body["complete"] is True
    by_name = {x["name"]: x for x in body["items"]}
    assert by_name["經濟金融"]["attempted"] == 85
    assert by_name["經濟金融"]["correct"] == 80
    assert by_name["銀行保險"]["transport_failures"] == 10
    assert by_name["銀行保險"]["parse_failures"] == 5


def test_question_text_is_only_returned_after_explicit_detail_request():
    c = client()
    row_id = c.get("/api/questions", params={"run_id": RUN_A, "limit": 1}).json()["items"][0]["row_id"]
    detail = c.get(f"/api/questions/{RUN_A}/{row_id}", params={"include_text": "true"})
    assert detail.status_code == 200
    assert detail.json()["raw_response"] == "A"
    list_item = c.get("/api/questions", params={"run_id": RUN_A, "limit": 1}).json()["items"][0]
    assert "raw_response" not in list_item


def test_comparison_is_descriptive_when_parser_provenance_is_missing():
    response = client().get("/api/comparisons", params={"run_ids": f"{RUN_A},{RUN_B}"})
    assert response.status_code == 200
    body = response.json()
    assert body["fair_ranking_enabled"] is False
    assert body["comparisons"][0]["delta"] is None
    assert "Parser version unknown" in body["comparisons"][0]["reasons"]


def test_workspace_is_server_selected_and_csv_formula_payload_is_escaped():
    c = client()
    workspaces = c.get("/api/workspaces").json()
    assert [x["id"] for x in workspaces["items"]] == ["evalhub-lab"]
    c.app.state.mlflow_source.runs[0]["model"]["name"] = "=1+1"
    content = c.get("/api/exports/runs.csv").text
    rows = list(csv.DictReader(io.StringIO(content)))
    assert rows[0]["model"] == "'=1+1"


def test_printable_export_applies_the_same_state_filters_as_the_dashboard():
    response = client().get("/api/exports/summary.html", params={"state": "failed"})
    assert response.status_code == 200
    assert "No matching evaluations." in response.text
    assert response.headers["content-disposition"].startswith("inline;")
