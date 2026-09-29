# 云端实验成本证据

[English](../2026-09-20-en/experiment-costs.md) · [阶段定义与计时边界](stage-definitions.md)

更新日期：2026-09-20；区域：东南亚；币种：USD。本报告包含部分账单快照及明确标注的零售价格测算场景，并非实验的最终账单。

## 截至观测时已报告的成本

在 **2026-09-20 02:26:23 UTC**，一次成功的 Cost Management `ActualCost` 查询覆盖了从 2026-09-18 00:00 UTC 到观测时刻的时间段。查询仅筛选 `rg-copilot-advisor-aca-exp` 和 `rg-copilot-advisor-flex-exp`，按资源组与服务分组，并对 `PreTaxCost` 求和。响应没有后续分页。

| 资源组 | 服务 | 已报告费用 USD |
|---|---|---:|
| ACA 实验 | 容器注册表（Container Registry） | 0.3536506134 |
| ACA 实验 | Log Analytics | 0.32984264235 |
| ACA 实验 | Microsoft Defender for Cloud | 0 |
| ACA 实验 | 存储（Storage） | 0.27449276 |
| Flex 实验 | Azure App Service | 0 |
| Flex 实验 | Microsoft Defender for Cloud | 0 |
| Flex 实验 | 存储（Storage） | 0.0000832 |
| **已报告小计** | | **0.95806921575** |

**该小计不是已完成工作负载的全部成本。**账单数据入库存在延迟：响应未返回 ACA 消耗费用行，这不能证明 ACA 计算免费或未被使用。必须区分已报告的零值行与缺失的服务。后续费用、抵扣、调整及税费尚未计入。应在账单数据补齐以及实验收尾后重新查询。

已被版本控制忽略的证据文件 `deploy/out/bench/experiment-cost-snapshot-20260920.json` 包含确切的请求、观测时间、响应列和数据行。未查询其他资源组的费用。SHA-256：`f37f22e342dfad80cda586b33dae123ce3d64656702369856afd6502022d7456`。

2026-09-19 04:39:53 UTC 的初始快照报告费用为 $0.07103551796。该快照仍原样保存在 `deploy/out/bench/experiment-cost-snapshot.json`；SHA-256：`d4a5fe4629734861a5680ee36abc229ffba719bdbea88f0af680289801939670`。

## 公开零售价格，而非实际计费数量

以下东南亚区域的 USD 费率于 2026-09-19 从 [Azure 零售价格 API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices) 获取。表中展示付费费率；不假设可使用免费额度、协议折扣或订阅级剩余额度。

| 服务／计量项 | 付费费率 | 计量项 ID |
|---|---:|---|
| Basic ACR 注册表 | $0.1666/day | `5c9e7a65-5784-494c-9718-7749d4075dd9` |
| ACR Task vCPU 时长 | $0.0001/vCPU-second | `bb93fa13-a4e2-4027-85b9-b06e3353ffc3` |
| ACA 活动 vCPU | $0.000034/vCPU-second | `5345ba68-7e2a-5eac-b829-98a6329c2272` |
| ACA 空闲 vCPU | $0.000004/vCPU-second | `72c036f2-6920-5fae-8459-96038d34e82e` |
| ACA 活动内存 | $0.000004/GiB-second | `5d2b5080-8fa7-581a-b8ae-fec637063514` |
| ACA 空闲内存 | $0.000004/GiB-second | `375db157-0f59-520a-be46-d449cd6797ab` |
| ACA 请求 | $0.40/million | `bdb6456b-7218-5020-bde6-f3268dc3f6b2` |
| Flex 按需（On Demand）执行时间 | $0.000037/GB-second | `9d043203-101b-507d-9f78-e2c8fa27f5bd` |
| Flex 按需（On Demand）执行次数 | $0.000004/10 executions | `e796b62c-4d21-5f05-b759-669864e7d41f` |

### 30 天 ACA 常驻副本的示例测算

假设恰好一个 0.25-vCPU／0.5-GiB 副本持续运行 2,592,000 秒：

- 若平台将每一秒都判定为符合空闲计费条件：`(0.25 * 0.000004 + 0.5 * 0.000004) * 2592000 = $7.776`。
- 若每一秒都属于活动状态：`(0.25 * 0.000034 + 0.5 * 0.000004) * 2592000 = $27.216`。
- 保留一个 Basic 注册表还需增加 `$0.1666 * 30 = $4.998`，尚未计入构建任务。

这些只是算术测算场景，**并非实测的月度预测或成本上限**。后台队列轮询、状态协调和网络流量会影响活动／空闲分类。横向扩展会增加副本；并发实验最多观测到 20 个副本。未计入存储、操作、日志、请求、出站流量、独立观测器、网络治理相关费用及外部依赖。观测器属于实验成本，并非生产环境的必需组件。初始价格查询未发现独立的 NSP 计量项；这不能证明所有网络／治理成本都为零。

### Flex 对比仍未实测

空的 Flex 基础设施已完成预配，但仍在等待实际区域配额证据，因此尚未部署应用代码或工作负载。故没有每个问题的 Flex 实测成本，也没有成本／性能对比结果。

在没有 Always Ready 实例的情况下，公开的付费计量公式为 `0.000037 * billable_GB_seconds + 0.0000004 * billable_executions`，尚未计入免费额度及其他服务。函数的计费执行时间不等于基准测试的端到端延迟；HTTP、队列工作进程及状态协调的执行都必须计入。应遵循平台的时长取整规则，而不是简单地将 ACA 回复延迟乘以 512 MB。

参见 [Flex 计费](https://learn.microsoft.com/azure/azure-functions/flex-consumption-plan#billing)和 [ACA 计费](https://learn.microsoft.com/azure/container-apps/billing)。

## 成本归属与收尾

- 现有的 OpenAI 和 Search 资源位于旧订阅中，未计入新资源小计。其模型／API 增量使用量、现有 Search 基线成本及任何 GitHub／网页搜索费用必须单独报告。尚未取得这些资源最终入账的增量费用。
- 即使未提交任何聊天机器人问题，保留注册表、Storage、监控和常驻观测器仍可能产生费用。ACA `minReplicas=0` 不能证明实际副本数或总成本为零。
- [受保护的收尾操作](aca-benchmark-closure.md)禁用了目标基准测试访问和观测器入口，保留目标 min0／Teams 入口，并停用了所有观测器修订版本。2026-09-20 02:24–02:25 UTC 的独立读取发现两者实际均为 replicas0。观测器保留的 min1 模板处于停用状态，并非常驻运行。由于注册表／存储／监控仍被保留，依然不能声称总成本为零。
- 删除资源需要另行获得用户明确确认；未为生成本报告而删除任何资源。
