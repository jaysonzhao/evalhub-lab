"""Authenticated API for the project's preconfigured evaluation PipelineRun."""

from __future__ import annotations

import os
import re
import base64
import hashlib
import json
import threading
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import requests
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


router = APIRouter(prefix="/api/pipeline-runs", tags=["pipeline-runs"])

DATASETS = ("hkmmlu", "tmmluplus", "cmmlu", "qualbench")
PROFILES = ("smoke", "coverage", "full")
PROFILE_TIMEOUTS = {
    "smoke": {"pipeline": "2h15m", "tasks": "2h", "finally": "15m"},
    "coverage": {"pipeline": "7h", "tasks": "6h30m", "finally": "30m"},
    "full": {"pipeline": "43h", "tasks": "41h", "finally": "2h"},
}
MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PIPELINE_RUN_LOCK = threading.Lock()


class PipelineRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal["smoke", "coverage", "full"] = "smoke"
    datasets: list[Literal["hkmmlu", "tmmluplus", "cmmlu", "qualbench"]] = Field(min_length=1, max_length=4)
    model_name: str = Field(min_length=1, max_length=128)
    api_key: SecretStr = Field(min_length=1, max_length=4096, repr=False)
    confirm_full_run: bool = False

    @field_validator("datasets")
    @classmethod
    def datasets_are_unique(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("Select each benchmark only once.")
        return values

    @field_validator("model_name")
    @classmethod
    def model_id_is_a_single_path_segment(cls, value: str) -> str:
        if not MODEL_ID_RE.fullmatch(value):
            raise ValueError("Select a model returned by the MaaS model list.")
        return value


class MaaSModelsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(min_length=1, max_length=4096, repr=False)


def _settings() -> dict[str, str]:
    values = {
        "namespace": os.getenv("PIPELINE_NAMESPACE", "evalhub-lab"),
        "name": os.getenv("PIPELINE_NAME", "maas-evaluation"),
        "maas_root": os.getenv("PIPELINE_MAAS_ROOT", "https://maas.apps.cluster-v4vsc.v4vsc.sandbox287.opentlc.com").rstrip("/"),
        "secret_name": os.getenv("PIPELINE_MODEL_SECRET_NAME", "dashboard-maas-api-key"),
    }
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", values["namespace"]):
        raise HTTPException(status_code=503, detail="Pipeline namespace is not configured correctly.")
    maas_root = urlsplit(values["maas_root"])
    if (
        maas_root.scheme != "https" or not maas_root.hostname or maas_root.username or maas_root.password
        or maas_root.path not in {"", "/"} or maas_root.query or maas_root.fragment
    ):
        raise HTTPException(status_code=503, detail="The approved MaaS gateway is not configured as an HTTPS origin.")
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", values["secret_name"]):
        raise HTTPException(status_code=503, detail="The approved model Secret reference is invalid.")
    return values


def _fetch_models(api_key: str, settings: dict[str, str]) -> list[dict[str, Any]]:
    try:
        response = requests.get(
            settings["maas_root"] + "/v1/models",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=(5, 20),
            allow_redirects=False,
        )
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="The MaaS model catalog could not be reached.") from None
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="MaaS rejected this API key.")
    if response.status_code == 403:
        raise HTTPException(status_code=403, detail="This MaaS API key cannot list available models.")
    if not response.ok:
        raise HTTPException(status_code=502, detail="MaaS could not return its model catalog.")
    try:
        body = response.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="MaaS returned an invalid model catalog.") from None
    entries = body.get("data") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        raise HTTPException(status_code=502, detail="MaaS returned an invalid model catalog.")
    models = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not MODEL_ID_RE.fullmatch(model_id) or item.get("ready") is False:
            continue
        models.append({"id": model_id, "ready": item.get("ready") is True})
    models.sort(key=lambda item: item["id"].casefold())
    return models


def _require_dashboard_user(request: Request) -> str:
    user = request.headers.get("x-forwarded-user") or request.headers.get("x-auth-request-user")
    if not user:
        raise HTTPException(status_code=401, detail="Sign in through the dashboard to access PipelineRuns.")
    return user


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    request_host = forwarded_host or request.headers.get("host", "")
    parsed = urlsplit(origin or "")
    if parsed.scheme != "https" or not parsed.netloc or parsed.netloc.casefold() != request_host.casefold():
        raise HTTPException(status_code=403, detail="PipelineRun requests must come from this dashboard.")


