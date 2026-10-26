# v2 剩余任务清单

依据：`PROJECT.md` 与 `项目开发说明_v2.md`。按依赖顺序执行；每个任务完成并通过验证后再进入下一个。

## Phase 1：数据基础与来源身份

## Task 1：建立 schema v4 与默认集合迁移

**Description:** 增加来源别名、集合、标签、来源身份元数据和幂等时间桶的 schema 基础，把现有来源关联到默认集合，不访问外网、不合并历史重复来源。

**Acceptance criteria:**
- [x] v3 数据库副本可幂等升级到 schema v4。
- [x] 既有来源全部关联默认集合，关系数据和 Child 数量不丢失。
- [x] 未执行历史重复来源删除、合并或重新抓取。
- [x] 幂等时间桶表完成迁移，旧 Job 可按 `created_at` 回填。

**Verification:**
- [x] `pytest tests/unit/test_db.py -q`
- [x] 使用 v3 数据库副本检查表、外键、唯一索引和行数。

**Dependencies:** None

**Files likely touched:**
- `app/infra/db.py`
- `tests/unit/test_db.py`

**Estimated scope:** Medium — 2 files

## Task 2：实现最终 URL、来源别名和 Job 身份字段

**Description:** 以重定向后的最终 URL 更新来源长期身份，保留提交 URL、重定向 URL 和 HTML canonical/AMP 线索为别名；Job Item 暴露最终 URL。

**Acceptance criteria:**
- [x] 新来源 `canonical_url` 是最终规范化 URL，提交 URL 仍可追溯。
- [x] 同一来源刷新时最终 URL 改变会追加别名并保持 `source_id`。
- [x] Job Item 包含 `final_url`，不存在别名时回退到初始 canonical URL。

**Verification:**
- [x] `pytest tests/integration/test_ingestion_service.py tests/integration/test_ingestions_api.py -q`
- [x] 使用重定向夹具验证提交 URL、最终 URL 和别名。

**Dependencies:** Task 1

**Files likely touched:**
- `app/services/source.py`
- `app/services/ingestion.py`
- `app/services/ingestion_jobs.py`
- `app/api/schemas.py`
- `tests/integration/test_ingestion_service.py`

**Estimated scope:** Medium — 5 files

## Task 3：实现正文指纹和新入库精确去重

**Description:** 对规范化正文计算版本化 SHA-256，跨 URL 复用相同正文的既有来源；刷新与另一来源冲突时保留旧数据并失败。

**Acceptance criteria:**
- [x] 新 URL 与既有 completed 来源正文完全相同时，不新增来源、Child、FTS 或 Chroma 数据。
- [x] 重复 Item 标记 skipped / `duplicate_content` 并返回既有 `source_id`。
- [x] 刷新正文与其他来源重复时失败，原来源数据和索引保持可用。
- [x] 升级前历史重复来源不自动合并。

**Verification:**
- [x] `pytest tests/unit/test_fingerprint.py tests/integration/test_ingestion_service.py tests/integration/test_ingestions_api.py -q`
- [x] AMP、打印版、镜像和普通 URL 使用同一内容夹具验证只保留一套索引。

**Dependencies:** Task 2

**Files likely touched:**
- `app/domain/fingerprint.py`
- `app/services/source_identity.py`
- `app/services/ingestion.py`
- `tests/unit/test_fingerprint.py`
- `tests/integration/test_ingestion_service.py`

**Estimated scope:** Medium — 5 files

## Checkpoint A：数据身份

- [x] schema v4 迁移通过。
- [x] 最终 URL 和别名可追溯。
- [x] 新入库与刷新去重正确，历史数据保持原状。
- [x] 人工审查后进入内容质量阶段。

## Phase 2：内容质量与双语

## Task 4：建立中英文正文质量黄金集

**Description:** 建立至少 30 个固定网页快照和人工标注，覆盖中文、英文、混合内容、标题、段落、列表、代码、表格和长章节。

**Acceptance criteria:**
- [x] 黄金集不依赖实时网络，CI 可离线运行。
- [x] 报告标题、正文召回、噪声和关键结构块的逐页结果。
- [x] 当前实现基线被记录，不以暂未达标为由修改阈值。

**Verification:**
- [x] `uv run pytest tests/content_quality/test_extraction_quality.py -q`
- [x] 人工抽查黄金集标注和至少 5 个失败样本。

**Dependencies:** None；测试资产可与 Task 1–3 并行建设。

**Files likely touched:**
- `tests/content_quality/cases.json`
- `tests/content_quality/test_extraction_quality.py`
- `scripts/content_quality_report.py`

**Estimated scope:** Medium — 3 files

## Task 5：增加确定性语言元数据

