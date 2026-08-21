# GitHub Copilot Advisor — 设计文档

> 状态:**已确认(全部小节经逐节评审通过)**
> 日期:2026-08-21

## 1. 项目背景与目标

面向使用 GitHub Copilot 的企业客户,做一个可发布到群聊平台(Teams、企业微信、飞书)的
advisor agent。用户在群里 @agent 提问,agent 回答所有 GitHub Copilot 使用问题。

客户问题分布(来自 Teams 群历史 50 个去重案例、10 个主题域):
- P0:Credits/Token/计费/上下文治理(24%)、稳定性/超时/登录/网络(20%)、Agent/Subagent/模型路由(14%)
- P1:IDE/插件/Remote 兼容性、文件与会话管理、MCP 集成与密钥、额度与 Usage 可见性
- P2:WorkIQ/M365 扩展、API 能力边界、产出型高级用例

问题分布广,覆盖不同工具(CLI、VS Code、IntelliJ、GitHub 网页)和环境因素(网络等)。

## 2. 已确认的关键决策

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 平台范围 | 第一版只做 Teams(@提及 + 1:1 私聊);agent 核心平台无关,企微/飞书后续加适配层 |
| 2 | 群问答采集 | 起步人工整理导入;自动沉淀(bot 监听"已解决"标记)后续版本做,架构预留 connector 扩展点 |
| 3 | 升级联系人 | 静态配置表:channel/租户 → CSAM/CSA(姓名、邮箱、Teams ID、是否在群)。在群里就 @,不在就给联系方式 |
| 4 | 技术栈 | Python + Azure OpenAI(GPT-4o/4.1)+ Azure AI Search |
| 5 | 编排框架 | Microsoft Agent Framework(2026-04 已 1.0 GA,Python/.NET),取代自建 tool loop;与 Foundry hosted agents 原生兼容 |
| 6 | GitHub Copilot SDK | 第一版不用作核心引擎(technical preview、订阅制授权不适合服务端 bot、定位是编码 agent 而非知识问答)。作为 LLM provider 抽象下的预留 backend,待 GA 后可加 adapter 接入 |
| 7 | Web search | 可插拔多 provider:Bing Grounding(Azure AI Foundry)/ Tavily / WorkIQ 等,支持 failover 链,配置驱动 |
| 8 | 多轮对话 | 支持。按 Teams 会话线程(reply thread / 1:1 会话)维护状态;会话存储起步用内存,接口预留 Cosmos DB/Redis |
| 9 | 回答语言 | 跟随提问语言(中文问中文答,英文问英文答);引用英文源保留原文链接 |
| 10 | 部署形态 | 三种都支持:本地进程 / 容器(Container Apps 等)/ Azure hosted agent。核心功能与库先行 |
| 11 | 数据源筛选 | vscode 仓库按 Copilot 相关标签筛 closed issues;专门反馈仓库(vscode-copilot-release、copilot-intellij-feedback)可全量 |
| 12 | 数据管道 | 配置驱动(sources.yaml):选 repo + 标签/状态/时间等过滤条件即可新增数据源,不改代码 |
| 13 | 项目拆分 | monorepo 三个独立 project:ingestion / agent / channels(每渠道一个子项目),契约收敛在 shared 包(索引 schema + 消息模型),依赖单向 channels→agent→shared、ingestion→shared |

## 3. 回答策略(升级瀑布)

1. **组合检索 `search_solutions`** — 知识库(Azure AI Search hybrid,已解决问题,优先)
   与 GitHub live search(指定 repo 的 open issues/discussions)并行,超时预算内合并,
   KB 结果排前(详见 7.2)
2. **Web 搜索兜底** — 仅在第 1 级 no_results 时;多 provider + failover(最新 blog、版本更新等)
3. **通用建议** — 网络测试、重启 VS Code、重试等 + 开工单指引
4. **人工升级** — 查静态配置表找 CSAM/CSA;在群里直接 @,不在则给联系方式

