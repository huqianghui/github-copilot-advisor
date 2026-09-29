# ACA 基准测试收尾——已完成，未执行删除

[English](../2026-09-20-en/aca-benchmark-closure.md) · [阶段定义与计时边界](stage-definitions.md)

用户授权在保护机制下，以可逆方式关闭临时观测器及目标基准测试功能，同时保留正常的 Teams 入口和全部持久化状态。

## 实际操作与回读验证

首次变更尝试于 **2026-09-19T07:47:27.442982Z** 开始。操作禁用了观测器入口并修改了目标的 `BENCHMARK_ENABLED` 标志，随后在设置回读保护检查处停止。未尝试删除或回滚。

对该确切目标进行只读 Resource Graph 变更历史查询，仅发现：

- 已获授权的环境值从 `True` 变为 `false`；
- 预配状态及自动生成的最新修订版本元数据发生变化。

独立比较新旧修订版本模板，也仅发现该环境值不同。回归测试增加了对 `--no-wait` 之后未变化的陈旧回读结果进行限时轮询的覆盖；无关配置漂移仍会导致失败。剩余收尾操作已重新获得精确的 WhatIf 批准。沿用原始变更窗口，未重新计时。

受保护的恢复操作于 **2026-09-19T08:06:45.534051Z** 完成：距首次变更 **1,158.091 秒（19m18s）**，未超过 20 分钟限制。

实际最终回读结果：

| 检查对象 | 最终证据 |
|---|---|
| ACA 目标基准测试功能 | `BENCHMARK_ENABLED=false` |
| ACA 目标伸缩设置 | min0；后续独立 ARM 回读发现 replicas0 |
| 正常 Teams 入口 | 保留外部 HTTPS 入口及原始配置 |
| 基准测试访问拒绝检查 | 未经身份验证的空 POST `/bench/messages` 返回404 |
| Teams 路由保留检查 | 未经身份验证的空 POST `/api/messages` 返回401 |
| 临时观测器入口 | 已禁用（`ingress=null`） |
| 临时观测器活动修订版本 | 无 |
| 临时观测器副本 | 0，包括明确停用的修订版本 |
| 临时观测器状态 | `suspended`, `BENCHMARK_AUTOSTART=false` |
| 资源、身份、角色记录、结果数据 | 均予以保留；未执行删除、RBAC、策略或 Flex 操作 |

这些拒绝访问探测既不是基准测试问题，也不是 Teams 功能测试。它们短暂唤醒了目标：探测完成时存在一个副本，min0 保持不变；之后的完整控制平面回读发现副本数已为零。辅助工具从未为了达到某个副本数量而强制关闭正常的 Teams 服务。

观测器暂停采用了文档规定的可逆操作，停用 `ca-bench-cskd5yazfko42--0000007`。保留模板上的 min1/max1 配置没有改变，也未被解释为 max0；停用修订版本的实际副本数为零。重新开放需要单独批准操作并提供有效的观测器计划，不能直接重新激活已过期的镜像。

## 完整的受保护操作命令

```powershell
.\deploy\bench\Close.ps1 -ParametersFile deploy\out\plans\benchmark-close.json

.\deploy\bench\Close.ps1 -ParametersFile deploy\out\plans\benchmark-close.json `
  -Mode Apply `
  -Confirmation 'BENCH-CLOSE:aca:8cf1c6ed-743f-4f0b-ac18-df6267235e34:e3d7ca9304a7f283'
```

在保护机制中止操作并完成独立变更历史审查后，同一预览命令生成了新的剩余操作确认：

```powershell
.\deploy\bench\Close.ps1 -ParametersFile deploy\out\plans\benchmark-close.json `
  -Mode Apply `
  -Confirmation 'BENCH-CLOSE:aca:8cf1c6ed-743f-4f0b-ac18-df6267235e34:8ce8e2c670bf1861'
```

## 证据

位于已被版本控制忽略的 `deploy/out/bench/aca-cold/` 下：

- `guarded-closure-preview-2.txt`、`guarded-closure-apply.txt`；
- `guarded-closure-first-blocked.json`、`closure-change-history-paths.json`、`closure-safe-change-detail.json`、`closure-drift-diagnostic.json`；
- `guarded-closure-resume-preview.txt`、`guarded-closure-resume-apply.txt`；
- `guarded-closure-command.json`、`guarded-closure-resume-command.json`；
- `guarded-closure-final-readback.json`。

持久化控制记录为 `deploy/out/runs/observer-deployment.closure.json`。它记录了原始截止时间、静止状态证据、预定操作、受保护配置的哈希、路由检查，以及最终的修订版本／副本观测。它不会回显机密或无关环境值，并保留基础配置摘要，而不是编造新的部署制品／配置。

## 冷启动结果仍不完整

收尾操作**不会**修复冷启动样本组或改变其分类：实测结果仍为 **8/10** 个经过自然缩容至零验证的成功问题。尝试 `df12a3106f4340ec83c01234be5ada5f` 仍只有已导出的检查点，没有已导出的最终结果。其持久化状态既未删除，也未丢弃。未重复任何负载或 START。

执行收尾时，HEAD 仍为 `49100580008a769aafc87c4f12f497e048a7fe6f`。仅编辑了与收尾相关的部署辅助工具、测试和文档；未修改应用的业务、模型或身份验证代码。验证后，收尾代码提交为 `0fe3ec1`；这不会改变历史工作负载的制品绑定。

## 代码与验证

代码文件：

- `deploy/tools/src/advisor_deploy/benchmark_close.py`
- `deploy/tools/src/advisor_deploy/operations.py`——仅涉及收尾分派及可选的更短受检命令超时
- `deploy/tools/src/advisor_deploy/benchmark_deploy.py`——支持暂停状态
- `deploy/bench/Close.ps1`
- `deploy/tools/tests/test_benchmark_close.py`

先失败后通过的测试覆盖了：非静止状态下拒绝操作、历史不可变制品绑定、精确确认、环境／机密引用及 Teams 配置保持不变、输出脱敏、真实 ARM 默认字段呈现、陈旧读限时处理、停用后的排空、拒绝重置变更窗口，以及拒绝访问探测后如实报告目标副本数。

最终命令：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  deploy\tools\tests\test_benchmark_close.py `
  deploy\tools\tests\test_observer_deployment.py `
  deploy\tools\tests\test_docker_routing.py `
  deploy\tools\tests\test_powershell_contracts.py `
  deploy\tools\tests\test_deploy_workflows.py -q --tb=short
```

**65 项通过。**`git diff --check` 通过。独立实时回读确认：目标基准测试标志为 false、min0、replicas0，且保留了 Teams 入口；观测器入口为 null、无活动修订版本、replicas0、autostartfalse、状态为 suspended。

## 独立后续核查

在 2026-09-20 02:24–02:25 UTC，新的 CLI 控制平面读取再次确认：目标基准测试标志为 false、min0，活动修订版本已缩容至零且无副本；观测器入口为 null、AUTOSTART 为 false，所有修订版本均已停止且无副本。没有调用任何端点，因此本次验证未唤醒任何一个应用。

独立重跑这 65 项测试时，63 项通过，另有两项发生 Windows 子进程超时（运行历时 6h57 后出现负的剩余等待值），并非功能断言失败。随后原样重跑这两项失败测试，在 53.74 秒内通过；没有修改超时或行为。编辑器诊断未发现错误，全部 55 处 `Cli.run`／21 处 `Cli.rest` 调用点均通过了语义签名兼容性检查。
