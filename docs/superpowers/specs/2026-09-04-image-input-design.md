# 图片输入与分析 — 设计文档

> 状态:**已确认(逐节评审通过)**
> 日期:2026-09-04
> 关联:`2026-08-21-copilot-advisor-design.md`(主设计 spec)、
> `2026-08-21-tooling-decision-record.md`(工具准入准则)

## 1. 背景与目标

用户在 Teams 里贴截图提问是高频行为,当前链路完全不处理 `attachments`,
图片直接丢失。目标是让 agent 能读懂用户发的图,并把图中信息接入既有的
检索升级瀑布。

确认的三类场景:

| 场景 | 说明 |
|------|------|
| 报错截图(最主流) | VS Code / IntelliJ / CLI 的错误弹窗、红字堆栈、Authorization error |
| 配置界面截图 | settings.json、MCP 配置面板、插件设置页,问"我这样配对不对" |
| 纯图片(无文本) | 只丢一张图不说话,agent 自行判断图里是什么、要解决什么 |

**明确不在范围**:计费/用量页面截图。该场景涉及金额与个人明细,
与 `copilot_usage_lookup` 的隐私规则(群聊只给 org 级汇总)存在交叉,
需要单独评估,本期不做。

第三类场景决定了一个硬约束:**图片是第一类输入,不是文本的附属**。
`AdvisorRequest.text` 为空时整条链路必须照常跑通。

## 2. 已确认的关键决策

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 图片进 LLM 的方式 | **原生多模态直通**:图片作为 content parts 与文本一起送 vision 模型,模型在整个 tool loop 中都能回看原图 |
| 2 | 是否新增工具 | **否**。对照工具准入准则,图片理解是输入模态而非"实时事实";且纯图片场景下模型不看图就无法判断该不该调工具,逻辑上鸡生蛋。工具面仍为 5 个 |
| 3 | 会话历史留存 | **只存文字描述**,不存原图。靠 system prompt 要求模型复述图中关键信息,复述随 assistant 回答自然进历史。`SessionStore` 契约不变 |
| 4 | 复述的承载形式 | **prompt 隐式总结**,不定义结构化 schema。避免多一次 vision 调用(+2-4s),也避免 schema 面对三类差异极大的输入时僵化 |
| 5 | 附件形态范围 | **仅 inline image**(粘贴/拖入)。不支持 file upload 附件 |
| 6 | 模型与 API 版本 | 复用 `AZURE_OPENAI_CHAT_DEPLOYMENT`(主 spec 决策 4 定的 GPT-4o/4.1 原生支持 vision);`AZURE_OPENAI_API_VERSION` 默认 `2024-10-21` 已支持,不改 |

### 2.1 被否决的方案(避免重复讨论)

**前置 vision 预处理**(独立一次调用把图转文本,再走纯文本链路):改动面最小、
可测试性最好,但 +2-4s 延迟(主 spec §8.2 已承认 5-15s),且预处理时不知道用户
要问什么,产出的描述可能漏掉关键细节而**无法回看原图**补救。"配置界面截图"
需要反复审视 UI 细节,受损最重。

**新增 `analyze_image` 工具**:不满足工具准入准则第 1 条(实时事实标准);
纯图片场景下存在鸡生蛋问题。推回。

**file upload 附件支持**(`supportsFiles: true` + SharePoint `downloadUrl`):
YAGNI。inline image 在群聊频道和 1:1 都可用,而 file upload 是 1:1 专属;
报错截图基本都是粘贴的。砍掉后 manifest 无需改动,且消除了整条
SharePoint 预授权 URL 的安全争议路径(见 §5.2)。

## 3. 契约变更(shared)

`shared/src/advisor_shared/messages.py`:

```python
class ImageInput(BaseModel):
    data: bytes          # 原始字节,平台无关
    mime_type: str       # image/png | image/jpeg | ...
    name: str = ""       # Teams inline image 无文件名,留空

class AdvisorRequest(BaseModel):
    ...                  # 现有字段不变
    images: list[ImageInput] = []
```

**存字节而非 base64**:base64 编码是 Azure OpenAI 的输入要求,属于 backend
实现细节,不应污染跨渠道契约。企微/飞书未来接入时给的同样是字节。

**默认空列表**:纯文本路径零影响,现有测试与其他渠道 adapter 不需要改动。

`shared/src/advisor_shared/events.py` 的 `AdvisorEvent` 增加:

```python
image_count: int = 0
```

用于运营观测图片类问题的占比与瀑布终点分布 —— 这是判断"图片输入值不值得
继续投入"(如是否支持 file upload、是否需要更高 detail)的数据依据。

## 4. 端到端数据流

