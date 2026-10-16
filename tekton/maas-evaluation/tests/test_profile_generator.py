from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(REPOSITORY / "adapters" / "chinese-mcq"))

from datasets_config import QUALBENCH_FILE_SHA256  # noqa: E402
from runner.core import QUALBENCH_CORRECTION_SHA256, QUALBENCH_EXCLUDED_IDS  # noqa: E402


class DataProfileGeneratorTests(unittest.TestCase):
    def test_pinned_manifest_creates_profile_with_mmlu_revisions_and_qualbench_policy(self) -> None:
        counts = {"hkmmlu": (26368, 66), "tmmluplus": (20118, 66), "cmmlu": (11582, 67)}
        benchmarks = {}
        for index, (name, (rows, subject_count)) in enumerate(counts.items()):
            benchmarks[name] = {
                "row_count": rows,
                "canonical_jsonl_sha256": f"{index + 1:064x}",
                "revision": f"{index + 1:040x}",
                "subjects": [f"subject-{n}" for n in range(subject_count)],
            }
        audit = {
            "source_rows": 17316,
            "evaluated_rows": 17305,
            "excluded_ids": QUALBENCH_EXCLUDED_IDS,
            "evaluated_domain_type_counts": {f"cell-{n}": 1 for n in range(17)},
        }
        benchmarks["qualbench"] = {
            "row_count": 17305,
            "canonical_jsonl_sha256": "4" * 64,
            "evaluation_label": "QualBench diagnostic subset (eleven explicitly user-approved source anomalies excluded)",
            "source": {
                "revision": "ca88e81296ecec80ff7bbde1361c50bf0be2cb21",
                "sha256": QUALBENCH_FILE_SHA256,
                "correction_manifest_sha256": QUALBENCH_CORRECTION_SHA256,
            },
            "audit": audit,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({
                "protocol": "chinese-mcq-zero-shot-strict-v1",
                "benchmark_suite": "all-four",
                "benchmarks": benchmarks,
            }), encoding="utf-8")
            digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            (root / "manifest.sha256").write_text(digest + "  manifest.json\n", encoding="ascii")
            output_path = root / "dataset-profile.json"
            subprocess.run([
                sys.executable, str(ROOT / "scripts" / "make_dataset_profile.py"),
                "--manifest", str(manifest_path), "--provider-id", "chinese-mcq-v2",
                "--output", str(output_path),
            ], check=True, capture_output=True, text=True)
            profile = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(profile["datasets"]["hkmmlu"]["source_revision"], f"{1:040x}")
        self.assertEqual(profile["datasets"]["cmmlu"]["coverage_unit_count"], 67)
        self.assertEqual(profile["datasets"]["qualbench"]["row_count"], 17305)
        self.assertEqual(profile["datasets"]["qualbench"]["audit"]["excluded_ids"], QUALBENCH_EXCLUDED_IDS)

    def test_mmlu_only_profile_does_not_require_qualbench_corrections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({
                "protocol": "chinese-mcq-zero-shot-strict-v1",
                "benchmark_suite": "mmlu-three-only-preflight",
                "benchmarks": {
                    "hkmmlu": {
                        "row_count": 26368,
                        "canonical_jsonl_sha256": "1" * 64,
                        "revision": "a" * 40,
                        "subjects": ["subject-a", "subject-b"],
                    },
                },
            }), encoding="utf-8")
            digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            (root / "manifest.sha256").write_text(digest + "  manifest.json\n", encoding="ascii")
            output_path = root / "dataset-profile.json"
            subprocess.run([
                sys.executable, str(ROOT / "scripts" / "make_dataset_profile.py"),
                "--manifest", str(manifest_path), "--provider-id", "chinese-mcq-v2",
                "--output", str(output_path),
            ], check=True, capture_output=True, text=True)
            profile = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(list(profile["datasets"]), ["hkmmlu"])
        self.assertEqual(profile["datasets"]["hkmmlu"]["coverage_unit_count"], 2)


if __name__ == "__main__":
    unittest.main()
