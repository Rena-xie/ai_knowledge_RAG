# 正文提取质量黄金集

本目录验证 HTML 转 Markdown 的标题、正文、噪声控制和关键结构保留，不用检索
Recall 代替解析质量。

## 数据集

- 30 个真实公开页面：中文 15、英文 15。
- 2 个项目编写的双语边界夹具：只验证 `mixed` 分类与结构组合，不冒充真实站点。
- 真实页面覆盖 Python 官方文档、FastAPI 官方文档和 LangChain 官方博客。
- 快照抓取日期、最终 URL、HTTP 元数据、SHA-256 和人工标注均记录在
  `cases.json`。
- 快照冻结后，CI 不访问网络。站点内容更新不会造成随机失败。

公开页面只作为解析回归样本。其正文和站点资源仍受各自上游许可与使用条款约束；
本项目不将快照作为知识库生产数据分发。双语夹具由本项目编写。

## 指标

- 标题准确率：规范化后的提取标题与人工标注完全一致。
- 正文字符召回：命中的标注正文片段字符数 / 全部标注片段字符数。
- 非正文噪声率：命中的导航、页脚和订阅字符数 / 提取正文总字符数。
- 结构保留率：命中的代码、列表和表格标记数 / 全部结构标记数。
- 标题保留率：人工标注标题继续以 Markdown 标题出现。
- 语言准确率：确定性分类结果与数据清单一致。

所有指标同时按中文、英文、混合语言和总体报告。阈值定义在
`项目开发说明_v2.md` 第 7.6、17 节。

## 命令

```powershell
# 离线 CI 门禁
uv run pytest tests/content_quality/test_extraction_quality.py -q

# 生成逐页报告
uv run python scripts/content_quality_report.py
uv run python scripts/content_quality_report.py --json tests/content_quality/latest.json

# 显式访问当前公开页面；不属于默认 CI
uv run python scripts/content_quality_live.py
uv run python scripts/content_quality_live.py --strict

# 人工确认后刷新冻结快照和来源元数据
uv run python scripts/refresh_content_quality_snapshots.py
```

`content_quality_live.py` 把实时页面漂移与解析器回归分开报告。实时页面超过质量
阈值时应先检查页面是否更新，不能直接覆盖旧基线和人工标注。

## 基线失败样本

解析器修改前的基线保存在 `baseline.json`。实际抽查并修复的代表性失败包括：

| 样本 | 修改前现象 | 处理 |
| --- | --- | --- |
| `en-fastapi-first-steps` | 标题尾部保留脚注符 `¶` | 规范化标题并移除脚注符 |
| `en-fastapi-path-params` | 标题尾部保留脚注符 `¶` | 同上 |
| `en-fastapi-security` | 标题尾部保留脚注符 `¶` | 同上 |
| `zh-docs-introduction` | 交互代码提示符被压成普通正文 | `<pre>` 占位保护后恢复围栏代码 |
| `zh-docs-controlflow` | 多个 Markdown 标题含脚注符 | 标题与正文统一清理 |
| `en-fastapi-testing` | 终端输出残留 `span` 样式标签 | 仅移除已知行内包装标签 |

失败样本的目标不是提高表面分数，而是确保正文语义、代码缩进和结构在修改后仍然
可读、可切块、可检索。
