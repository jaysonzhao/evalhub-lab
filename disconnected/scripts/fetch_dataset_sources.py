#!/usr/bin/env python3
"""Stage the repository-pinned source files for later offline preparation."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "adapters" / "chinese-mcq"))

from datasets_config import (  # noqa: E402
    MMLU_SOURCES,
    QUALBENCH_FILE_SHA256,
    QUALBENCH_REPO,
    QUALBENCH_REVISION,
)

QUALBENCH_URL = (
    "https://raw.githubusercontent.com/mengze-hong/QualBench/"
    f"{QUALBENCH_REVISION}/data/QualBench.json"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cache_path(root: Path, repo: str, revision: str, filename: str) -> Path:
    relative = PurePosixPath(filename)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe repository filename: {filename!r}")
    return root / repo.replace("/", "__") / revision / Path(*relative.parts)


def stage_file(
    root: Path,
    repo: str,
    revision: str,
    filename: str,
    payload_path: Path,
) -> dict[str, Any]:
    destination = cache_path(root, repo, revision, filename)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    shutil.copyfile(payload_path, temporary)
    temporary.replace(destination)
    return {
        "path": filename,
        "sha256": sha256_file(destination),
        "bytes": destination.stat().st_size,
    }


def fetch_sources(output_dir: Path, include_qualbench: bool = True) -> dict[str, Any]:
    from huggingface_hub import HfApi, hf_hub_download

    output_dir.mkdir(parents=True, exist_ok=True)
    api = HfApi()
    sources: dict[str, Any] = {}
    for dataset, source in MMLU_SOURCES.items():
        repo = source["repo"]
        revision = source["revision"]
        names = api.list_repo_files(repo_id=repo, repo_type="dataset", revision=revision)
        if dataset == "hkmmlu":
            selected = sorted(
                name for name in names
                if name.startswith("data/multiple_choice/test/") and name.lower().endswith(".csv")
            )
        elif dataset == "tmmluplus":
            selected = sorted(
                name for name in names
                if PurePosixPath(name).name.startswith("test-") and name.endswith(".parquet")
            )
        else:
            selected = ["cmmlu_v1_0_1.zip"] if "cmmlu_v1_0_1.zip" in names else []
        if not selected:
            raise ValueError(f"No pinned test data files found for {dataset} at {repo}@{revision}")

        records = []
        for filename in selected:
            cached = Path(hf_hub_download(
                repo_id=repo,
                filename=filename,
                repo_type="dataset",
                revision=revision,
            ))
            records.append(stage_file(output_dir, repo, revision, filename, cached))
        sources[repo] = {"revision": revision, "files": records}
        print(f"Staged {dataset}: {len(records)} source file(s)")

    if include_qualbench:
        request = urllib.request.Request(QUALBENCH_URL, headers={"User-Agent": "rhoai-evalhub-lab/1.0"})
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = response.read()
        actual_hash = hashlib.sha256(payload).hexdigest()
        if actual_hash != QUALBENCH_FILE_SHA256:
            raise ValueError("QualBench downloaded bytes do not match the repository-pinned SHA-256")
        temp_source = output_dir / ".qualbench-source.partial"
        temp_source.write_bytes(payload)
        record = stage_file(
            output_dir, QUALBENCH_REPO, QUALBENCH_REVISION, "data/QualBench.json", temp_source,
        )
        temp_source.unlink()
        sources[QUALBENCH_REPO] = {"revision": QUALBENCH_REVISION, "files": [record]}
        print("Staged qualbench: 1 source file")

    manifest = {
        "schema_version": 1,
        "source_set": "mmlu-three-only" if not include_qualbench else "all-four",
        "generated_by": "disconnected/scripts/fetch_dataset_sources.py",
        "sources": sources,
    }
    manifest_path = output_dir / "source-manifest.json"
    encoded = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary = manifest_path.with_name(manifest_path.name + ".partial")
    temporary.write_bytes(encoded)
    temporary.replace(manifest_path)
    manifest_hash = hashlib.sha256(encoded).hexdigest()
    (output_dir / "source-manifest.sha256").write_text(
        f"{manifest_hash}  source-manifest.json\n", encoding="ascii",
    )
    print(f"Source cache manifest SHA-256: {manifest_hash}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mmlu-only", action="store_true", help="Do not stage QualBench")
    args = parser.parse_args()
    fetch_sources(args.output_dir, include_qualbench=not args.mmlu_only)


if __name__ == "__main__":
    main()
