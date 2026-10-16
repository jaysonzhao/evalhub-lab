"""Namespace-scoped, non-secret ConfigMap ledger for durable run recovery."""

from __future__ import annotations

import hashlib
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class LedgerError(RuntimeError):
    """Safe ledger error without response bodies or token values."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


LEDGER_CONFIGMAP_NAME = "maas-eval-ledger"


def run_key_prefix(pipeline_run_uid: str) -> str:
    digest = hashlib.sha256(pipeline_run_uid.encode("utf-8")).hexdigest()[:20]
    return "run-" + digest


def ledger_name() -> str:
    """All run records share one RBAC-constrained ConfigMap."""
    return LEDGER_CONFIGMAP_NAME


class ConfigMapLedger:
    """Persist run state and small dashboard exports outside PipelineRun PVC GC."""

    MAX_DATA_BYTES = 700_000

    def __init__(
        self,
        *,
        namespace: str,
        name: str,
        run_prefix: str,
        api_server: str,
        token_file: Path,
        ca_file: Path,
        opener: Any | None = None,
    ):
        self.namespace = namespace
        self.name = name
        self.object_url = (
            api_server.rstrip("/") + "/api/v1/namespaces/" + urllib.parse.quote(namespace, safe="")
            + "/configmaps/" + urllib.parse.quote(name, safe="")
        )
        try:
            context = ssl.create_default_context(cafile=str(ca_file))
        except (OSError, ssl.SSLError) as exc:
            raise LedgerError("Kubernetes ledger CA is unavailable or invalid.") from exc
        self.opener = opener or urllib.request.build_opener(
            NoRedirectHandler(), urllib.request.HTTPSHandler(context=context)
        )
        self.token_file = token_file
        self.run_prefix = run_prefix

    @classmethod
    def from_environment(cls, env: dict[str, str], pipeline_run_uid: str) -> "ConfigMapLedger":
        host = env.get("KUBERNETES_SERVICE_HOST", "").strip()
        port = env.get("KUBERNETES_SERVICE_PORT_HTTPS", env.get("KUBERNETES_SERVICE_PORT", "443")).strip()
        if not host or not port.isdigit():
            raise LedgerError("Kubernetes in-cluster API settings are unavailable for the durable ledger.")
        authority = f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"
        namespace = env.get("PIPELINE_NAMESPACE", env.get("POD_NAMESPACE", "")).strip()
        if not namespace:
            # The projected namespace file is the standard fallback for Tekton pods.
            namespace_path = Path(env.get("SERVICEACCOUNT_NAMESPACE_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/namespace"))
            try:
                namespace = namespace_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise LedgerError("Pipeline namespace is unavailable for the durable ledger.") from exc
        secret_dir = Path(env.get("SERVICEACCOUNT_DIR", "/var/run/secrets/kubernetes.io/serviceaccount"))
        instance = cls(
            namespace=namespace,
            name=LEDGER_CONFIGMAP_NAME,
            run_prefix=run_key_prefix(pipeline_run_uid),
            api_server="https://" + authority,
            token_file=Path(env.get("EVALHUB_TOKEN_FILE", str(secret_dir / "token"))),
            ca_file=Path(env.get("KUBERNETES_CA_FILE", str(secret_dir / "ca.crt"))),
        )
        return instance

    def _request(self, method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise LedgerError("Projected service-account token is unavailable for the durable ledger.") from exc
        if not token:
            raise LedgerError("Projected service-account token is empty for the durable ledger.")
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=encoded,
            method=method,
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                data = response.read()
                return int(response.status), json.loads(data) if data else {}
        except urllib.error.HTTPError as exc:
            if method == "GET" and exc.code == 404:
                exc.close()
                return 404, None
            if method == "PUT" and exc.code == 409:
                exc.close()
                return 409, None
            status = int(exc.code)
            exc.close()
            raise LedgerError(f"Kubernetes ledger API returned HTTP {status}.") from None
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError, json.JSONDecodeError) as exc:
            raise LedgerError(f"Kubernetes durable-ledger request failed ({type(exc).__name__}).") from None

    def read(self) -> dict[str, Any]:
        status, obj = self._request("GET", self.object_url)
        if status == 404:
            raise LedgerError("The durable ledger ConfigMap is missing; install it before running evaluations.")
        if not isinstance(obj, dict):
            raise LedgerError("Kubernetes returned an invalid durable run record.")
        try:
            key = self.run_prefix + ".json"
            raw = obj.get("data", {}).get(key)
            if not raw:
                raise LedgerError("No durable run record exists for this PipelineRun UID.")
            return json.loads(raw)
        except LedgerError:
            raise
        except (json.JSONDecodeError, AttributeError) as exc:
            raise LedgerError("Durable run record contains invalid ledger data.") from exc

    def write(self, state: dict[str, Any], files: dict[str, Path] | None = None) -> None:
        data: dict[str, str] = {
            self.run_prefix + ".json": json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        }
        for key, path in (files or {}).items():
            if path.is_file():
                suffix = {
                    "evaluation-bundle.json": "-bundle.json",
                    "summary.md": "-summary.md",
                    "gate.json": "-gate.json",
                    "diagnostics.json": "-diagnostics.json",
                }.get(key)
                if suffix:
                    data[self.run_prefix + suffix] = path.read_text(encoding="utf-8")

        for _ in range(4):
            status, existing = self._request("GET", self.object_url)
            if status == 404:
                raise LedgerError("The durable ledger ConfigMap is missing; install it before running evaluations.")
            if not isinstance(existing, dict):
                raise LedgerError("Kubernetes returned an invalid durable run record.")
            metadata = existing.get("metadata") or {}
            merged_data = {**(existing.get("data") or {}), **data}
            size = sum(len(key.encode("utf-8")) + len(value.encode("utf-8")) for key, value in merged_data.items())
            if size > self.MAX_DATA_BYTES:
                raise LedgerError("Durable run ledger exceeds the bounded ConfigMap storage size; prune old records.")
            manifest = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": self.name,
                    "namespace": self.namespace,
                    "resourceVersion": metadata.get("resourceVersion"),
                    "labels": {
                        **(metadata.get("labels") or {}),
                        "app.kubernetes.io/name": "maas-evaluation",
                    },
                },
                "data": merged_data,
            }
            status, _ = self._request("PUT", self.object_url, manifest)
            if status != 409:
                return
        raise LedgerError("Durable run record changed repeatedly; reconciliation is required.")
