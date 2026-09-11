# Teams 联调与部署

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

- [ ] 1:1 私聊问"Copilot 登录失败怎么办" → typing 指示 → 中文回答带引用链接
- [ ] channel 里不 @bot 发消息 → bot 无反应
- [ ] channel 里 @bot 提问 → 回答出现在同一 reply thread
- [ ] 同一 thread 里追问"还是不行,找个人吧" → 回复含 CSAM @提及或联系方式
- [ ] 英文提问 → 英文回答
- [ ] 停掉 AI Search(改错 endpoint)再提问 → 仍有回答(live/web 兜底)或明确道歉,进程不崩

### 图片输入(2026-09-05 新增)

单元测试覆盖了下载器与接线,但**没有任何自动化测试碰过真实 Teams 附件下载** ——
`state.temp.input_files` 那段 SDK 管线在测试里是手工塞的。下面几条是唯一的验证。

> **状态(2026-09-07):5/7 行为项 + 2/3 日志项已验证。** 未勾选的三条见下。

**做冒烟前必读 —— 两个会让你误判的约束:**

1. **改完代码必须重启 bot。** 2026-09-07 有一次差点把"进程跑的是旧代码"
   误判成"修复没生效" —— 症状是 traceback 指向一行**注释**(进程加载旧字节码,
   打印时读的是磁盘上的新文件)。
2. **会话记忆有两个丢失条件**:间隔 > **1 小时**(`InMemorySessionStore` 的
   TTL,已实测 3599s 保留 / 3601s 清空),或**进程重启**(纯内存,重启即清空,
   SDK 的 `MemoryStorage` 同理)。
   所以「贴图后追问」这条必须在 1 小时内、且中途不重启。
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

### 用户隔离

历史按租户、会话/线程、发送者隔离,回复仍公开发送到原群/线程。
同一分区按进入核心队列的顺序处理,其他分区可并行;不承诺按客户端发送时间重排。
上线切换不继承旧的混合群历史。历史仍在 1 小时 TTL 到期或进程重启后丢失,
最多保留 20 条消息;同一用户跨群、跨线程、跨私聊不共享。
缺少身份时会明确提示本次未处理,而不是进入共享历史。
本次不提供多实例共享历史或分布式排队。

以下双账号 Teams 验收尚未执行,执行后再逐项勾选:

- [ ] A、B 在同一群分别提供不同的非敏感测试标记,交错追问时各自只延续自己的标记。
- [ ] A 在同群连发两条问题,第二轮能够使用第一轮成功问答。
- [ ] A 在另一个群、频道线程或私聊追问,不自动继承原会话历史。
- [ ] 群聊和私聊并发,群内个人用量查询仍被隐私门禁拒绝。
- [ ] 原有 @触发、图片输入、引用和人工升级仍按原方式工作。
