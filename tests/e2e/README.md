# 端到端测试

`test_pipeline_e2e.py` 使用本地夹具站点（`fixtures/` 目录由 `http.server` 提供）
跑通完整 RAG 流水线：真实 HTTP 抓取 → 解析 → 切块 → 假 Embedding
→ FTS5/Chroma 写入 → 检索证据，不访问真实网络或 LLM。

注意：测试通过注入地址校验器放行回环地址；生产默认拦截内网与回环。