```
Teams inline image
  → TeamsImageDownloader(新,实现 SDK 的 InputFileDownloader)
  → AgentApplication 管线自动写入 state.temp.input_files
  → bot.py 读取并映射为 ImageInput
  → extract.to_advisor_request(activity, bot_id, images)
  → AdvisorRequest.images
  → AdvisorCore.handle(透传 + 历史标记 + 事件计数)
  → AgentBackend.run(text, history, images)
  → MAFBackend 构造 multimodal content parts
  → Azure OpenAI (GPT-4o vision) → 既有 5 工具瀑布
```

## 5. Teams adapter:inline image 下载

### 5.1 接入点

M365 Agents SDK 的下载管线已经存在:`AgentApplication` 在 before-turn
middleware 之后、activity handler 之前调用 `_handle_file_downloads`,
把结果写进 `state.temp.input_files`。`AgentApplication.__init__` 的 `**kwargs`
会把匹配 `ApplicationOptions` 的键透传,因此 `file_downloaders=[...]` 直接传即可。

但 **Python 包 `microsoft-agents-hosting-core` 1.4.0 只提供抽象基类,
没有任何具体实现**(`M365AttachmentDownloader` 只存在于 .NET 与 JS)。
运行时可用的只有:

- `InputFileDownloader` — ABC,仅 `async def download_files(self, context) -> list[InputFile]`
- `InputFile` — dataclass,字段仅 `content: bytes` / `content_type: str` /
  `content_url: Optional[str]`。**没有 `filename` 字段**(与 .NET 不同),
  这就是 `ImageInput.name` 对 Teams 恒为空的原因

因此需要自建 `channels/teams/src/teams_adapter/downloader.py`。

`__main__.py` 的 `build_agent_app()` 中 `connection_manager` 已在作用域内,
直接注入下载器即可。

### 5.2 安全设计(本节是该模块的主要设计约束)