## 4. 数据源清单

| 数据源 | 类型 | 过滤 |
|--------|------|------|
| github.com/orgs/githubcopilotfaq/discussions | github_discussions | answered=true |
| microsoft/vscode | github_issues | Copilot 相关标签 + closed |
| microsoft/vscode-copilot-release | github_issues | closed(专门仓库) |
| microsoft/copilot-intellij-feedback | github_issues | closed(JetBrains 专门仓库) |
| github/copilot-cli | github_issues + discussions | issues: closed;discussions: answered=true |
| community/community | github_discussions | Copilot 分类 |
| 群内已解决问答(人工整理) | manual_qa | 本地目录 markdown/JSON |

增量同步:每源维护 since 水位,跑完自动推进。

## 5. 架构总览 ✅(已确认)

两个独立服务 + 一个共享契约包,交汇点只有 Azure AI Search 索引:

```
Ingestion Service(定时批处理)
  sources.yaml → Connectors(github_issues/github_discussions/manual_qa)
    → 清洗/归一为统一 QA 文档 → Chunk → Embedding → Upsert
        ↓ 写
  Azure AI Search(hybrid 检索)
        ↑ 读
Advisor Agent Service(常驻在线)
  Teams 适配层(Bot Framework 薄壳,未来企微/飞书/CLI 同为薄壳)
    → Agent Core(MAF + Azure OpenAI)
       Tools:
         1. search_solutions(组合:AI Search KB + GitHub live 并行)
         2. web_search(多 provider + failover)
         3. escalate_to_human(工单指引 / 查联系人配置表)
         4. network_diagnostics(Azure 侧探测 + GitHub 状态页 + 客户自测指引)
         5. copilot_usage_lookup(Copilot 计费/用量 API,客户 token 授权制)
    → 会话状态存储(内存起步,接口留给 Cosmos/Redis)
```

仓库结构(monorepo,**三个独立 project**):

```
github-copilot-advisor/
├── shared/                  # 极小契约包:索引 schema + AdvisorRequest/Response 消息模型
├── ingestion/               # 项目1:数据抓取、清洗、提炼、导入(独立部署)
│   └── sources.yaml
├── agent/                   # 项目2:agent & tools 编排(MAF,平台无关)
└── channels/                # 项目3:渠道适配,每渠道一个独立子项目
    ├── teams/               #   v1 实现
    ├── wecom/               #   企业微信(占位,后续)
    ├── feishu/              #   飞书(占位,后续)
    └── web/                 #   Web UI/API(占位,后续)
```

**依赖方向单向:`channels/* → agent → shared`,`ingestion → shared`,禁止反向。**
对接新渠道 = 新增一个 adapter 子项目,agent core 代码零修改。
每个渠道 adapter 独立入口、独立依赖、独立部署单元。

隔离收益:生命周期不同(批处理 vs 常驻)、故障隔离、权限最小化
(ingestion:GitHub token + Search 写;agent:Search 读 + OpenAI)。

## 6. Ingestion 管道详细设计 ✅(已确认)

核心流程(每源独立执行、互不影响):

```
source config → Connector.fetch() → 归一化 → LLM 提炼 → 质量过滤 → Embed → Upsert → 推进水位
```

### 6.1 Connector 层(每种 type 一个实现,统一接口)

- `github_issues`:REST API 按 repo + labels + state + since 拉取,连同全部评论。
  答案提取:优先维护者/成员最后实质回复,其次 ➕ 最多评论,否则保留讨论串
- `github_discussions`:GraphQL API(answered 过滤只有 GraphQL 支持),answered=true 直接取 acceptedAnswer
- `manual_qa`:本地目录 markdown/JSON,一文件一 QA,frontmatter 放元数据(主题域、产品、日期)
- 预留:teams_qa 自动沉淀 connector(后续版本)

