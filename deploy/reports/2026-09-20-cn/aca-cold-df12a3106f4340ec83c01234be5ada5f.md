# ACA 冷启动尝试——证据不完整／未经验证

[English](../2026-09-20-en/aca-cold-df12a3106f4340ec83c01234be5ada5f.md) · [阶段定义与计时边界](stage-definitions.md)

运行：`df12a3106f4340ec83c01234be5ada5f`。

未导出最终结果字节。最后一个通过驱动器 MI 读取的检查点记录状态为 `running`、terminal_count0 和 pending_count0；其中没有已提交的问题意图。这不是成功的冷启动样本，不能将其虚构为删失请求，也不能据此证明最终停止原因。

封存的计划于 `2026-09-19T06:41:13.729465+00:00` 到期。只读的最终 Blob 监视器结束时仍未获得完整的最终载荷。ARM 后来显示驱动器容器未启动／未就绪，restartCount4；autostart 仍为 false。没有重复 START，也未修复源代码或策略。

原始检查点：`deploy/out/bench/aca-cold/df12a3106f4340ec83c01234be5ada5f-checkpoint.json`；SHA-256 `914d098f9c7a83dcd8fc1b0bdb8b4301c3afcb7bf2da241b9d6521a8429c6836`。
