# GitHub Copilot Advisor

面向企业用户的 GitHub Copilot 问答 agent:群聊 @提问 → 知识库/实时检索 →
分级升级(通用建议 → 工单指引 → CSAM/CSA)。

## 结构(monorepo,三个独立 project)

| 目录 | 职责 | 运行 |
|---|---|---|
| `shared/` | 契约:索引 schema、消息模型、事件 | (库) |
| `ingestion/` | 数据抓取→清洗→LLM 提炼→写入 AI Search | `uv run python -m ingestion run` |
| `agent/` | MAF + Azure OpenAI 编排与工具 | (库,由渠道装配) |
| `channels/teams/` | Teams Bot Framework 薄壳 | `uv run python -m teams_adapter` |

依赖方向:`channels/* → agent → shared`,`ingestion → shared`。

> 企业网络下 PyPI 访问受限时,`uv sync` 前可设置 `export UV_INDEX_URL=...`
> 指向内部代理,见 [DEVELOPMENT.md](DEVELOPMENT.md)。

## 快速开始

```bash
uv sync --all-packages       # 安装全部 workspace(企业网络见上,或先设置 UV_INDEX_URL)
uv run pytest                # 单元测试(不需要任何凭据)
cp .env.example .env         # 填 Azure/GitHub 凭据
uv run python -m ingestion run   # 灌知识库
uv run --env-file .env python -m teams_adapter  # 启动 Teams bot(见 docs/teams-setup.md)
```

## 回答策略

- 回答策略:保持知识库/GitHub live 优先;仅在 `no_results=true` 时转网络搜索。
  网络搜索先使用 `trusted` 范围(高置信度来源),结果足以回答就停止;
  为空或不足以回答时才使用 `general` 范围扩展其它来源。
  高置信度范围为 Copilot 官方文档与 GitHub Changelog、VS Code 更新与
  `microsoft/vscode` issues、Copilot Community discussion、`githubcopilotfaq`。
  来源策略集中在 [source_policy.py](agent/src/advisor_agent/search/source_policy.py);
  Community 单条 discussion 的 URL 不含分类,需标题或摘要包含 Copilot 才按高置信度处理。
  来源置信度不代表内容一定相关或问题已解决,仍需模型判断证据是否充分。
- 默认回答格式:2–3 句话总结 → 3–5 条建议 → 最多 3 个实际使用的来源。
  中文正文目标不超过 600 个字符(不含来源),用户明确要求详情时可放宽;
  Teams 不重复追加已有链接,也不展示未被回答引用的检索结果。
- 回答质量回归:现有 `test_eval_behavior.py` 中的 `quality-` 用例以固定检索
  夹具验证真实模型的分级搜索决策、回答结构和长度,不依赖搜索网站实时排序。
  可用 `uv run --no-sync --env-file .env pytest -m integration agent/tests/test_eval_behavior.py -k quality-`
  单独运行;报告保存在 `agent/tests/output/`。

## 文档

- 设计 spec:`docs/superpowers/specs/2026-08-21-copilot-advisor-design.md`
- 图片输入设计:`docs/superpowers/specs/2026-09-04-image-input-design.md`
- **已知问题清单**:[docs/known-issues.md](docs/known-issues.md)
- AI Search 索引配置与灌数:[docs/search-index-setup.md](docs/search-index-setup.md)
- 实现计划:`docs/superpowers/plans/`
- 开发环境(workspace 安装、企业网络代理):`DEVELOPMENT.md`
- Teams 联调:`docs/teams-setup.md`
