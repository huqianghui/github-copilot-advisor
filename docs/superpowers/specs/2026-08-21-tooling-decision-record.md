# 工具设计决策记录(Tooling Decision Record)

> 状态:随工具演进持续维护的活文档
> 日期:2026-08-21
> 关联:`2026-08-21-copilot-advisor-design.md`(主设计 spec)

本文档记录:客户问题主题的全景、每个主题"是否值得成为工具"的评估结论与理由、
工具设计的通用准则,以及 prompt 设计考量。**未来任何新工具提案,先对照本文档的
准则与既有结论评估,再动 spec 和计划。**

## 1. 工具准入准则(评估任何新工具提案的基准)

一个主题值得成为独立工具,当且仅当同时满足:

1. **实时事实标准**:回答需要"静态知识(KB/文档)拿不到的实时事实"——
   实况数字、探测结果、权威 registry 数据。纯概念/配置方法/最佳实践类
   永远走 KB + web_search,不做工具。
2. **web_search 不可替代标准**:通用 web 搜索覆盖不了,或经 eval/线上数据
   证明覆盖质量不够(过时、不准)。**"API 更精确"不构成充分理由 ——
   精度问题必须先被数据证实,再造工具**(见 §3 version_lookup 案例)。
3. **投入产出标准**:主题流量占比(见 §2 表)与授权/维护成本相称。
   需要客户侧授权(token/admin 配合)的工具,必须设计"未授权时的优雅降级"。

工具的隐性成本(每加一个都要付):prompt 变长、LLM 多一次路由决策、
误触发面扩大、维护面扩大。默认答案是"不加"。

## 2. 客户问题主题全景与工具映射(2026-08 Teams 群 50 案例基线)

| # | 主题域 | 占比/优先级 | 实时事实? | 工具结论 |
|---|--------|------------|-----------|----------|
| 1 | Credits/Token/计费/上下文治理 | 24% / P0 | 有:org 计费与用量 API | ✅ `copilot_usage_lookup` |
| 2 | 稳定性/超时/无响应/登录与网络 | 20% / P0 | 有:链路探测+状态页 | ✅ `network_diagnostics` |
| 3 | Agent/Subagent/模型路由 | 14% / P0 | 部分:模型可用列表变化快 | ❌ 不建 —— 变化信息在官方 changelog/docs,web_search 覆盖;概念走 KB |
| 4 | IDE/插件/Remote 兼容性 | 12% / P1 | 有:registry 版本数据 | ❌ 暂不建(version_lookup 被推回,见 §3);prompt 引导 + eval 监控,数据证实不足再回头 |
| 5 | 文件/上下文/会话管理 | 10% / P1 | 无(纯使用方法) | ❌ KB + web_search |
| 6 | MCP 集成与密钥管理 | 8% / P1 | 无(配置方法类) | ❌ KB;密钥安全建议是静态知识 |
| 7 | 额度/套餐/Usage 可见性 | 4% / P1 | 有 | ✅ 已被 `copilot_usage_lookup` 覆盖(与主题 1 同一组 API) |
| 8 | WorkIQ/M365 扩展 | 4% / P2 | 少:admin center 授权状态理论可查,但需 M365 admin token | ❌ v1 走 KB + web_search;若高频再评估 Graph API 工具 |
| 9 | API/平台能力边界 | 2% / P2 | 无(文档性) | ❌ KB |
| 10 | 产出型高级用例 | 2% / P2 | 无(经验性) | ❌ KB |

**v1 工具面(5 个,固定):** `search_solutions`、`web_search`、`escalate_to_human`、
`network_diagnostics`、`copilot_usage_lookup`。

## 3. 已推回的提案(含理由 —— 避免重复讨论)

### version_lookup(主题 4,2026-08-21 推回)

**提案**:查询插件权威版本与兼容区间。技术方案已论证可行,备查:
- 分发是集中式的,查询入口只有三个 registry,覆盖全部平台:
  - VS Code Marketplace API → `GitHub.copilot` / `GitHub.copilot-chat`(VS Code 全系)
  - JetBrains Marketplace API → plugin 17718 `com.github.copilot`,**一个插件覆盖
    IntelliJ/WebStorm/PyCharm/GoLand/Rider 全家桶**;兼容性按 build number 区间
    (since/until,如 243.0+)声明,所有 JetBrains IDE 共享统一 build 体系
    (IntelliJ 2024.3 = WebStorm 2024.3 = build 243.x),查一次即回答所有 IDE
  - GitHub Releases API → `github/copilot-cli`
  - 例外:Visual Studio(非 VS Code)内置 Copilot 无独立插件 API,返回官方文档链接
- 返回形态:latest_version, published_at, since_build/until_build, release_url

