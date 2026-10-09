# 运维脚本

计划放置的脚本（实现前保留本说明，见《项目介绍.md》5.4、5.6）：

- `reindex.py`：从 SQLite 权威数据重建 FTS5 与 Chroma 派生索引。
- `check_consistency.py`：比对三处索引的 `source_id` 集合差异。
- `backup.py`：停服状态下打包 SQLite 文件与 Chroma 目录。
