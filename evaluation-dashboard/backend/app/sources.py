"""Read-only, project-scoped adapters for MLflow and EvalHub."""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from .metrics import finite_number

MAX_RUNS = 5_000
MAX_EXPERIMENTS = 1_000
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_QUESTION_ROWS = 100_000
MAX_JOB_PAGES = 50
MAX_ARTIFACT_PATHS = 500
ALLOWED_QUESTION_ARTIFACTS = {"detailed_results.jsonl", "detailed_results.json", "detailed_results.csv"}


def _timestamp_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp() * 1000)


class SourceError(RuntimeError):
    """Safe source error for API responses; never contains credentials."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def _error_kind(status: int | None, message: str = "") -> tuple[str, str]:
    if status in (401,):
        return "authentication", "The source rejected the app identity. Check its projected service-account token."
    if status in (403,):
        return "authorization", "The app identity lacks read access to this project or source resource."
    if status in (404,):
        return "unsupported_or_missing", "The requested source object or API capability is unavailable."
    if status in (429, 500, 502, 503, 504):
        return "source_unavailable", "The source is temporarily unavailable; retry after a short delay."
    lowered = message.lower()
    if "certificate" in lowered or "ssl" in lowered or "tls" in lowered:
        return "tls", "TLS verification failed. Check the mounted cluster service CA bundle."
    return "source_unavailable", "The source could not be reached. Check the configured service and network policy."


def _safe_path(path: str) -> bool:
    parts = Path(path).parts
    return bool(path) and not Path(path).is_absolute() and ".." not in parts and "\\" not in path


def _dict_tags(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if v is not None}
    if isinstance(raw, list):
        return {str(x.get("key")): str(x.get("value")) for x in raw if isinstance(x, dict) and x.get("key") is not None and x.get("value") is not None}
    return {}


def normalize_mlflow_run(run: Any, experiment: Any | None = None) -> dict[str, Any]:
    """Normalize SDK and REST-shaped run records without inferring missing fields."""
    if isinstance(run, dict):
        info = run.get("info") or {}
        data = run.get("data") or {}
        metrics_raw = data.get("metrics") or {}
        if isinstance(metrics_raw, list):
            metrics = {m.get("key"): m.get("value") for m in metrics_raw if isinstance(m, dict) and m.get("key")}
        else:
            metrics = metrics_raw
        params = data.get("params") or {}
        if isinstance(params, list):
            params = {p.get("key"): p.get("value") for p in params if isinstance(p, dict) and p.get("key")}
        tags = _dict_tags(data.get("tags"))
        info_get = info.get
    else:
        info = run.info
        data = run.data
        metrics = data.metrics or {}
        params = data.params or {}
        tags = _dict_tags(data.tags)
        info_get = lambda key, default=None: getattr(info, key, default)

    run_id = info_get("run_id") or info_get("run_uuid")
    experiment_id = info_get("experiment_id")
    protocol = tags.get("protocol") or tags.get("eval.protocol")
    benchmark = params.get("benchmark_id") or tags.get("benchmark_id") or tags.get("benchmark")
    model_name = params.get("model_name") or tags.get("model") or tags.get("model_name")
    numeric_metrics = {
        str(key): number
        for key, value in metrics.items()
        if key and (number := finite_number(value)) is not None
    }
    return {
        "schema_version": "evaluation-dashboard.v1",
        "source_id": f"mlflow:{experiment_id}:{run_id}",
        "source": "mlflow",
        "workspace": os.getenv("MLFLOW_WORKSPACE", "evalhub-lab"),
        "experiment_id": str(experiment_id) if experiment_id is not None else None,
        "experiment_name": getattr(experiment, "name", None) if experiment is not None else None,
        "run_id": str(run_id) if run_id is not None else None,
        # EvalHub's MLflow runs carry the owning job ID even when the
        # benchmark result does not include an MLflow run ID to join on.
        "job_id": tags.get("evaluation_job_id") or tags.get("evalhub_job_id"),
        "run_name": info_get("run_name") or tags.get("mlflow.runName"),
        "state": str(info_get("status", "UNKNOWN")).lower(),
        "start_time_ms": info_get("start_time"),
        "end_time_ms": info_get("end_time"),
        "model": {"name": model_name or "Unknown model", "revision": tags.get("model_revision") or params.get("model_revision")},
        "benchmark": str(benchmark) if benchmark is not None else None,
        "provider_id": params.get("provider_id") or tags.get("provider_id"),
        "protocol": protocol,
        "metrics_raw": numeric_metrics,
        "params": {str(k): str(v) for k, v in params.items() if k is not None and v is not None},
        "tags": tags,
        "source_refs": {"mlflow_run_id": str(run_id) if run_id is not None else None},
        "completeness": {"summary_metrics": True, "question_artifact": "unknown"},
        "warnings": [],
    }


def normalize_evalhub_job(job: dict[str, Any], workspace: str) -> dict[str, Any]:
    """Support the inspected lab export and raw RHOAI 3.5 EvalHub response."""
    resource = job.get("resource") if isinstance(job.get("resource"), dict) else {}
    status = job.get("status") if isinstance(job.get("status"), dict) else {}
    results = job.get("results") if isinstance(job.get("results"), dict) else {}
    job_id = job.get("job_id") or job.get("id") or resource.get("id")
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", job_id):
        return {"valid": False, "reason": "invalid job identifier"}

    model = job.get("model") or results.get("model") or {}
    if not isinstance(model, dict):
        model = {}
    # Raw EvalHub responses can carry request specifications at job level
    # while results contains the measured metrics. Lab exports store measured
    # rows at job level, so prefer results only when it has a benchmark list.
    benchmarks = results.get("benchmarks") if isinstance(results.get("benchmarks"), list) else job.get("benchmarks")
    if not isinstance(benchmarks, list):
        benchmarks = status.get("benchmarks") if isinstance(status.get("benchmarks"), list) else []
    experiment = job.get("experiment") or results.get("experiment") or {}
    if not isinstance(experiment, dict):
        experiment = {}

    normalized_benchmarks = []
    for item in benchmarks:
        if not isinstance(item, dict):
            continue
        benchmark_id = item.get("id") or item.get("benchmark_id")
        if not isinstance(benchmark_id, str) or not benchmark_id:
            continue
        metrics = item.get("metrics") or {}
        if not isinstance(metrics, dict):
            metrics = {}
        # A job-level MLflow ID is unambiguous only for a single-benchmark job.
        mlflow_run_id = item.get("mlflow_run_id")
        if not mlflow_run_id and len(benchmarks) == 1:
            mlflow_run_id = results.get("mlflow_run_id") or job.get("mlflow_run_id")
        normalized_benchmarks.append(
            {
                "benchmark": benchmark_id,
                "provider_id": item.get("provider_id"),
                "state": item.get("status"),
                "metrics_raw": {str(k): v for k, v in metrics.items()},
                "mlflow_run_id": str(mlflow_run_id) if mlflow_run_id else None,
                "instance_id": item.get("instance_id") or item.get("id"),
            }
        )

    return {
        "valid": True,
        "schema_version": "rhoai-evalhub-3.5/raw-or-lab-export",
        "source_id": f"evalhub:{workspace}:{job_id}",
        "source": "evalhub",
        "workspace": workspace,
        "job_id": job_id,
        "name": job.get("name") or results.get("name") or resource.get("name"),
        "state": status.get("state") or job.get("state") or "unknown",
        "created_at": resource.get("created_at") or job.get("created_at"),
        "updated_at_ms": _timestamp_ms(resource.get("updated_at") or job.get("updated_at")),
        "model": {"name": model.get("name"), "url": model.get("url"), "revision": model.get("revision")},
        "experiment": experiment,
        "benchmarks": normalized_benchmarks,
        "source_refs": {"evalhub_job_id": job_id},
        "warnings": [],
    }


class MLflowSource:
    """MLflow SDK connector using the pod's Kubernetes namespaced credentials."""

    def __init__(self, client: Any | None = None):
        self._client = client

    def client(self) -> Any:
        if self._client is None:
            uri = os.getenv("MLFLOW_TRACKING_URI")
            if not uri:
                raise SourceError("configuration", "MLflow tracking URI is not configured.")
            try:
                from mlflow.tracking import MlflowClient
                self._client = MlflowClient(tracking_uri=uri)
            except Exception as exc:
                kind, message = _error_kind(None, str(exc))
                raise SourceError(kind, message) from None
        return self._client

    def list_experiments(self) -> tuple[list[Any], bool]:
        from mlflow.entities import ViewType

        client = self.client()
        experiments: list[Any] = []
        token = None
        pages = 0
        while pages < 20 and len(experiments) < MAX_EXPERIMENTS:
            page = client.search_experiments(
                view_type=ViewType.ACTIVE_ONLY,
                max_results=min(200, MAX_EXPERIMENTS - len(experiments)),
                page_token=token,
            )
            experiments.extend(page)
            token = getattr(page, "token", None)
            pages += 1
            if not token or not page:
                return experiments, True
        return experiments[:MAX_EXPERIMENTS], not bool(token)

    def list_runs(self, limit: int = MAX_RUNS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        from mlflow.entities import ViewType

        client = self.client()
        experiments, experiments_complete = self.list_experiments()
        normalized: list[dict[str, Any]] = []
        complete = experiments_complete
        pages_read = 0
        for experiment in experiments:
            token = None
            while len(normalized) < min(limit, MAX_RUNS) and pages_read < 200:
                page = client.search_runs(
                    experiment_ids=[experiment.experiment_id],
                    filter_string="",
                    run_view_type=ViewType.ACTIVE_ONLY,
                    max_results=min(200, min(limit, MAX_RUNS) - len(normalized)),
                    page_token=token,
                    order_by=["attribute.start_time DESC"],
                )
                normalized.extend(normalize_mlflow_run(run, experiment) for run in page)
                token = getattr(page, "token", None)
                pages_read += 1
                if not token or not page:
                    break
            if len(normalized) >= min(limit, MAX_RUNS) or pages_read >= 200:
                complete = False
                break
        return normalized, {
            "complete": complete,
            "loaded_runs": len(normalized),
            "limit": min(limit, MAX_RUNS),
            "experiment_count": len(experiments),
            "pages_read": pages_read,
            "api_version": "MLflow Tracking REST API 2.0 via SDK 3.11.1",
        }

    def get_run(self, run_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Fa-f0-9-]{8,128}", run_id):
            raise SourceError("validation", "Invalid run ID.")
        try:
            run = self.client().get_run(run_id)
            experiment = self.client().get_experiment(run.info.experiment_id)
            return normalize_mlflow_run(run, experiment)
        except SourceError:
            raise
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            kind, message = _error_kind(status, str(exc))
            raise SourceError(kind, message) from None

    def list_artifacts(self, run_id: str, max_paths: int = MAX_ARTIFACT_PATHS) -> list[dict[str, Any]]:
        if not re.fullmatch(r"[A-Fa-f0-9-]{8,128}", run_id):
            raise SourceError("validation", "Invalid run ID.")
        client = self.client()
        paths: list[dict[str, Any]] = []
        queue: list[tuple[str, int]] = [("", 0)]
        while queue and len(paths) < max_paths:
            path, depth = queue.pop(0)
            try:
                entries = client.list_artifacts(run_id, path or None)
            except TypeError:
                entries = client.list_artifacts(run_id, path) if path else client.list_artifacts(run_id)
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                kind, message = _error_kind(status, str(exc))
                raise SourceError(kind, message) from None
            for entry in entries:
                artifact_path = getattr(entry, "path", "")
                if not _safe_path(artifact_path):
                    continue
                is_dir = bool(getattr(entry, "is_dir", False))
                size = getattr(entry, "file_size", None)
                info = {"path": artifact_path, "is_dir": is_dir, "size_bytes": size}
                paths.append(info)
                if is_dir and depth < 2:
                    queue.append((artifact_path, depth + 1))
                if len(paths) >= max_paths:
                    break
        return paths

    def _download_bytes(self, run_id: str, artifact_path: str, byte_limit: int) -> bytes:
        if not _safe_path(artifact_path):
            raise SourceError("validation", "Artifact path is not permitted.")
        entries = self.list_artifacts(run_id)
        match = next((item for item in entries if item["path"] == artifact_path and not item["is_dir"]), None)
        if match is None:
            raise SourceError("artifact_unavailable", "The requested evaluation artifact is not listed for this run.")
        size = match.get("size_bytes")
        if isinstance(size, int) and size > byte_limit:
            raise SourceError("artifact_limit", "The evaluation artifact exceeds the configured size limit.")
        try:
            with tempfile.TemporaryDirectory(prefix="eval-dashboard-") as directory:
                downloaded = self.client().download_artifacts(run_id, artifact_path, dst_path=directory)
                content_path = Path(downloaded)
                if not content_path.is_file():
                    raise SourceError("artifact_unavailable", "The evaluation artifact download did not produce a file.")
                if content_path.stat().st_size > byte_limit:
                    raise SourceError("artifact_limit", "The evaluation artifact exceeds the configured size limit.")
                return content_path.read_bytes()
        except SourceError:
            raise
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            kind, message = _error_kind(status, str(exc))
            raise SourceError(kind, message) from None

    def load_summary(self, run_id: str) -> tuple[dict[str, Any] | None, str | None]:
        try:
            artifacts = self.list_artifacts(run_id)
            item = next((x for x in artifacts if not x["is_dir"] and Path(x["path"]).name == "summary.json"), None)
            if not item:
                return None, "Summary artifact is not included in this run."
            content = self._download_bytes(run_id, item["path"], 2 * 1024 * 1024)
            parsed = json.loads(content)
            return (parsed, None) if isinstance(parsed, dict) else (None, "Summary artifact is not a JSON object.")
        except SourceError as exc:
            if exc.kind in {"artifact_unavailable", "unsupported_or_missing"}:
                return None, exc.message
            raise
        except (ValueError, json.JSONDecodeError):
            return None, "Summary artifact is not valid JSON."

    def load_questions(self, run_id: str) -> dict[str, Any]:
        artifacts = self.list_artifacts(run_id)
        item = next(
            (
                x
                for x in artifacts
                if not x["is_dir"] and Path(x["path"]).name in ALLOWED_QUESTION_ARTIFACTS
            ),
            None,
        )
        if not item:
            return {"rows": [], "available": False, "complete": False, "loaded_count": 0, "path": None, "warning": "Per-question results are not included in this run's saved artifacts."}
        data = self._download_bytes(run_id, item["path"], MAX_ARTIFACT_BYTES)
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        try:
            if item["path"].endswith(".jsonl"):
                for line_number, line in enumerate(data.splitlines(), 1):
                    if line_number > MAX_QUESTION_ROWS:
                        warnings.append(f"Artifact row limit reached at {MAX_QUESTION_ROWS:,} rows.")
                        break
                    if len(line) > 256 * 1024:
                        warnings.append(f"Skipped oversized row {line_number}.")
                        continue
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        warnings.append(f"Skipped malformed JSONL row {line_number}.")
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
            elif item["path"].endswith(".json"):
                parsed = json.loads(data)
                source_rows = parsed if isinstance(parsed, list) else parsed.get("results", []) if isinstance(parsed, dict) else []
                if not isinstance(source_rows, list):
                    source_rows = []
                for row in source_rows[:MAX_QUESTION_ROWS]:
                    if isinstance(row, dict):
                        rows.append(row)
                if len(source_rows) > MAX_QUESTION_ROWS:
                    warnings.append(f"Artifact row limit reached at {MAX_QUESTION_ROWS:,} rows.")
            elif item["path"].endswith(".csv"):
                import io

                for row_number, row in enumerate(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))), 1):
                    if row_number > MAX_QUESTION_ROWS:
                        warnings.append(f"Artifact row limit reached at {MAX_QUESTION_ROWS:,} rows.")
                        break
                    rows.append(row)
        except (ValueError, UnicodeDecodeError, csv.Error):
            warnings.append("Question artifact could not be parsed completely.")
        return {
            "rows": rows,
            "available": True,
            "complete": not warnings,
            "loaded_count": len(rows),
            "path": item["path"],
            "warning": " ".join(warnings) or None,
        }


