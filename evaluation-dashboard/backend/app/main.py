"""FastAPI app for inspecting authorized MLflow and EvalHub evaluation data."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import threading
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from .metrics import CHINESE_PROTOCOL, COUNT_KEYS, finite_number, normalize_metrics, parse_bool
from .pipeline_runs import router as pipeline_runs_router
from .sources import MAX_QUESTION_ROWS, EvalHubSource, MLflowSource, SourceError

WORKSPACE = os.getenv("MLFLOW_WORKSPACE", "evalhub-lab")
SCHEMA_VERSION = "evaluation-dashboard.v1"
MAX_EXPORT_ROWS = 5_000
QUESTION_PAGE_SIZE = 100
STATIC_DIR = Path(__file__).parent / "static"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _redact_map(source: dict[str, Any]) -> dict[str, str]:
    blocked = ("token", "password", "secret", "api_key", "credential", "authorization")
    return {str(k): str(v)[:2_000] for k, v in source.items() if not any(term in str(k).lower() for term in blocked)}


def _state(value: Any) -> str:
    state = str(value or "unknown").lower()
    if state in {"finished", "completed", "complete", "succeeded", "success"}:
        return "completed"
    if state in {"failed", "partially_failed", "cancelled", "running", "pending"}:
        return state
    return state


def _score(run: dict[str, Any]) -> dict[str, Any] | None:
    for metric in run.get("metrics", []):
        if metric.get("key") == "overall_accuracy" and metric.get("value") is not None:
            return {"key": "overall_accuracy", "value": metric["value"], "unit": metric.get("unit", "raw"), "label": metric.get("label", "Overall accuracy"), "mapped": bool(metric.get("mapped")), "status": metric.get("status")}
    return None


def _metric_values(raw: dict[str, Any], protocol: str | None) -> list[dict[str, Any]]:
    return normalize_metrics(raw, protocol)


def _comparison(run_a: dict[str, Any], run_b: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if run_a.get("benchmark") != run_b.get("benchmark"):
        reasons.append("Different benchmark")
    if run_a.get("protocol") != run_b.get("protocol"):
        reasons.append("Scoring protocol differs")
    for field, label in (
        ("dataset_revision", "Dataset revision"),
        ("manifest_digest", "Manifest/correction digest"),
        ("sample_id_digest", "Selected question set"),
        ("prompt_version", "Prompt version"),
        ("parser_version", "Parser version"),
        ("scorer_version", "Scorer version"),
        ("denominator_definition", "Denominator definition"),
        ("shot_count", "Shot count"),
        ("temperature", "Temperature"),
        ("top_p", "Top-p"),
        ("token_budget", "Token budget"),
        ("timeout_policy", "Timeout policy"),
        ("retry_policy", "Retry policy"),
        ("seed_policy", "Seed policy"),
        ("concurrency", "Concurrency setting"),
    ):
        left, right = run_a.get("provenance", {}).get(field), run_b.get("provenance", {}).get(field)
        if left is None or right is None:
            reasons.append(f"{label} unknown")
        elif left != right:
            reasons.append(f"{label} differs")
    eligible = not reasons and _score(run_a) is not None and _score(run_b) is not None
    a_score, b_score = _score(run_a), _score(run_b)
    delta = None
    if eligible and a_score and b_score and a_score.get("unit") == "%" and b_score.get("unit") == "%":
        delta = round(float(b_score["value"]) - float(a_score["value"]), 6)
    return {
        "eligible": eligible,
        "status": "eligible" if eligible else "unknown_or_incompatible",
        "reasons": reasons,
        "baseline_run_id": run_a.get("run_id") or run_a.get("source_id"),
        "candidate_run_id": run_b.get("run_id") or run_b.get("source_id"),
        "delta": delta,
        "delta_unit": "percentage points" if delta is not None else None,
        "ranking_enabled": eligible,
    }


def _job_conflicts(job: dict[str, Any], run: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for benchmark in job.get("benchmarks", []):
        if benchmark.get("mlflow_run_id") != run.get("run_id"):
            continue
        for key, raw_value in benchmark.get("metrics_raw", {}).items():
            source_value = run.get("metrics_raw", {}).get(key)
            source_number, job_number = finite_number(source_value), finite_number(raw_value)
            if source_number is not None and job_number is not None and abs(source_number - job_number) > 1e-9:
                conflicts.append({"metric": key, "mlflow_value": source_value, "evalhub_value": raw_value})
    return conflicts


def _join_sources(mlflow_runs: list[dict[str, Any]], jobs: list[dict[str, Any]], protocol_by_run: dict[str, str] | None = None) -> list[dict[str, Any]]:
    protocol_by_run = protocol_by_run or {}
    runs = [dict(run) for run in mlflow_runs]
    by_id = {run.get("run_id"): run for run in runs if run.get("run_id")}
    by_job_id: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        job_id = run.get("job_id")
        if job_id:
            by_job_id.setdefault(str(job_id), []).append(run)
    linked_jobs: set[str] = set()
    evalhub_only: list[dict[str, Any]] = []
    duplicate_launch_run_ids: set[str] = set()

    for job in jobs:
        benchmarks = job.get("benchmarks", [])
        job_id = job.get("job_id")
        for benchmark in benchmarks:
            run_id = benchmark.get("mlflow_run_id")
            canonical_run = by_id.get(run_id) if run_id else None
            # EvalHub may create an empty MLflow run when a job is launched, then
            # write the measured result to a second run and return that run's ID.
            # Keep the result run as the dashboard row and hide only the empty,
            # still-open launch record when this explicit link proves the pair.
            if job_id and canonical_run and (canonical_run.get("metrics_raw") or benchmark.get("metrics_raw")):
                for candidate in by_job_id.get(str(job_id), []):
                    candidate_id = candidate.get("run_id")
                    same_benchmark = candidate.get("benchmark") == benchmark.get("benchmark")
                    if not same_benchmark and len(benchmarks) == 1:
                        same_benchmark = not candidate.get("benchmark")
                    if (
                        candidate_id
                        and candidate_id != run_id
                        and same_benchmark
                        and not candidate.get("metrics_raw")
                        and _state(candidate.get("state")) in {"running", "unknown"}
                    ):
                        duplicate_launch_run_ids.add(str(candidate_id))

            matches: list[tuple[dict[str, Any], str]] = []
            run = by_id.get(run_id) if run_id else None
            if run is not None:
                matches.append((run, "mlflow_run_id"))
            if job_id:
                candidates = [
                    candidate
                    for candidate in by_job_id.get(str(job_id), [])
                    if candidate.get("benchmark") == benchmark.get("benchmark")
                ]
                if not candidates and len(benchmarks) == 1:
                    candidates = [
                        candidate
                        for candidate in by_job_id.get(str(job_id), [])
                        if not candidate.get("benchmark")
                    ]
                existing_runs = {id(candidate) for candidate, _ in matches}
                matches.extend(
                    (candidate, "evaluation_job_id")
                    for candidate in candidates
                    if id(candidate) not in existing_runs
                )

            if matches:
                for run, linked_by in matches:
                    run["job_id"] = job_id
                    run["evalhub_state"] = job.get("state")
                    run["evalhub_name"] = job.get("name")
                    run.setdefault("source_refs", {})["evalhub_job_id"] = job_id
                    job_state = _state(benchmark.get("state") or job.get("state"))
                    if _state(run.get("state")) in {"running", "pending", "unknown"} and job_state in {"completed", "failed", "partially_failed", "cancelled"}:
                        # EvalHub owns the evaluation lifecycle; callbacks can
                        # leave the corresponding MLflow bookkeeping run open.
                        run["state"] = job_state
                    if not run.get("metrics_raw") and benchmark.get("metrics_raw"):
                        # Prefer measured benchmark metrics from the explicitly
                        # linked EvalHub result when the MLflow row is empty.
                        run["metrics_raw"] = dict(benchmark["metrics_raw"])
                    if (run.get("model") or {}).get("name") in (None, "", "Unknown model"):
                        model = job.get("model") or {}
                        if model.get("name"):
                            run["model"] = {"name": model["name"], "revision": model.get("revision")}
                    if not run.get("benchmark"):
                        run["benchmark"] = benchmark.get("benchmark")
                    if not run.get("provider_id"):
                        run["provider_id"] = benchmark.get("provider_id")
                    run["source_conflicts"] = _job_conflicts(job, run)
                    run.setdefault("source_provenance", []).append({"source": "evalhub", "job_id": job_id, "benchmark": benchmark.get("benchmark"), "linked_by": linked_by})
                linked_jobs.add(job.get("job_id", ""))
            else:
                protocol = job.get("protocol")
                raw_metrics = benchmark.get("metrics_raw", {})
                normalized = _metric_values(raw_metrics, protocol)
                evalhub_only.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "source_id": f"evalhub:{job.get('workspace')}:{job.get('job_id')}:{benchmark.get('provider_id')}:{benchmark.get('benchmark')}",
                        "source": "evalhub",
                        "workspace": job.get("workspace", WORKSPACE),
                        "experiment_id": str(job.get("experiment", {}).get("id")) if job.get("experiment", {}).get("id") is not None else None,
                        "experiment_name": job.get("experiment", {}).get("name"),
                        "run_id": None,
                        "job_id": job.get("job_id"),
                        "run_name": job.get("name"),
                        "state": _state(benchmark.get("state") or job.get("state")),
                        "start_time_ms": None,
                        "end_time_ms": None,
                        "model": {"name": (job.get("model") or {}).get("name") or "Unknown model", "revision": (job.get("model") or {}).get("revision")},
                        "benchmark": benchmark.get("benchmark"),
                        "provider_id": benchmark.get("provider_id"),
                        "protocol": protocol,
                        "metrics_raw": raw_metrics,
                        "metrics": normalized,
                        "score": next((m for m in normalized if m.get("key") == "overall_accuracy"), None),
                        "params": {},
                        "tags": {},
                        "source_refs": {"evalhub_job_id": job.get("job_id"), "mlflow_run_id": None},
                        "completeness": {"summary_metrics": True, "question_artifact": "unavailable"},
                        "provenance": {"protocol": protocol, "denominator_definition": None},
                        "warnings": ["This result is visible from EvalHub but has no explicit MLflow run link."],
                        "source_provenance": [{"source": "evalhub", "job_id": job.get("job_id"), "benchmark": benchmark.get("benchmark")}],
                    }
                )

    for run in runs:
        protocol = protocol_by_run.get(run.get("run_id")) or run.get("protocol")
        run["protocol"] = protocol
        run["state"] = _state(run.get("state"))
        run["metrics"] = _metric_values(run.get("metrics_raw", {}), protocol)
        run["score"] = _score(run)
        run["params"] = _redact_map(run.get("params", {}))
        run["tags"] = _redact_map(run.get("tags", {}))
        p = run.get("params", {})
        tags = run.get("tags", {})
        run["provenance"] = {
            "protocol": protocol,
            "dataset_revision": tags.get("dataset_revision") or p.get("dataset_revision"),
            "manifest_digest": tags.get("manifest_digest") or p.get("manifest_digest"),
            "sample_id_digest": tags.get("sample_id_digest") or p.get("sample_id_digest"),
            "prompt_version": tags.get("prompt_version") or p.get("prompt_version"),
            "parser_version": tags.get("parser_version") or p.get("parser_version"),
            "scorer_version": tags.get("scorer_version") or p.get("scorer_version"),
            "denominator_definition": tags.get("denominator_definition") or p.get("denominator_definition"),
            "shot_count": p.get("shot_count") or p.get("num_fewshot"),
            "temperature": p.get("temperature"),
            "top_p": p.get("top_p"),
            "token_budget": p.get("max_tokens") or p.get("token_budget"),
            "timeout_policy": p.get("timeout_policy"),
            "retry_policy": p.get("retry_policy"),
            "seed_policy": p.get("seed_policy") or p.get("seed"),
            "concurrency": p.get("concurrency"),
        }
        if protocol == CHINESE_PROTOCOL and run.get("benchmark"):
            run["provenance"].setdefault("denominator_definition", "all attempted questions, including invalid/failed attempts")
        run.setdefault("source_provenance", [{"source": "mlflow", "run_id": run.get("run_id")}])
        run.setdefault("source_conflicts", [])
    return [run for run in runs if str(run.get("run_id")) not in duplicate_launch_run_ids] + evalhub_only


def _filter_runs(runs: list[dict[str, Any]], *, benchmark: str | None = None, model: str | None = None, state: str | None = None, protocol: str | None = None, experiment_id: str | None = None, started_after: int | None = None, started_before: int | None = None, query: str | None = None) -> list[dict[str, Any]]:
    result = runs
    if benchmark:
        result = [r for r in result if r.get("benchmark") == benchmark]
    if model:
        result = [r for r in result if r.get("model", {}).get("name") == model]
    if state:
        result = [r for r in result if r.get("state") == state]
    if protocol:
        result = [r for r in result if r.get("protocol") == protocol]
    if experiment_id:
        result = [r for r in result if r.get("experiment_id") == experiment_id]
    if started_after is not None:
        result = [r for r in result if isinstance(r.get("start_time_ms"), (int, float)) and r["start_time_ms"] >= started_after]
    if started_before is not None:
        result = [r for r in result if isinstance(r.get("start_time_ms"), (int, float)) and r["start_time_ms"] < started_before]
    if query:
        needle = query.casefold()
        result = [r for r in result if needle in " ".join(str(x or "") for x in (r.get("run_name"), r.get("benchmark"), r.get("model", {}).get("name"), r.get("run_id"), r.get("job_id"))).casefold()]
    return sorted(result, key=lambda r: (r.get("end_time_ms") or r.get("start_time_ms") or 0), reverse=True)


def _csv_safe(value: Any) -> str:
    text = "" if value is None else str(value)
    stripped = text.lstrip()
    if stripped.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def _questions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return rows


def _outcome(row: dict[str, Any]) -> str:
    transport = parse_bool(row.get("transport_ok"))
    parsed = parse_bool(row.get("parse_ok"))
    correct = parse_bool(row.get("correct"))
    if transport is False:
        return "transport_failed"
    if parsed is False:
        return "parse_failed"
    if correct is True:
        return "correct"
    if correct is False:
        return "wrong"
    return "unknown"


def _question_projection(row: dict[str, Any], index: int, include_text: bool = False) -> dict[str, Any]:
    question_id = row.get("id") or row.get("question_id") or row.get("sample_id") or f"row-{index}"
    result: dict[str, Any] = {
        "row_id": str(index),
        "question_id": str(question_id)[:512],
        "subject": row.get("subject") or row.get("category") or row.get("domain"),
        "domain": row.get("domain") or row.get("category"),
        "question_type": row.get("question_type") or row.get("original_question_type") or row.get("effective_question_type"),
        "outcome": _outcome(row),
        "correct": parse_bool(row.get("correct")),
        "transport_ok": parse_bool(row.get("transport_ok")),
        "parse_ok": parse_bool(row.get("parse_ok")),
        "truncated": parse_bool(row.get("truncated")),
        "latency_ms": finite_number(row.get("latency_ms")),
        "usage": row.get("usage") if isinstance(row.get("usage"), dict) else None,
        "error_type": str(row.get("error_type"))[:500] if row.get("error_type") is not None else None,
        "source_artifact": None,
        "prompt_included": bool(row.get("question") or row.get("prompt") or row.get("input")),
    }
    if include_text:
        for target, keys in (
            ("question", ("question", "prompt", "input", "query")),
            ("options", ("options", "choices")),
            ("context", ("context", "passage")),
            ("gold", ("gold", "target", "answer")),
            ("prediction", ("prediction", "parsed_prediction")),
            ("raw_response", ("response", "raw_response", "completion")),
            ("reference_explanation", ("reference_explanation", "explanation")),
        ):
            value = next((row.get(k) for k in keys if row.get(k) is not None), None)
            if isinstance(value, (str, int, float, bool)) or value is None:
                result[target] = str(value)[:20_000] if value is not None else None
            elif isinstance(value, (list, dict)):
                result[target] = json.dumps(value, ensure_ascii=False)[:20_000]
    return result


def create_app(mlflow_source: MLflowSource | None = None, evalhub_source: EvalHubSource | None = None) -> FastAPI:
    app = FastAPI(title="Model Evaluation Dashboard API", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.mlflow_source = mlflow_source or MLflowSource()
    app.state.evalhub_source = evalhub_source or EvalHubSource()
    app.state.cache = {"expires": 0.0, "value": None}
    app.state.cache_lock = threading.Lock()
    app.state.question_cache = {}
    app.include_router(pipeline_runs_router)

    def catalog(force: bool = False) -> dict[str, Any]:
        with app.state.cache_lock:
            cached = app.state.cache
            if not force and cached["value"] is not None and cached["expires"] > time.monotonic():
                return cached["value"]
            sources: dict[str, Any] = {}
            mlflow_runs: list[dict[str, Any]] = []
            jobs: list[dict[str, Any]] = []
            try:
                mlflow_runs, completeness = app.state.mlflow_source.list_runs()
                sources["mlflow"] = {"status": "ok", "completeness": completeness}
            except SourceError as exc:
                sources["mlflow"] = {"status": "error", "kind": exc.kind, "message": exc.message}
            except Exception:
                sources["mlflow"] = {"status": "error", "kind": "source_unavailable", "message": "MLflow could not be reached. Check the connector and service health."}
            try:
                jobs, completeness = app.state.evalhub_source.list_jobs()
                sources["evalhub"] = {"status": "ok", "completeness": completeness}
            except SourceError as exc:
                sources["evalhub"] = {"status": "error", "kind": exc.kind, "message": exc.message}
            except Exception:
                sources["evalhub"] = {"status": "error", "kind": "source_unavailable", "message": "EvalHub could not be reached. Check the connector and service health."}
            protocol_by_run = {
                run.get("run_id"): run.get("protocol")
                for run in mlflow_runs
                if run.get("run_id") and run.get("protocol")
            }
            runs = _join_sources(mlflow_runs, jobs, protocol_by_run)
            warnings = []
            for name, health in sources.items():
                if health.get("status") != "ok":
                    warnings.append({"source": name, "kind": health.get("kind"), "message": health.get("message")})
            value = {
                "schema_version": SCHEMA_VERSION,
                "workspace": {"id": WORKSPACE, "label": WORKSPACE, "source_mode": "live", "authorization_mode": "project-scoped read-only service identity"},
                "captured_at": _utc_now(),
                "timezone_default": "Asia/Hong_Kong",
                "sources": sources,
                "runs": runs,
                "warnings": warnings,
                "data_completeness": "complete" if all(s.get("status") == "ok" and s.get("completeness", {}).get("complete", False) for s in sources.values()) else "partial",
            }
            app.state.cache = {"expires": time.monotonic() + 30, "value": value}
            return value

    def require_source(catalog_value: dict[str, Any], source: str) -> None:
        health = catalog_value["sources"].get(source, {})
        if health.get("status") != "ok" and not catalog_value.get("runs"):
            raise HTTPException(status_code=503, detail={"kind": health.get("kind", "source_unavailable"), "message": health.get("message", f"{source} is unavailable.")})

    @app.get("/api/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/health/ready")
    def ready() -> dict[str, Any]:
        return {"status": "ok", "workspace": WORKSPACE}

    @app.get("/api/me")
    def me(request: Request) -> dict[str, Any]:
        # Only the OAuth proxy service is exposed. It removes incoming identity headers and
        # supplies these headers after authentication.
        return {
            "user": request.headers.get("x-forwarded-user") or request.headers.get("x-auth-request-user") or "Authenticated project viewer",
            "groups": [x for x in request.headers.get("x-forwarded-groups", "").split(",") if x],
            "workspace": WORKSPACE,
            "authorization_mode": "project-scoped read-only service identity",
        }

    @app.get("/api/workspaces")
    def workspaces() -> dict[str, Any]:
        return {"items": [{"id": WORKSPACE, "label": WORKSPACE, "source": "live", "authorization_mode": "project-scoped read-only service identity"}], "selected": WORKSPACE}

    @app.get("/api/metrics/dictionary")
    def metric_dictionary() -> dict[str, Any]:
        from .metrics import MAPPING_VERSION, METRIC_HELP

        return {"schema_version": SCHEMA_VERSION, "mapping_version": MAPPING_VERSION, "protocol": CHINESE_PROTOCOL, "definitions": METRIC_HELP}

    @app.get("/api/runs")
    def runs(
        benchmark: str | None = Query(default=None, max_length=100),
        model: str | None = Query(default=None, max_length=200),
        state: str | None = Query(default=None, max_length=40),
        protocol: str | None = Query(default=None, max_length=200),
        experiment_id: str | None = Query(default=None, max_length=100),
        started_after: int | None = Query(default=None, ge=0),
        started_before: int | None = Query(default=None, ge=0),
        q: str | None = Query(default=None, max_length=120),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=MAX_EXPORT_ROWS),
        refresh: bool = False,
    ) -> dict[str, Any]:
        data = catalog(force=refresh)
        require_source(data, "mlflow")
        items = _filter_runs(data["runs"], benchmark=benchmark, model=model, state=state, protocol=protocol, experiment_id=experiment_id, started_after=started_after, started_before=started_before, query=q)
        page = items[offset : offset + limit]
        by_benchmark: dict[str, int] = {}
        for run in items:
            if run.get("benchmark"):
                by_benchmark[run["benchmark"]] = by_benchmark.get(run["benchmark"], 0) + 1
        filter_runs = data["runs"]
        filter_options = {
            "benchmarks": sorted({r["benchmark"] for r in filter_runs if r.get("benchmark")}),
            "models": sorted({r.get("model", {}).get("name") for r in filter_runs if r.get("model", {}).get("name")}),
            "states": sorted({r["state"] for r in filter_runs if r.get("state")}),
            "protocols": sorted({r["protocol"] for r in filter_runs if r.get("protocol")}),
            "experiments": sorted({(r.get("experiment_id"), r.get("experiment_name")) for r in filter_runs if r.get("experiment_id")}, key=lambda x: x[0]),
        }
        completed = sum(1 for r in items if r.get("state") == "completed")
        failed = sum(1 for r in items if r.get("state") in {"failed", "partially_failed", "cancelled"})
        unknown_comparability = sum(1 for r in items if not r.get("provenance", {}).get("sample_id_digest") or not r.get("provenance", {}).get("parser_version") or not r.get("provenance", {}).get("scorer_version"))
        return {
            "schema_version": SCHEMA_VERSION,
            "workspace": data["workspace"],
            "captured_at": data["captured_at"],
            "timezone_default": data["timezone_default"],
            "sources": data["sources"],
            "data_completeness": data["data_completeness"],
            "total": len(items),
            "overview": {"run_count": len(items), "completed_count": completed, "failed_or_cancelled_count": failed, "benchmark_count": len(by_benchmark), "unknown_comparability_count": unknown_comparability},
            "filter_options": filter_options,
            "offset": offset,
            "limit": limit,
            "items": page,
            "warnings": data["warnings"],
        }

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict[str, Any]:
        data = catalog()
        run = next((r for r in data["runs"] if r.get("run_id") == run_id), None)
        if run is None:
            raise HTTPException(status_code=404, detail={"kind": "not_found", "message": "Run is not available in the configured workspace."})
        try:
            detail = app.state.mlflow_source.get_run(run_id)
            summary, summary_warning = app.state.mlflow_source.load_summary(run_id)
            artifacts = app.state.mlflow_source.list_artifacts(run_id)
        except SourceError as exc:
            if run.get("source") == "evalhub":
                detail, summary, summary_warning, artifacts = run, None, exc.message, []
            else:
                raise HTTPException(status_code=404 if exc.kind == "unsupported_or_missing" else 503, detail={"kind": exc.kind, "message": exc.message}) from None
        result = dict(run)
        result.update({k: detail.get(k) for k in ("experiment_id", "experiment_name", "run_name", "state", "start_time_ms", "end_time_ms", "model", "benchmark", "provider_id", "protocol", "metrics_raw", "params", "tags") if detail.get(k) is not None})
        safe_summary = None
        if isinstance(summary, dict):
            metadata = summary.get("run_metadata") if isinstance(summary.get("run_metadata"), dict) else {}
            selected_ids = summary.get("selected_ids")
            sample_digest = None
            if isinstance(selected_ids, list) and all(isinstance(x, (str, int)) for x in selected_ids):
                canonical_ids = sorted(str(x) for x in selected_ids)
                sample_digest = hashlib.sha256(json.dumps(canonical_ids, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
            safe_metadata_keys = {
                "framework", "dataset", "model_name", "seed", "temperature", "max_tokens", "reasoning_effort",
                "request_timeout_seconds", "max_retries", "concurrency", "limit", "stratify", "per_stratum_limit",
                "per_subject_limit", "prompt_protocol", "data_manifest_sha256", "data_source_revision",
                "evaluation_label", "quality_gate_pass", "quality_thresholds", "observed_quality",
            }
            safe_metadata = {k: metadata[k] for k in safe_metadata_keys if k in metadata}
            safe_summary = {
                k: summary[k]
                for k in ("job_id", "benchmark", "model_name", "attempted", "valid", "correct", "transport_failures", "parse_failures", "truncated", "metrics")
                if k in summary
            }
            safe_summary["run_metadata"] = safe_metadata
            safe_summary["selected_id_count"] = len(selected_ids) if isinstance(selected_ids, list) else None
            safe_summary["sample_id_digest"] = sample_digest
            result["artifact_metadata"] = safe_summary
            metadata_protocol = metadata.get("prompt_protocol")
            manifest_digest = metadata.get("data_manifest_sha256")
            result["provenance"] = {
                **result.get("provenance", {}),
                "protocol": result.get("protocol") or metadata_protocol,
                "dataset_revision": metadata.get("data_source_revision"),
                "manifest_digest": manifest_digest,
                "sample_id_digest": sample_digest,
                "prompt_version": metadata.get("prompt_version"),
                "parser_version": metadata.get("parser_version"),
                "scorer_version": metadata.get("scorer_version"),
                "denominator_definition": metadata.get("denominator_definition"),
                "shot_count": metadata.get("shot_count") or metadata.get("num_fewshot"),
                "temperature": metadata.get("temperature"),
                "top_p": metadata.get("top_p"),
                "token_budget": metadata.get("max_tokens"),
                "timeout_policy": metadata.get("request_timeout_seconds"),
                "retry_policy": metadata.get("max_retries"),
                "seed_policy": metadata.get("seed"),
                "concurrency": metadata.get("concurrency"),
            }
            result["quality_gate_pass"] = parse_bool(metadata.get("quality_gate_pass"))
            if metadata_protocol and not result.get("protocol"):
                result["protocol"] = metadata_protocol
                result["metrics"] = _metric_values(result.get("metrics_raw", {}), metadata_protocol)
                result["score"] = _score(result)
            attempted = finite_number(summary.get("attempted") or summary.get("selected_count"))
            if attempted is not None and run.get("metrics_raw", {}).get("attempted_count") is not None and attempted != run["metrics_raw"].get("attempted_count"):
                result.setdefault("warnings", []).append(f"Source summary attempted count {attempted:g} differs from MLflow attempted_count {run['metrics_raw'].get('attempted_count')}.")
        result["artifacts"] = [
            {"path": x["path"], "size_bytes": x.get("size_bytes")}
            for x in artifacts
            if not x["is_dir"] and (Path(x["path"]).name == "summary.json" or Path(x["path"]).name in {"detailed_results.jsonl", "detailed_results.json", "detailed_results.csv"})
        ]
        result["completeness"] = {**result.get("completeness", {}), "summary_artifact": "available" if safe_summary else "unavailable", "summary_warning": summary_warning}
        result["metrics"] = _metric_values(result.get("metrics_raw", {}), result.get("protocol"))
        result["score"] = _score(result)
        if result.get("source_conflicts"):
            result.setdefault("warnings", []).append("EvalHub and MLflow reported different values for one or more linked metrics; both source values are retained.")
        return result

    @app.get("/api/runs/{run_id}/dimensions")
    def run_dimensions(run_id: str, dimension: str = Query(default="subject", pattern="^(subject|question_type)$")) -> dict[str, Any]:
        run, artifact = question_data(run_id)
        raw = run.get("metrics_raw", {})
        if artifact.get("available"):
            groups: dict[str, dict[str, int]] = {}
            for row in artifact["rows"]:
                key = row.get("question_type") or row.get("original_question_type") or row.get("effective_question_type") if dimension == "question_type" else row.get("subject") or row.get("category") or row.get("domain")
                if not isinstance(key, str) or not key:
                    continue
                counts = groups.setdefault(key, {"attempted": 0, "correct": 0, "valid": 0, "transport_failures": 0, "parse_failures": 0})
                counts["attempted"] += 1
                transport, parsed, correct = parse_bool(row.get("transport_ok")), parse_bool(row.get("parse_ok")), parse_bool(row.get("correct"))
                if transport is False:
                    counts["transport_failures"] += 1
                elif parsed is False:
                    counts["parse_failures"] += 1
                else:
                    counts["valid"] += 1
                if correct is True:
                    counts["correct"] += 1
            metric_prefix = "question_type_accuracy." if dimension == "question_type" else "category_accuracy."
            expected = finite_number(raw.get("attempted_count"))
            complete = bool(artifact.get("complete")) and expected is not None and expected == artifact.get("loaded_count")
            items = []
            for name, counts in groups.items():
                source_value = finite_number(raw.get(metric_prefix + name))
                items.append({
                    "name": name,
                    **counts,
                    "filtered_subset_accuracy": round(counts["correct"] / counts["attempted"] * 100, 6) if complete and counts["attempted"] else None,
                    "source_accuracy": source_value,
                    "source_metric_key": metric_prefix + name if metric_prefix + name in raw else None,
                    "complete": complete,
                })
            items.sort(key=lambda x: ((x["filtered_subset_accuracy"] if x["filtered_subset_accuracy"] is not None else -1), x["name"]))
            return {"run_id": run_id, "dimension": dimension, "available": True, "complete": complete, "expected_count": int(expected) if expected is not None else None, "loaded_count": artifact.get("loaded_count"), "items": items, "warning": artifact.get("warning") or (None if complete else "Filtered counts are provisional because artifact completeness could not be verified."), "metric_source": "MLflow summary metrics plus authorized question artifact"}

        metric_prefix = "question_type_accuracy." if dimension == "question_type" else "category_accuracy."
        items = []
        for key, value in raw.items():
            if key.startswith(metric_prefix):
                name = key[len(metric_prefix) :]
                metric = next((x for x in _metric_values({key: value}, run.get("protocol")) if x["key"] == key), {})
                items.append({"name": name, "attempted": None, "correct": None, "valid": None, "transport_failures": None, "parse_failures": None, "filtered_subset_accuracy": None, "source_accuracy": value, "source_metric_key": key, "complete": False, "label": metric.get("label")})
        return {"run_id": run_id, "dimension": dimension, "available": bool(items), "complete": False, "expected_count": raw.get("attempted_count"), "loaded_count": 0, "items": items, "warning": artifact.get("warning") or "Only source summary metrics are available; question-level counts cannot be verified.", "metric_source": "MLflow summary metrics"}

    def question_data(run_id: str, force: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        data = catalog()
        run = next((r for r in data["runs"] if r.get("run_id") == run_id), None)
        if run is None:
            raise HTTPException(status_code=404, detail={"kind": "not_found", "message": "Run is not available in the configured workspace."})
        cache_key = f"{WORKSPACE}:{run_id}"
        cached = app.state.question_cache.get(cache_key)
        if not force and cached and cached[0] > time.monotonic():
            return run, cached[1]
        try:
            result = app.state.mlflow_source.load_questions(run_id)
        except SourceError as exc:
            result = {"rows": [], "available": False, "complete": False, "loaded_count": 0, "path": None, "warning": exc.message}
        app.state.question_cache[cache_key] = (time.monotonic() + 60, result)
        return run, result

    @app.get("/api/questions")
    def questions(
        run_id: str = Query(..., min_length=8, max_length=128),
        subject: str | None = Query(default=None, max_length=200),
        question_type: str | None = Query(default=None, max_length=100),
        outcome: str | None = Query(default=None, max_length=40),
        q: str | None = Query(default=None, max_length=100),
        limit: int = Query(default=QUESTION_PAGE_SIZE, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=100_000),
    ) -> dict[str, Any]:
        run, artifact = question_data(run_id)
        if not artifact["available"]:
            return {"run_id": run_id, "available": False, "complete": False, "loaded_count": 0, "total": 0, "items": [], "warning": artifact.get("warning")}
        projected = [_question_projection(row, i, include_text=False) for i, row in enumerate(artifact["rows"])]
        subset = projected
        if subject:
            subset = [r for r in subset if r.get("subject") == subject or r.get("domain") == subject]
        if question_type:
            subset = [r for r in subset if r.get("question_type") == question_type]
        # Outcome and text search are inspection filters. They must never turn the
        # original run score or its dimension subset score into an error-only score.
        items = subset
        if outcome:
            items = [r for r in items if r.get("outcome") == outcome]
        if q:
            needle = q.casefold()
            source_rows = artifact["rows"]
            matched_indexes = {
                str(i)
                for i, row in enumerate(source_rows)
                if any(needle in str(row.get(field) or "").casefold() for field in ("question", "prompt", "input", "query", "id", "question_id", "category", "domain", "subject"))
            }
            items = [r for r in items if r.get("row_id") in matched_indexes]
        summary_attempted = finite_number(run.get("metrics_raw", {}).get("attempted_count"))
        complete = bool(artifact.get("complete")) and (summary_attempted is None or summary_attempted == artifact.get("loaded_count"))
        rows_page = items[offset : offset + limit]
        subset_accuracy = None
        if complete and (subject or question_type) and subset and all(r.get("correct") is not None for r in subset):
            subset_accuracy = round(sum(1 for r in subset if r.get("correct") is True) / len(subset) * 100, 6)
        return {
            "run_id": run_id,
            "available": True,
            "complete": complete,
            "loaded_count": artifact.get("loaded_count"),
            "expected_count": int(summary_attempted) if summary_attempted is not None else None,
            "total": len(items),
            "filtered_subset_accuracy": subset_accuracy,
            "items": rows_page,
            "warning": artifact.get("warning") or (None if complete else "Question artifact is incomplete or its row count differs from MLflow. Filtered accuracy is disabled."),
        }

    @app.get("/api/questions/{run_id}/{row_id}")
    def question_detail(run_id: str, row_id: int, include_text: bool = Query(default=False)) -> dict[str, Any]:
        if row_id < 0 or row_id >= MAX_QUESTION_ROWS:
            raise HTTPException(status_code=404, detail={"kind": "not_found", "message": "Question row is unavailable."})
        _, artifact = question_data(run_id)
        if not artifact["available"] or row_id >= len(artifact["rows"]):
            raise HTTPException(status_code=404, detail={"kind": "not_found", "message": artifact.get("warning") or "Question row is not available."})
        return _question_projection(artifact["rows"][row_id], row_id, include_text=include_text)

    @app.get("/api/comparisons")
    def comparisons(run_ids: str = Query(..., min_length=17, max_length=800)) -> dict[str, Any]:
        ids = [x for x in run_ids.split(",") if x]
        if not 2 <= len(ids) <= 6 or len(set(ids)) != len(ids):
            raise HTTPException(status_code=422, detail={"kind": "validation", "message": "Select between two and six distinct runs."})
        data = catalog()
        selected = []
        for run_id in ids:
            run = next((r for r in data["runs"] if r.get("run_id") == run_id or r.get("source_id") == run_id), None)
            if run is None:
                raise HTTPException(status_code=404, detail={"kind": "not_found", "message": "One or more selected runs are unavailable in this workspace."})
            selected.append(run_detail(run_id) if run.get("run_id") else run)
        matrix = []
        baseline = selected[0]
        for candidate in selected[1:]:
            matrix.append(_comparison(baseline, candidate))
        return {"schema_version": SCHEMA_VERSION, "workspace": WORKSPACE, "runs": selected, "baseline": baseline.get("run_id") or baseline.get("source_id"), "comparisons": matrix, "fair_ranking_enabled": all(x["eligible"] for x in matrix)}

    def filtered_for_export(benchmark: str | None, model: str | None, state: str | None, protocol: str | None, experiment_id: str | None, started_after: int | None, started_before: int | None, q: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        data = catalog()
        rows = _filter_runs(data["runs"], benchmark=benchmark, model=model, state=state, protocol=protocol, experiment_id=experiment_id, started_after=started_after, started_before=started_before, query=q)
        return data, rows[:MAX_EXPORT_ROWS]

    @app.get("/api/exports/runs.csv")
    def export_csv(
        benchmark: str | None = None,
        model: str | None = None,
        state: str | None = None,
        protocol: str | None = None,
        experiment_id: str | None = None,
        started_after: int | None = Query(default=None, ge=0),
        started_before: int | None = Query(default=None, ge=0),
        q: str | None = Query(default=None, max_length=120),
    ) -> Response:
        _, rows = filtered_for_export(benchmark, model, state, protocol, experiment_id, started_after, started_before, q)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["run_id", "job_id", "model", "benchmark", "state", "overall_accuracy", "unit", "attempted", "valid", "correct", "transport_failures", "parse_failures", "truncated", "protocol", "experiment_id", "start_time_utc", "end_time_utc", "workspace", "comparability"])
        for run in rows:
            metrics = run.get("metrics_raw", {})
            score = _score(run) or {}
            vals = [
                run.get("run_id"), run.get("job_id"), run.get("model", {}).get("name"), run.get("benchmark"), run.get("state"), score.get("value"), score.get("unit"),
                metrics.get("attempted_count"), metrics.get("valid_count"), metrics.get("correct_count"), metrics.get("transport_failures"), metrics.get("parse_failures"), metrics.get("truncated_responses"), run.get("protocol"), run.get("experiment_id"), run.get("start_time_ms"), run.get("end_time_ms"), run.get("workspace"),
                "eligible only when strict cohort provenance matches" if run.get("provenance", {}).get("sample_id_digest") else "unknown",
            ]
            writer.writerow([_csv_safe(v) for v in vals])
        return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=\"evaluation-runs.csv\"", "X-Export-Row-Limit": str(MAX_EXPORT_ROWS)})

    @app.get("/api/exports/runs.json")
    def export_json(
        benchmark: str | None = None,
        model: str | None = None,
        state: str | None = None,
        protocol: str | None = None,
        experiment_id: str | None = None,
        started_after: int | None = Query(default=None, ge=0),
        started_before: int | None = Query(default=None, ge=0),
        q: str | None = Query(default=None, max_length=120),
    ) -> dict[str, Any]:
        data, rows = filtered_for_export(benchmark, model, state, protocol, experiment_id, started_after, started_before, q)
        return {
            "schema_version": SCHEMA_VERSION,
            "source": "live",
            "workspace": data["workspace"],
            "captured_at_utc": data["captured_at"],
            "display_timezone": "Asia/Hong_Kong",
            "filter_state": {"benchmark": benchmark, "model": model, "state": state, "protocol": protocol, "experiment_id": experiment_id, "started_after": started_after, "started_before": started_before, "q": q},
            "metric_mapping_version": "chinese-mcq-zero-shot-strict-v1/1",
            "returned_count": len(rows),
            "row_limit": MAX_EXPORT_ROWS,
            "data_completeness": data["data_completeness"],
            "limitations": data["warnings"],
            "evaluations": rows,
        }

    @app.get("/api/exports/summary.html", response_class=HTMLResponse)
    def export_printable_summary(
        benchmark: str | None = None,
        model: str | None = None,
        state: str | None = None,
        protocol: str | None = None,
        experiment_id: str | None = None,
        started_after: int | None = Query(default=None, ge=0),
        started_before: int | None = Query(default=None, ge=0),
        q: str | None = Query(default=None, max_length=120),
    ) -> HTMLResponse:
        data, rows = filtered_for_export(benchmark, model, state, protocol, experiment_id, started_after, started_before, q)
        lines = []
        for run in rows:
            score = _score(run)
            score_text = f"{score['value']} {score['unit']}" if score else "Not available"
            lines.append(
                "<tr>" + "".join(
                    f"<td>{escape(str(value if value is not None else 'Unknown'))}</td>"
                    for value in (run.get("model", {}).get("name"), run.get("benchmark"), score_text, run.get("metrics_raw", {}).get("attempted_count"), run.get("state"), run.get("run_id") or run.get("job_id"))
                ) + "</tr>"
            )
        body = "".join(lines) or '<tr><td colspan="6">No matching evaluations.</td></tr>'
        html = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Evaluation summary</title>
<style>body{{font:15px system-ui,sans-serif;color:#19232e;margin:32px}}h1{{margin-bottom:4px}}.meta{{color:#526274}}table{{border-collapse:collapse;width:100%;margin-top:24px}}th,td{{border:1px solid #cbd5df;padding:9px;text-align:left;overflow-wrap:anywhere}}th{{background:#edf2f6}}@media print{{body{{margin:12mm}}}}</style>
<h1>Model evaluation summary</h1><p class="meta">Workspace {escape(WORKSPACE)} · Source {escape(data['workspace']['source_mode'])} · Captured {escape(data['captured_at'])} UTC · Display timezone Asia/Hong_Kong · Rows {len(rows)}</p>
<p>Accuracy units are shown as sourced; filtered question subsets do not replace the original run score. Fair comparisons require matched dataset, prompt, scoring, and denominator provenance.</p>
<table><thead><tr><th>Model</th><th>Benchmark</th><th>Overall accuracy</th><th>Attempted</th><th>State</th><th>Run / job ID</th></tr></thead><tbody>{body}</tbody></table></html>"""
        return HTMLResponse(html, headers={"Content-Disposition": "inline; filename=\"evaluation-summary.html\""})

    @app.get("/")
    def index() -> Response:
        index_path = STATIC_DIR / "index.html"
        if index_path.is_file():
            return FileResponse(index_path)
        return HTMLResponse("Dashboard frontend assets have not been built yet.", status_code=503)

    if STATIC_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets", check_dir=False), name="assets")
    return app


app = create_app()
