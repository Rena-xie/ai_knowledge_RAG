# 检索评测集

放置评测语料清单、问题集与标注结果（当前 148 个语言问法 / 84 个独立问题），以及
运行评测的脚本。

- `corpus.json`：评测语料清单（真实公开文档站点的 URL）。入库顺序即清单顺序，
  `child_id` 依赖该顺序，**不要重排**；站点内容变化后需重新标注题库。
- `questions.json`：148 个问法，其中中文 128、英文 10、中英混合 10。每道题记录
  问题、类别（专有名词 / 语义改写 / 代码查询 / 跨章节 / 无答案）、语言和标注的相关
  `child_id`。同义及跨语言问法（`q01` / `q01a` / `q01b` / `q01e`）共享同一组
  相关 Child，**不构成本质不同的独立问题**。
- `child_parent_map.json`：冻结评测库导出的 Child→Parent 映射，覆盖全部题目标注
  以及当前 raw result 中的全部最终返回 Child。当前为 475 条兼容超集；映射缺失或
  重复键必须报错。
- `questions_language_slices.json`：可从主题库复现的 20 条英文/混合语言切片。
- `results/`：每次运行的结果 JSON，含标准 Recall@5、HitRate@5、Precision@5、
  MRR@10、nDCG@10、空证据准确率与每题最高 Dense 相似度
  （用于校准 `MIN_DENSE_SIMILARITY`）。

公开结果：

- `2026-10-20-v2-128q-literal.json`：历史 128 题 rerank 开结果。旧文件中的
  `answer_type`、`citations_validated` 和 `citation_count` 来自已移除的 RAG
  生成链路，只用于历史溯源，不参与当前检索验收。
- `2026-10-20-v2-128q-literal-grouped.json`：按 84 个独立问题重新聚合的结果。
- `2026-10-26-v2-evidence-148q.json`：在冻结 `eval-kb-v3` 上重新执行 148 个
  语言问法的原始结果。所有指标使用最终返回的 `evidences`；`fused_ids` 仅作诊断。
- `2026-10-26-v2-evidence-148q-baseline.json`：历史 Child 口径 baseline，保留用于
  诊断和前后对照，不再作为当前正式验收口径。
- `2026-10-26-v2-parent-evidence-148q-baseline.json`：重复来源清理前生成的
  Parent 过渡 baseline，标记为 `provisional`，仅保留历史对照。
- `2026-10-26-v2-clean-r2-148q.json`：清理 source `119` 后重新执行的原始结果。
- `2026-10-26-v2-parent-clean-r2-148q.json`：正式 Parent-level 干净基线，状态为
  `accepted`。
- `*-fused-candidates.invalid.json`：早期错误口径结果，仅保留用于故障溯源。
  这些文件使用内部融合候选计算指标，不能用于验收。

当前正式 Parent baseline（84 个独立问题）：

| Recall@5 | Precision@5 | HitRate@5 | MRR@10 | nDCG@10 | 空证据准确率 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0.8368 | 0.2005 | 0.8734 | 0.8009 | 0.7956 | 1.0 |

对应 Child 诊断指标仍为：

| Child Recall@5 | Child Precision@5 | Child HitRate@5 | Child MRR@10 | Child nDCG@10 |
| ---: | ---: | ---: | ---: | ---: |
| 0.6379 | 0.1686 | 0.7519 | 0.6842 | 0.6226 |

历史基线和消融结果保留在本地，不纳入公开仓库；需要时可以重新运行评测生成。

指标口径：

- `Recall@5` = 前 5 条中命中的唯一相关 Parent 数 / 该题全部唯一相关 Parent 数；
- `HitRate@5` = 前 5 条是否至少命中一个相关 Parent；
- `Precision@5` = 命中的唯一相关 Parent 数 / 5，仅作观察，不设交付阻断阈值。
- `MRR@10` = 第一个相关 Parent 的倒数排名。
- `nDCG@10` = 按相关 Parent 标注计算的排序增益。
- `空证据准确率` = 无答案题中返回空证据的比例。

注意：

- 以上结果使用的是 `E:/ai_kb_data/eval-kb-v3`（165 页 / 4181 Child）。默认
  `--workdir` 指向 `eval-kb/`（旧的 153 页 / 3683 Child），两者不可混用，否则
  `child_id` 标注会整体错位。
- 2026-10-20 新增 `q65`–`q84`（跨章节 10、代码查询 10）后，题集为 128 题，
  并已补跑 rerank 开/关基线，结果见上。
- 2026-10-26 新增 10 条英文和 10 条中英混合问法。它们复用所属独立问题的标注，
  用于语言鲁棒性，不增加独立问题数量。
- 当前有效评测使用最终 `evidences`，无答案准确率为 `1.0`。历史文件中的
  `answer_type=insufficient_evidence` 属于已删除的问答链路，不能替代 RAG 空证据指标。

标注约定：

- 相关 `child` 应覆盖"能直接回答该问题"的切片；同一章节内仅重复或轻微展开的
  近邻切片，可以只标最直接的一条。
- 跨章节题至少来自 2 个不同章节或文档，每个方面至少标一条。
- 标注后重新导出切块清单（`--dump-children`），确认所有 `child_id` 仍然存在；
  语料或切块规则变化会使标注整体失效。
- Child 标注仍是原始相关性真值；正式指标映射到 Parent 后去重，同一 Parent 只计一次。
- Child 级指标仅作为检索、rerank 和证据构造诊断，不参与正式通过判定。

运行方式（评测库默认落在 `CHROMA_PATH` 同级的 `eval-kb/`，可重复运行续传）：

```powershell
uv run python scripts/run_eval.py --workdir E:/ai_kb_data/eval-kb-v3 --output tests/evals/results/latest.json
uv run python scripts/run_eval.py --dump-children tests/evals/children.json
uv run python scripts/run_eval.py --use-current-db      # 改用自己的知识库

uv run python scripts/eval_grouped_metrics.py `
  --results tests/evals/results/2026-10-26-v2-clean-r2-148q.json `
  --output tests/evals/results/2026-10-26-v2-parent-clean-r2-148q.json `
  --embedding-model BAAI/bge-m3 `
  --rerank-model BAAI/bge-reranker-v2-m3 `
  --source-commit 765712f `
  --baseline-status accepted `
  --note "清理 source 119 后的正式 Parent 基线"
```
