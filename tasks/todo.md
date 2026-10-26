# v2 Core RAG 任务清单

> 目标：明确 URL 入库、混合检索、质量评测、基础安全和 API 成本观测。
>
> 已删除的多集合、标签、范围过滤、高级幂等、复杂统计和迁移平台不属于当前 v2。
>
> **当前状态**：Core RAG 功能、质量、安全和自动化验证已通过；按本轮要求
> 未创建 Git 提交，因此版本提交证据仍待生成。

## 已完成基线

### Task 1：来源身份与正文精确去重

- [x] 最终 URL 成为长期来源地址。
- [x] 提交 URL、重定向 URL 和历史 URL 保存为别名。
- [x] 相同正文只保留一个来源和一套索引。
- [x] 相同最终 URL、不同正文返回 `SOURCE_IDENTITY_CONFLICT`。
- [x] 历史重复来源不自动合并。
- [x] Commit：`0ee031b`

### Task 2：中英文正文提取质量

- [x] 建立 30 个真实公开页面和 2 个双语边界夹具。
- [x] 标题、正文、噪声和结构指标达到冻结门槛。
- [x] 支持 `zh/en/mixed/unknown` 确定性语言元数据。
- [x] 离线质量测试和可选 live 检查分离。
- [x] Commit：`5b94325`

### Task 3：来源正文只读预览

- [x] completed 来源可分页返回 Parent/Child 正文树。
- [x] 非 completed 或数据不完整返回 `SOURCE_CONTENT_NOT_READY`。
- [x] Parent/Child 顺序稳定，不提供修改接口。
- [x] 接口不暴露 `embedding_text`。
- [x] Commit：`507bf48`

### 已完成检查点

- [x] 来源身份和迁移基线。
- [x] 内容质量与双语元数据。
- [x] 来源正文和 Child 可查看。

## Task 4：收敛已删除能力的公开字段

**Description:** 删除集合、标签等已移出 v2 范围的公开响应字段和小规模测试依赖。
不执行破坏性数据库删除；遗留表可以作为内部结构保留。

**Acceptance criteria:**

- [x] 来源正文预览 Schema 不再返回 collections/tags。
- [x] OpenAPI 不暴露集合/标签管理或过滤接口。
- [x] README 和 v2 文档不再把组织能力描述为 v2 功能。
- [x] 现有来源、Parent、Child 和检索行为不变。

**Verification:**

- [x] `uv run pytest tests/integration/test_source_inspection.py tests/integration/test_api_sources.py -q`
- [x] `uv run ruff check .`
- [x] `uv run mypy`

**Dependencies:** Task 3

## Task 5：补齐 84 个独立问题的评测报告

**Description:** 主验收改为 84 个独立问题；补充 grouped Precision@5、语言切片和
当前 baseline，不允许用问法变体替代独立问题口径。

**Acceptance criteria:**

- [x] grouped 结果包含 Recall@5、Precision@5、HitRate@5、MRR@10、nDCG@10。
- [x] 148 个语言问法只作为鲁棒性观察，84 个独立问题仍是主验收样本数。
- [x] 中文 128、英文 10、中英混合 10 条问法分别报告。
- [x] 记录语料、题库、切块版本、Embedding 模型、rerank 模型和来源 commit。
- [x] 当前结果写入独立 baseline 文件，不覆盖历史结果。

**Verification:**

- [x] `uv run pytest tests/evals/test_questions_schema.py tests/unit/test_evaluation.py tests/unit/test_grouped_evaluation.py -q`
- [x] 运行 20 个新增英文/混合问法并归档真实结果。
- [x] 修正评测使用最终 `evidences`，完整重跑 148 个问法。
- [x] 生成 84 个独立问题的 corrected baseline。

**Baseline:** `tests/evals/results/2026-10-26-v2-evidence-148q-baseline.json`

**Dependencies:** Task 3

## Task 6：把核心检索指标做到达标

**Description:** 基于 Task 5 的失败样本改善切块、混合召回、RRF、rerank 和字面量
策略，不通过修改标注或降低门槛“达标”。

**Historical Child baseline:** Recall@5 `0.6323`、Precision@5 `0.1661`、
HitRate@5 `0.7544`、MRR@10 `0.7019`、nDCG@10 `0.6274`、无答案 `1.0`。

**Provisional Parent baseline:** Recall@5 `0.8300`、Precision@5 `0.1975`、
HitRate@5 `0.8734`、MRR@10 `0.8161`、nDCG@10 `0.7986`、无答案 `1.0`。

**Final clean Parent baseline:** Recall@5 `0.8368`、Precision@5 `0.2005`、
HitRate@5 `0.8734`、MRR@10 `0.8009`、nDCG@10 `0.7956`、无答案 `1.0`。

**Acceptance criteria:**

