"""Container entry point for the Tekton runner image."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runner.core import main  # noqa: E402


if __name__ == "__main__":
    main()