def _kubernetes_api(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    content_type: str = "application/json",
) -> dict[str, Any]:
    token_path = os.getenv("KUBERNETES_TOKEN_PATH", "/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = os.getenv("KUBERNETES_CA_PATH", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    try:
        with open(token_path, encoding="utf-8") as token_file:
            token = token_file.read().strip()
    except OSError:
        raise HTTPException(status_code=503, detail="Dashboard workload credentials are unavailable.") from None
    if not token or not os.path.isfile(ca_path):
        raise HTTPException(status_code=503, detail="Dashboard workload credentials are unavailable.")

    port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    url = f"https://kubernetes.default.svc:{port}{path}"
    try:
        response = requests.request(
            method,
            url,
            params=params,
            json=body,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": content_type},
            verify=ca_path,
            timeout=(3, 12),
            allow_redirects=False,
        )
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="OpenShift could not be reached. Try again shortly.") from None
    finally:
        del token

    if not response.ok:
        if response.status_code == 403:
            raise HTTPException(status_code=503, detail="The dashboard service identity is not authorized for this PipelineRun action.")
        if response.status_code == 404:
            raise HTTPException(status_code=503, detail="A required OpenShift resource is not available in this project.")
        if response.status_code in {400, 422}:
            raise HTTPException(status_code=422, detail="OpenShift rejected the configured pipeline request.")
        raise HTTPException(status_code=502, detail="OpenShift could not complete the PipelineRun request.")
    try:
        result = response.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="OpenShift returned an invalid PipelineRun response.") from None
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="OpenShift returned an invalid PipelineRun response.")
    return result


def _run_summary(resource: dict[str, Any]) -> dict[str, Any]:
    metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
    spec = resource.get("spec") if isinstance(resource.get("spec"), dict) else {}
    status = resource.get("status") if isinstance(resource.get("status"), dict) else {}
    params = spec.get("params") if isinstance(spec.get("params"), list) else []
    values = {item.get("name"): item.get("value") for item in params if isinstance(item, dict)}
    condition = next(
        (item for item in status.get("conditions", []) if isinstance(item, dict) and item.get("type") == "Succeeded"),
        {},
    )
    if condition.get("status") == "True":
        state = "completed"
    elif condition.get("status") == "False":
        state = "failed"
    elif status.get("startTime"):
        state = "running"
    else:
        state = "pending"
    reason = condition.get("reason")
    return {
        "name": str(metadata.get("name") or "")[:253],
        "created_at": metadata.get("creationTimestamp"),
        "profile": values.get("profile") if values.get("profile") in PROFILES else "unknown",
        "datasets": [value for value in values.get("datasets", []) if value in DATASETS]
        if isinstance(values.get("datasets"), list)
        else [],
        "model": str(values.get("model-name") or "unknown")[:200],
        "status": state,
        "reason": str(reason)[:100] if reason else None,
    }


@router.get("")
def list_pipeline_runs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    _require_dashboard_user(request)
    settings = _settings()
    namespace = quote(settings["namespace"], safe="")
    result = _kubernetes_api(
        "GET",
        f"/apis/tekton.dev/v1/namespaces/{namespace}/pipelineruns",
        params={"labelSelector": "app.kubernetes.io/name=maas-evaluation", "limit": str(limit)},
    )
    items = result.get("items") if isinstance(result.get("items"), list) else []
    projected = [_run_summary(item) for item in items if isinstance(item, dict)]
    projected.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {"namespace": settings["namespace"], "pipeline": settings["name"], "items": projected[:limit]}


@router.post("/models")
def list_maas_models(payload: MaaSModelsRequest, request: Request) -> dict[str, Any]:
    _require_dashboard_user(request)
    _require_same_origin(request)
    return {"items": _fetch_models(payload.api_key.get_secret_value(), _settings())}


def _clear_api_key(settings: dict[str, str]) -> None:
    namespace = quote(settings["namespace"], safe="")
    secret_name = quote(settings["secret_name"], safe="")
    _kubernetes_api(
        "PATCH",
        f"/api/v1/namespaces/{namespace}/secrets/{secret_name}",
        body={"data": None},
        content_type="application/merge-patch+json",
    )


def _has_unfinished_dashboard_jobs(settings: dict[str, str], pipeline_runs: list[dict[str, Any]]) -> bool:
    dashboard_runs = []
    for run in pipeline_runs:
        metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
        labels = metadata.get("labels") if isinstance(metadata.get("labels"), dict) else {}
        if labels.get("maas-evaluation.opendatahub.io/credential-source") == "dashboard-input":
            dashboard_runs.append(metadata.get("uid"))
    if not dashboard_runs:
        return False

    namespace = quote(settings["namespace"], safe="")
    ledger = _kubernetes_api("GET", f"/api/v1/namespaces/{namespace}/configmaps/maas-eval-ledger")
    data = ledger.get("data") if isinstance(ledger.get("data"), dict) else {}
    terminal_states = {"completed", "failed", "cancelled", "partially_failed"}
    uncertain_submissions = {"in_flight", "unknown", "ambiguous"}
    for uid in dashboard_runs:
        if not isinstance(uid, str) or not uid:
            return True
        digest = hashlib.sha256(uid.encode("utf-8")).hexdigest()[:20]
        try:
            state = json.loads(data.get(f"run-{digest}.json", ""))
        except (json.JSONDecodeError, TypeError):
            return True
        jobs = state.get("jobs") if isinstance(state, dict) else None
        if not isinstance(jobs, list):
            return True
        for job in jobs:
            if not isinstance(job, dict):
                continue
            if job.get("job_id") and job.get("remote_state") not in terminal_states:
                return True
            if not job.get("job_id") and job.get("submission_state") in uncertain_submissions:
                return True
    return False


