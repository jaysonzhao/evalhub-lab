#!/usr/bin/env python3
"""Verify the bundled canonical datasets and copy them onto the mounted PVC."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path


SOURCE = Path("/bundle")
TARGET = Path(os.environ.get("TARGET_DATA_DIR", "/target"))
BENCHMARKS = {"hkmmlu", "tmmluplus", "cmmlu", "qualbench"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_bundle() -> dict:
    manifest_path = SOURCE / "manifest.json"
    hash_path = SOURCE / "manifest.sha256"
    manifest_bytes = manifest_path.read_bytes()
    detached_hash = hash_path.read_text(encoding="ascii").split()[0]
    if sha256_file(manifest_path) != detached_hash:
        raise SystemExit("dataset bundle manifest checksum does not match manifest.sha256")
    manifest = json.loads(manifest_bytes)
    benchmarks = manifest.get("benchmarks")
    if not isinstance(benchmarks, dict) or not benchmarks:
        raise SystemExit("dataset bundle has no benchmark entries")
    for name, entry in benchmarks.items():
        if name not in BENCHMARKS or not isinstance(entry, dict):
            raise SystemExit(f"unsupported dataset bundle entry: {name!r}")
        filename = entry.get("canonical_jsonl")
        if filename != f"{name}.jsonl":
            raise SystemExit(f"unexpected canonical path for {name}: {filename!r}")
        dataset_path = SOURCE / filename
        if not dataset_path.is_file() or sha256_file(dataset_path) != entry.get("canonical_jsonl_sha256"):
            raise SystemExit(f"canonical dataset checksum mismatch: {name}")
    return manifest


def copy_file_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", delete=False) as stream:
        temporary = Path(stream.name)
        with source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, stream)
    os.chmod(temporary, 0o644)
    temporary.replace(target)


def main() -> None:
    manifest = validate_bundle()
    TARGET.mkdir(parents=True, exist_ok=True)
    names = sorted(manifest["benchmarks"])
    # The PVC is dedicated to benchmark data; remove only known canonical files
    # so an older bundle cannot leave a stale benchmark visible to EvalHub.
    for name in BENCHMARKS:
        (TARGET / f"{name}.jsonl").unlink(missing_ok=True)
    for filename in ("manifest.json", "manifest.sha256", *(f"{name}.jsonl" for name in names)):
        copy_file_atomic(SOURCE / filename, TARGET / filename)
    print(json.dumps({"target": str(TARGET), "benchmarks": names}, sort_keys=True))


if __name__ == "__main__":
    main()
