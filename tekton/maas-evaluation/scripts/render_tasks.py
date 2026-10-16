#!/usr/bin/env python3
"""Render Tekton Tasks with a registry image pinned by immutable digest."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}$")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-image", required=True, help="Published runner image reference ending in @sha256:<64 hex>")
    parser.add_argument("--namespace", default="evalhub-lab", help="Namespace used by the runner ServiceAccount RoleBinding")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "dist")
    args = parser.parse_args()
    if not IMAGE.fullmatch(args.runner_image):
        parser.error("--runner-image must be an immutable image digest, not a tag")
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", args.namespace):
        parser.error("--namespace must be a valid Kubernetes namespace name")
    root = Path(__file__).resolve().parents[1]
    tasks = (root / "tasks" / "tasks.yaml.tmpl").read_text(encoding="utf-8")
    if tasks.count("__RUNNER_IMAGE__") != 6:
        raise SystemExit("Task template image placeholder count changed; review before rendering.")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "tasks.yaml").write_text(tasks.replace("__RUNNER_IMAGE__", args.runner_image), encoding="utf-8")
    for name in ("pipeline.yaml", "pipelinerun.example.yaml", "rbac.yaml", "ledger.yaml"):
        content = (root / name).read_text(encoding="utf-8")
        if name == "rbac.yaml":
            content = content.replace("__PIPELINE_NAMESPACE__", args.namespace)
        (args.output_dir / name).write_text(content, encoding="utf-8")
    print(f"Rendered Tasks with pinned image {args.runner_image}")
    print(f"Output: {args.output_dir}")


if __name__ == "__main__":
    main()
