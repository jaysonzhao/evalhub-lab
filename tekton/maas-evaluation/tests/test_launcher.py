from __future__ import annotations

import base64
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import launch_eval  # noqa: E402


class LaunchHelperTests(unittest.TestCase):
    def test_secret_manifest_is_generated_without_creating_twice_or_putting_key_in_argv(self) -> None:
        key = "SENTINEL_API_KEY_FOR_TEST_ONLY"
        calls = []

        def fake_oc(args, input_bytes=None):
            calls.append((args, input_bytes))
            if "--dry-run=client" in args:
                manifest = {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "maas-gpt-oss-20b-012345abcdef"},
                    "data": {"api-key": base64.b64encode(key.encode()).decode()},
                }
                return subprocess.CompletedProcess(args, 0, json.dumps(manifest).encode(), b"")
            return subprocess.CompletedProcess(args, 0, b"created", b"")

        original = launch_eval.run_oc
        launch_eval.run_oc = fake_oc
        try:
            launch_eval.create_secret("evalhub-lab", "maas-gpt-oss-20b-012345abcdef", key, "abc123")
        finally:
            launch_eval.run_oc = original

        self.assertEqual(len(calls), 2)
        self.assertIn("--dry-run=client", calls[0][0])
        self.assertEqual(calls[0][1], key.encode())
        self.assertNotIn(key, " ".join(calls[0][0] + calls[1][0]))
        stored_manifest = json.loads(calls[1][1])
        self.assertTrue(stored_manifest["immutable"])
        self.assertEqual(stored_manifest["data"]["api-key"], base64.b64encode(key.encode()).decode())
        self.assertEqual(stored_manifest["metadata"]["labels"]["maas-evaluation.opendatahub.io/correlation"], "abc123")


if __name__ == "__main__":
    unittest.main()
