# 运维脚本

脚本清单（见《项目介绍.md》5.4、5.6）：

- `reindex.py`：从 SQLite 权威数据重建 FTS5 与 Chroma 派生索引。

  ```powershell
  uv run python scripts/reindex.py            # 全量重建
  uv run python scripts/reindex.py --source-id 3
  ```

- `check_consistency.py`：比对三处数据的 `child_id` 集合差异，不一致时退出码为 1。

  ```powershell
  uv run python scripts/check_consistency.py
  ```

- `backup.py`：备份 SQLite（在线一致性快照）与 Chroma 目录，建议先停服。

  ```powershell
  uv run python scripts/backup.py --target backups
  ```
