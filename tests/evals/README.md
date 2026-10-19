# 检索评测集

放置评测语料清单、问题集与标注结果（当前 128 题 / 84 个独立问题），以及运行评测的
脚本（见《项目开发说明_v2.md》11.4）。

- `corpus.json`：评测语料清单（真实公开文档站点的 URL）。入库顺序即清单顺序，
  `child_id` 依赖该顺序，**不要重排**；站点内容变化后需重新标注题库。
- `questions.json`：每道题记录问题、类别（专有名词 / 语义改写 / 代码查询 / 跨章节 /
  无答案）、标注的相关 `child_id` 集合。同义改写（`q01` / `q01a` / `q01b`）共享同一组
  相关 Child，**不构成独立样本**；汇总时应同时报告独立问题口径。
- `results/`：每次运行的结果 JSON，含标准 Recall@5、HitRate@5、Precision@5、
  MRR@10、nDCG@10、引用有效率、无答案识别率与每题最高 Dense 相似度
  （用于校准 `MIN_DENSE_SIMILARITY`）。

主要结果：

- `2026-10-19-v2-108q-final2-standard.json`：108 题、rerank 开，按标准 Recall@5 计算。
- `2026-10-19-v2-108q-final2.json`：同一轮的原始 Any-Hit 结果，便于审计口径变化。
- `2026-10-20-v2-108q-no-rerank.json`：同语料同题集、关闭 rerank 的对照基线。
- `*-grouped.json`：按独立问题重新聚合的结果，由 `scripts/eval_grouped_metrics.py` 生成。

指标口径：

- `Recall@5` = 前 5 条中命中的相关 Child 数 / 该题全部相关 Child 数；
- `HitRate@5` = 前 5 条是否至少命中一个相关 Child；
- `Precision@5` = 命中的相关 Child 数 / 5。

注意：

- 以上结果使用的是 `E:/ai_kb_data/eval-kb-v3`（165 页 / 4181 Child）。默认
  `--workdir` 指向 `eval-kb/`（旧的 153 页 / 3683 Child），两者不可混用，否则
  `child_id` 标注会整体错位。
- 2026-10-20 新增 `q65`–`q84`（跨章节 10、代码查询 10）后，题集为 128 题；
  这 20 道题尚未重跑基线，旧结果只覆盖 `q01`–`q64`。

运行方式（评测库默认落在 `CHROMA_PATH` 同级的 `eval-kb/`，可重复运行续传）：

```powershell
uv run python scripts/run_eval.py --workdir E:/ai_kb_data/eval-kb-v3 --output tests/evals/results/latest.json
uv run python scripts/run_eval.py --dump-children tests/evals/children.json
uv run python scripts/run_eval.py --use-current-db      # 改用自己的知识库

uv run python scripts/eval_grouped_metrics.py `
  --results tests/evals/results/2026-10-19-v2-108q-final2-standard.json `
  --output tests/evals/results/2026-10-19-v2-108q-final2-grouped.json
```
