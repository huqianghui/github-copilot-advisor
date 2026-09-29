# Teams 联调与部署

本文前四节保留原本地 dev tunnel 联调流程。ACA/Flex 的独立云部署入口见
[部署说明](../deploy/README.md),归档验证状态见
[中文实验报告](../deploy/reports/2026-09-20-cn/summary.md)
([English](../deploy/reports/2026-09-20-en/summary.md))。
云实验使用新的 Bot/托管身份,不要替换本地 Bot 的消息地址或 `.env` 凭据。

## 一、Azure Bot 注册(一次性)

1. Azure Portal → 创建资源 → **Azure Bot**
   - Bot handle:copilot-advisor-dev
   - 定价层:F0(开发)
   - 应用类型:**Single Tenant**
   - Creation type:创建新的 Microsoft App ID,或使用同租户的现有 App Registration
2. 记录 **Microsoft App ID** 和 **Tenant ID**;在 App Registration →
   Certificates & secrets 创建 client secret,记录 secret 的 **Value**
3. 回到资源组,打开资源类型为 **Azure Bot**
   (`Microsoft.BotService/botServices`)的 `copilot-advisor-dev`
4. Azure Bot 资源 → Settings → Channels → 添加 **Microsoft Teams** 渠道

> 2025-07-31 后 Azure 已不再支持新建 Multi Tenant Bot,新建时只能使用
> Single Tenant 或 User-assigned managed identity 是正常现象。本项目本地联调使用
> Single Tenant + client secret。

## 二、本地联调(dev tunnel)

### 1. 安装并启动隧道

```powershell
# 使用 Microsoft 365 账号登录,创建允许匿名访问的隧道
devtunnel user login
devtunnel create --allow-anonymous
devtunnel port create -p 3978
devtunnel host
```

保持此终端运行,并记录输出中的 `Connect via browser` URL。也可以改用
`ngrok http 3978`。

### 2. 配置 Azure Bot

回到 Azure Portal 中资源类型为 **Azure Bot**
(`Microsoft.BotService/botServices`)的资源,选择 Settings → Configuration,
在 **Messaging endpoint** 填写完整的公网 URL,追加 `/api/messages`,然后选择
Apply。例如:

```text
https://<tunnel-and-port>.<region>.devtunnels.ms/api/messages
```

不要手工拼接 tunnel ID;请以 `devtunnel host` 输出的
`Connect via browser` URL 为准。


### 3. 配置环境变量并启动

```powershell
# 复制配置模板,然后在 .env 中补充真实值
Copy-Item .env.example .env
Copy-Item agent\escalation.example.yaml agent\escalation.yaml

# .env 中至少需要(SingleTenant 的 tenant id, 落在 ...__TENANTID):
# CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID=<Microsoft App ID>
# CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET=<client secret>
# CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID=<Directory (tenant) ID>
# 以及 AZURE_OPENAI_* / AZURE_SEARCH_* / GITHUB_TOKEN / TAVILY_API_KEY

# 显式加载 .env 后启动;服务默认监听 3978
uv run --env-file .env python -m teams_adapter
```

## 三、装进 Teams

1. https://dev.teams.microsoft.com → Apps → New app,Bot 指向上面的 App ID
2. Preview in Teams,即可 1:1 私聊;添加到某个 team 后在 channel @提及

## 四、冒烟清单

- [ ] 1:1 私聊问"Copilot 登录失败怎么办" → 处理期间并行续发 typing → 中文回答带引用链接
- [ ] 慢请求处理中持续显示 typing;最终回复不等待 typing 请求,回复后不再续发
- [ ] 群聊未 @ Bot、未通过入口认证的请求不产生 typing
- [ ] channel 里不 @bot 发消息 → bot 无反应
- [ ] channel 里 @bot 提问 → 回答出现在同一 reply thread
- [ ] 同一 thread 里追问"还是不行,找个人吧" → 回复含 CSAM @提及或联系方式
- [ ] 英文提问 → 英文回答
- [ ] 停掉 AI Search(改错 endpoint)再提问 → 仍有回答(live/web 兜底)或明确道歉,进程不崩