- [x] 评测只使用最终 `evidences`，`fused_ids` 仅保留为诊断。
- [x] 正式指标使用最终 Parent 证据，Child 指标仅保留为诊断。
- [x] 使用完整 Child→Parent 映射从旧 raw result 生成 Parent 过渡基线。
- [x] Recall@5，84 个独立问题 ≥ 0.80。
- [x] Precision@5 仅作观察，不设交付阻断阈值；当前为 0.2005。
- [x] HitRate@5 ≥ 0.85。
- [x] MRR@10 ≥ 0.70。
- [x] nDCG@10 ≥ 0.70。
- [x] 无答案准确率 ≥ 0.90。
- [x] 中文、英文和混合问题分别报告，不出现单语言整体失效。

**Verification:**

- [x] 检索、题集和聚合相关测试通过。
- [x] 在去除重复来源的独立副本上重跑 148 个问法。
- [x] 使用原有 84 组口径生成 Parent 正式指标。
- [x] 核对无答案、语言切片和低相似度失败样本。

**Dependencies:** Task 5

**Baseline:** `tests/evals/results/2026-10-26-v2-parent-clean-r2-148q.json`

## Task 7：增加最小 API 成本观测

**Description:** 只记录 Embedding 和 rerank API 调用量、输入量、单价和估算费用，
不建设账户、充值、余额或多租户计费。

**Implementation boundary:** 费用是按配置单价计算的估算值；无供应商 usage 时按
字符数估算，不等同于供应商最终 token 账单；迁移前历史调用没有记录；并发请求下
预算检查存在小幅竞态窗口。

**Acceptance criteria:**

- [x] 记录 operation、provider、model、请求数、输入量、币种和费用。
- [x] 优先使用供应商 usage；无 usage 时明确标记估算口径。
- [x] 费用记录关联 request_id、job_id 和可用 source_id。
- [x] 入库任务可查看 Embedding 成本。
- [x] 检索可查看 Query Embedding 和 rerank 成本。
- [x] `GET /api/v1/costs?since_hours=24` 返回按操作和模型聚合的成本。
- [x] 超出配置预算时拒绝新的付费调用，并返回稳定错误码。
- [x] 不实现复杂账单和供应商未来价格预测。

**Verification:**

- [x] `uv run pytest tests/unit/test_cost.py tests/integration/test_api_costs.py -q`
- [x] 使用 Fake 响应验证 usage、估算、聚合和预算拒绝。
- [x] 检查日志和 API 不泄露密钥或完整上游响应。

**Dependencies:** Task 3

## Task 8：补齐核心安全与启动失败边界

**Description:** 保留现有 SSRF、robots、重定向、体积和密钥边界，补齐安全异常摘要、
启动配置快速失败和日志关联。

**Acceptance criteria:**

- [x] 未知异常只返回稳定错误码和安全文案。
- [x] 空 API Key、空 Base URL、非正数超时/上限在启动时失败。
- [x] `request_id`、`job_id` 和可用 `source_id` 在结构化日志中实际输出。
- [x] SSRF、重定向、robots、内容类型和体积负向测试通过。
- [x] 服务默认只监听回环地址；文档明确无鉴权不可公网部署。
- [x] 不新增鉴权、多用户或公网部署功能。

**Verification:**

- [x] 安全与错误处理相关测试通过。
- [x] API 错误和日志中不包含密钥、内部路径或完整上游响应。

**Dependencies:** Task 3

## Task 9：Core RAG 系统验收

**Description:** 在同一个最终提交上运行核心 E2E、全量测试、静态检查和质量评测，
形成 v2 Core RAG 验收记录。

**Acceptance criteria:**

- [x] URL 入库至证据检索主链路通过。
- [x] 中文、英文和混合资料可入库和检索。
- [x] 84 个独立问题所有硬性质量门槛通过。
- [x] API 成本可观测且预算拒绝有效。
- [x] 安全负向测试通过。
- [x] README、PROJECT、v2、OpenAPI 和实现一致。
- [x] 全量 pytest、覆盖率和 E2E 在当前工作区通过。
- [ ] 生成同一代码状态的 Git commit 证据；本轮按要求未提交。
- [ ] 在最终提交状态上重跑正式检索评测并同步更新 `source_commit` 元数据。

**Verification:**

- [x] `uv run ruff check .`
- [x] `uv run mypy`
- [x] `uv run pytest --cov --cov-report=term-missing`
- [x] 运行核心 E2E 和完整检索评测。
- [x] 归档指标、成本和测试证据；commit 证据待生成。

**Dependencies:** Task 4–8

## Core RAG 验收检查点

- [x] 明确 URL 可以稳定写入知识库。
- [x] Dense + BM25 + RRF + rerank 返回可追溯证据。
- [x] 84 个独立问题的硬性质量指标达标。
- [x] Precision@5 已记录为观察指标，不阻断交付；Recall 未牺牲。
- [x] API 成本可观测并受预算限制。
- [x] SSRF、robots、体积、密钥和错误信息边界通过。
- [x] 人工确认真实使用流程和失败行为。
