# 检索评测集

放置评测语料清单、问题集与标注结果（当前 128 题 / 84 个独立问题），以及运行评测的
脚本。

- `corpus.json`：评测语料清单（真实公开文档站点的 URL）。入库顺序即清单顺序，
  `child_id` 依赖该顺序，**不要重排**；站点内容变化后需重新标注题库。
- `questions.json`：每道题记录问题、类别（专有名词 / 语义改写 / 代码查询 / 跨章节 /
  无答案）、标注的相关 `child_id` 集合。同义改写（`q01` / `q01a` / `q01b`）共享同一组
  相关 Child，**不构成独立样本**；汇总时应同时报告独立问题口径。
- `results/`：每次运行的结果 JSON，含标准 Recall@5、HitRate@5、Precision@5、
  MRR@10、nDCG@10、引用有效率、无答案识别率与每题最高 Dense 相似度
  （用于校准 `MIN_DENSE_SIMILARITY`）。

公开结果：

- `2026-10-20-v2-128q-literal.json`：当前实现的 128 题 rerank 开结果。
- `2026-10-20-v2-128q-literal-grouped.json`：按 84 个独立问题重新聚合的结果。

历史基线和消融结果保留在本地，不纳入公开仓库；需要时可以重新运行评测生成。

指标口径：

- `Recall@5` = 前 5 条中命中的相关 Child 数 / 该题全部相关 Child 数；
- `HitRate@5` = 前 5 条是否至少命中一个相关 Child；
- `Precision@5` = 命中的相关 Child 数 / 5。

注意：

- 以上结果使用的是 `E:/ai_kb_data/eval-kb-v3`（165 页 / 4181 Child）。默认
  `--workdir` 指向 `eval-kb/`（旧的 153 页 / 3683 Child），两者不可混用，否则
  `child_id` 标注会整体错位。
- 2026-10-20 新增 `q65`–`q84`（跨章节 10、代码查询 10）后，题集为 128 题，
  并已补跑 rerank 开/关基线，结果见上。

标注约定：

- 相关 `child` 应覆盖"能直接回答该问题"的切片；同一章节内仅重复或轻微展开的
  近邻切片，可以只标最直接的一条。
- 跨章节题至少来自 2 个不同章节或文档，每个方面至少标一条。
- 标注后重新导出切块清单（`--dump-children`），确认所有 `child_id` 仍然存在；
  语料或切块规则变化会使标注整体失效。
- child 级 Recall 对标注颗粒度敏感：检索到同一文档的其他切片会被判为未命中。
  排查时可参考 source 级命中率，但验收仍以 child 级为准，不做事后补标。

运行方式（评测库默认落在 `CHROMA_PATH` 同级的 `eval-kb/`，可重复运行续传）：

```powershell
uv run python scripts/run_eval.py --workdir E:/ai_kb_data/eval-kb-v3 --output tests/evals/results/latest.json
uv run python scripts/run_eval.py --dump-children tests/evals/children.json
uv run python scripts/run_eval.py --use-current-db      # 改用自己的知识库

uv run python scripts/eval_grouped_metrics.py `
  --results tests/evals/results/2026-10-20-v2-128q-literal.json `
  --output tests/evals/results/2026-10-20-v2-128q-literal-grouped.json
```
