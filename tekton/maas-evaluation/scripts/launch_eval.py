#!/usr/bin/env python3
"""Securely create a model Secret (if needed) and trigger a Tekton PipelineRun."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runner.core import EvaluationError, parse_dataset_csv, validate_https_url  # noqa: E402
from runner.ledger import ledger_name, run_key_prefix  # noqa: E402


PROFILE_TIMEOUTS = {
    "smoke": ("2h15m", "2h", "15m"),
    "coverage": ("7h", "6h30m", "30m"),
    "full": ("43h", "41h", "2h"),
}


def run_oc(args: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["oc", *args], input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def secret_prefix(model: str) -> str:
    safe = re.sub(r"[^a-z0-9-]+", "-", model.lower()).strip("-")
    safe = re.sub(r"-+", "-", safe)[:40].rstrip("-") or "model"
    return "maas-" + safe


def create_secret(namespace: str, name: str, api_key: str, correlation: str) -> None:
    # The key travels only over stdin to oc; it is never a process argument or file.
    created = run_oc([
        "create", "secret", "generic", name, "-n", namespace,
        "--from-file=api-key=/dev/stdin", "--dry-run=client", "-o", "json",
    ], input_bytes=api_key.encode("utf-8"))
    if created.returncode != 0:
        raise EvaluationError("OpenShift could not create the model Secret; response values were withheld.")
    try:
        manifest = json.loads(created.stdout)
    except json.JSONDecodeError as exc:
        raise EvaluationError("OpenShift returned an invalid Secret manifest.") from exc
    manifest["immutable"] = True
    metadata = manifest.setdefault("metadata", {})
    metadata["labels"] = {
        **metadata.get("labels", {}),
        "app.kubernetes.io/managed-by": "maas-eval-launcher",
        "app.kubernetes.io/name": "maas-evaluation",
        "maas-evaluation.opendatahub.io/correlation": correlation,
    }
    metadata["annotations"] = {
        **metadata.get("annotations", {}),
        "maas-evaluation.opendatahub.io/launcher-owned": "true",
    }
    applied = run_oc(["create", "-n", namespace, "-f", "-"], input_bytes=json.dumps(manifest).encode("utf-8"))
    if applied.returncode != 0:
        raise EvaluationError("OpenShift could not persist the model Secret; response values were withheld.")


def _remove_secret_if_run_absent(namespace: str, secret_name: str, correlation: str) -> None:
    selector = f"maas-evaluation.opendatahub.io/correlation={correlation}"
    lookup = run_oc(["get", "pipelineruns.tekton.dev", "-n", namespace, "-l", selector, "-o", "json"])
    if lookup.returncode != 0:
        print("PipelineRun creation was uncertain; the helper-created Secret was retained for reconciliation.")
        return
    try:
        items = json.loads(lookup.stdout).get("items", [])
    except json.JSONDecodeError:
        print("PipelineRun creation was uncertain; the helper-created Secret was retained for reconciliation.")
        return
    if items:
        print("A matching PipelineRun exists; the helper-created Secret was retained.")
        return
    removed = run_oc(["delete", "secret", secret_name, "-n", namespace, "--wait=false"])
    if removed.returncode == 0:
        print("Removed the helper-created Secret after confirming no correlated PipelineRun exists.")
    else:
        print("No PipelineRun was found, but Secret cleanup failed; inspect only this helper-owned Secret.")


def build_pipeline_run(args: argparse.Namespace, namespace: str, secret_name: str, datasets: list[str], correlation: str) -> dict:
    validate_https_url(args.endpoint, label="MaaS endpoint")
    return {
        "apiVersion": "tekton.dev/v1",
        "kind": "PipelineRun",
        "metadata": {
            "generateName": "maas-eval-",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "maas-evaluation",
                "maas-evaluation.opendatahub.io/correlation": correlation,
            },
        },
        "spec": {
            "pipelineRef": {"name": args.pipeline},
            "taskRunTemplate": {"serviceAccountName": "maas-eval-runner"},
            "params": [
                {"name": "maas-endpoint", "value": args.endpoint.rstrip("/")},
                {"name": "model-secret-name", "value": secret_name},
                {"name": "model-name", "value": args.model},
                {"name": "profile", "value": args.profile},
                {"name": "datasets", "value": datasets},
            ],
            "workspaces": [{
                "name": "results",
                "volumeClaimTemplate": {
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "resources": {"requests": {"storage": args.storage}},
                    },
                },
            }],
            "timeouts": {"pipeline": args.pipeline_timeout, "tasks": args.tasks_timeout, "finally": args.finally_timeout},
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="Approved HTTPS MaaS API base URL")
    parser.add_argument("--model", required=True, help="Served model ID; discovery is not performed for unreadable Secret refs")
    parser.add_argument("--profile", choices=("smoke", "coverage", "full"), default="smoke")
    parser.add_argument("--datasets", required=True, help="Comma-separated IDs or all: hkmmlu,tmmluplus,cmmlu,qualbench")
    parser.add_argument("--secret-ref", help="Reuse an approved tenant Secret without reading its contents")
    parser.add_argument("--namespace", default=os.getenv("PIPELINE_NAMESPACE", "evalhub-lab"))
    parser.add_argument("--pipeline", default="maas-evaluation")
    parser.add_argument("--storage", default="1Gi")
    parser.add_argument("--pipeline-timeout")
    parser.add_argument("--tasks-timeout")
    parser.add_argument("--finally-timeout")
    parser.add_argument("--dry-run", action="store_true", help="Print the PipelineRun manifest without creating a Secret or PipelineRun")
    args = parser.parse_args()
    try:
        datasets = parse_dataset_csv(args.datasets)
        defaults = PROFILE_TIMEOUTS[args.profile]
        args.pipeline_timeout = args.pipeline_timeout or defaults[0]
        args.tasks_timeout = args.tasks_timeout or defaults[1]
        args.finally_timeout = args.finally_timeout or defaults[2]
        if args.profile == "full" and "qualbench" in datasets:
            raise EvaluationError("Full profile cannot include the QualBench diagnostic subset.")
        model = args.model.strip()
        if not model or "\n" in model:
            raise EvaluationError("--model must be a non-empty served model name.")
        correlation = secrets.token_hex(16)
        helper_created = args.secret_ref is None
        if args.secret_ref:
            secret_name = args.secret_ref
        else:
            secret_name = secret_prefix(model) + "-" + secrets.token_hex(6)
        manifest = build_pipeline_run(args, args.namespace, secret_name, datasets, correlation)
        if args.dry_run:
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
            print("No Secret or PipelineRun was created.")
            return
        if helper_created:
            key = getpass.getpass("MaaS API key (input hidden): ")
            if not key:
                raise EvaluationError("MaaS API key cannot be empty.")
            create_secret(args.namespace, secret_name, key, correlation)
            del key
        created = run_oc(["create", "-n", args.namespace, "-f", "-", "-o", "json"], input_bytes=json.dumps(manifest).encode("utf-8"))
        if created.returncode != 0:
            if helper_created:
                _remove_secret_if_run_absent(args.namespace, secret_name, correlation)
            raise EvaluationError("OpenShift could not create the PipelineRun; response values were withheld.")
        try:
            pipelinerun = json.loads(created.stdout)
            name = pipelinerun["metadata"]["name"]
        except (json.JSONDecodeError, KeyError, TypeError):
            name = "created (name unavailable in client response)"
        print(f"PipelineRun: {name}")
        print("Namespace: " + args.namespace)
        print("Profile: " + args.profile)
        print("Datasets: " + ", ".join(datasets))
        print("Model: " + model)
        print("Model Secret reference: " + secret_name)
        uid = None
        try:
            created_meta = json.loads(created.stdout).get("metadata", {})
            uid = created_meta.get("uid")
        except (json.JSONDecodeError, AttributeError):
            pass
        if uid:
            print("PipelineRun UID: " + uid)
            print("Durable run record: ConfigMap " + ledger_name() + ", data key " + run_key_prefix(uid) + ".json")
        print("No MaaS key was written to the PipelineRun.")
        print(f"Follow: tkn pipelinerun logs {name} -n {args.namespace} -f")
    except EvaluationError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