class EvalHubSource:
    """Read-only RHOAI 3.5 EvalHub REST connector for one configured tenant."""

    def __init__(self, base_url: str | None = None, tenant: str | None = None, token_path: str | None = None, ca_bundle: str | None = None, session: requests.Session | None = None):
        self.base_url = (base_url or os.getenv("EVALHUB_BASE_URL", "")).rstrip("/")
        self.tenant = tenant or os.getenv("EVALHUB_TENANT", "evalhub-lab")
        self.token_path = token_path or os.getenv("SERVICE_ACCOUNT_TOKEN_PATH", "/var/run/secrets/kubernetes.io/serviceaccount/token")
        self.ca_bundle = ca_bundle or os.getenv("SERVICE_CA_BUNDLE", "/etc/ca/service-ca.crt")
        self.session = session or requests.Session()

    def _token(self) -> str:
        try:
            token = Path(self.token_path).read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if not token:
            raise SourceError("authentication", "The projected service-account token is unavailable.")
        return token

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.base_url:
            raise SourceError("configuration", "EvalHub service URL is not configured.")
        url = f"{self.base_url}/api/v1/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self._token()}", "X-Tenant": self.tenant, "Accept": "application/json"}
        verify: str | bool = self.ca_bundle if Path(self.ca_bundle).is_file() else True
        last_kind, last_message = "source_unavailable", "The EvalHub source could not be reached."
        for attempt in range(3):
            try:
                response = self.session.get(url, headers=headers, params=params, timeout=(4, 20), verify=verify, allow_redirects=False)
                if response.status_code == 200:
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise SourceError("unsupported_version", "EvalHub returned an unexpected response shape.")
                    return payload
                last_kind, last_message = _error_kind(response.status_code, response.text[:200])
                if response.status_code not in (429, 500, 502, 503, 504):
                    raise SourceError(last_kind, last_message)
            except SourceError:
                raise
            except requests.exceptions.SSLError as exc:
                last_kind, last_message = _error_kind(None, str(exc))
                raise SourceError(last_kind, last_message) from None
            except (requests.Timeout, requests.ConnectionError, ValueError) as exc:
                last_kind, last_message = _error_kind(None, str(exc))
            if attempt < 2:
                time.sleep(0.25 * (2**attempt))
        raise SourceError(last_kind, last_message)

    def list_jobs(self, page_size: int = 100) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        offset = 0
        total: int | None = None
        for page_number in range(MAX_JOB_PAGES):
            payload = self._get("evaluations/jobs", {"limit": min(max(page_size, 1), 100), "offset": offset})
            items = payload.get("items")
            if not isinstance(items, list):
                # Some lab export endpoints return the list directly under jobs.
                items = payload.get("jobs")
            if not isinstance(items, list):
                raise SourceError("unsupported_version", "EvalHub job list did not include an items/jobs array.")
            total_value = payload.get("total_count") or payload.get("total")
            if isinstance(total_value, int):
                total = total_value
            for item in items:
                if isinstance(item, dict):
                    record = normalize_evalhub_job(item, self.tenant)
                    if record.get("valid"):
                        normalized.append(record)
            offset += len(items)
            if not items or len(items) < min(max(page_size, 1), 100) or (total is not None and offset >= total):
                return normalized, {"complete": True, "loaded_jobs": len(normalized), "total_jobs": total, "pages_read": page_number + 1, "api_version": "RHOAI 3.5 EvalHub REST API v1"}
        return normalized, {"complete": False, "loaded_jobs": len(normalized), "total_jobs": total, "pages_read": MAX_JOB_PAGES, "api_version": "RHOAI 3.5 EvalHub REST API v1"}

    def get_job(self, job_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", job_id):
            raise SourceError("validation", "Invalid EvalHub job ID.")
        return normalize_evalhub_job(self._get(f"evaluations/jobs/{job_id}"), self.tenant)