Teams 附件下载器有已披露漏洞 [GHSA-7vwx-582j-j332](https://github.com/openclaw/openclaw/security/advisories/GHSA-7vwx-582j-j332):
下载时把 bot bearer token 泄漏给攻击者可影响的域名。`contentUrl` 来自
activity payload,不能当作可信输入。

规则(全部为强制):

1. **精确 host 白名单**:`smba.trafficmanager.net`、`api.botframework.com`。
   **host 不在白名单 → 直接跳过该附件,不发起任何请求**。
   inline image 必然来自 Microsoft host,所以"跳过"比"不带 token 下载"更严格。
   - 用精确 host 相等比较,不用后缀匹配。SDK 源码注释特别指出
     `trafficmanager.net` 是共享命名空间,后缀匹配会放进第三方域名
   - 不包含 `*.asm.skype.com`:该形态只在解析 `text/html` 附件时才会出现,
     而本设计明确不解析 HTML 附件(见下)
2. **`follow_redirects=False`**:跟随重定向会把 `Authorization` header
   带到重定向目标,这是 token 泄漏的主路径
3. **401/403 后绝不自动重试加 token** —— 这正是上述 CVE 的成因

### 5.3 下载逻辑

```
download_files(context):
  1. channel 门禁:仅 msteams
  2. 遍历 context.activity.attachments:
     - 丢弃 content_type == "text/html"(Teams 会与图片一并下发,
       不过滤会导致去下载 HTML)
     - 仅保留 content_type 以 "image/" 开头的
     - 取 contentUrl;host 不在白名单 → 跳过
  3. 取 bot token(仅用于白名单 host):
       provider = connection_manager.get_token_provider_from_activity(
           context.identity, context.activity)
       token = await provider.get_access_token(
           context.identity.get_token_audience(),   # https://api.botframework.com
           context.identity.get_token_scope())
  4. httpx GET,带 Authorization: Bearer <token>,follow_redirects=False,
     超时 10s
  5. 校验与限额(见下),返回 list[InputFile]
```

限额(常量,防 token 爆炸与 DoS):

| 限额 | 值 | 超限行为 |
|------|-----|---------|
| 单图字节 | 4 MB | 跳过该图 |
| 单条消息图片数 | 4 张 | 取前 4 张 |
| 单图下载超时 | 10 s | 跳过该图 |

**下载失败一律不抛异常**:记录 warning 并跳过,链路降级为纯文本继续。
图片是增强,不能成为新的失败源。

### 5.4 触发与空消息防护

`should_respond` 不变(群聊仍需 @提及,1:1 全部响应)。

但 `to_advisor_request` 在纯图片场景下,text 剥离 mention 后为空。
`extract.py` 增加纯函数谓词 `is_empty(request) -> bool`
(`not request.text and not request.images`),逻辑保留在 `extract.py` 以维持
"bot.py 只做 SDK 对象与 dict 的桥接"的既有分层。

### 5.5 丢弃图片的信息回传

下载器出于失败、超尺寸或超数量而丢弃图片时是静默的,agent 无从得知。
若不回传,会产生两个坏结果:纯图片消息在下载全失败时变成**零响应**;
超数量时 agent 无法告知用户有图被忽略。

`bot.py` 持有判断所需的全部信息 —— 原始 activity 的图片附件数与
`state.temp.input_files` 的存活数:

```python
raw_count = 图片类附件数(content_type 以 "image/" 开头)
skipped   = raw_count - len(request.images)
```

据此分三种情况处理:

| 条件 | 行为 |
|------|------|
| `is_empty(request)` 且 `raw_count == 0` | 静默返回(用户确实没发内容) |
| `is_empty(request)` 且 `raw_count > 0` | 回复"图片没取到,能否把错误信息贴成文字",不调用 agent |
| `skipped > 0` 且仍有存活图片 | 向 `request.text` 追加 `(另有 N 张图片未能获取)`,由模型在回答中自然带出 |

第三种情况用追加文本而非新增契约字段:这是给模型的提示而非结构化数据,
`AdvisorRequest` 不必为此增加字段。

**已知行为**:SDK 的下载管线在 handler 之前运行,对 `should_respond` 判否的
消息也会先下载。当前 bot 在频道中只收到 @提及消息,影响可忽略。
**若未来开启 RSC 全量监听频道消息,必须在 downloader 内补 mention 门禁**,
否则会为大量不响应的消息下载图片。

## 6. Agent 侧:多模态直通

### 6.1 backend 协议

`agent/src/advisor_agent/backend.py`:

```python
async def run(self, user_text: str, history: list[dict],
              images: list[ImageInput] | None = None) -> str: ...
```

可选参数 + 默认 `None`,现有实现与测试无需改动。

### 6.2 MAFBackend 消息构造

**仅当 `images` 非空时**切换为 content parts 数组,否则保持现有纯字符串
`content` —— 确保纯文本路径的请求体逐字节不变,不引入任何行为漂移:

```python
if images:
    content = [{"type": "text",
                "text": user_text or "(用户只发了图片,无文字说明)"}]
    for img in images:
        b64 = base64.b64encode(img.data).decode()
        content.append({"type": "image_url", "image_url": {
            "url": f"data:{img.mime_type};base64,{b64}",
            "detail": "auto"}})
    messages.append({"role": "user", "content": content})
else:
    messages.append({"role": "user", "content": user_text})
```

`detail: "auto"` 让模型按图尺寸自选。报错截图的小字需要 high 精度,
但无脑指定 high 会让每张图固定涨到约 2.5k token;auto 对大图会自动升级。

图片只出现在**当前轮**的 user message 中。历史消息永远是纯字符串
(见 §7),因此多轮追问不会重复消耗图片 token。

### 6.3 vision 不可用时的降级

若部署的模型不支持 vision,Azure OpenAI 返回 400。**在 `MAFBackend` 内部捕获,
剥掉 images 重试一次**,并在结果中说明"当前部署未启用图片理解"。

放在 backend 内部而非依赖 `core.py` 的重试:`core.py` 的
`_MAX_ATTEMPTS = 2` 是无差别重试,两次都会以同样方式失败,最终落到通用道歉
文案 `FALLBACK_MESSAGE`,用户无从得知真实原因。

## 7. Prompt 规则

`agent/src/advisor_agent/prompts.py`,接在现有 8 条策略之后:

**规则 9(图片处理)**:收到图片时,先用 1-2 句复述图中关键信息
(错误原文 / 配置项 / 界面位置),再走正常升级瀑布。
**把图中读到的错误文本作为 `search_solutions` 的 query 主体**,
不要用"用户发了一张截图"这类空泛 query。

复述对用户是有价值的 —— 他能立刻看出 agent 有没有读错图;同时复述随
assistant 回答写入会话历史,这就是决策 3 中"图片信息的历史载体"。

**规则 10(图中敏感信息)**:图片里若出现 token、API key、cookie、
完整邮箱、账单金额等敏感信息,**不要复述到回答里**,
只说明"图中含敏感信息已略过"。

理由:截图本身已经在群里,群成员都看得到,复述不构成对群成员的新泄漏;
但 agent 把 token 转成**文本**会让它进入会话历史、`AdvisorEvent.question_summary`
以及 Application Insights 日志 —— 这是图片输入新增的泄漏面,原本不存在。

## 8. 会话历史

沿用决策 3:只存文字,不存原图。`SessionStore` 协议(`str` content)一行不动。

assistant 侧由规则 9 的复述天然承载,`core.py` 现有的历史写入机制已经覆盖。

user 侧需要补图片标记,否则纯图片场景会写入空 turn:

```python
user_note = (f"[图片×{len(request.images)}] {request.text}".strip()
             if request.images else request.text)
```

**已接受的代价**:后续追问图中未被复述到的细节(如"左下角那行写什么")
可能答不出。缓解手段是规则 9 要求复述"关键信息",以及 eval 用例对复述
质量的回归监控(§10)。若线上数据显示追问失败率高,再评估"保留最近 N 轮原图"
的混合策略。

## 9. 错误处理与降级

补入主 spec §10.1 的失败姿态表:

| 故障 | 行为 |
|---|---|
| 图片下载失败/超时 | 跳过该图,以文本继续;若无任何图片存活且文本为空 → 按 §5.5 回复"图片没取到,能否把错误信息贴成文字" |
| 图片超尺寸/超数量 | 截断到限额;按 §5.5 向 text 追加"另有 N 张图片未能获取",由模型带出 |
| host 不在白名单 | 跳过,记录 warning(安全事件,不静默) |
| 部署不支持 vision(400) | `MAFBackend` 内剥图重试一次,回复说明未启用图片理解 |
| 非图片附件(doc/xlsx/html) | 直接忽略,不发起下载 |

## 10. 测试策略

补入主 spec §10.3:

1. **`downloader`(安全回归,最关键)**:非白名单 host 不发出任何请求;
   重定向不跟随;401/403 不重试加 token;`text/html` 与非 `image/*` 被过滤;
   尺寸与数量限额生效;下载异常不向外抛。用 `respx` mock HTTP
2. **`extract`**:`to_advisor_request` 正确携带 images;`is_empty` 对
   空文本+空图判真、空文本+有图判假
3. **`bot` 的丢弃回传(§5.5)**:无附件+空文本 → 静默不回复;
   有图片附件但全部下载失败+空文本 → 回复提示文案且**不调用 agent**;
   部分丢弃 → `request.text` 含"另有 N 张图片未能获取"
4. **`maf_backend`**:有图时 content parts 结构正确(text part 在前、
   data URL 格式、detail 字段);**无图时仍为纯字符串**(防回归);
   vision 400 触发剥图重试
5. **`core`**:user 历史含 `[图片×N]` 标记;`image_count` 正确进入 `AdvisorEvent`
6. **`eval`**(追加用例,不改现有断言):`agent/tests/eval_cases.yaml` 增加可选
   `images:` 字段(fixture 相对路径),新增 2 个用例 —— 报错截图、配置截图,
   断言回答包含图中错误关键词且瀑布终点合理。
   fixture 用**真实小截图**(约 50 KB)而非代码合成图:断言的是真实 OCR 质量,
   合成图测不出来。eval 仍在 `-m integration` 下,默认不跑

## 11. 实现改动清单

| 文件 | 改动 |
|------|------|
| `shared/src/advisor_shared/messages.py` | 新增 `ImageInput`;`AdvisorRequest.images` |
| `shared/src/advisor_shared/events.py` | `AdvisorEvent.image_count` |
| `channels/teams/src/teams_adapter/downloader.py` | **新建** `TeamsImageDownloader` |
| `channels/teams/src/teams_adapter/extract.py` | `to_advisor_request` 加 images 参数;新增 `is_empty` |
| `channels/teams/src/teams_adapter/bot.py` | 启用当前被丢弃的 `_state`,读 `state.temp.input_files` 并映射为 `ImageInput`;实现 §5.5 的丢弃回传三分支 |
| `channels/teams/src/teams_adapter/__main__.py` | `AgentApplication(..., file_downloaders=[...])` |
| `agent/src/advisor_agent/backend.py` | 协议加 `images` 可选参数 |
| `agent/src/advisor_agent/maf_backend.py` | 多模态构造 + vision 400 剥图重试 |
| `agent/src/advisor_agent/core.py` | images 透传 backend;历史标记;事件 `image_count` |
| `agent/src/advisor_agent/prompts.py` | 规则 9、10 |
| 测试 | 见 §10 |

依赖:`httpx`(已在用,dev 侧有 `respx`)。**无新增第三方依赖。**

## 12. 已知边界与未来口子

- **仅 Teams**:其他渠道 adapter 仍是占位。契约已平台无关(`ImageInput` 用字节),
  企微/飞书接入时各自实现下载即可,agent core 零改动
- **不支持 file upload 附件**:群聊频道本就只能用 inline image;
  若 1:1 场景出现真实需求,重启时 §5.2 的安全规则需要扩展 —— SharePoint
  `downloadUrl` 是预授权 URL,**不应附加 botframework token**
  (官方 .NET 实现无条件附加,其源码注释自承 downloadUrl 为
  "attacker-controllable",不宜照抄)
- **不支持计费/用量截图**:见 §1,需与 `copilot_usage_lookup` 的隐私规则
  一并评估后再开
- **不做图片输出**:agent 只读图,不生成图
- **GCC High / DoD / 世纪互联环境**不支持 bot 收发文件,该环境下功能自动降级
  为纯文本(下载全部失败并跳过)
