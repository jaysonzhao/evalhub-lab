from app.metrics import CHINESE_PROTOCOL, finite_number, normalize_metrics, parse_bool
from app.sources import normalize_evalhub_job, normalize_mlflow_run


def test_false_strings_are_not_truthy():
    assert parse_bool("False") is False
    assert parse_bool("true") is True
    assert parse_bool("unknown") is None


def test_percent_mapping_preserves_zero_and_does_not_guess_ratio_scale():
    result = {x["key"]: x for x in normalize_metrics({"overall_accuracy": 0, "valid_accuracy": 0.5}, CHINESE_PROTOCOL)}
    assert result["overall_accuracy"]["value"] == 0
    assert result["overall_accuracy"]["unit"] == "%"
    assert result["valid_accuracy"]["value"] == 0.5
    assert result["valid_accuracy"]["unit"] == "%"


def test_non_finite_and_unmapped_metrics_are_not_scaled():
    assert finite_number(float("nan")) is None
    assert finite_number(float("inf")) is None
    result = {x["key"]: x for x in normalize_metrics({"new_metric": 0.4, "mean_ttft_ms": 0}, CHINESE_PROTOCOL)}
    assert result["new_metric"]["unit"] == "raw"
    assert result["new_metric"]["value"] == 0.4
    assert result["mean_ttft_ms"]["status"] == "measurement_unknown"
    assert result["mean_ttft_ms"]["raw_value"] == 0


def test_category_names_with_periods_are_preserved():
    metric = normalize_metrics({"category_accuracy.finance.and_banking": 72.5}, CHINESE_PROTOCOL)[0]
    assert metric["dimension"] == "finance.and_banking"
    assert metric["value"] == 72.5


def test_normalizes_raw_evalhub_shape_without_inventing_mlflow_link():
    job = normalize_evalhub_job(
        {
            "resource": {"id": "job-abc", "tenant": "evalhub-lab", "created_at": "2026-10-16T00:00:00Z"},
            "status": {"state": "completed"},
            "results": {"name": "sample", "model": {"name": "gpt-oss-20b"}, "benchmarks": [{"id": "hkmmlu", "provider_id": "chinese-mcq-v2", "metrics": {"overall_accuracy": 66.5}}]},
        },
        "evalhub-lab",
    )
    assert job["valid"] is True
    assert job["benchmarks"][0]["benchmark"] == "hkmmlu"
    assert job["benchmarks"][0]["mlflow_run_id"] is None


def test_raw_evalhub_measured_results_take_precedence_over_request_benchmarks():
    job = normalize_evalhub_job(
        {
            "resource": {"id": "job-abc"},
            "status": {"state": "completed"},
            "benchmarks": [{"id": "hkmmlu", "config": {"shots": 0}}],
            "results": {"benchmarks": [{"id": "hkmmlu", "metrics": {"overall_accuracy": 76}}]},
        },
        "evalhub-lab",
    )
    assert job["benchmarks"][0]["metrics_raw"]["overall_accuracy"] == 76


def test_normalizes_lab_export_shape_and_explicit_run_id():
    job = normalize_evalhub_job(
        {
            "job_id": "job-abc",
            "name": "sample",
            "model": {"name": "gpt-oss-20b"},
            "experiment": {"name": "sample-exp"},
            "benchmarks": [{"id": "cmmlu", "provider_id": "provider-id", "metrics": {"overall_accuracy": 55}, "mlflow_run_id": "0123456789abcdef0123456789abcdef"}],
        },
        "evalhub-lab",
    )
    assert job["benchmarks"][0]["mlflow_run_id"] == "0123456789abcdef0123456789abcdef"


def test_job_level_run_id_is_not_fanned_out_across_multiple_benchmarks():
    job = normalize_evalhub_job(
        {
            "resource": {"id": "job-abc"},
            "results": {"mlflow_run_id": "0123456789abcdef0123456789abcdef", "benchmarks": [{"id": "hkmmlu"}, {"id": "cmmlu"}]},
        },
        "evalhub-lab",
    )
    assert [x["mlflow_run_id"] for x in job["benchmarks"]] == [None, None]


def test_mlflow_run_normalizer_handles_sdk_like_rest_data():
    run = {
        "info": {"run_id": "0123456789abcdef0123456789abcdef", "experiment_id": "3", "status": "FINISHED", "start_time": 1000, "end_time": 2000},
        "data": {
            "metrics": [{"key": "overall_accuracy", "value": 80}],
            "params": [{"key": "benchmark_id", "value": "hkmmlu"}, {"key": "model_name", "value": "gpt-oss-20b"}],
            "tags": [{"key": "protocol", "value": CHINESE_PROTOCOL}],
        },
    }
    normalized = normalize_mlflow_run(run)
    assert normalized["benchmark"] == "hkmmlu"
    assert normalized["model"]["name"] == "gpt-oss-20b"
    assert normalized["metrics_raw"]["overall_accuracy"] == 80
    assert normalized["protocol"] == CHINESE_PROTOCOL
