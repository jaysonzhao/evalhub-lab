#!/usr/bin/env python3
"""Render a small deployment template using explicitly supplied environment values."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from string import Template


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = args.template.read_text(encoding="utf-8")
    try:
        rendered = Template(source).substitute(os.environ)
    except KeyError as exc:
        parser.error(f"environment variable {exc.args[0]} is required")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
