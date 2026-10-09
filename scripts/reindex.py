"""重建派生索引：uv run python scripts/reindex.py [--source-id N]"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    from app.cli.maintenance import run_reindex

    return run_reindex()


if __name__ == "__main__":
    raise SystemExit(main())