**Description:** 根据正文中的 CJK、拉丁字符比例写入 `zh/en/mixed/unknown`，仅用于元数据和分语言评测，不新增模型依赖。

**Acceptance criteria:**
- [x] 中文、英文、混合和空正文样本分类稳定。
- [x] 语言元数据随来源预览返回，不改变检索路径。
- [x] 语言判定只使用确定性规则，不产生网络调用。

**Verification:**
- [x] `uv run pytest tests/unit/test_language.py tests/integration/test_ingestion_service.py -q`

**Dependencies:** Task 4

**Files likely touched:**
- `app/domain/language.py`
- `app/services/ingestion.py`
- `tests/unit/test_language.py`
- `tests/integration/test_ingestion_service.py`

**Estimated scope:** Small — 4 files

## Task 6：按黄金集修正正文提取

**Description:** 先读取 Task 4 基线，只修正已证实失败的标题、正文或结构提取，不进行无证据的解析器重写。

**Acceptance criteria:**
- [x] 标题准确率 ≥ 95%。
- [x] 正文字符召回 ≥ 95%，非正文噪声 ≤ 10%。
- [x] 关键结构保留率 ≥ 95%，中英文各自报告结果。
- [x] 解析器变化不降低已有上游夹具通过率。

**Verification:**
- [x] `uv run pytest tests/content_quality tests/unit/test_parser.py tests/unit/test_chunking_parse.py -q`
- [x] 对比 Task 4 基线并记录失败样本变化。

**Dependencies:** Task 4, Task 5

**Files likely touched:**
- `app/infra/parser.py`
- `app/domain/chunking.py`
- `tests/content_quality/cases.json`
- `tests/unit/test_parser.py`

**Estimated scope:** Medium — 4 files

## Checkpoint B：内容质量

- [x] 黄金集离线可重复。
- [x] 中英文解析达到批准阈值。
- [x] 没有引入本地模型或新运行时依赖。
- [x] 人工审查失败样本后进入来源预览阶段。

## Phase 3：来源预览与知识组织

## Task 7：实现来源正文和切块只读预览

**Description:** 增加来源元数据、别名、集合、标签、Parent 和 Child 的分页只读查询。

**Acceptance criteria:**
- [x] completed 来源可分页返回完整正文树。
- [x] 非 completed 来源返回 `SOURCE_CONTENT_NOT_READY`。
- [x] 预览顺序稳定，内容与 SQLite 一致，不提供修改接口。

**Verification:**
- [x] `uv run pytest tests/integration/test_api_sources.py -q`
- [x] 长文档分页边界和空 Parent/Child 异常数据测试。

**Dependencies:** Task 3

**Files likely touched:**
- `app/services/source_inspection.py`
- `app/api/sources.py`
- `app/api/schemas.py`
- `tests/integration/test_api_sources.py`

**Estimated scope:** Medium — 4 files

## Task 8：实现集合与标签领域服务

**Description:** 增加集合和标签的创建、删除、关联和范围解析服务，保持来源正文和索引不被组织操作修改。

**Acceptance criteria:**
- [ ] 一个来源可属于多个集合和多个标签。
- [ ] 删除集合或标签只删除关联。
- [ ] 默认集合不可删除，未指定组织时自动使用默认集合。
- [ ] 解析空过滤集合时返回明确的空范围。

**Verification:**
- [ ] `uv run pytest tests/integration/test_organization_service.py -q`

**Dependencies:** Task 1

**Files likely touched:**
- `app/services/organization.py`
- `tests/integration/test_organization_service.py`

**Estimated scope:** Medium — 2 files

## Task 9：暴露集合、标签和来源关联 API

**Description:** 增加集合、标签和来源关联 REST 接口，并让来源详情体现组织信息。

**Acceptance criteria:**
- [ ] 集合和标签支持创建、列表、删除。
- [ ] 来源支持批量加入集合、添加和移除标签。
- [ ] API 校验名称、数量和未找到错误，不触发重新 Embedding。

**Verification:**
- [ ] `uv run pytest tests/integration/test_api_organization.py tests/integration/test_api_sources.py -q`
- [ ] 检查 OpenAPI 中新增请求字段和错误响应。

**Dependencies:** Task 7, Task 8

**Files likely touched:**
- `app/api/collections.py`
- `app/api/sources.py`
- `app/api/schemas.py`
- `app/main.py`
- `tests/integration/test_api_organization.py`

**Estimated scope:** Medium — 5 files

## Checkpoint C：组织与预览

- [x] 用户可查看解析正文和 Child。
- [ ] 集合、标签和来源关联符合语义。
- [ ] 删除组织对象不影响正文和索引。
- [ ] 人工审查 API 契约后进入过滤检索阶段。

## Phase 4：过滤召回与双语评测

## Task 10：为 FTS5 与 Chroma 增加来源范围条件