### 6.2 归一化与索引 schema(对齐 AI Search 常规设计)

**QA 不切割:一个 QA = 一条索引记录。** 字段贴合 AI Search 默认约定:

| 字段 | 说明 | 属性 |
|------|------|------|
| id | 源+原始ID 哈希,幂等 | key |
| title | 归一化问题标题 | searchable,semantic-title |
| content | **LLM 提炼后的问答内容**(问题要点+解决方案,几百 token 内)——回答引用的就是它 | searchable,semantic-content |
| keywords | 标签/产品/主题域 | searchable+filterable+facetable,semantic-keywords |
| raw_content | 原始问题+讨论串全文(超长截断) | **searchable 但 retrievable=false**,只为提高命中率,不出现在结果中 |
| url | 原始链接 | retrievable |
| source / doc_type / product_area | 数据源、类型、产品域 | filterable |
| created_at / resolved_at | 时间 | filterable, sortable |
| content_vector | embedding(打在 title+提炼 body 上,语义干净) | 向量检索 |

导入时 LLM 做一次提炼,原始内容只服务于召回;超长处理发生在导入时,检索时永远是完整一条记录。
Hybrid 检索:BM25 + 向量 + semantic ranker(字段名 title/content/keywords 与 semantic
configuration 槽位一一对应,零映射)。embedding 打在 title+content 上。

### 6.3 清洗与质量过滤

剥离 HTML/issue 模板噪声、机器人评论;丢弃无实质答案的(如仅被 stale-bot 关闭的 issue)。

### 6.4 Embedding + Upsert

Azure OpenAI text-embedding-3-large,批量;mergeOrUpload,id 幂等重跑安全。

### 6.5 增量与状态

每源一个 since 水位(上次成功同步时间),存本地 JSON/blob,跑完推进;--full-refresh 强制全量。

### 6.6 运行形态(三种触发,内核同一 CLI)

CLI 入口:`python -m ingestion run [--source NAME]`;单源失败不影响其他源;结尾输出摘要报告。

1. **本地/CI**:cron / 手动
2. **容器**:Container Apps Job 定时触发
3. **Foundry 原生**:打包为 hosted agent 容器,由 **Foundry Routine**(preview)定时触发;
   未来可用 Routine 的 GitHub 事件触发(issue opened/closed)做准实时增量。
   Routines 是部署形态之一而非唯一依赖(仍 preview)。

## 7. Agent 与工具详细设计 ✅(已确认)

### 7.1 Agent Core

单 agent(MAF + Azure OpenAI)+ 五个工具(检索两级 + 升级 + 网络诊断 + 用量查询)。
升级瀑布由 system prompt 策略驱动 +
组合工具内的确定性编排,不做硬编码状态机。

处理管线(带扩展点):

```
用户消息 → [QueryPlanner] → agent tool loop → [AnswerEvaluator] → 回复
             (扩展点,v1 no-op)                  (扩展点,v1 no-op)
```

多 agent 口子:QueryPlanner(query 改写/拆解/意图路由)与 AnswerEvaluator
(回答质量评估/引用校验/主动升级判定)为显式 Python 接口,第一版 no-op 直通。
未来可升级为 MAF 子 agent,不动核心结构。

### 7.2 Tools

**1. `search_solutions(query, product_area?)` —— 组合检索工具(第一优先)**

内部 asyncio 并行:
- `knowledge_search`:AI Search hybrid(BM25+向量+semantic),返回 title/content/url/score
- `github_live_search`:GitHub API 搜指定 repo 清单(配置驱动,与 sources.yaml 对齐:
  vscode、vscode-copilot-release、copilot-intellij-feedback、copilot-cli、community 等)
  的 open issues/discussions

合并策略(代码保证,非模型决定):
- 总超时预算(~8s):到点只回来一个就用一个
- 都回来:合并去重,KB 结果排前,标注 source(kb / github-live)
- 都空/都失败:返回明确 "no_results" 信号

