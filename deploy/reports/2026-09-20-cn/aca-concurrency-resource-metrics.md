# ACA 并发样本组 B——实际资源指标补充

[English](../2026-09-20-en/aca-concurrency-resource-metrics.md) · [阶段定义与计时边界](stage-definitions.md)

最初以原始 REST 格式发出的指标请求失败后，这些只读 Azure Monitor 指标查询通过 CLI 成功执行。原始采集记录／报告保持不变；本补充报告提供当时缺失指标的实际测量值。目标制品：`31882ec48e652906874e3d5687bf0b675dfbb841d66531d84ee23a204cd7a4d0`。查询使用 PT1M，并采用按 UTC 分钟对齐的边界覆盖每次运行的确切窗口。边缘时间桶可能包含最多一分钟的相邻活动。下列均值是已报告的每分钟平均值的均值，不是逐请求利用率。缺失采样点仍为未知。RestartCount 展示已报告指标的最大值，不会通过累加重复的瞬时值样本编造结果。仅凭这些资源观测，无法确定瓶颈或平台的绝对并发上限。

| C | 指标 | 采样点数 | 每分钟平均值的均值 | 已报告的最大值 | 状态 |
|---:|---|---:|---:|---:|---|
| 1 | CpuPercentage | 8 | 13.25 | 64.0 | 成功（Success） |
| 1 | MemoryPercentage | 8 | 23.6875 | 24.0 | 成功（Success） |
| 1 | Replicas | 8 | 1.0 | 1.0 | 成功（Success） |
| 1 | RestartCount | 8 | 0.0 | 0.0 | 成功（Success） |
| 2 | CpuPercentage | 5 | 24.6 | 51.0 | 成功（Success） |
| 2 | MemoryPercentage | 5 | 24.4 | 25.0 | 成功（Success） |
| 2 | Replicas | 5 | 1.0 | 1.0 | 成功（Success） |
| 2 | RestartCount | 5 | 0.0 | 0.0 | 成功（Success） |
| 4 | CpuPercentage | 4 | 39.625 | 72.0 | 成功（Success） |
| 4 | MemoryPercentage | 4 | 25.25 | 26.0 | 成功（Success） |
| 4 | Replicas | 4 | 1.0 | 1.0 | 成功（Success） |
| 4 | RestartCount | 4 | 0.0 | 0.0 | 成功（Success） |
| 8 | CpuPercentage | 4 | 47.733333333333334 | 72.0 | 成功（Success） |
| 8 | MemoryPercentage | 4 | 25.9 | 27.0 | 成功（Success） |
| 8 | Replicas | 4 | 1.875 | 3.0 | 成功（Success） |
| 8 | RestartCount | 4 | 0.0 | 0.0 | 成功（Success） |
| 16 | CpuPercentage | 11 | 22.051168276168276 | 101.0 | 成功（Success） |
| 16 | MemoryPercentage | 11 | 23.006426906426906 | 27.0 | 成功（Success） |
| 16 | Replicas | 11 | 14.272727272727273 | 20.0 | 成功（Success） |
| 16 | RestartCount | 11 | 0.0 | 0.0 | 成功（Success） |

## 来源证据

- C=2, `2026-09-19T03:54:00Z` / `2026-09-19T03:59:00Z`：[指标证据](../../out/bench/aca-concurrency/39117b0d856d44598a1ec26042a6a94c-resource-metrics.json), SHA-256 `9908caf21f5d8d2b2ae76054cc279c75c3217b97eebf33a7ed3b250a74156d4d`。
- C=4, `2026-09-19T03:58:00Z` / `2026-09-19T04:02:00Z`：[指标证据](../../out/bench/aca-concurrency/af7aa66860534abf9db46cc10d66e6fc-resource-metrics.json), SHA-256 `3ddf8f834c0ac254cbe4b506cc335a03557c7ba549c0a1e47a90398d089997f8`。
- C=16, `2026-09-19T04:04:00Z` / `2026-09-19T04:15:00Z`：[指标证据](../../out/bench/aca-concurrency/d2d5be88042c4b768a983034b4384af1-resource-metrics.json), SHA-256 `52df4225e97da842353386af3839fe724b734a0fd11b3f8df86b785d9d33b652`。
- C=8, `2026-09-19T04:01:00Z` / `2026-09-19T04:05:00Z`：[指标证据](../../out/bench/aca-concurrency/e8cc8d8da0af49a3a42056e16984a50a-resource-metrics.json), SHA-256 `d33e88bcee954af9f49c434d1512a357c9c7dab31b5d209f55ee1867587cdf05`。
- C=1, `2026-09-19T03:47:00Z` / `2026-09-19T03:55:00Z`：[指标证据](../../out/bench/aca-concurrency/f8de5c62587d4f1395c32f8bf38bda83-resource-metrics.json), SHA-256 `59cab1863b3459514632396630ef3cd25cc5b0c9379101bf91cc23126b852f49`。
