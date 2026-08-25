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

## 文档

- 设计 spec:`docs/superpowers/specs/2026-08-21-copilot-advisor-design.md`
- 实现计划:`docs/superpowers/plans/`
- 开发环境(workspace 安装、企业网络代理):`DEVELOPMENT.md`
- Teams 联调:`docs/teams-setup.md`