**Description:** 在适配器层支持按允许来源集合过滤，避免先全库取 Top-K 再丢弃范围外结果。

**Acceptance criteria:**
- [ ] FTS5 的 MATCH 与来源范围在同一查询中生效。
- [ ] Chroma 使用 `source_id` 元数据过滤。
- [ ] 空来源集合不执行适配器查询。
- [ ] 现有无过滤搜索行为保持不变。

**Verification:**
- [ ] `uv run pytest tests/unit/test_fts.py tests/unit/test_vector.py -q`

**Dependencies:** Task 9

**Files likely touched:**
- `app/infra/fts.py`
- `app/infra/vector.py`
- `tests/unit/test_fts.py`
- `tests/unit/test_vector.py`

**Estimated scope:** Medium — 4 files

## Task 11：接入检索过滤器并暴露降级信息

**Description:** 将集合、标签和 source_id 过滤解析后传入检索，公开 `degraded_routes` 并保持 RRF/rerank 顺序。

**Acceptance criteria:**
- [ ] 多集合 OR、多标签 AND、不同过滤类型交集。
- [ ] 无匹配来源时返回空证据，不退回全库检索。
- [ ] `/search` 返回实际降级路径。
- [ ] 不存在集合或标签返回明确错误。

**Verification:**
- [ ] `uv run pytest tests/integration/test_retrieval_service.py tests/integration/test_api_search.py -q`

**Dependencies:** Task 10

**Files likely touched:**
- `app/services/retrieval.py`
- `app/api/search.py`
- `app/api/schemas.py`
- `tests/integration/test_retrieval_service.py`
- `tests/integration/test_api_search.py`

**Estimated scope:** Medium — 5 files

## Task 12：建立中英文检索评测集

**Description:** 在当前 128 题之外增加中文、英文和混合语言切片，分别报告 Recall、HitRate、MRR 和空证据准确率。

**Acceptance criteria:**
- [ ] 中文、英文和混合问法各有独立样本。
- [ ] 语料包含页面快照哈希和切块版本。
- [ ] 结果按语言分组，不与旧生成问答指标混算。
- [ ] 标准 Recall@5 是否达到 0.80 明确记录。

**Verification:**
- [ ] `uv run pytest tests/evals/test_questions_schema.py tests/unit/test_evaluation.py -q`
- [ ] 运行受控真实评测并归档结果 JSON。

**Dependencies:** Task 11

**Files likely touched:**
- `tests/evals/questions.json`
- `tests/evals/corpus.json`
- `tests/evals/test_questions_schema.py`
- `tests/unit/test_evaluation.py`

**Estimated scope:** Medium — 4 files

## Checkpoint D：过滤与双语检索

- [ ] 过滤在召回阶段生效。
- [ ] 中英文指标分别可复现。
- [ ] 降级状态可观察。
- [ ] 人工审查评测结果后进入可靠性阶段。

## Phase 5：可靠性、安全与可观测性

## Task 13：脱敏异常信息并修复 Job 日志关联

**Description:** 仅向 API 返回稳定错误码和安全摘要，同时让结构化日志实际输出 Job 关联字段。

**Acceptance criteria:**
- [ ] 未知异常原文只写日志，不进入 Source/Item API。
- [ ] JSON 日志实际包含 `job_id`、`item_id`、`client_run_id`。
- [ ] 健康检查和通用异常响应不泄露内部路径。

**Verification:**
- [ ] `uv run pytest tests/unit/test_logging.py tests/unit/test_ingestion_jobs.py tests/integration/test_api_error_handling.py -q`

**Dependencies:** Task 12

**Files likely touched:**
- `app/core/logging.py`
- `app/services/ingestion_jobs.py`
- `app/services/ingestion.py`
- `tests/unit/test_logging.py`
- `tests/unit/test_ingestion_jobs.py`

**Estimated scope:** Medium — 5 files

## Task 14：实现 24 小时幂等窗口

**Description:** 让相同 Idempotency-Key 只在 24 小时内复用 Job，过期后允许创建新 Job。

**Acceptance criteria:**
- [ ] 24 小时内重复 key 返回原 Job。
- [ ] 超过窗口后旧 key 可创建新 Job。
- [ ] 并发相同 key 仍只创建一个有效 Job。

**Verification:**
- [ ] `uv run pytest tests/unit/test_ingestion_jobs.py tests/integration/test_ingestions_api.py -q`

**Dependencies:** Task 1

**Files likely touched:**
- `app/services/ingestion_jobs.py`
- `app/infra/db.py`
- `tests/unit/test_ingestion_jobs.py`
- `tests/integration/test_ingestions_api.py`

**Estimated scope:** Medium — 4 files

## Task 15：拒绝无效启动配置

