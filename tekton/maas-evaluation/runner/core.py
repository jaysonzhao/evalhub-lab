"""Safe, checkpointed Tekton-to-EvalHub workflow for Chinese MCQ benchmarks.

The runner uses only Python's standard library. It never reads the MaaS API key;
EvalHub resolves the tenant-scoped Kubernetes Secret named by the request.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .ledger import ConfigMapLedger, LedgerError


DATASET_ORDER = ("hkmmlu", "tmmluplus", "cmmlu", "qualbench")
TERMINAL_STATES = {"completed", "failed", "cancelled", "partially_failed"}
QUALBENCH_CORRECTION_SHA256 = "17bdc1f3d4814b763a1801bf04aa8c934e2c27ea9f05c5a97aad8ea2cc2eee8d"
QUALBENCH_EXCLUDED_IDS = [
    "qualbench/14861", "qualbench/15175", "qualbench/4224", "qualbench/4229",
    "qualbench/4233", "qualbench/4234", "qualbench/4253", "qualbench/4354",
    "qualbench/8131", "qualbench/8132", "qualbench/8133",
]
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")


class EvaluationError(RuntimeError):
    """Safe operational error; API bodies and credential values are omitted."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_datasets(values: list[str], *, expand_all: bool = False) -> list[str]:
    """Validate a typed selection and return its canonical execution order."""
    if not values:
        raise EvaluationError("Select at least one dataset; an empty selection is not 'all'.")
    normalized = [str(value).strip().lower() for value in values]
    if any(not value for value in normalized):
        raise EvaluationError("Dataset selection contains an empty value.")
    if "all" in normalized:
        if not expand_all or len(normalized) != 1:
            raise EvaluationError("'all' is a launcher convenience and cannot be mixed with dataset IDs.")
        normalized = list(DATASET_ORDER)
    unknown = sorted(set(normalized) - set(DATASET_ORDER))
    if unknown:
        raise EvaluationError(f"Unknown dataset ID(s): {', '.join(unknown)}")
    if len(normalized) != len(set(normalized)):
        raise EvaluationError("Dataset selection contains duplicates.")
    selected = set(normalized)
    return [dataset for dataset in DATASET_ORDER if dataset in selected]


def parse_dataset_csv(value: str, *, expand_all: bool = True) -> list[str]:
    """Parse the launcher's comma-separated convenience without dropping empties."""
    if value is None or value == "":
        raise EvaluationError("Pass --datasets with one or more dataset IDs.")
    return normalize_datasets(value.split(","), expand_all=expand_all)


def validate_https_url(value: str, *, label: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment
    ):
        raise EvaluationError(f"{label} must be an HTTPS URL without userinfo, query, or fragment.")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise EvaluationError(f"{label} has an invalid port.") from exc
    return parsed


def validate_endpoint(endpoint: str, prefixes: list[str]) -> str:
    parsed = validate_https_url(endpoint, label="MaaS endpoint")
    if not prefixes:
        raise EvaluationError("No approved MaaS endpoint prefixes are configured.")
    matched = False
    for prefix in prefixes:
        approved = validate_https_url(prefix, label="Approved MaaS prefix")
        base = prefix.rstrip("/")
        if (
            parsed.scheme.lower() == approved.scheme.lower()
            and parsed.hostname.lower() == approved.hostname.lower()
            and parsed.port == approved.port
            and (parsed.path.rstrip("/") == approved.path.rstrip("/")
                 or parsed.path.startswith(base + "/"))
        ):
            matched = True
            break
    if not matched:
        raise EvaluationError("MaaS endpoint is outside the configured host and path allowlist.")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _valid_secret_name(name: str) -> bool:
    return bool(DNS_LABEL_RE.fullmatch(name))


def _valid_claim_name(name: str) -> bool:
    return bool(DNS_LABEL_RE.fullmatch(name))


def validate_endpoint_secret_binding(endpoint: str, secret_name: str, bindings: list[dict[str, Any]]) -> None:
    if not _valid_secret_name(secret_name):
        raise EvaluationError("model-secret-name must be a valid Kubernetes Secret name.")
    for binding in bindings:
        prefix = binding.get("endpoint_prefix")
        if not isinstance(prefix, str):
            continue
        base = prefix.rstrip("/")
        if endpoint != base and not endpoint.startswith(base + "/"):
            continue
        allowed_names = binding.get("secret_names", [])
        if secret_name in allowed_names:
            return
        for pattern in binding.get("secret_name_patterns", []):
            if re.fullmatch(str(pattern), secret_name):
                return
    raise EvaluationError("The selected Secret is not approved for this MaaS endpoint.")


def profile_parameters(profile: str, dataset: str, dataset_info: dict[str, Any]) -> dict[str, Any]:
    """Return reviewed adapter parameters; full QualBench is always disallowed."""
    if profile not in {"smoke", "coverage", "full"}:
        raise EvaluationError("profile must be smoke, coverage, or full.")
    if dataset == "qualbench" and profile == "full":
        raise EvaluationError(
            "Full QualBench is disabled: the configured data is the approved diagnostic subset."
        )
    base: dict[str, Any] = {
        "seed": 42,
        "temperature": 0,
        "max_tokens": 128,
        "reasoning_effort": "low",
        "concurrency": 2,
        "data_manifest": "manifest.json",
        "data_manifest_sha256": dataset_info["manifest_sha256"],
        "quality_thresholds": {
            "transport_failures": 0,
            "parse_failures": 0,
            "truncated_responses": 0,
        },
    }
    if profile == "smoke":
        base["limit"] = min(5, int(dataset_info["row_count"]))
    elif profile == "coverage":
        if dataset == "qualbench":
            base.update({"stratify": "domain_question_type", "per_stratum_limit": 1})
        else:
            base["per_subject_limit"] = 1
    return base


def expected_attempts(profile: str, dataset: str, dataset_info: dict[str, Any]) -> int:
    if profile == "smoke":
        return min(5, int(dataset_info["row_count"]))
    if profile == "coverage":
        return int(dataset_info["coverage_unit_count"])
    return int(dataset_info["row_count"])