**2. `web_search(query, freshness?)` —— 第二级兜底**

Provider 链抽象:按配置顺序尝试(Bing Grounding → Tavily → WorkIQ...),
单 provider 失败/超时自动 failover。每 provider 一个 adapter,配置驱动。
返回统一格式:title/snippet/url/date。

**3. `escalate_to_human(reason)` —— 最后一级**

(channel_id 由 backend 从当前请求注入,LLM 只提供 reason —— 一句话概括已尝试路径,供接手人参考)

查静态配置表:channel → CSAM/CSA(姓名、邮箱、Teams user ID、是否在群)。
agent 据此决定 @提及(在群)或给联系方式(不在群)。
开工单指引为 system prompt 静态知识,不单独做工具。

**4. `network_diagnostics()` —— 主动网络诊断(超时/登录/断连类问题)**

关键事实:agent 跑在 Azure,探测的是 **Azure 出口视角**,测不到客户的
corporate egress/proxy/firewall。因此定位为"排除性证据 + 客户自测指引"两条腿:

- Agent 侧并行探测(配置驱动 `diagnostics.yaml`,超时 5s):
  `https://github.com/login`、`https://api.github.com/user`(预期 401,可达即通)、
  `https://copilot-proxy.githubusercontent.com`;企业客户按 channel 配置的
  enterprise_slug 追加 `https://github.com/enterprises/{slug}`。
  记录:可达性、HTTP 状态码、延迟
- 查 GitHub 官方状态页 API(githubstatus.com summary)
- 证据合成:agent 侧全通 + 状态页正常 → "GitHub 服务正常,问题大概率在贵司
  出口/代理/防火墙"(排除账号失效与 GitHub 故障);状态页有 incident → 贴链接
- 返回中附客户自测 curl 命令(客户在自己机器跑,测的才是客户网络)+
  Copilot allowlist 文档链接,提示网络组加白

**5. `copilot_usage_lookup(question_type, username?)` —— Copilot 计费/用量实况查询**

覆盖占比最高的 P0 主题(Credits/计费 24%)。只读 GitHub API:
`/orgs/{org}/copilot/billing`(seat 总量/计费模式)、`/orgs/{org}/copilot/billing/seats`
(成员 seat 明细)、`/orgs/{org}/settings/billing/usage`(premium requests 用量)。

- **token 归属**:查客户 org 需客户授权。按 channel 配置 `github_org` +
  `org_token_env`(环境变量名,存客户 org 的只读 fine-grained PAT)。
  **配置了就查真实数字;未配置返回 not_configured,LLM 转为指引:
  需贵组织 org admin 创建只读 PAT(billing/copilot read)并交给运营方配置**
- **隐私规则**:org 级汇总可在群里答;指向具体个人的明细只在 1:1 私聊答
  (代码层判断 is_group 拦截,非仅靠 prompt)
- **只读铁律**:工具层只实现 GET;建议客户 token 只授 read 权限,双保险

### 7.3 System prompt 策略(核心规则)

1. 永远先 `search_solutions`;KB(已解决)优先引用,GitHub live(讨论中)作为
   "该问题正在讨论中"的补充信息
2. 返回 no_results 才允许 `web_search`
3. 仍无好答案 → 通用排查建议(网络测试、重启、重试、升级版本)+ 开工单指引
4. 用户表示"还是不行/不满意"或涉及账务/合同 → `escalate_to_human` 升级到人
5. 回答语言跟随提问;引用永远带原始 url;不确定就说不确定,不编造
6. 问题涉及超时/登录失败/断连/Authorization error → 在 search_solutions 之后
   主动调用 network_diagnostics,把探测证据合进回答(从"给建议"升级为"给证据")
7. 计费/额度/seat 类问题:概念性解答走 search_solutions;涉及"我们组织的实际
   数字"时调 copilot_usage_lookup;群聊中只给 org 级汇总,个人明细引导私聊