**Description:** 启动自检拒绝空 Embedding Key、无效 API Base、非正数关键参数；不在普通沙箱中访问未授权外部服务。

**Acceptance criteria:**
- [ ] 空 Embedding Key 或空白 Base 启动失败。
- [ ] 非正数 Top-K、超时和请求体限制启动失败。
- [ ] 有效配置不受影响，错误信息不包含密钥。

**Verification:**
- [ ] `uv run pytest tests/unit/test_settings.py tests/unit/test_selfcheck.py -q`

**Dependencies:** Task 13

**Files likely touched:**
- `app/core/settings.py`
- `app/core/selfcheck.py`
- `tests/unit/test_settings.py`
- `tests/unit/test_selfcheck.py`

**Estimated scope:** Small — 4 files

## Task 16：补齐任务统计与失败分布

**Description:** 统计接口增加成功率和错误码分布，使用数据库聚合避免加载全部 Job。

**Acceptance criteria:**
- [ ] 返回时间窗口、状态数、成功率、平均耗时、队列深度和失败码分布。
- [ ] 大数据量下不把所有 Job 加载到 Python 列表。
- [ ] 现有统计字段保持兼容。

**Verification:**
- [ ] `uv run pytest tests/unit/test_ingestion_jobs.py tests/integration/test_ingestions_api.py -q`

**Dependencies:** Task 14

**Files likely touched:**
- `app/services/ingestion_jobs.py`
- `app/api/stats.py`
- `app/api/schemas.py`
- `tests/integration/test_ingestions_api.py`

**Estimated scope:** Medium — 4 files

## Task 17：加固索引重建互斥与失败边界

**Description:** 为重建增加进程内互斥和明确失败/恢复状态，避免在线并发重建损坏派生索引。

**Acceptance criteria:**
- [ ] 在线重建期间第二个重建被拒绝。
- [ ] Embedding/FTS/Chroma 任一失败不会损坏 SQLite 权威数据。
- [ ] 失败后一致性检查能定位不一致，重新执行重建可恢复。

**Verification:**
- [ ] `uv run pytest tests/integration/test_maintenance_service.py -q`

**Dependencies:** Task 16

**Files likely touched:**
- `app/services/maintenance.py`
- `app/cli/maintenance.py`
- `tests/integration/test_maintenance_service.py`
- `scripts/README.md`

**Estimated scope:** Medium — 4 files

## Checkpoint E：可靠性与安全

- [ ] 异常、日志、统计、幂等和重建测试通过。
- [ ] 启动配置失效时快速失败。
- [ ] RAG 仍不调用 LLM。
- [ ] 人工审查后进入迁移和发布验收。

## Phase 6：迁移与发布

## Task 18：完成真实数据副本迁移与恢复演练

**Description:** 用当前真实 SQLite/Chroma 的副本验证 v3→v4 迁移、备份、恢复和回滚，不操作生产数据。

**Acceptance criteria:**
- [ ] 迁移前后来源数、Parent/Child 数和索引 ID 差异可解释。
- [ ] 默认集合及既有来源关联正确。
- [ ] 备份可恢复，回滚到 v3 后可读取。
- [ ] 不自动访问外网或合并历史重复来源。

**Verification:**
- [ ] 在临时副本上执行备份、迁移、一致性检查和恢复。
- [ ] 人工核对迁移报告。

**Dependencies:** Task 17

**Files likely touched:**
- `tests/integration/test_maintenance_service.py`
- `scripts/README.md`
- `PROJECT.md`
- `项目开发说明_v2.md`

**Estimated scope:** Medium — 4 files

## Task 19：完成 v2 最终验收与文档同步

**Description:** 更新实现状态、评测结果、README、OpenAPI 说明和遗留风险，执行最终质量门禁。

**Acceptance criteria:**
- [ ] 所有 Must 场景有实际证据。
- [ ] README、PROJECT、v2、OpenAPI 和实现一致。
- [ ] Ruff、mypy、全量测试和覆盖率在同一个提交通过。
- [ ] 历史旧结果明确标注为非 v2 验收指标。

**Verification:**
- [ ] `uv run ruff check .`
- [ ] `uv run mypy`
- [ ] `uv run pytest --cov --cov-report=term-missing`
- [ ] 人工复核验收表和未解决风险。

**Dependencies:** Task 18

**Files likely touched:**
- `README.md`
- `PROJECT.md`
- `项目开发说明_v2.md`
- `tests/evals/README.md`
- `.github/workflows/ci.yml`

**Estimated scope:** Medium — 5 files

## Final Checkpoint

- [ ] v2 所有新能力与既有回归通过。
- [ ] 迁移、备份和回滚有实际证据。
- [ ] 无已知 P0/P1 问题。
- [ ] 用户审核后发布。
