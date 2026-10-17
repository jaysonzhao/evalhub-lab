#!/usr/bin/env python3
"""Render the Chinese MCQ EvalHub provider definition for an internal image."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = (ROOT / "adapters" / "chinese-mcq" / "provider.yaml").read_text(encoding="utf-8")
    source, count = re.subn(
        r"(?m)^(\s*image:)\s*image-registry\.openshift-image-registry\.svc:5000/\$\{NAMESPACE\}/chinese-mcq-adapter:latest\s*$",
        r"\1 ${ADAPTER_IMAGE}",
        source,
    )
    if count != 1:
        raise SystemExit("Could not find exactly one adapter image field in provider.yaml")
    try:
        rendered = Template(source).substitute(os.environ)
    except KeyError as exc:
        parser.error(f"environment variable {exc.args[0]} is required")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
