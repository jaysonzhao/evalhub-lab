"""Fetch pinned public benchmark revisions, normalize them, and emit hashes."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from datasets_config import (
    MMLU_SOURCES,
    QUALBENCH_FILE_SHA256,
    QUALBENCH_REPO,
    QUALBENCH_REVISION,
    canonical_json_bytes,
    normalize_mmlu_row,
    normalize_qualbench_rows,
    sha256_bytes,
)


QUALBENCH_URL = (
    "https://raw.githubusercontent.com/mengze-hong/QualBench/"
    f"{QUALBENCH_REVISION}/data/QualBench.json"
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_dataset_file(repo: str, filename: str, revision: str) -> Path:
    from huggingface_hub import hf_hub_download

    return Path(hf_hub_download(
        repo_id=repo,
        filename=filename,
        repo_type="dataset",
        revision=revision,
        token=os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN"),
    ))


def _dataset_files(repo: str, revision: str) -> list[str]:
    from huggingface_hub import HfApi

    return HfApi().list_repo_files(repo_id=repo, repo_type="dataset", revision=revision)


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _load_hkmmlu() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source = MMLU_SOURCES["hkmmlu"]
    files = sorted(
        name for name in _dataset_files(source["repo"], source["revision"])
        if name.startswith("data/multiple_choice/test/") and name.lower().endswith(".csv")
    )
    if len(files) != source["expected_subjects"]:
        raise ValueError(f"HKMMLU expected 66 test subjects, found {len(files)}")
    rows, source_files = [], []
    for filename in files:
        path = _download_dataset_file(source["repo"], filename, source["revision"])
        subject = PurePosixPath(filename).stem
        source_files.append({"path": filename, "sha256": _sha256_file(path), "bytes": path.stat().st_size})
        for index, raw in enumerate(_read_csv(path)):
            rows.append(normalize_mmlu_row("hkmmlu", subject, index, raw))
    if len(rows) != source["expected_rows"]:
        raise ValueError(f"HKMMLU expected {source['expected_rows']:,} rows, found {len(rows):,}")
    return rows, source_files


def _load_tmmluplus() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source = MMLU_SOURCES["tmmluplus"]
    files = sorted(
        name for name in _dataset_files(source["repo"], source["revision"])
        if PurePosixPath(name).name.startswith("test-") and name.endswith(".parquet")
    )
    subjects = sorted({PurePosixPath(name).parent.name for name in files})
    if len(subjects) != source["expected_subjects"]:
        raise ValueError(f"TMMLU+ expected 66 test subjects, found {len(subjects)}")
    import pyarrow.parquet as parquet

    rows, source_files, per_subject_row = [], [], Counter()
    for filename in files:
        path = _download_dataset_file(source["repo"], filename, source["revision"])
        subject = PurePosixPath(filename).parent.name
        source_files.append({"path": filename, "sha256": _sha256_file(path), "bytes": path.stat().st_size})
        for raw in parquet.read_table(path).to_pylist():
            source_row = per_subject_row[subject]
            rows.append(normalize_mmlu_row("tmmluplus", subject, source_row, raw))
            per_subject_row[subject] += 1
    if len(rows) != source["expected_rows"]:
        raise ValueError(f"TMMLU+ expected 20,118 rows, found {len(rows)}")
    return rows, source_files


def _load_cmmlu() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source = MMLU_SOURCES["cmmlu"]
    archive_path = _download_dataset_file(source["repo"], "cmmlu_v1_0_1.zip", source["revision"])
    archive_hash = _sha256_file(archive_path)
    rows, source_files = [], []
    with zipfile.ZipFile(archive_path) as archive:
        names = sorted(
            name for name in archive.namelist()
            if name.startswith("test/") and name.lower().endswith(".csv")
        )
        if len(names) != source["expected_subjects"]:
            raise ValueError(f"CMMLU expected 67 test subjects, found {len(names)}")
        for filename in names:
            payload = archive.read(filename)
            subject = PurePosixPath(filename).stem
            source_files.append({"path": filename, "sha256": sha256_bytes(payload), "bytes": len(payload)})
            with io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8-sig", newline="") as stream:
                for index, raw in enumerate(csv.DictReader(stream)):
                    # The mirror has an unnamed CSV index column; the canonical loader ignores it.
                    rows.append(normalize_mmlu_row("cmmlu", subject, index, raw))
    if len(rows) != source["expected_rows"]:
        raise ValueError(f"CMMLU expected 11,582 rows, found {len(rows)}")
    return rows, [{"path": "cmmlu_v1_0_1.zip", "sha256": archive_hash, "members": source_files}]


def _load_qualbench(correction_file: Path) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    request = urllib.request.Request(QUALBENCH_URL, headers={"User-Agent": "rhoai-evalhub-lab/1.0"})
    with urllib.request.urlopen(request, timeout=90) as response:
        source_bytes = response.read()
    if sha256_bytes(source_bytes) != QUALBENCH_FILE_SHA256:
        raise ValueError("QualBench downloaded bytes do not match the pinned source hash")
    correction_bytes = correction_file.read_bytes()
    rows, audit = normalize_qualbench_rows(source_bytes, correction_bytes)
    source_info = {
        "repo": QUALBENCH_REPO,
        "revision": QUALBENCH_REVISION,
        "file": "data/QualBench.json",
        "sha256": sha256_bytes(source_bytes),
        "bytes": len(source_bytes),
        "correction_manifest_sha256": sha256_bytes(correction_bytes),
    }
    return rows, audit, source_info


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return _sha256_file(path)


def prepare(output_dir: Path, correction_file: Path, include_qualbench: bool = True) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    benchmark_rows: dict[str, list[dict[str, Any]]] = {}
    benchmark_sources: dict[str, Any] = {}
    for name, loader in (
        ("hkmmlu", _load_hkmmlu),
        ("tmmluplus", _load_tmmluplus),
        ("cmmlu", _load_cmmlu),
    ):
        rows, sources = loader()
        if not rows:
            raise ValueError(f"{name} contains no rows")
        benchmark_rows[name] = rows
        benchmark_sources[name] = sources

    qualbench_audit = None
    qualbench_source = None
    if include_qualbench:
        qualbench_rows, qualbench_audit, qualbench_source = _load_qualbench(correction_file)
        benchmark_rows["qualbench"] = qualbench_rows

    manifest: dict[str, Any] = {
        "protocol": "chinese-mcq-zero-shot-strict-v1",
        "benchmark_suite": "all-four" if include_qualbench else "mmlu-three-only-preflight",
        "generated_by": "adapters/chinese-mcq/prepare_datasets.py",
        "preparation_environment": {
            "python": sys.version.split()[0],
            "huggingface_hub": __import__("huggingface_hub").__version__,
            "pyarrow": __import__("pyarrow").__version__,
        },
        "benchmarks": {},
    }
    if include_qualbench:
        manifest.update({
            "qualbench_protocol": "qualbench-zero-shot-strict-v1",
            "qualbench_evaluation_label": qualbench_audit["evaluation_label"],
        })
    for name, rows in benchmark_rows.items():
        path = output_dir / f"{name}.jsonl"
        rows_hash = _write_jsonl(path, rows)
        entry: dict[str, Any] = {
            "row_count": len(rows),
            "canonical_jsonl": path.name,
            "canonical_jsonl_sha256": rows_hash,
            "prompt_language": MMLU_SOURCES[name]["prompt_language"] if name in MMLU_SOURCES else "simplified",
        }
        if name in MMLU_SOURCES:
            entry.update({
                "repository": MMLU_SOURCES[name]["repo"],
                "revision": MMLU_SOURCES[name]["revision"],
                "source_split": "test",
                "source_files": benchmark_sources[name],
                "subjects": sorted({row["subject"] for row in rows}),
                "source_rows_by_subject": {
                    subject: sum(row["subject"] == subject for row in rows)
                    for subject in sorted({row["subject"] for row in rows})
                },
            })
        elif name == "qualbench":
            entry.update({
                "source": qualbench_source,
                "source_split": "released",
                "source_file": "data/QualBench.json",
                "source_row_count": qualbench_audit["source_rows"],
                "evaluated_row_count": qualbench_audit["evaluated_rows"],
                "evaluation_label": qualbench_audit["evaluation_label"],
                "audit": qualbench_audit,
            })
        manifest["benchmarks"][name] = entry

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_bytes(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    manifest["manifest_sha256"] = _sha256_file(manifest_path)
    # A detached hash file avoids a self-referential manifest checksum.
    (output_dir / "manifest.sha256").write_text(manifest["manifest_sha256"] + "  manifest.json\n", encoding="ascii")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--corrections", type=Path,
        default=Path(__file__).resolve().parents[2] / "config" / "qualbench-corrections-diagnostic-v2.json",
    )
    parser.add_argument(
        "--mmlu-only", action="store_true",
        help="Prepare only the three MMLU datasets while QualBench source anomalies are under review.",
    )
    args = parser.parse_args()
    result = prepare(args.output_dir, args.corrections, include_qualbench=not args.mmlu_only)
    print(json.dumps({
        "output_dir": str(args.output_dir),
        "benchmarks": {name: item["row_count"] for name, item in result["benchmarks"].items()},
        "qualbench_audit": result["benchmarks"].get("qualbench", {}).get("audit"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