### 7.4 多轮会话

- MAF thread/session 维护历史;会话 key = `AdvisorRequest.conversation_key` —— 平台无关的
  不透明字符串,agent core 只作字典 key 使用、从不解析。**如何推导是各渠道 adapter 的私有
  实现**:Teams 用 conversation.id(天然含 reply thread);企微/飞书等未来各自用本平台的
  会话/话题标识推导,core 与其他 adapter 均不受影响
- 存储接口抽象:v1 in-memory(TTL+条数上限),留 Cosmos DB/Redis 实现位
- 升级推进依赖对话历史,无显式状态字段

### 7.5 LLM Provider 抽象

默认 Azure OpenAI(chat GPT-4o/4.1);MAF 支持多 chat client,预留 Copilot SDK backend(待 GA)。

## 8. 渠道适配与 Teams 接入 ✅(已确认)

### 8.1 渠道适配原则

- 每个渠道 = channels/ 下一个独立子项目(独立入口、依赖、部署单元),零业务逻辑纯协议转换
- 与 agent core 的唯一契约:`AdvisorRequest`(text、conversation_key、channel_id、user、
  is_group)/ `AdvisorResponse`(markdown、citations[]、mentions[]),定义在 shared 包
- @提及渲染:agent core 只输出结构化"建议 @某人"指令(含平台 user ID),
  mention entity 由各渠道 adapter 自己拼装
- 新渠道(企微/飞书/web)= 新子项目,不改 agent core

### 8.2 Teams adapter(v1)

技术:Bot Framework SDK(Python)+ Azure Bot Service 注册(Entra 应用)

```
Teams 客户端(@提及 / 1:1)
  → Azure Bot Service → HTTPS POST /api/messages(aiohttp/FastAPI)
  → 验签 → 提取文本(剥离@标记)/conversation/channel/发送者/是否群聊
  → AdvisorRequest → Agent Core → AdvisorResponse
  → 渲染:markdown 子集 + 引用链接列表 + mention entity + 长回答用 Adaptive Card/分段
```

关键规则:
- 触发:channel 仅 @提及响应;1:1 全部响应;其余群消息忽略(自动沉淀留后续)
- 即时反馈:先发 typing indicator,agent 完成再回正文(检索+LLM 5-15s)
- conversation_key(Teams adapter 私有推导规则):直接取 Teams conversation.id ——
  channel 回帖时其值为 `"{channel_id};messageid={根消息ID}"`,天然同串共享会话;1:1 即会话 id
- 本地开发:dev tunnel / Bot Framework Emulator / 测试租户

## 9. 升级流程与联系人配置 ✅(已确认)

### 9.1 租户配置表(escalation.yaml,agent 项目内)

每个 channel 条目除联系人外,还承载该客户租户的可选能力配置:
- `enterprise_slug`:企业客户的 GitHub enterprise 标识,network_diagnostics 据此
  追加探测 `https://github.com/enterprises/{slug}`
- `github_org` + `org_token_env`:copilot_usage_lookup 用;org_token_env 是
  **环境变量名**(如 `ORG_TOKEN_CUSTOMER_A`),token 本体永远只在环境变量里,
  不进配置文件。两字段齐全才启用用量查询,否则工具返回 not_configured

```yaml
defaults:                        # 无 channel 匹配时兜底
  support_ticket_url: https://support.github.com/
  contacts:
    - role: CSA
      name: 张三
      email: zhangsan@example.com

channels:
  - channel_id: "19:abc...@thread.tacv2"
    tenant: 客户A
    enterprise_slug: customer-a          # 可选:网络诊断探测企业端点
    github_org: customer-a-org           # 可选:用量查询(与 org_token_env 成对)
    org_token_env: ORG_TOKEN_CUSTOMER_A  # 环境变量名,token 不进配置文件
    contacts:
      - role: CSAM
        name: 李四
        email: lisi@microsoft.com
        teams_user_id: "29:1a2b..."   # 有此字段且 in_channel=true 才可 @
        in_channel: true
      - role: CSA
        name: 王五
        email: wangwu@microsoft.com
        in_channel: false             # 不在群,只给联系方式
```

