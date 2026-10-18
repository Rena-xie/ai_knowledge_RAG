# AI Knowledge Base

面向个人 AI 学习的网页知识采集、混合检索与有据问答系统。

[![CI](https://github.com/Rena-xie/ai_knowledge_RAG/actions/workflows/ci.yml/badge.svg)](https://github.com/Rena-xie/ai_knowledge_RAG/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## 项目简介

学习资料散落在大量公开网页中：收藏即失联、检索靠记忆、引用无出处。

本项目把这些网页变成一座**可在本地运行、可追溯来源的知识库**：

> 提交一个公开网页 URL → 抓取并提取正文 → 结构化切块 → 建立向量与全文双索引 →
> 用自然语言提问 → 得到**每条结论都能点回原文**的回答。

设计目标是"功能完整、可正常运行、数据可重建"：SQLite 是唯一权威存储，FTS5 与
Chroma 是可随时重建的派生索引；回答必须携带服务端校验过的引用；证据不足时明确
拒答，而不是让模型自由发挥。

## 核心功能

| 能力 | 实现要点 |
| --- | --- |
| **网页知识收录** | URL 规范化与去重、SSRF 防护、`robots.txt` 合规检查、同主机限速、正文提取、结构感知切块、批量向量化、双索引写入 |
| **来源管理** | 分页列表、详情与状态查询、错误码回传、级联删除（关系数据 + 两处派生索引）、重复 URL 幂等处理 |
| **混合检索** | Dense 向量召回 + BM25 稀疏召回并行执行，RRF 融合后恢复完整 Parent 上下文；支持相关性门槛与融合头部保底 |
| **有据问答** | 证据按不可信输入包裹，模型只能引用上下文内标识；服务端逐条校验引用，无效引用剔除并标记；证据不足返回固定话术 |
| **可运维** | 启动自检、索引全量/按来源重建、三处数据一致性校验、数据备份、滚动日志、`request_id` 全链路追踪 |
| **工程质量** | 223 个测试、分支覆盖率 90.29%（门禁 85%）、Ruff + mypy 零错误、GitHub Actions CI |

## 系统架构

```mermaid
flowchart TB
    subgraph CLIENT["调用方"]
        UI["浏览器 / API 客户端"]
        AGENT["AI Learning Agent（预留接入）"]
    end

    subgraph APILAYER["API 层 · FastAPI"]
        SRC["/api/v1/sources<br/>来源收录与管理"]
        SEARCH["/api/v1/search<br/>检索调试"]
        QUERY["/api/v1/query<br/>有据问答"]
        HEALTH["/health/live · /health/ready"]
    end

    subgraph SERVICE["应用服务层"]
        S1["SourceService"]
        S2["IngestionService"]
        S3["RetrievalService"]
        S4["QAService"]
    end

    subgraph DOMAIN["领域逻辑层 · 纯函数"]
        D1["URL 规范化<br/>状态机"]
        D2["结构感知切块<br/>Parent / Child"]
        D3["RRF 融合<br/>相关性门槛"]
        D4["引用解析与校验"]
    end

    subgraph INFRA["基础设施层 · 适配器"]
        DB[("SQLite<br/>权威存储")]
        FTS[("FTS5 + jieba<br/>BM25 索引")]
        VEC[("Chroma<br/>向量索引")]
        FETCH["HTTPX 抓取<br/>SSRF / robots.txt"]
        EMB["Embedding API<br/>bge-m3"]
        LLM["LLM API<br/>DeepSeek"]
    end

    UI --> SRC & SEARCH & QUERY & HEALTH
    AGENT -.-> SRC & QUERY
    SRC --> S1 --> S2
    SEARCH --> S3
    QUERY --> S3 --> S4
    S2 --> D1 --> FETCH --> D2 --> EMB
    S2 --> DB & FTS & VEC
    S3 --> EMB & FTS & VEC --> D3 --> DB
    S4 --> LLM --> D4
```

两条核心流水线：

```text
入库：URL 校验 → 抓取/正文提取 → 结构化切块 → Embedding → FTS5 + Chroma 写入 → 状态更新
问答：问题 → Dense + BM25 双路召回 → RRF 融合 → 父级上下文恢复 → 相关性门槛 → LLM → 引用校验
```

### 关键设计取舍

- **权威存储与派生索引分离**：SQLite 保存来源与切块；FTS5、Chroma 均可从关系库
  全量重建，索引损坏或更换向量库不需要人工修复数据。
- **Parent / Child 两级切块**：Parent 保留章节完整性，Child 是检索与引用的最小单位；
  标题路径参与索引以提升专有名词召回，命中后恢复父级上下文避免语义割裂。
- **双路召回而不是单向量检索**：BM25 补足专有名词与代码符号（`//`、`==`、`->`），
  RRF 只使用物理排名融合，避免两路分数量纲不一致。
- **跨三处存储用补偿清理**：SQLite、FTS5、Chroma 无法原子提交，写入失败时回滚已写
  部分，宁可失败可见，也不留下"只有向量库有、关系库没有"的脏数据。
- **单机优先**：模块化单体、同步入库、单进程运行，刻意不引入 Docker、Redis 与任务
  队列；用启动自检和 60 秒入库预算控制运行边界。

完整设计、状态机、错误码与 13 条架构决策记录见
[《项目开发说明_v1.md》](项目开发说明_v1.md)（唯一权威设计文档）。

## 项目目录

```text
.
├── app/
│   ├── main.py                 # 应用装配：路由注册、中间件、生命周期
│   ├── api/                    # HTTP 层：路由与 Pydantic 模型，不含业务逻辑
│   │   ├── sources.py          # 来源收录、列表、详情、删除
│   │   ├── search.py           # 检索调试接口
│   │   ├── query.py            # 有据问答接口
│   │   ├── health.py           # 存活 / 就绪检查
│   │   ├── middleware.py       # request_id、请求体大小限制
│   │   ├── errors.py           # 统一错误响应
│   │   └── schemas.py          # 请求 / 响应数据模型
│   ├── services/               # 应用服务层：编排业务流程
│   │   ├── source.py           # 来源生命周期与级联删除
│   │   ├── ingestion.py        # 抓取 → 解析 → 切块 → 向量化 → 双索引
│   │   ├── retrieval.py        # 双路召回 → RRF → 父级上下文
│   │   ├── qa.py               # 提示词组装、引用校验、证据不足处理
│   │   ├── evaluation.py       # 检索指标计算
│   │   └── maintenance.py      # 重建索引、一致性校验、备份
│   ├── domain/                 # 领域逻辑层：纯函数，无 I/O
│   │   ├── chunking.py         # 结构感知 Parent / Child 切块
│   │   ├── rrf.py              # 倒数排名融合
│   │   ├── citations.py        # 引用解析与校验
│   │   ├── state.py            # 来源状态机
│   │   └── url.py              # URL 规范化与鉴重
│   ├── infra/                  # 基础设施层：外部依赖适配器
│   │   ├── db.py               # SQLite / SQLAlchemy
│   │   ├── fts.py              # FTS5 全文索引
│   │   ├── vector.py           # Chroma 向量索引
│   │   ├── fetcher.py          # 网页抓取与重试
│   │   ├── transport.py        # SSRF 防护：固定已校验 IP 建连
│   │   ├── robots.py           # robots.txt 检查与缓存
│   │   ├── parser.py           # 正文提取
│   │   ├── embedding.py        # Embedding API 客户端
│   │   └── llm.py              # LLM API 客户端
│   ├── core/                   # 配置、日志、错误码、启动自检
│   └── prompts/                # 分层提示词模板（system / user）
├── tests/
│   ├── unit/                   # 领域逻辑单测
│   ├── integration/            # 服务与 API（临时 SQLite / Chroma）
│   ├── e2e/                    # 全流水线（本地夹具 + 假外部客户端）
│   └── evals/                  # 评测语料、题库与历史结果
├── scripts/                    # 索引重建、一致性校验、备份、评测
│   ├── reindex.py
│   ├── check_consistency.py
│   ├── backup.py
│   └── run_eval.py
├── .github/workflows/ci.yml    # ruff + mypy + pytest（覆盖率门槛 85%）
├── pyproject.toml
├── uv.lock                     # 依赖完全锁定
└── .env.example                # 配置项与默认值
```

## 技术栈

| 层次 | 选型 |
| --- | --- |
| 语言与工程 | Python 3.13、uv、Ruff、mypy、pytest、GitHub Actions |
| Web 框架 | FastAPI、Uvicorn、Pydantic Settings |
| 数据存储 | SQLite + SQLAlchemy（权威）、SQLite FTS5 + jieba（BM25）、Chroma（向量） |
| 采集与解析 | HTTPX、Trafilatura |
| 外部模型 | SiliconFlow `BAAI/bge-m3`（Embedding）、DeepSeek `deepseek-flash`（生成） |

## 效果与验证

评测语料为真实公开文档站点（廖雪峰 Python 教程 + LangChain Deep Agents 文档）：
124 个 URL、成功入库 123 页、2590 个 Child 切块，22 道题覆盖专有名词、语义改写、
代码查询、跨章节与无答案五类场景。

| 指标 | 结果 |
| --- | --- |
| Recall@5 | **0.7647**（优化前 0.7059） |
| Precision@5 | 0.2235 |
| 引用有效率 | **1.0** |
| 无答案识别率 | **1.0**（优化前 0.0） |

工程验证（2026-10-16 本机复跑）：

| 检查 | 结果 |
| --- | --- |
| `pytest --cov` | 223 passed，分支覆盖率 **90.29%** |
| `ruff check .` | All checks passed |
| `mypy` | 47 个源文件，无错误 |

一次问答调用的响应结构（示意）：

```json
{
  "answer": "BM25 用于补足向量检索对专有名词和代码符号不敏感的问题……[S12:C57]",
  "answer_type": "answered",
  "citations": [
    {
      "source_id": 12,
      "child_id": 57,
      "title": "RAG 中的 BM25 实践",
      "url": "https://example.com/rag-bm25",
      "heading_path": "RAG / Retrieval / BM25"
    }
  ],
  "citations_validated": true,
  "usage": {"prompt_tokens": 1832, "completion_tokens": 96}
}
```

## 已知限制与路线图

V1 明确冻结在"单机、单用户、无前端"范围内：

- 仅监听 `127.0.0.1`，**没有鉴权**；不要直接暴露到局域网或公网。
- 无 Web 界面，通过 `/docs` 或 API 客户端操作。
- 一次收录一个 URL，入库为同步调用（总预算 60 秒）；不做站点级爬虫与增量抓取。
- 只解析静态 HTML 正文，不支持 PDF、图片、音视频与 JS 动态渲染页面。
- 真实语料 Recall@5 为 0.7647，低于项目自定的 0.8 目标；未命中集中在语义改写与
  长文档定位，V2 计划引入 Query Rewrite 与 Cross-Encoder 重排序。
- 生成侧目前只评估引用有效性与无答案识别率，尚未覆盖答案忠实性指标。

## 部署指南

### 前置要求

- Python 3.13（由 `.python-version` 锁定；uv 缺失时会自动下载对应解释器）
- [uv](https://docs.astral.sh/uv/)
- 两个外部 API Key：SiliconFlow（Embedding）与 DeepSeek（生成）。调用会产生费用。
- 主力开发与验收环境为 Windows；CI 在 Ubuntu 上执行同一套测试。

> **Chroma 路径必须是纯 ASCII**：`CHROMA_PATH` 解析为绝对路径后若包含中文等非 ASCII
> 字符，chromadb 重启后会无法读取 HNSW 索引。启动自检会直接拒绝这类配置；可把
> `CHROMA_PATH` 指向项目外的 ASCII 目录（如 `E:\ai_kb_data\chroma`）。

### 1. 获取代码并安装依赖

```powershell
git clone https://github.com/Rena-xie/ai_knowledge_RAG.git
cd ai_knowledge_RAG
uv sync --frozen
```

### 2. 配置环境变量

```powershell
Copy-Item .env.example .env
```

macOS / Linux 使用 `cp .env.example .env`。然后编辑 `.env`，至少填写：

```dotenv
EMBEDDING_API_KEY=你的 SiliconFlow Key
LLM_API_KEY=你的 DeepSeek Key
```

其余参数均有默认值，含义见 [《项目开发说明_v1.md》10.2](项目开发说明_v1.md)。`.env` 已被
`.gitignore` 排除，不会进入版本库。

### 3. 启动服务

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

**必须保持单进程（`--workers 1`）**：进程内入库锁与 Chroma PersistentClient 都依赖
单进程约束，多进程会导致重复入库与索引冲突。

启动时会校验必填配置，并自动完成自检（SQLite/FTS5 可用、数据目录可写），同时回收
上次中断的入库记录。任一项失败会直接拒绝启动并给出原因。

### 4. 验证部署

打开交互式文档：<http://127.0.0.1:8000/docs>

```powershell
# 健康检查
curl.exe http://127.0.0.1:8000/health/live
curl.exe http://127.0.0.1:8000/health/ready

# 收录一个公开网页（同步完成，单次预算 60 秒）
curl.exe -X POST http://127.0.0.1:8000/api/v1/sources `
  -H "Content-Type: application/json" `
  -d '{"url":"https://example.com"}'

# 基于知识库提问
curl.exe -X POST http://127.0.0.1:8000/api/v1/query `
  -H "Content-Type: application/json" `
  -d '{"question":"这个页面讲了什么？"}'
```

`/api/v1/search` 是调试接口，会返回 Dense、BM25、RRF 三组结果与耗时，便于定位检索
问题；`/api/v1/query` 返回答案、结构化引用与 `citations_validated` 标记。

> 示例使用单引号包裹 JSON（PowerShell 7 / bash 通用）。Windows PowerShell 5.1 若出现
> 引号被吞，改用 `-d "{\"url\":\"https://example.com\"}"` 或直接在 `/docs` 页面调试。

### 5. 常用运维命令

```powershell
# 从 SQLite 全量重建派生索引（也可 --source-id 3 只重建单个来源）
uv run python scripts/reindex.py

# 校验三处数据的 child_id 集合是否一致，不一致时退出码为 1
uv run python scripts/check_consistency.py

# 备份 SQLite 与 Chroma（建议先停服）
uv run python scripts/backup.py --target backups

# 运行检索与引用评测（会写入独立评测库，可重复执行）
uv run python scripts/run_eval.py
```

### 6. 运行测试

```powershell
uv run pytest -q
uv run pytest --cov --cov-report=term-missing
uv run ruff check .
uv run mypy
```

### 7. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| `uv trampoline failed to canonicalize script path` | 项目目录被移动/重命名导致 `.venv` 启动器失效。删除 `.venv` 后重新执行 `uv sync`（由 `uv.lock` 完整复现）。 |
| 启动时报 Chroma 路径含非 ASCII | 将 `CHROMA_PATH` 指向纯 ASCII 目录后重启。 |
| 入库返回 `INGEST_BUSY` | V1 入库串行，等待当前任务完成后重试。 |
| 页面抓取返回 `ROBOTS_DISALLOWED` | 目标站点 `robots.txt` 禁止抓取，属于预期行为。 |
| 问答返回 `insufficient_evidence` | 检索最高相似度低于 `MIN_DENSE_SIMILARITY`（默认 0.58）；确认库内已收录相关内容，换 Embedding 模型后需重新校准该阈值。 |

## 文档与许可

- 架构、接口契约、状态机与决策记录：[项目开发说明_v1.md](项目开发说明_v1.md)
- V2 开发计划与验收标准：[项目开发说明_v2.md](项目开发说明_v2.md)
- 运维脚本说明：[scripts/README.md](scripts/README.md)
- 评测集说明：[tests/evals/README.md](tests/evals/README.md)

本项目以 [MIT 许可证](LICENSE) 开源。
