# AI Knowledge Base

面向个人 AI 学习的网页知识采集与检索系统：收录公开网页 → 结构化切块 → 双路检索（Dense + BM25 + RRF）→ 大模型生成带来源引用的回答。

设计与开发规范见 [项目介绍.md](项目介绍.md)（唯一权威文档）；开发前请先读该文档的「摘要」与第 12 章。

## 环境要求

- Windows 本地单机；Python 3.13（由 `.python-version` 锁定）
- [uv](https://docs.astral.sh/uv/) 管理虚拟环境与依赖

## 快速开始

```powershell
uv sync                                  # 创建 .venv 并安装依赖
Copy-Item .env.example .env              # 填写 API 密钥与模型配置
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

启动后访问 <http://127.0.0.1:8000/docs>。

## 常用命令

| 目的 | 命令 |
| --- | --- |
| 安装 / 同步依赖 | `uv sync` |
| 运行测试 | `uv run pytest -q` |
| 静态检查 | `uv run ruff check .` |
| 格式化 | `uv run ruff format .` |

## 运行约束

- 必须单进程运行（`--workers 1`），原因见《项目介绍.md》10.6。
- `.env` 与 `data/`（SQLite、Chroma 数据）不纳入 Git 管理。
- 目录结构说明见《项目介绍.md》4.6。