def validate_data_profile(profile_doc: dict[str, Any], provider_id: str, selected: list[str]) -> None:
    if profile_doc.get("schema_version") != "maas-eval-data-profile.v1":
        raise EvaluationError("Dataset profile schema/version is missing or unsupported.")
    if profile_doc.get("provider_id") != provider_id:
        raise EvaluationError("Dataset profile provider ID does not match runtime configuration.")
    manifest_sha = profile_doc.get("manifest_sha256")
    if not isinstance(manifest_sha, str) or not SHA256_RE.fullmatch(manifest_sha):
        raise EvaluationError("Dataset profile does not contain a valid prepared-data manifest hash.")
    entries = profile_doc.get("datasets")
    if not isinstance(entries, dict):
        raise EvaluationError("Dataset profile has no benchmark entries.")
    for dataset in selected:
        item = entries.get(dataset)
        if not isinstance(item, dict):
            raise EvaluationError(f"The selected dataset {dataset} is unavailable in the prepared-data profile.")
        if not isinstance(item.get("row_count"), int) or item["row_count"] <= 0:
            raise EvaluationError(f"The selected dataset {dataset} has no validated rows.")
        if item.get("manifest_sha256") != manifest_sha:
            raise EvaluationError(f"The selected dataset {dataset} has a mismatched manifest hash.")
        if not SHA256_RE.fullmatch(str(item.get("canonical_jsonl_sha256", ""))):
            raise EvaluationError(f"The selected dataset {dataset} has no canonical JSONL checksum.")
        if not item.get("source_revision"):
            raise EvaluationError(f"The selected dataset {dataset} has no pinned source revision.")
        if not isinstance(item.get("coverage_unit_count"), int) or item["coverage_unit_count"] <= 0:
            raise EvaluationError(f"The selected dataset {dataset} has no validated coverage count.")
        if dataset == "qualbench":
            audit = item.get("audit") or {}
            if (
                item.get("row_count") != 17305
                or audit.get("source_rows") != 17316
                or audit.get("evaluated_rows") != 17305
                or audit.get("excluded_ids") != QUALBENCH_EXCLUDED_IDS
                or item.get("correction_manifest_sha256") != QUALBENCH_CORRECTION_SHA256
                or "diagnostic subset" not in str(item.get("evaluation_label", "")).lower()
                or item.get("coverage_unit_count") != 17
            ):
                raise EvaluationError("QualBench profile does not match the approved diagnostic manifest.")


def metric_map(raw: Any) -> dict[str, float | int | str | None]:
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    result: dict[str, float | int | str | None] = {}
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = item.get("metric_name") or item.get("key") or item.get("name")
            if key is not None:
                result[str(key)] = item.get("metric_value", item.get("value"))
    return result


def dashboard_metric_rows(raw: Any) -> list[dict[str, Any]]:
    """Preserve raw metric values and attach only reviewed scale/unit labels."""
    rows = []
    for key, value in metric_map(raw).items():
        if key in {"overall_accuracy", "valid_accuracy", "request_success_rate", "parse_success_rate"} \
                or key in {"subject_macro_accuracy", "domain_macro_accuracy"} \
                or key.startswith("category_accuracy.") or key.startswith("question_type_accuracy."):
            unit, mapped = "%", True
        elif key in {"attempted_count", "valid_count", "correct_count", "transport_failures", "parse_failures", "truncated_responses"}:
            unit, mapped = "questions", True
        else:
            unit, mapped = "raw", False
        numeric: float | None
        try:
            numeric = float(value) if not isinstance(value, bool) and value is not None else None
        except (TypeError, ValueError):
            numeric = None
        valid_scale = numeric is not None and numeric == numeric and abs(numeric) != float("inf")
        if unit == "%" and valid_scale and not 0 <= numeric <= 100:
            unit, mapped = "raw", False
        if unit == "questions" and valid_scale and (numeric < 0 or not numeric.is_integer()):
            unit, mapped = "raw", False
        rows.append({
            "key": key,
            "raw_value": value,
            "value": (int(numeric) if unit == "questions" and valid_scale else numeric) if valid_scale else None,
            "unit": unit,
            "status": "available" if valid_scale else "invalid",
            "mapped": mapped,
        })
    return rows


