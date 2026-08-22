# Teams 联调与部署

## 一、Azure Bot 注册(一次性)

1. Azure Portal → 创建资源 → **Azure Bot**
   - Bot handle:copilot-advisor-dev
   - 定价层:F0(开发)
   - 应用类型:Multi Tenant,让向导自动创建 App Registration
2. 记录 **Microsoft App ID**;在 App Registration → Certificates & secrets
   创建 client secret,记录值
3. Bot 资源 → Channels → 添加 **Microsoft Teams** 渠道

## 二、本地联调(dev tunnel)

```bash
# 1. 启动隧道(devtunnel CLI;或用 ngrok http 3978)
devtunnel host -p 3978 --allow-anonymous

# 2. Azure Bot → Configuration → Messaging endpoint 填:
#    https://<tunnel-id>.devtunnels.ms/api/messages

# 3. 环境变量(复制 .env.example 为 .env 并补充):
export TEAMS_APP_ID=<Microsoft App ID>
export TEAMS_APP_PASSWORD=<client secret>
# 计划 2 的 AZURE_OPENAI_* / AZURE_SEARCH_* / GITHUB_TOKEN / TAVILY_API_KEY 同样需要
cp agent/escalation.example.yaml agent/escalation.yaml  # 填真实联系人

# 4. 启动
uv run python -m teams_adapter
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

> 本清单需要真实 Azure Bot 注册与 Teams 租户,需人工逐项在 Teams 客户端中验证,不在自动化测试范围内。
