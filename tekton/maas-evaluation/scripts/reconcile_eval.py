#!/usr/bin/env python3
"""Read a durable Tekton/EvalHub run record and optionally refresh remote status."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner.core import EvalHubClient, EvaluationError  # noqa: E402
from runner.ledger import ConfigMapLedger, LedgerError, ledger_name, run_key_prefix  # noqa: E402


def run_oc(args: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["oc", *args], input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def get_json(args: list[str], *, missing_message: str) -> dict[str, Any]:
    result = run_oc(args)
    if result.returncode != 0:
        raise EvaluationError(missing_message)
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise EvaluationError("OpenShift returned invalid JSON while reading the run record.") from exc
    if not isinstance(value, dict):
        raise EvaluationError("OpenShift returned an unexpected object while reading the run record.")
    return value


def run_uid(namespace: str, value: str) -> str:
    if value.startswith("uid:"):
        return value[4:]
    obj = get_json(
        ["get", "pipelinerun", value, "-n", namespace, "-o", "json"],
        missing_message="PipelineRun was not found or is not readable in the selected namespace.",
    )
    uid = (obj.get("metadata") or {}).get("uid")
    if not isinstance(uid, str) or not uid:
        raise EvaluationError("PipelineRun object did not contain a UID.")
    return uid


def tracking_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        if re.search(r"(?i)(token|key|secret|password|auth)=", parsed.query):
            return None
        return value
    except ValueError:
        return None


def refresh_state(state: dict[str, Any], runtime: dict[str, Any], *, ca_bundle: str | None) -> None:
    token_result = run_oc(["whoami", "-t"])
    if token_result.returncode != 0 or not token_result.stdout.strip():
        raise EvaluationError("OpenShift CLI has no usable login for EvalHub status refresh.")
    with tempfile.TemporaryDirectory(prefix="maas-eval-reconcile-") as directory:
        token_path = Path(directory) / "token"
        os.chmod(directory, 0o700)
        token_path.write_bytes(token_result.stdout.strip())
        os.chmod(token_path, 0o600)
        api = EvalHubClient(
            runtime["evalhub_url"], runtime["tenant"], token_path,
            ca_bundle or runtime.get("ca_bundle_path"),
        )
        unresolved: list[str] = []
        for job in state.get("jobs", []):
            job_id = job.get("job_id")
            if not job_id:
                try:
                    matches = api.find_by_tag(job["correlation_tag"])
                    exact = [item for item in matches if job["correlation_tag"] in _tags(item)
                             and (item.get("name") or (item.get("resource") or {}).get("name")) == job["name"]]
                    ids = sorted({found for item in exact if (found := _resource_id(item))})
                except EvaluationError as exc:
                    job["last_error"] = str(exc)
                    unresolved.append(job.get("dataset", "unknown"))
                    continue
                if len(ids) == 1:
                    job_id = ids[0]
                    job["job_id"] = job_id
                    job["submission_state"] = "reconciled"
                elif len(ids) > 1:
                    job["submission_state"] = "ambiguous"
                    job["last_error"] = "Multiple exact correlation matches; operator resolution is required."
                    unresolved.append(job.get("dataset", "unknown"))
                    continue
                else:
                    job["submission_state"] = "unknown"
                    job["last_error"] = "No exact correlation match was found."
                    unresolved.append(job.get("dataset", "unknown"))
                    continue
            try:
                detail = api.get_job(job_id)
            except EvaluationError as exc:
                job["last_error"] = str(exc)
                job["remote_state"] = "status_unavailable"
                unresolved.append(job.get("dataset", "unknown"))
                continue
            status = detail.get("status") if isinstance(detail.get("status"), dict) else {}
            job["remote_state"] = str(status.get("state") or detail.get("state") or "unknown").lower()
            job["last_error"] = None
            result_block = detail.get("results") if isinstance(detail.get("results"), dict) else {}
            benchmarks = result_block.get("benchmarks", []) if isinstance(result_block.get("benchmarks"), list) else []
            matching = [item for item in benchmarks if isinstance(item, dict) and
                        (item.get("id") or item.get("benchmark_id")) == job.get("dataset")]
            if len(matching) == 1:
                job["mlflow_run_id"] = matching[0].get("mlflow_run_id") or result_block.get("mlflow_run_id")
                job["mlflow_url"] = tracking_url(matching[0].get("mlflow_run_url") or result_block.get("mlflow_experiment_url"))
            if job["remote_state"] not in {"completed", "failed", "cancelled", "partially_failed"}:
                unresolved.append(job.get("dataset", "unknown"))
        state["reconciliation"] = {"refreshed": True, "unresolved_datasets": sorted(set(unresolved))}


def _tags(job: dict[str, Any]) -> set[str]:
    tags = job.get("tags") or (job.get("metadata") or {}).get("tags") or []
    if isinstance(tags, list):
        return {str(value) for value in tags}
    if isinstance(tags, dict):
        return {str(key) + ":" + str(value) for key, value in tags.items()}
    return set()


def _resource_id(job: dict[str, Any]) -> str | None:
    resource = job.get("resource") if isinstance(job.get("resource"), dict) else {}
    value = resource.get("id") or job.get("id") or job.get("job_id")
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9-]{1,128}", value):
        return value
    return None


def persist(namespace: str, name: str, obj: dict[str, Any], state: dict[str, Any], data_key: str) -> None:
    obj.setdefault("data", {})[data_key] = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    size = sum(len(key.encode("utf-8")) + len(value.encode("utf-8")) for key, value in obj["data"].items())
    if size > ConfigMapLedger.MAX_DATA_BYTES:
        raise EvaluationError("Durable run ledger exceeds the bounded ConfigMap storage size.")
    encoded = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    result = run_oc(["replace", "-n", namespace, "-f", "-"], input_bytes=encoded)
    if result.returncode != 0:
        raise EvaluationError("Could not update the durable run record; concurrent change or RBAC issue.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="evalhub-lab")
    parser.add_argument("--pipelinerun", required=True, help="PipelineRun name or uid:<UID>")
    parser.add_argument("--refresh", action="store_true", help="Refresh remote EvalHub job state and reconcile uncertain submissions")
    parser.add_argument("--ca-bundle", type=Path, help="Optional CA bundle for the EvalHub HTTPS endpoint")
    parser.add_argument("--export-dir", type=Path, help="Write the durable dashboard bundle and summary into this directory")
    args = parser.parse_args()
    try:
        uid = run_uid(args.namespace, args.pipelinerun)
        name = ledger_name()
        prefix = run_key_prefix(uid)
        data_key = prefix + ".json"
        ledger_obj = get_json(
            ["get", "configmap", name, "-n", args.namespace, "-o", "json"],
            missing_message="Durable run ConfigMap is missing or unreadable.",
        )
        try:
            state = json.loads((ledger_obj.get("data") or {}).get(data_key, "{}"))
        except json.JSONDecodeError as exc:
            raise EvaluationError("Durable run record contains invalid JSON.") from exc
        if not state:
            raise EvaluationError("No durable record exists for this PipelineRun UID.")
        if args.refresh:
            runtime_obj = get_json(
                ["get", "configmap", "maas-evaluation-runtime", "-n", args.namespace, "-o", "json"],
                missing_message="EvalHub runtime ConfigMap is missing or unreadable.",
            )
            try:
                runtime = json.loads((runtime_obj.get("data") or {})["runtime.json"])
            except (KeyError, json.JSONDecodeError) as exc:
                raise EvaluationError("EvalHub runtime ConfigMap has no valid runtime.json entry.") from exc
            if args.ca_bundle:
                runtime["ca_bundle_path"] = str(args.ca_bundle)
            refresh_state(state, runtime, ca_bundle=str(args.ca_bundle) if args.ca_bundle else None)
            persist(args.namespace, name, ledger_obj, state, data_key)
        if args.export_dir:
            args.export_dir.mkdir(parents=True, exist_ok=True)
            for filename, suffix in (
                ("evaluation-bundle.json", "-bundle.json"),
                ("summary.md", "-summary.md"),
                ("gate.json", "-gate.json"),
                ("diagnostics.json", "-diagnostics.json"),
            ):
                content = (ledger_obj.get("data") or {}).get(prefix + suffix)
                if content is not None:
                    (args.export_dir / filename).write_text(content, encoding="utf-8")
        selection = state.get("selection") or {}
        print(f"PipelineRun UID: {uid}")
        print(f"Durable record: ConfigMap/{name} data key {data_key} in {args.namespace}")
        print(f"Profile: {selection.get('profile', 'unknown')}")
        print("Datasets: " + ", ".join(selection.get("selected_datasets", [])))
        print("Dataset | EvalHub job | Remote state | MLflow")
        for job in state.get("jobs", []):
            mlflow = job.get("mlflow_url") or job.get("mlflow_run_id") or "unavailable"
            print(f"{job.get('dataset')} | {job.get('job_id') or 'unknown'} | {job.get('remote_state', 'unknown')} | {mlflow}")
        print("Unresolved/active remote jobs: " + ", ".join(
            job.get("job_id") or job.get("dataset", "unknown") for job in state.get("jobs", [])
            if not job.get("job_id") or job.get("remote_state") not in {"completed", "failed", "cancelled", "partially_failed"}
        ) or "none")
        if args.export_dir:
            print("Exported available report files to " + str(args.export_dir))
        print("This command never cancels EvalHub jobs or deletes model Secrets.")
    except (EvaluationError, LedgerError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