def assess_dataset_result(
    *, profile: str, dataset: str, dataset_info: dict[str, Any], state: str,
    raw_metrics: Any, quality_thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    metrics = metric_map(raw_metrics)

    def number(key: str) -> float | None:
        value = metrics.get(key)
        if isinstance(value, bool):
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        return numeric if numeric == numeric and abs(numeric) != float("inf") else None

    expected = expected_attempts(profile, dataset, dataset_info)
    attempted = number("attempted_count")
    valid = number("valid_count")
    correct = number("correct_count")
    transport = number("transport_failures")
    parse = number("parse_failures")
    truncated = number("truncated_responses")
    required = {
        "attempted_count": attempted,
        "valid_count": valid,
        "correct_count": correct,
        "transport_failures": transport,
        "parse_failures": parse,
        "truncated_responses": truncated,
    }
    reasons: list[str] = []
    data_reasons: list[str] = []
    if state != "completed":
        reasons.append(f"EvalHub state is {state or 'unknown'}.")
    if any(value is None for value in required.values()):
        reason = "One or more required count/quality metrics are missing or non-numeric."
        reasons.append(reason)
        data_reasons.append(reason)
    else:
        if attempted != expected:
            reason = f"attempted_count={attempted:g}; expected {expected}."
            reasons.append(reason)
            data_reasons.append(reason)
        if not (0 <= correct <= valid <= attempted):
            reason = "Counts are inconsistent: require 0 <= correct <= valid <= attempted."
            reasons.append(reason)
            data_reasons.append(reason)
        if transport + parse + valid != attempted:
            reason = "Attempted rows do not reconcile with valid, transport-failed, and parse-failed counts."
            reasons.append(reason)
            data_reasons.append(reason)
        if transport != 0 or parse != 0 or truncated != 0:
            reason = "Smoke/comparability quality gate requires zero transport, parse, and truncation failures."
            reasons.append(reason)
            data_reasons.append(reason)

    thresholds = quality_thresholds or {}
    accuracy = number("overall_accuracy")
    model_status = "not_configured"
    if "overall_accuracy_min" in thresholds:
        threshold = float(thresholds["overall_accuracy_min"])
        if accuracy is None:
            model_status = "failed"
            reasons.append("Configured overall_accuracy threshold cannot be evaluated because the metric is missing.")
        elif not 0 <= threshold <= 100:
            model_status = "failed"
            reasons.append("Configured accuracy threshold is outside the reviewed 0-100 percent scale.")
        elif accuracy < threshold:
            model_status = "failed"
            reasons.append(f"overall_accuracy={accuracy:g}% is below configured {threshold:g}%.")
        else:
            model_status = "passed"

    return {
        "dataset": dataset,
        "execution_outcome": "passed" if state == "completed" else "failed",
        "data_quality_outcome": "passed" if not data_reasons else "failed",
        "model_quality_outcome": model_status,
        "artifact_outcome": "pending",
        "expected_attempts": expected,
        "metrics": metrics,
        "reasons": reasons,
    }


def _unwrap_items(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if isinstance(response, dict):
        items = response.get("items") or response.get("data")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        return [response]
    return []


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class EvalHubClient:
    """Small REST client that refreshes the projected SA token on every request."""

    def __init__(
        self, base_url: str, tenant: str, token_file: Path,
        ca_bundle: str | None = None, opener: Any | None = None,
    ):
        validate_https_url(base_url, label="EvalHub URL")
        self.base_url = base_url.rstrip("/")
        self.tenant = tenant
        self.token_file = token_file
        context = ssl.create_default_context(cafile=ca_bundle or None)
        self.opener = opener or urllib.request.build_opener(
            NoRedirectHandler(), urllib.request.HTTPSHandler(context=context)
        )

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, *, retries: int = 3) -> Any:
        url = self.base_url + path
        encoded = canonical_json(body) if body is not None else None
        attempts = max(1, retries if method.upper() == "GET" else 1)
        for attempt in range(attempts):
            try:
                token = self.token_file.read_text(encoding="utf-8").strip()
                if not token:
                    raise EvaluationError("Projected EvalHub service-account token is empty.")
                headers = {
                    "Authorization": f"Bearer {token}",
                    "X-Tenant": self.tenant,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "maas-evaluation-tekton/1.0",
                }
                request = urllib.request.Request(url, data=encoded, headers=headers, method=method.upper())
                with self.opener.open(request, timeout=30) as response:
                    data = response.read()
                if not data:
                    return {}
                try:
                    return json.loads(data)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise EvaluationError("EvalHub returned an invalid JSON response.") from exc
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                if method.upper() == "GET" and status in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                    time.sleep(min(8.0, 0.5 * (2**attempt)) + random.uniform(0.0, 0.25))
                    continue
                raise EvaluationError(f"EvalHub returned HTTP {status} for {method.upper()} {path}.") from None
            except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
                if method.upper() == "GET" and attempt + 1 < attempts:
                    time.sleep(min(8.0, 0.5 * (2**attempt)) + random.uniform(0.0, 0.25))
                    continue
                # Do not emit the exception text: URL handlers can include query strings.
                raise EvaluationError(f"EvalHub {method.upper()} request failed ({type(exc).__name__}).") from None
        raise EvaluationError(f"EvalHub {method.upper()} request failed after bounded retries.")

    def get_provider(self, provider_id: str) -> dict[str, Any]:
        path = "/api/v1/evaluations/providers/" + urllib.parse.quote(provider_id, safe="")
        payload = self.request("GET", path)
        if not isinstance(payload, dict):
            raise EvaluationError("EvalHub provider response has an unexpected shape.")
        return payload

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.request("POST", "/api/v1/evaluations/jobs", payload, retries=1)
        if not isinstance(response, dict):
            raise EvaluationError("EvalHub accepted the request but returned an unexpected job shape.")
        return response

    def get_job(self, job_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", job_id):
            raise EvaluationError("Stored EvalHub job ID is invalid.")
        payload = self.request("GET", "/api/v1/evaluations/jobs/" + urllib.parse.quote(job_id, safe=""))
        if not isinstance(payload, dict):
            raise EvaluationError("EvalHub job response has an unexpected shape.")
        return payload

    def find_by_tag(self, tag: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"tags": tag, "limit": 100})
        payload = self.request("GET", "/api/v1/evaluations/jobs?" + query)
        return _unwrap_items(payload)


def _resource_id(job: dict[str, Any]) -> str | None:
    resource = job.get("resource") if isinstance(job.get("resource"), dict) else {}
    value = resource.get("id") or job.get("id") or job.get("job_id")
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9-]{1,128}", value):
        return value
    return None


def _tags(job: dict[str, Any]) -> set[str]:
    value = job.get("tags") or (job.get("metadata") or {}).get("tags") or []
    if isinstance(value, list):
        return {str(tag) for tag in value}
    if isinstance(value, dict):
        return {str(key) + ":" + str(item) for key, item in value.items()}
    return set()


def reconcile_by_correlation(client: EvalHubClient, entry: dict[str, Any]) -> tuple[str | None, str]:
    """Adopt one exact correlated job; never resubmit an uncertain POST."""
    try:
        matches = [item for item in client.find_by_tag(entry["correlation_tag"])
                   if entry["correlation_tag"] in _tags(item)
                   and (item.get("name") or (item.get("resource") or {}).get("name")) == entry["name"]]
    except EvaluationError:
        return None, "unknown"
    ids = sorted({job_id for item in matches if (job_id := _resource_id(item))})
    if len(ids) == 1:
        return ids[0], "reconciled"
    if len(ids) > 1:
        return None, "ambiguous"
    return None, "unknown"