@router.post("", status_code=201)
def create_pipeline_run(payload: PipelineRunRequest, request: Request) -> dict[str, Any]:
    _require_dashboard_user(request)
    _require_same_origin(request)
    if payload.profile == "full" and not payload.confirm_full_run:
        raise HTTPException(status_code=422, detail="Confirm the full-profile request before starting it.")
    if payload.profile == "full" and "qualbench" in payload.datasets:
        raise HTTPException(status_code=422, detail="Full QualBench is disabled for the approved diagnostic subset.")

    settings = _settings()
    api_key = payload.api_key.get_secret_value()
    models = _fetch_models(api_key, settings)
    if not any(item["id"] == payload.model_name for item in models):
        raise HTTPException(status_code=422, detail="The selected MaaS model is not available to this API key.")

    namespace = quote(settings["namespace"], safe="")
    secret_name = quote(settings["secret_name"], safe="")
    secret_path = f"/api/v1/namespaces/{namespace}/secrets/{secret_name}"
    secret_data = base64.b64encode(api_key.encode("utf-8")).decode("ascii")
    endpoint = settings["maas_root"] + "/llm/" + quote(payload.model_name, safe="-._~")

    body = {
        "apiVersion": "tekton.dev/v1",
        "kind": "PipelineRun",
        "metadata": {
            "generateName": "maas-eval-",
            "namespace": settings["namespace"],
            "labels": {
                "app.kubernetes.io/name": "maas-evaluation",
                "maas-evaluation.opendatahub.io/created-by": "dashboard",
                "maas-evaluation.opendatahub.io/credential-source": "dashboard-input",
            },
        },
        "spec": {
            "pipelineRef": {"name": settings["name"]},
            "taskRunTemplate": {"serviceAccountName": "maas-eval-runner"},
            "params": [
                {"name": "maas-endpoint", "value": endpoint},
                {"name": "model-secret-name", "value": settings["secret_name"]},
                {"name": "model-name", "value": payload.model_name},
                {"name": "profile", "value": payload.profile},
                {"name": "datasets", "value": payload.datasets},
            ],
            "workspaces": [{
                "name": "results",
                "volumeClaimTemplate": {
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "resources": {"requests": {"storage": "1Gi"}},
                    },
                },
            }],
            "timeouts": PROFILE_TIMEOUTS[payload.profile],
        },
    }
    with _PIPELINE_RUN_LOCK:
        current = _kubernetes_api(
            "GET",
            f"/apis/tekton.dev/v1/namespaces/{namespace}/pipelineruns",
            params={"labelSelector": "app.kubernetes.io/name=maas-evaluation", "limit": "50"},
        )
        current_items = current.get("items") if isinstance(current.get("items"), list) else []
        active = [_run_summary(item) for item in current_items if isinstance(item, dict)]
        if any(item["status"] in {"pending", "running"} for item in active):
            raise HTTPException(status_code=409, detail="Wait for the active evaluation to finish before changing the shared run credential.")
        if _has_unfinished_dashboard_jobs(settings, current_items):
            raise HTTPException(status_code=409, detail="A previous EvalHub job may still be using the MaaS key. Reconcile its remote job state before starting another run.")
        try:
            _kubernetes_api(
                "PATCH",
                secret_path,
                body={"data": {"api-key": secret_data}},
                content_type="application/merge-patch+json",
            )
        except HTTPException:
            try:
                _clear_api_key(settings)
            except HTTPException:
                raise HTTPException(status_code=503, detail="The temporary MaaS credential could not be cleared after a setup error.") from None
            raise
        try:
            resource = _kubernetes_api(
                "POST",
                f"/apis/tekton.dev/v1/namespaces/{namespace}/pipelineruns",
                body=body,
            )
        except HTTPException as exc:
            # A timeout can occur after the API server accepted the request.
            # Keep the key until the run is visible or its finally task clears it.
            if exc.status_code != 502:
                try:
                    _clear_api_key(settings)
                except HTTPException:
                    raise HTTPException(status_code=503, detail="PipelineRun creation failed and the temporary MaaS credential could not be cleared.") from None
            raise
    return _run_summary(resource)
