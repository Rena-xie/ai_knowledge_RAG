# 检索评测集

放置评测语料清单、问题集与标注结果（起步 20 题，目标 50 题），以及运行评测的脚本（见《项目开发说明_v1.md》11.4）。

- `corpus.json`：评测语料清单（真实公开文档站点的 URL）。入库顺序即清单顺序，`child_id` 依赖该顺序，**不要重排**；站点内容变化后需重新标注题库。
- `questions.json`：每道题记录问题、类别（专有名词 / 语义改写 / 代码查询 / 跨章节 / 无答案）、标注的相关 `child_id` 集合。
- `results/`：每次运行的结果 JSON，含 Recall@5、引用有效率、无答案识别率与每题最高 Dense 相似度（用于校准 `MIN_DENSE_SIMILARITY`）。

运行方式（评测库默认落在 `CHROMA_PATH` 同级的 `eval-kb/`，可重复运行续传）：

```powershell
uv run python scripts/run_eval.py                       # 入库语料并评测
uv run python scripts/run_eval.py --dump-children tests/evals/children.json
uv run python scripts/run_eval.py --use-current-db      # 改用自己的知识库
```
