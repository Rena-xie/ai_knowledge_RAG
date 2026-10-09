"""备份数据：uv run python scripts/backup.py [--target DIR]"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    from app.cli.maintenance import run_backup

    return run_backup()


if __name__ == "__main__":
    raise SystemExit(main())