class Workflow:
    def __init__(self, env: dict[str, str] | None = None, api: EvalHubClient | None = None):
        self.env = env if env is not None else dict(os.environ)
        self.work_dir = Path(self.env.get("RESULTS_DIR", "/workspace/results"))
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.work_dir / "state.json"
        self.runtime_path = Path(self.env.get("RUNTIME_CONFIG", "/etc/maas-evaluation/runtime.json"))
        self.data_profile_path = Path(self.env.get("DATA_PROFILE_PATH", "/etc/maas-evaluation/dataset-profile.json"))
        self.runtime = json.loads(self.runtime_path.read_text(encoding="utf-8"))
        self.data_profile = json.loads(self.data_profile_path.read_text(encoding="utf-8"))
        self.pipeline_run_uid = self.env.get("PIPELINE_RUN_UID", "").strip()
        self.ledger: ConfigMapLedger | None = None
        if self.env.get("LEDGER_REQUIRED", "false").lower() == "true":
            if not self.pipeline_run_uid:
                raise EvaluationError("Tekton PipelineRun UID is unavailable for the durable run ledger.")
            try:
                self.ledger = ConfigMapLedger.from_environment(self.env, self.pipeline_run_uid)
            except LedgerError as exc:
                raise EvaluationError(str(exc)) from None
        self.api = api or EvalHubClient(
            self.runtime["evalhub_url"],
            self.runtime["tenant"],
            Path(self.env.get("EVALHUB_TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token")),
            self.runtime.get("ca_bundle_path"),
        )

    def save(self, state: dict[str, Any]) -> None:
        temp = self.state_path.with_suffix(".json.tmp")
        with temp.open("wb") as stream:
            stream.write(canonical_json(state) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.state_path)
        if self.ledger:
            try:
                self.ledger.write(state, self._ledger_files())
            except LedgerError as exc:
                raise EvaluationError(str(exc)) from None

    def _ledger_files(self) -> dict[str, Path]:
        return {
            "evaluation-bundle.json": self.work_dir / "evaluation-bundle.json",
            "summary.md": self.work_dir / "summary.md",
            "gate.json": self.work_dir / "gate.json",
            "diagnostics.json": self.work_dir / "diagnostics.json",
        }

    def load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise EvaluationError("The preflight checkpoint is missing.") from exc
        if state.get("schema_version") != "maas-eval-checkpoint.v1":
            raise EvaluationError("The run checkpoint schema is invalid.")
        return state

    def preflight(
        self, *, endpoint: str, model_secret_name: str, model_name: str,
        profile: str, datasets: list[str],
    ) -> dict[str, Any]:
        selected = normalize_datasets(datasets)
        prefixes = self.runtime.get("approved_endpoint_prefixes", [])
        endpoint = validate_endpoint(endpoint, prefixes)
        validate_endpoint_secret_binding(endpoint, model_secret_name, self.runtime.get("endpoint_secret_bindings", []))
        if not model_name.strip() or any(ord(ch) < 32 for ch in model_name):
            raise EvaluationError("Pass an explicit served model name; model discovery is not enabled for Secret refs.")
        provider_id = str(self.runtime.get("provider_id", ""))
        if not provider_id:
            raise EvaluationError("EvalHub provider ID is missing from runtime configuration.")
        validate_data_profile(self.data_profile, provider_id, selected)
        if profile not in {"smoke", "coverage", "full"}:
            raise EvaluationError("profile must be smoke, coverage, or full.")
        for dataset in selected:
            info = self.data_profile["datasets"][dataset]
            profile_parameters(profile, dataset, info)
            data_ref = self.runtime.get("test_data_refs", {}).get(dataset)
            self._validate_test_data_ref(data_ref, model_secret_name)

        provider = self.api.get_provider(provider_id)
        provider_resource = provider.get("resource") or {}
        if provider_resource.get("id") != provider_id:
            raise EvaluationError("EvalHub provider response ID does not match the configured provider ID.")
        available = set()
        for item in provider.get("benchmarks", []):
            if isinstance(item, dict):
                benchmark_id = item.get("id") or item.get("benchmark_id")
                if isinstance(benchmark_id, str):
                    available.add(benchmark_id)
        missing = sorted(set(selected) - available)
        if missing:
            raise EvaluationError("EvalHub provider is missing selected benchmark ID(s): " + ", ".join(missing))

        pipeline_uid = self.env.get("PIPELINE_RUN_UID", "").strip()
        pipeline_name = self.env.get("PIPELINE_RUN_NAME", "").strip()
        if not pipeline_uid or not pipeline_name:
            raise EvaluationError("Tekton PipelineRun UID/name context is unavailable.")
        selection = {
            "schema_version": "maas-eval-selection.v1",
            "pipeline_run_uid": pipeline_uid,
            "pipeline_run_name": pipeline_name,
            "tenant": self.runtime["tenant"],
            "provider_id": provider_id,
            "maas_endpoint": endpoint,
            "model_name": model_name.strip(),
            "model_secret_name": model_secret_name,
            "profile": profile,
            "selected_datasets": selected,
            "dataset_manifest_sha256": self.data_profile["manifest_sha256"],
            "dataset_source": self.data_profile.get("source_manifest", {}),
            "profile_hash": None,
            "frozen_at": datetime.now(UTC).isoformat(),
        }
        selection["profile_hash"] = sha256_json({k: v for k, v in selection.items() if k not in {"profile_hash", "frozen_at"}})
        jobs = []
        for dataset in selected:
            correlation_tag = f"tekton-correlation:{pipeline_uid}:{dataset}"
            name = f"maas-{pipeline_uid[:8]}-{dataset}"
            jobs.append({
                "dataset": dataset,
                "name": name,
                "correlation_tag": correlation_tag,
                "job_id": None,
                "submission_state": "not_started",
                "remote_state": "not_submitted",
                "last_error": None,
            })
        state = {
            "schema_version": "maas-eval-checkpoint.v1",
            "selection": selection,
            "jobs": jobs,
            "submission_outcome": "not_started",
            "wait_outcome": "not_started",
            "results": [],
            "events": [{"time": datetime.now(UTC).isoformat(), "event": "preflight_passed"}],
        }
        self.save(state)
        return state

    @staticmethod
    def _validate_test_data_ref(value: Any, model_secret_name: str) -> None:
        if not isinstance(value, dict):
            raise EvaluationError("Each selected benchmark requires a configured S3 or PVC test_data_ref.")
        if set(value) == {"pvc"} and isinstance(value["pvc"], dict):
            pvc = value["pvc"]
            if (
                set(pvc) != {"claim_name"}
                or not isinstance(pvc.get("claim_name"), str)
                or not _valid_claim_name(pvc["claim_name"])
            ):
                raise EvaluationError("PVC test_data_ref requires a valid tenant-local claim_name.")
            return
        if set(value) != {"s3"} or not isinstance(value["s3"], dict):
            raise EvaluationError("Each selected benchmark requires exactly one S3 or PVC test_data_ref.")
        s3 = value["s3"]
        if not all(isinstance(s3.get(k), str) and s3[k].strip() for k in ("bucket", "key", "secret_ref")):
            raise EvaluationError("S3 test_data_ref requires bucket, key, and tenant Secret reference.")
        if not _valid_secret_name(s3["secret_ref"]) or s3["secret_ref"] == model_secret_name:
            raise EvaluationError("The S3 credential Secret must be valid and separate from the MaaS Secret.")

    def submit(self, *, datasets: list[str]) -> dict[str, Any]:
        state = self.load()
        selection = state["selection"]
        selected = normalize_datasets(datasets)
        if selected != selection["selected_datasets"]:
            raise EvaluationError("Submitted dataset array differs from the immutable preflight selection.")
        if state["submission_outcome"] != "not_started":
            raise EvaluationError("This submission checkpoint is already started; do not resubmit it.")
        state["submission_outcome"] = "submitting"
        self.save(state)
        for job in state["jobs"]:
            dataset = job["dataset"]
            info = self.data_profile["datasets"][dataset]
            request_body = {
                "name": job["name"],
                "description": f"{dataset} {selection['profile']} evaluation from Tekton PipelineRun {selection['pipeline_run_name']}",
                "model": {
                    "url": selection["maas_endpoint"],
                    "name": selection["model_name"],
                    "auth": {"secret_ref": selection["model_secret_name"]},
                },
                "benchmarks": [{
                    "provider_id": selection["provider_id"],
                    "id": dataset,
                    "parameters": profile_parameters(selection["profile"], dataset, info),
                    "test_data_ref": self.runtime["test_data_refs"][dataset],
                }],
                "experiment": {"name": f"maas-evaluation-{selection['profile']}"},
                "tags": [
                    "tekton", "maas-evaluation",
                    job["correlation_tag"],
                    f"tekton-pipelinerun-uid:{selection['pipeline_run_uid']}",
                    f"benchmark:{dataset}",
                    f"profile-hash:{selection['profile_hash'][:16]}",
                    f"profile:{selection['profile']}",
                ],
            }
            job["submission_state"] = "in_flight"
            job["remote_state"] = "submission_unknown"
            self.save(state)
            try:
                response = self.api.submit_job(request_body)
                job_id = _resource_id(response)
                if not job_id:
                    job["submission_state"] = "unknown"
                    job["last_error"] = "EvalHub accepted the POST but returned no usable job ID."
                    adopted, disposition = reconcile_by_correlation(self.api, job)
                    if adopted:
                        job["job_id"] = adopted
                        job["submission_state"] = disposition
                        job["remote_state"] = "pending"
                    else:
                        job["submission_state"] = disposition
                    self.save(state)
                    if not adopted:
                        self._mark_remaining_not_submitted(state, job["dataset"])
                        break
                else:
                    job["job_id"] = job_id
                    job["submission_state"] = "accepted"
                    job["remote_state"] = "pending"
                    job["last_error"] = None
                    self.save(state)
            except EvaluationError as exc:
                # POST is intentionally never retried. The server may have accepted it.
                job["submission_state"] = "unknown"
                job["last_error"] = str(exc)
                adopted, disposition = reconcile_by_correlation(self.api, job)
                if adopted:
                    job["job_id"] = adopted
                    job["submission_state"] = disposition
                    job["remote_state"] = "pending"
                else:
                    job["submission_state"] = disposition
                    job["remote_state"] = "submission_unknown"
                self.save(state)
                if not adopted:
                    self._mark_remaining_not_submitted(state, job["dataset"])
                    break

        submitted = sum(bool(job.get("job_id")) for job in state["jobs"])
        unknown = any(job["submission_state"] in {"unknown", "ambiguous"} for job in state["jobs"])
        state["submission_outcome"] = "complete" if submitted == len(state["jobs"]) and not unknown else "partial_or_unknown"
        state["events"].append({"time": datetime.now(UTC).isoformat(), "event": state["submission_outcome"]})
        self.save(state)
        return state

    @staticmethod
    def _mark_remaining_not_submitted(state: dict[str, Any], current_dataset: str) -> None:
        seen = False
        for item in state["jobs"]:
            if item["dataset"] == current_dataset:
                seen = True
            elif seen and not item.get("job_id"):
                item["submission_state"] = "not_submitted_after_unknown_post"
                item["remote_state"] = "not_submitted"

    def wait(self, *, poll_seconds: int | None = None, deadline_seconds: int | None = None) -> dict[str, Any]:
        state = self.load()
        profile = state["selection"]["profile"]
        waits = self.runtime.get("wait_deadlines_seconds", {})
        deadline_seconds = int(deadline_seconds or waits.get(profile, 3600))
        poll_seconds = max(1, int(poll_seconds or self.runtime.get("poll_seconds", 15)))
        if deadline_seconds <= 0:
            raise EvaluationError("Evaluation polling deadline must be positive.")
        start = time.monotonic()
        unresolved = [job for job in state["jobs"] if not job.get("job_id")]
        if unresolved:
            state["wait_outcome"] = "unresolved_submission"
            for item in unresolved:
                if item.get("remote_state") == "submission_unknown":
                    item["remote_state"] = "submission_unknown"
            self.save(state)
            return state

        while True:
            all_terminal = True
            for item in state["jobs"]:
                try:
                    remote = self.api.get_job(item["job_id"])
                    status = remote.get("status") if isinstance(remote.get("status"), dict) else {}
                    remote_state = str(status.get("state") or remote.get("state") or "unknown").lower()
                    item["remote_state"] = remote_state
                    item["last_error"] = None
                except EvaluationError as exc:
                    item["remote_state"] = "status_unavailable"
                    item["last_error"] = str(exc)
                    all_terminal = False
                    continue
                if remote_state not in TERMINAL_STATES:
                    all_terminal = False
            state["wait_outcome"] = "terminal" if all_terminal else "polling"
            self.save(state)
            if all_terminal:
                break
            elapsed = time.monotonic() - start
            if elapsed >= deadline_seconds:
                for item in state["jobs"]:
                    if item.get("job_id") and item.get("remote_state") not in TERMINAL_STATES:
                        item["remote_state"] = "deadline_exceeded"
                state["wait_outcome"] = "deadline_exceeded"
                self.save(state)
                break
            time.sleep(min(poll_seconds + random.uniform(0, 2), deadline_seconds - elapsed))
        state["events"].append({"time": datetime.now(UTC).isoformat(), "event": state["wait_outcome"]})
        self.save(state)
        return state

    def collect(self) -> dict[str, Any]:
        state = self.load()
        selection = state["selection"]
        entries = self.data_profile["datasets"]
        results: list[dict[str, Any]] = []
        for item in state["jobs"]:
            dataset = item["dataset"]
            detail: dict[str, Any] = {}
            if item.get("job_id"):
                detail = self.api.get_job(item["job_id"])
            status = detail.get("status") if isinstance(detail.get("status"), dict) else {}
            job_state = str(status.get("state") or detail.get("state") or item.get("remote_state") or "unknown").lower()
            result_block = detail.get("results") if isinstance(detail.get("results"), dict) else {}
            benchmark_rows = result_block.get("benchmarks")
            if not isinstance(benchmark_rows, list):
                benchmark_rows = status.get("benchmarks") if isinstance(status.get("benchmarks"), list) else []
            matched = [row for row in benchmark_rows if isinstance(row, dict) and
                       (row.get("id") or row.get("benchmark_id")) == dataset]
            if len(matched) > 1:
                metrics: Any = {}
                benchmark_state = "duplicate_result"
                result_error = "EvalHub returned multiple rows for one requested benchmark."
            elif not matched:
                metrics = {}
                benchmark_state = job_state
                result_error = "EvalHub result does not include the requested benchmark."
            else:
                result = matched[0]
                metrics = result.get("metrics") or {}
                if not isinstance(metrics, (dict, list)):
                    metrics = {}
                benchmark_state = str(result.get("status") or job_state).lower()
                result_error = None
            mlflow_run_id = None
            mlflow_url = result_block.get("mlflow_experiment_url") or detail.get("mlflow_experiment_url")
            if len(matched) == 1:
                mlflow_run_id = matched[0].get("mlflow_run_id") or result_block.get("mlflow_run_id")
                mlflow_url = matched[0].get("mlflow_run_url") or mlflow_url
            mlflow_url = self._safe_tracking_url(mlflow_url)
            assessed = assess_dataset_result(
                profile=selection["profile"], dataset=dataset, dataset_info=entries[dataset],
                state=benchmark_state, raw_metrics=metrics,
                quality_thresholds=self.runtime.get("quality_thresholds", {}).get(dataset),
            )
            assessed.update({
                "job_id": item.get("job_id"),
                "evalhub_state": job_state,
                "provider_id": selection["provider_id"],
                "model_name": selection["model_name"],
                "model_endpoint": selection["maas_endpoint"],
                "profile_hash": selection["profile_hash"],
                "dataset_manifest_sha256": entries[dataset]["manifest_sha256"],
                "canonical_jsonl_sha256": entries[dataset]["canonical_jsonl_sha256"],
                "source_revision": entries[dataset]["source_revision"],
                "evaluation_label": entries[dataset].get("evaluation_label", dataset),
                "mlflow_run_id": str(mlflow_run_id) if mlflow_run_id else None,
                "mlflow_url": mlflow_url,
                "result_error": result_error,
                "sample_ids_sha256": None,
                "question_rows_available": False,
            })
            if assessed["execution_outcome"] != "passed" and result_error and result_error not in assessed["reasons"]:
                assessed["reasons"].append(result_error)
            assessed["artifact_outcome"] = "mlflow_linked_summary_only" if mlflow_run_id or mlflow_url else "missing_mlflow_link"
            results.append(assessed)

        selected = set(selection["selected_datasets"])
        bundle = {
            "schema_version": "model-evaluation-dashboard-bundle.v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "profile": selection["profile"],
            "profile_hash": selection["profile_hash"],
            "selected_datasets": selection["selected_datasets"],
            "datasets": [{
                "dataset": item["dataset"],
                "status": item["execution_outcome"],
                "evalhub_job_id": item.get("job_id"),
                "mlflow_run_id": item.get("mlflow_run_id"),
                "mlflow_url": item.get("mlflow_url"),
                "metrics": item["metrics"],
                "metrics_display": dashboard_metric_rows(item["metrics"]),
                "expected_attempts": item["expected_attempts"],
                "provenance": {
                    "source_revision": item["source_revision"],
                    "dataset_manifest_sha256": item["dataset_manifest_sha256"],
                    "canonical_jsonl_sha256": item["canonical_jsonl_sha256"],
                    "sample_ids_sha256": None,
                    "evaluation_label": item["evaluation_label"],
                },
            } for item in results],
            "not_selected": [dataset for dataset in DATASET_ORDER if dataset not in selected],
            "question_rows": {"status": "unavailable_in_pipeline_bundle", "rows": []},
            "source_manifests": {
                "prepared_data_manifest_sha256": selection["dataset_manifest_sha256"],
                "source_manifest": selection["dataset_source"],
                "datasets": {item["dataset"]: {
                    "source_revision": item["source_revision"],
                    "canonical_jsonl_sha256": item["canonical_jsonl_sha256"],
                    "evaluation_label": item["evaluation_label"],
                } for item in results},
            },
            "evaluations": [{
                "schema_version": "evaluation-dashboard.v1",
                "source": "evalhub",
                "source_id": f"evalhub:{selection['tenant']}:{item.get('job_id') or selection['pipeline_run_uid']}:{item['provider_id']}:{item['dataset']}",
                "workspace": selection["tenant"],
                "job_id": item.get("job_id"),
                "run_id": item.get("mlflow_run_id"),
                "run_name": item.get("job_id"),
                "state": item.get("evalhub_state"),
                "model": {"name": item["model_name"], "url": item["model_endpoint"]},
                "benchmark": item["dataset"],
                "provider_id": item["provider_id"],
                "protocol": self.data_profile.get("protocol"),
                "metrics_raw": item["metrics"],
                "metrics": dashboard_metric_rows(item["metrics"]),
                "score": next((metric for metric in dashboard_metric_rows(item["metrics"]) if metric["key"] == "overall_accuracy"), None),
                "source_refs": {"evalhub_job_id": item.get("job_id"), "mlflow_run_id": item.get("mlflow_run_id"), "mlflow_url": item.get("mlflow_url")},
                "provenance": {
                    "protocol": self.data_profile.get("protocol"),
                    "source_revision": item["source_revision"],
                    "manifest_sha256": item["dataset_manifest_sha256"],
                    "canonical_jsonl_sha256": item["canonical_jsonl_sha256"],
                    "sample_ids_sha256": None,
                    "evaluation_label": item["evaluation_label"],
                },
                "completeness": {"summary_metrics": True, "question_artifact": "not_exported_by_pipeline"},
                "warnings": ["The pipeline bundle is summary-only; per-question artifact availability at the source MLflow run was not checked."],
            } for item in results],
            "summary_only": True,
            "source": "EvalHub and MLflow",
        }
        bundle_path = self.work_dir / "evaluation-bundle.json"
        bundle_path.write_bytes(json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
        report_path = self.work_dir / "summary.md"
        lines = [
            "# MaaS evaluation run", "", f"PipelineRun: `{selection['pipeline_run_name']}`",
            f"Model: `{selection['model_name']}`", f"Profile: `{selection['profile']}`",
            f"Selected datasets: {', '.join(selection['selected_datasets'])}", "",
            "| Dataset | EvalHub job | State | Accuracy (%) | Attempted | Valid | Data quality | Model quality | MLflow |",
            "|---|---|---|---:|---:|---:|---|---|---|",
        ]
        for result in results:
            acc = result["metrics"].get("overall_accuracy")
            accuracy = str(acc) if acc is not None else "unknown"
            attempted = result["metrics"].get("attempted_count")
            expected = result["expected_attempts"]
            attempted_display = f"{attempted}/{expected}" if attempted is not None else f"unknown/{expected}"
            valid = result["metrics"].get("valid_count")
            mlflow = result.get("mlflow_url") or result.get("mlflow_run_id") or "missing"
            lines.append(
                f"| {result['dataset']} | {result.get('job_id') or 'unknown'} | {result.get('evalhub_state')} "
                f"| {accuracy} | {attempted_display} | {valid if valid is not None else 'unknown'} "
                f"| {result['data_quality_outcome']} | {result['model_quality_outcome']} | {mlflow} |"
            )
        lines.extend(["", "Accuracy is reported on the adapter's 0–100 scale. This export is summary-only; it does not invent question rows."])
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        state["results"] = results
        state["collection"] = {
            "bundle_path": str(bundle_path),
            "bundle_sha256": sha256_file(bundle_path),
            "summary_path": str(report_path),
            "summary_sha256": sha256_file(report_path),
            "summary_only": True,
        }
        state["events"].append({"time": datetime.now(UTC).isoformat(), "event": "collection_written"})
        self.save(state)
        return state

    @staticmethod
    def _safe_tracking_url(value: Any) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = urllib.parse.urlsplit(value)
            if parsed.scheme not in {"https"} or not parsed.hostname or parsed.username or parsed.password:
                return None
            if re.search(r"(?i)(token|key|secret|password|auth)=", parsed.query):
                return None
            return value
        except ValueError:
            return None

    def gate(self) -> dict[str, Any]:
        state = self.load()
        selection = state["selection"]
        results_by_dataset = {item.get("dataset"): item for item in state.get("results", [])}
        failures: list[str] = []
        if set(results_by_dataset) != set(selection["selected_datasets"]):
            failures.append("Collected result IDs do not exactly match the frozen selection.")
        if len(state["jobs"]) != len(selection["selected_datasets"]):
            failures.append("Expected exactly one EvalHub job record per selected dataset.")
        for dataset in selection["selected_datasets"]:
            result = results_by_dataset.get(dataset)
            if not result:
                failures.append(f"{dataset}: no collected result.")
                continue
            for field in ("execution_outcome", "data_quality_outcome"):
                if result.get(field) != "passed":
                    failures.append(f"{dataset}: {field}={result.get(field)}.")
            if result.get("artifact_outcome") != "mlflow_linked_summary_only":
                failures.append(f"{dataset}: MLflow artifact/run link is missing.")
            if result.get("model_quality_outcome") == "failed":
                failures.append(f"{dataset}: configured model-quality threshold failed.")
        unknown = [job["dataset"] for job in state["jobs"] if not job.get("job_id")]
        if unknown:
            failures.append("Submission state is unknown or incomplete for: " + ", ".join(unknown))
        outcome = "passed" if not failures else "failed"
        gate_doc = {
            "schema_version": "maas-eval-gate.v1",
            "pipeline_run_uid": selection["pipeline_run_uid"],
            "profile_hash": selection["profile_hash"],
            "selected_datasets": selection["selected_datasets"],
            "execution_outcome": "passed" if all(r.get("execution_outcome") == "passed" for r in state.get("results", [])) else "failed",
            "data_quality_outcome": "passed" if all(r.get("data_quality_outcome") == "passed" for r in state.get("results", [])) else "failed",
            "model_quality_outcome": "not_configured" if all(r.get("model_quality_outcome") == "not_configured" for r in state.get("results", [])) else ("failed" if any(r.get("model_quality_outcome") == "failed" for r in state.get("results", [])) else "passed"),
            "artifact_outcome": "mlflow_linked_summary_only" if all(r.get("artifact_outcome") == "mlflow_linked_summary_only" for r in state.get("results", [])) else "failed",
            "pipeline_gate": outcome,
            "failures": failures,
        }
        gate_path = self.work_dir / "gate.json"
        gate_path.write_bytes(json.dumps(gate_doc, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
        state["gate"] = gate_doc
        state["events"].append({"time": datetime.now(UTC).isoformat(), "event": "gate_" + outcome})
        self.save(state)
        return state

    def diagnostics(self) -> dict[str, Any]:
        if not self.state_path.exists():
            record = {"schema_version": "maas-eval-diagnostics.v1", "checkpoint": "missing", "note": "Preflight did not write a run checkpoint."}
        else:
            state = self.load()
            record = {
                "schema_version": "maas-eval-diagnostics.v1",
                "pipeline_run_uid": state.get("selection", {}).get("pipeline_run_uid"),
                "pipeline_run_name": state.get("selection", {}).get("pipeline_run_name"),
                "selected_datasets": state.get("selection", {}).get("selected_datasets", []),
                "jobs": [{k: job.get(k) for k in ("dataset", "job_id", "submission_state", "remote_state", "last_error")} for job in state.get("jobs", [])],
                "gate": state.get("gate"),
                "collection": state.get("collection"),
                "remaining_remote_jobs": [job.get("job_id") for job in state.get("jobs", []) if job.get("job_id") and job.get("remote_state") not in TERMINAL_STATES],
                "note": "Cancelling a PipelineRun does not cancel remote EvalHub jobs; reconcile these owned IDs manually.",
            }
        path = self.work_dir / "diagnostics.json"
        path.write_bytes(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
        if self.ledger:
            try:
                state = self.load() if self.state_path.exists() else {
                    "schema_version": "maas-eval-checkpoint.v1",
                    "pipeline_run_uid": self.pipeline_run_uid,
                    "checkpoint": "missing",
                    "jobs": [],
                    "events": [],
                }
                self.ledger.write(state, self._ledger_files())
            except (LedgerError, EvaluationError) as exc:
                raise EvaluationError(str(exc)) from None
        return record


def run_preflight(workflow: Workflow, args: Any) -> None:
    state = workflow.preflight(
        endpoint=args.endpoint, model_secret_name=args.model_secret_name,
        model_name=args.model_name, profile=args.profile, datasets=args.dataset,
    )
    _write_result(Path(args.result_path), state["selection"]["profile_hash"])
    print("Preflight passed for " + ", ".join(state["selection"]["selected_datasets"]))


def _write_result(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value[:3500], encoding="utf-8")


def command_results(workflow: Workflow, command: str, args: Any) -> None:
    if command == "preflight":
        run_preflight(workflow, args)
    elif command == "submit":
        state = workflow.submit(datasets=args.dataset)
        job_ids = [{"dataset": job["dataset"], "job_id": job.get("job_id"), "submission_state": job["submission_state"]} for job in state["jobs"]]
        _write_result(Path(args.job_ids_result), json.dumps(job_ids, separators=(",", ":")))
        print(f"Recorded {sum(bool(job.get('job_id')) for job in state['jobs'])} of {len(state['jobs'])} requested EvalHub jobs.")
    elif command == "wait":
        state = workflow.wait()
        brief = [{"dataset": j["dataset"], "job_id": j.get("job_id"), "state": j.get("remote_state")} for j in state["jobs"]]
        _write_result(Path(args.wait_result), json.dumps(brief, separators=(",", ":")))
        print("EvalHub wait outcome: " + state["wait_outcome"])
    elif command == "collect":
        state = workflow.collect()
        links = {r["dataset"]: {"job_id": r.get("job_id"), "mlflow_url": r.get("mlflow_url"), "mlflow_run_id": r.get("mlflow_run_id")} for r in state["results"]}
        _write_result(Path(args.links_result), json.dumps(links, separators=(",", ":")))
        _write_result(Path(args.bundle_digest_result), state["collection"]["bundle_sha256"])
        print("Wrote summary-only result bundle " + state["collection"]["bundle_sha256"])
    elif command == "gate":
        state = workflow.gate()
        gate = state["gate"]
        _write_result(Path(args.outcome_result), gate["pipeline_gate"])
        if gate["failures"]:
            for failure in gate["failures"]:
                print("GATE FAILURE: " + failure)
            raise SystemExit(1)
        print("All selected datasets completed with required data-quality checks and MLflow links.")
        if gate["model_quality_outcome"] == "not_configured":
            print("No accuracy threshold was configured; this pass is not a model-quality acceptance decision.")
    elif command == "diagnostics":
        record = workflow.diagnostics()
        print("Diagnostics written; remaining owned EvalHub job IDs: " + ",".join(record.get("remaining_remote_jobs", [])))


def parse_args(argv: list[str] | None = None) -> Any:
    import argparse

    parser = argparse.ArgumentParser(prog="maas-eval-runner")
    subs = parser.add_subparsers(dest="command", required=True)
    preflight = subs.add_parser("preflight")
    preflight.add_argument("--endpoint", required=True)
    preflight.add_argument("--model-secret-name", required=True)
    preflight.add_argument("--model-name", required=True)
    preflight.add_argument("--profile", choices=("smoke", "coverage", "full"), required=True)
    preflight.add_argument("--dataset", nargs="*", required=True)
    preflight.add_argument("--result-path", default="/tekton/results/profile-hash")
    submit = subs.add_parser("submit")
    submit.add_argument("--dataset", nargs="*", required=True)
    submit.add_argument("--job-ids-result", default="/tekton/results/job-ids")
    wait = subs.add_parser("wait")
    wait.add_argument("--wait-result", default="/tekton/results/wait-summary")
    collect = subs.add_parser("collect")
    collect.add_argument("--links-result", default="/tekton/results/mlflow-links")
    collect.add_argument("--bundle-digest-result", default="/tekton/results/bundle-digest")
    gate = subs.add_parser("gate")
    gate.add_argument("--outcome-result", default="/tekton/results/outcome")
    subs.add_parser("diagnostics")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        workflow = Workflow()
        command_results(workflow, args.command, args)
    except EvaluationError as exc:
        print("ERROR: " + str(exc))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