Typing 默认每 3 秒尝试发送,单次发送超时为 2 秒;实际显示由 Teams 客户端和网络
决定。日志及非阻塞边界见 [Development](../DEVELOPMENT.md#非阻塞-typing)。

### 用户隔离

历史按租户、会话/线程、发送者隔离,回复仍公开发送到原群/线程。
同一分区按进入核心队列的顺序处理,其他分区可并行;不承诺按客户端发送时间重排。
上线切换不继承旧的混合群历史。历史按每条消息写入时间保留 30 天,
模型只读取当前用户会话最近 20 条未过期消息(通常约 10 轮问答)。
新消息不会延长旧消息的保留期,超过上下文窗口的有效历史仍保留。
当前本地内存模式在进程重启后仍会丢失历史;同一用户跨群、跨线程、跨私聊不共享。
缺少身份时会明确提示本次未处理,而不是进入共享历史。
当前本地内存模式不提供多实例共享历史或分布式排队;云端实现进度见
[云部署计划](superpowers/plans/2026-09-17-cloud-deployment-experiments.md)。
本地 `direct` 模式同一键的快速消息会在核心 turn 串行期间等待;
该模式不加队列深度上限,也不做 Teams 消息去重。
若平台重投递同一 activity,本地模式可能再次处理并回复;不要写成已复现故障。
云端 `queued` 模式在快速确认前持久受理任务,按分区有序执行并记录幂等状态;
HTTP 202 只证明受理,不证明回答完成。外部发送结果不确定时不盲目重发,
因此不能承诺严格 exactly-once 回复。

### 云端独立验收

本地曾通过的 Teams 验收不自动适用于新 Bot。分别侧载两个实验 Bot 后验证:
私聊、群 @/未 @、图片、引用、人工升级、同线程连续消息、A/B/A 用户隔离及重启后历史。
压测使用独立 Entra 实验入口和测试回执接收器,测量的是完整应用链路,
不是 Teams 客户端网络时延;它也不代替真实 Teams 附件/展示验收。
未得到实际客户端证据的项目保持 `not_run`。

> **状态(2026-09-14):双账号 Teams 用户隔离验证通过(用户确认)。**

其余未勾选项尚未收到单独验证结果,保持待确认:

- [x] 同群双账号用户上下文隔离验证通过。
- [ ] A 在同群连发两条问题,第二轮能够使用第一轮成功问答。
- [ ] A 在另一个群、频道线程或私聊追问,不自动继承原会话历史。
- [ ] 群聊和私聊并发,群内个人用量查询仍被隐私门禁拒绝。
- [ ] 原有 @触发、图片输入、引用和人工升级仍按原方式工作。
- [ ] 如实际使用共享频道/跨租户场景,核验身份提取是否按两租户字段一致性规则接受或拒绝;不要让不一致租户绕过校验。
- [ ] 单人连续快速发送 3 条非敏感消息,记录每条等待时长与回复次数,检查是否出现重复回复;若有失败,照实记录。

### 图片输入(2026-09-05 新增)

单元测试覆盖了下载器与接线,但**没有任何自动化测试碰过真实 Teams 附件下载** ——
`state.temp.input_files` 那段 SDK 管线在测试里是手工塞的。下面几条是唯一的验证。

> **状态(2026-09-07):5/7 行为项 + 2/3 日志项已验证。** 未勾选的三条见下。

**做冒烟前必读 —— 两个会让你误判的约束:**

1. **改完代码必须重启 bot。** 2026-09-07 有一次差点把"进程跑的是旧代码"
   误判成"修复没生效" —— 症状是 traceback 指向一行**注释**(进程加载旧字节码,
   打印时读的是磁盘上的新文件)。
2. **当前记忆范围**:每条消息在写入后 **30 天**过期,模型只看本用户会话最近
   **20 条**有效历史;本地**进程重启**仍会清空纯内存历史和 SDK 的 `MemoryStorage`。
   所以「贴图后追问」应在同一用户会话、历史窗口内完成,且中途不重启。
   2026-09-07 的 3599s 保留 / 3601s 清空实测属于旧的 1 小时 TTL 实现,
   不作为当前 30 天策略的实测证据。
   **最省事的做法:先重启,然后一口气做完全部冒烟动作。**

- [x] **群里 @bot 粘贴报错截图 + 一句话** → 回答**开头先复述图中错误原文**,再给方案
      (复述是 prompt 规则 9 要求的,也是图片信息进入会话历史的唯一载体)
- [x] **群里 @bot 只粘贴截图、不打字** → 正常回答,**不是零响应**
      (这条曾是设计缺陷:text 剥 mention 后为空 + 图没存活 = 静默退出)
- [ ] **1:1 私聊粘贴截图** → 正常回答
- [x] **贴图后在同一 thread 里同一用户追问"那第二步怎么做"** → 答得上来
      (证明复述确实进了历史 —— 原图不会重传)
- [ ] **一次粘贴 6 张图** → 回答里出现「另有 2 张图片未能获取」
      (`MAX_IMAGES = 4`,超出的被截断)
- [x] **发一个 `.docx` 附件** → 被忽略,不报错,**且不会误报"另有 1 张图片未能获取"**
      (两个计数函数都按 `image/*` 过滤,口径必须一致)
- [ ] **贴一张含 token/密钥的截图** → 回答**不复述那段密钥**,只说"图中含敏感信息已略过"
      (prompt 规则 10;这是图片输入新增的泄漏面 —— 截图群里都看得见,
      但转成文字会进会话历史)

**同时看日志**(`uv run --env-file .env python -m teams_adapter` 的输出):

- [x] 每条带图消息的 `advisor_event` 里 `image_count` 与实际张数一致
- [x] 全程没有任何一行日志出现 base64、图片内容或 bearer token
- [ ] 若某张图下载失败,`downloader` 只记 `host=...`,**绝不记完整 URL**

> 本清单需要真实 Azure Bot 注册与 Teams 租户,需人工逐项在 Teams 客户端中验证,不在自动化测试范围内。