### 9.2 升级触发条件(system prompt 规则,依赖多轮上下文)

1. 用户明确不满意/未解决("还是不行"、"没用"、"找人吧")
2. 已给过通用建议+开工单指引后再次求助
3. 超出自助范围的问题(账务纠纷、合同、配额调整、组织级配置)→ 直接升级,不走完整瀑布

### 9.3 升级回复组成

- 已尝试路径简述(让被 @的人有上下文)
- 开工单指引(若未给过):GitHub Support 入口 + 建议附带的诊断信息(日志位置、版本号)
- 联系人:in_channel=true 且有 teams_user_id → mentions[] @指令;否则姓名+邮箱
- 无 channel 匹配 → defaults 兜底

### 9.4 边界

不承诺响应时限;v1 不自动创建工单(只给指引);不在群里输出密钥/敏感信息。

## 10. 错误处理、可观测性与测试策略 ✅(已确认)

### 10.1 错误处理(每层失败姿态)

| 故障 | 行为 |
|---|---|
| AI Search 不可用 | search_solutions 降级为仅 github_live_search;全失败则如实告知+通用建议 |
| web provider 失败 | failover 链依次尝试;全挂跳过该级进通用建议 |
| Azure OpenAI 限流/超时 | 指数退避重试 2 次;仍失败回复固定道歉文案(adapter 兜底,不静默) |
| GitHub API 限流 | ingestion:退避+断点续传(水位不推进);live search:跳过不阻塞 |
| ingestion 单源失败 | 隔离,其他源继续;摘要报告标红,exit code 非 0 供告警 |
| LLM 提炼失败(单条) | 跳过并记录,不中断批次 |
| 会话存储丢失 | 优雅降级为单轮问答 |

### 10.2 可观测性(v1 不自建控制台,用 Azure 原生栈)

- **结构化事件约定(shared 包定义)**:每次问答一条标准记录 —— conversation_key、渠道、
  问题摘要、瀑布终点(kb_hit/live_hit/web/generic_advice/escalated)、各工具延迟、
  failover 次数、是否 @人。OTel(MAF 原生)→ Application Insights
- **Azure Workbook 仪表盘**(JSON 模板入 repo):运营视图(问题量、各级命中率、升级率、
  主题分布)/ 健康视图(延迟 P50/P95、failover 频率、限流、错误率)/ ingestion 视图
  (每源运行结果、条数、水位)
- **告警**:ingestion 连续失败、agent 错误率超阈 → 邮件/Teams
- **v2 口子**:自建 admin 控制台归入 channels/web(问答记录浏览、KB 覆盖率、escalation
  配置管理、手动触发 ingestion)。v1 事件格式已定,v2 只加前端不改埋点

### 10.3 测试策略

1. **单元测试**:答案提取、清洗规则、合并去重、escalation 查表、语言检测(mock 外部依赖)
2. **契约测试**:shared 的 schema 与消息模型 —— ingestion 写入的文档可被 agent 检索消费;
   三项目各自对 shared 跑契约用例
3. **集成测试**(真实资源,单独 marker):AI Search 建索引+写入+hybrid 查询往返;
   Azure OpenAI;GitHub API(小数据量)
4. **Agent 行为评估(eval set,回归核心防线)**:从 50 个真实案例挑代表问题
   (每个 P0/P1 主题域 ≥2),断言:该命中 KB 的命中、该升级的升级、语言跟随正确;
   prompt 每次改动必跑
5. **Teams adapter**:mock activity 单元测试 + 上线前真实租户手工冒烟
