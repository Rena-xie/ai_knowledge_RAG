"""运行评测：uv run python scripts/run_eval.py [--questions ...] [--output ...]"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    from app.cli.eval import run_eval

    return run_eval()


if __name__ == "__main__":
    raise SystemExit(main())
