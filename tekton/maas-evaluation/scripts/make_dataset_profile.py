#!/usr/bin/env python3
"""Validate prepared-data/manifest.json and emit a compact runner profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_ROOT))
sys.path.insert(0, str(ROOT / "adapters" / "chinese-mcq"))
from datasets_config import QUALBENCH_FILE_SHA256  # noqa: E402
from runner.core import QUALBENCH_CORRECTION_SHA256, QUALBENCH_EXCLUDED_IDS  # noqa: E402

DATASET_ORDER = ("hkmmlu", "tmmluplus", "cmmlu", "qualbench")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual_sha = digest(manifest_path)
    detached = manifest_path.with_name("manifest.sha256")
    if not detached.exists() or detached.read_text(encoding="ascii").split()[0] != actual_sha:
        raise SystemExit("Prepared-data manifest.sha256 does not match manifest.json.")
    entries = manifest.get("benchmarks", {})
    if not isinstance(entries, dict) or not entries:
        raise SystemExit("Prepared data manifest must contain at least one benchmark.")
    unknown = sorted(set(entries) - set(DATASET_ORDER))
    if unknown:
        raise SystemExit("Prepared data manifest contains unknown benchmark ID(s): " + ", ".join(unknown))
    selected = tuple(dataset for dataset in DATASET_ORDER if dataset in entries)
    result = {
        "schema_version": "maas-eval-data-profile.v1",
        "provider_id": args.provider_id,
        "protocol": manifest.get("protocol"),
        "manifest_sha256": actual_sha,
        "source_manifest": {
            "path": manifest_path.name,
            "protocol": manifest.get("protocol"),
            "benchmark_suite": manifest.get("benchmark_suite", "all-four"),
        },
        "datasets": {},
    }
    for dataset in selected:
        entry = entries[dataset]
        source = entry.get("source", {})
        if dataset == "qualbench":
            audit = entry.get("audit", {})
            if (
                entry.get("row_count") != 17305
                or audit.get("source_rows") != 17316
                or audit.get("evaluated_rows") != 17305
                or audit.get("excluded_ids") != QUALBENCH_EXCLUDED_IDS
                or source.get("sha256") != QUALBENCH_FILE_SHA256
                or source.get("correction_manifest_sha256") != QUALBENCH_CORRECTION_SHA256
                or "diagnostic subset" not in str(entry.get("evaluation_label", "")).lower()
            ):
                raise SystemExit("QualBench manifest does not match the approved diagnostic-subset policy.")
            coverage_count = len(audit.get("evaluated_domain_type_counts", {}))
            source_revision = source.get("revision")
        else:
            source_revision = entry.get("revision")
            coverage_count = len(entry.get("subjects", []))
        canonical_sha = entry.get("canonical_jsonl_sha256")
        if not canonical_sha or not source_revision or not entry.get("row_count"):
            raise SystemExit(f"Prepared data entry {dataset} is missing source/hash/count metadata.")
        result["datasets"][dataset] = {
            "row_count": int(entry["row_count"]),
            "coverage_unit_count": int(coverage_count),
            "manifest_sha256": actual_sha,
            "canonical_jsonl_sha256": canonical_sha,
            "source_revision": source_revision,
            "evaluation_label": entry.get("evaluation_label", dataset),
        }
        if dataset == "qualbench":
            result["datasets"][dataset].update({
                "source_row_count": audit["source_rows"],
                "correction_manifest_sha256": source["correction_manifest_sha256"],
                "audit": {
                    "source_rows": audit["source_rows"],
                    "evaluated_rows": audit["evaluated_rows"],
                    "excluded_ids": audit["excluded_ids"],
                    "observed_strata": audit.get("evaluated_domain_type_counts", {}),
                },
            })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "manifest_sha256": actual_sha, "datasets": list(selected)}, indent=2))


if __name__ == "__main__":
    main()
