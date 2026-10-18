# 端到端测试

`test_pipeline_e2e.py` 使用本地夹具站点（`fixtures/` 目录由 `http.server` 提供）
跑通完整流水线：真实 HTTP 抓取 → 解析 → 切块 → 假 Embedding → FTS5/Chroma 写入
→ 检索 → 问答（假 LLM），不访问真实网络（见《项目开发说明_v1.md》11.1）。

注意：测试通过注入地址校验器放行回环地址；生产默认拦截内网与回环。
