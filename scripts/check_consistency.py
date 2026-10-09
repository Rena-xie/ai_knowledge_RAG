"""校验三处数据一致性：uv run python scripts/check_consistency.py"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    from app.cli.maintenance import run_consistency

    return run_consistency()


if __name__ == "__main__":
    raise SystemExit(main())