**推回理由**(准则 2 不满足):web_search 搜 "copilot jetbrains plugin latest
version" 首条即 marketplace 页面,LLM 读 snippet 可答。边际价值(权威精度、
完整 build 区间)未经数据证实,不足以支付第 6 个工具的隐性成本。YAGNI。

**替代措施**(已落地):
1. system prompt 一行引导:版本/兼容类问题,web_search 查询词带
   "marketplace"/"plugin",优先引用 marketplace.visualstudio.com /
   plugins.jetbrains.com / github.com releases 的结果
2. eval 集含版本类用例(ide-intellij-version + WebStorm 兼容用例)

**重启条件**:eval 或线上事件数据显示版本类问题的 web 结果经常过时/答错
(如该主题 stage=web 的回答被用户否定率明显高于其他主题)。重启时本节技术
方案直接可用。

## 4. 既有工具的设计考量(新工具设计时对照)

### 通用模式(所有工具遵守)

- **配置驱动、按租户隔离**:租户级能力(enterprise_slug、github_org、
  org_token_env)全部挂在 escalation.yaml 的 channel 条目上;token 本体只在
  环境变量,配置文件只存变量名
- **优雅降级**:外部依赖不可用/未授权时返回结构化的降级信号
  (not_configured / unknown / no_results),由 LLM 转为对用户的指引,
  绝不抛异常打断回答
- **副作用走 RunContext**(contextvars),不解析 LLM 自由文本
- **判定逻辑在代码不在 LLM**:verdict/合并/隐私拦截等规则由代码保证,
  LLM 只做解释与组织语言

### network_diagnostics 特有考量

- **视角诚实**:agent 跑在 Azure,探测的是 Azure 出口视角,测不到客户的
  corporate egress/proxy/firewall。定位是**排除性证据**("GitHub 服务端正常,
  问题大概率在贵司出口")+ **客户自测命令**(curl 模板,客户在自己机器跑,
  测的才是客户网络)两条腿
- 4xx(如 api.github.com/user 的 401)= 链路通(能到达),不是失败
- 端点清单配置驱动(diagnostics.yaml),与 Copilot allowlist 文档对齐

### copilot_usage_lookup 特有考量

- **token 归属**:查客户 org 必须客户授权(org admin 发只读 fine-grained PAT:
  Copilot read + billing read)。给了就查真实数字;没给就返回 guidance 告知
  怎么授权,并附自查路径(GitHub Settings → Copilot → Usage)
- **隐私分级(代码层拦截,非 prompt 约束)**:org 级汇总可在群聊答;
  指向具体个人的明细(user_usage)群聊中一律拦截,引导 1:1 私聊
- **只读铁律**:工具层物理上只实现 GET;建议客户 token 只授 read,双保险

## 5. Prompt 设计考量(工具路由规则的演进原则)

当前路由规则(spec 7.3,7 条)的设计逻辑:

1. **瀑布有序**:search_solutions 永远第一;web_search 有明确前置条件
   (no_results);escalate_to_human 有明确触发词/场景清单
2. **主动诊断触发词显式列举**:超时/登录失败/断连/Authorization error →
   network_diagnostics。新增触发场景时扩这个列表,不要写抽象规则
   ("网络类问题"),LLM 对具体错误字符串的匹配远比抽象分类可靠
3. **实况查询与概念解答分流**:计费类问题先 KB(概念),用户问"我们组织的
   实际数字"才调 usage 工具 —— 区分信号是"我们/我的 + 数字类疑问"
4. **降级信号的转译规则写进 prompt**:not_configured → 原样传达 guidance;
   privacy_blocked → 引导私聊。LLM 不自由发挥降级文案
5. 每加一个工具,必须同步:工具 docstring(何时用+何时不用)、prompt 路由
   规则、eval 用例(expect_tool_called 断言)。三者缺一即回归风险

## 6. 主题域 → keywords 分类体系(检索质量的基础设施)

10 个主题域固化为 ingestion 的分类标签(提炼时由 LLM 打标进 keywords 字段):

```
billing-credits | stability-network | agent-routing | ide-compat |
context-session | mcp-integration | usage-visibility | m365-workiq |
platform-limits | advanced-usecases
```

用途:
- 检索:keywords 是 filterable/facetable,支撑 product_area 之外的主题过滤
- 运营:事件(AdvisorEvent)按主题聚合,观测各主题命中率/升级率 ——
  **这是判断"哪个主题该回头建工具"的数据来源**(§3 重启条件的度量基础)
- eval:评估集按主题覆盖(每个 P0/P1 主题 ≥2 用例)

## 7. 未来提案模板

新工具提案按此格式追加到本文档 §8,评审通过才动 spec/计划:

```
### <工具名>(主题 N,日期)
- 回答什么实时事实:
- web_search 为什么不够(附 eval/线上数据):
- 授权/降级设计:
- 流量占比与成本评估:
- 结论:采纳 / 推回(理由)
```

## 8. 提案记录

(暂无新提案)
