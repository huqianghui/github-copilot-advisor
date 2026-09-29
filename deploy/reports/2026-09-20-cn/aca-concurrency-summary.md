# ACA 并发样本组 B——实测结果

[English](../2026-09-20-en/aca-concurrency-summary.md) · [阶段定义与计时边界](stage-definitions.md)

采集结束时间：2026-09-19T04:16:50.577985+00:00；处置状态：`runner_stopped:request_deadline`。制品 `31882ec48e652906874e3d5687bf0b675dfbb841d66531d84ee23a204cd7a4d0`；提交 `49100580008a769aafc87c4f12f497e048a7fe6f`。使用真实 Agent、用例 kb-01、空／新会话历史记录，按计划依次执行并发档位 1/2/4/8/16/32/64/128。本样本组不与预热样本组 A 合并统计。观测器修订版本：ca-bench-cskd5yazfko42--0000006。采集通过观测器 MI 和 CLI 授权的 Log Analytics／指标查询只读进行；未执行 START、重启、更新、停止或部署。

| C | 已尝试 | 成功（Success） | 降级 | 失败 | 未知 | 删失 | 均值 ms | P50 ms | P95 ms | P99 ms | 回答/秒 | 停止原因 | 采样充足 | 模型 HTTP / 429（覆盖率） |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|
| 1 | 50 | 49 | 1 | 0 | 0 | 0 | 8276.615 | 8223.881 | 9796.260 | 10169.091 | 0.120 | 已达到目标（target_reached） | 是（True） | 150 / 0 (50/50) |
| 2 | 51 | 50 | 1 | 0 | 0 | 0 | 8141.066 | 8120.724 | 9109.614 | 9787.614 | 0.242 | 已达到目标（target_reached） | 是（True） | 153 / 0 (51/51) |
| 4 | 76 | 76 | 0 | 0 | 0 | 0 | 9654.781 | 9770.836 | 11154.536 | 11289.485 | 0.410 | 已达到目标（target_reached） | 是（True） | 228 / 0 (76/76) |
| 8 | 154 | 98 | 56 | 0 | 0 | 0 | 10242.556 | 8977.133 | 18010.883 | 21049.372 | 0.817 | 已达到目标（target_reached） | 是（True） | 462 / 0 (154/154) |
| 16 | 320 | 95 | 224 | 0 | 0 | 1 | 9495.446 | 8968.147 | 14626.552 | 16814.165 | 0.517 | 请求截止时间到达（request_deadline） | 是（True） | 977 / 161 (319/320) |

## 解读与局限

延迟分位数来自成功问题的回调正文观测。降级、失败、未知和删失的工作在各档位报告中仍分别记录。采样充分性采用运行器的终态观测／最小时长规则，不代表满足 SLA 或通过容量验证。模型 HTTP/429 总数仅来自实际观测到的依赖计数器，不是推断的模型轮次；缺失的回调计数器仍为未知。各档位均关联资源指标和安全的已完成阶段日志。不声称任何平台绝对上限。如果最终停止原因不是 target_reached，就停止观测后续计划档位；此采集器不会发出云端停止操作。到达时间边界时，只根据持久化检查点计数报告未完成工作，不会编造最终结果。最新持久化进度：`{"collected_at":"2026-09-19T04:16:20.590360+00:00","new_finals":[{"run_id":"d2d5be88042c4b768a983034b4384af1","level":16,"attempted":320,"stop_reason":"request_deadline","sha256":"2f5218de2cd08c8e64e11185298fd7161fb69c0d2b33b84d06ca31d5e68be513"}],"prefix_count":9,"progress":[]}`。安全采集错误：`[]`。

## 证据与 SHA-256

各档位报告哈希标识原始导出报告的字节，而非添加了导航、可移植链接或中文翻译的归档副本。原始结果与阶段日志的哈希仍标识未改变的证据。

- C=1: [aca-concurrency-1-f8de5c62587d4f1395c32f8bf38bda83.md](aca-concurrency-1-f8de5c62587d4f1395c32f8bf38bda83.md)
  - 原始结果 `f8de5c62587d4f1395c32f8bf38bda83.json`：`f075fc94d4b310d401b5814aad8b441739be75edb811aa097028b791142c1e28`
  - 安全阶段日志：`b72a115ee60b2c30aa6e96eabcbbf9f129b48a28e4b13300c1ae0db2a614b54e`
  - 资源指标：`unavailable`
  - 各档位报告：`4ee333720eceef1a9e0e79c549ad3286ce7778d6c122977e8704e216d86cbef7`
- C=2: [aca-concurrency-2-39117b0d856d44598a1ec26042a6a94c.md](aca-concurrency-2-39117b0d856d44598a1ec26042a6a94c.md)
  - 原始结果 `39117b0d856d44598a1ec26042a6a94c.json`：`cb2bd1f11b1403c9c092310cb49b2ab5e2eba62df9d92a15b431abf7eb5b81c2`
  - 安全阶段日志：`de59110bdb922118df41ad8fafea45b0d773dafc2444eb5899bb0872bc24f2ba`
  - 资源指标：`unavailable`
  - 各档位报告：`d33aab8ffb4598ece1f711156e1e0cda5cc042156274b32895c8f849dc5aa697`
- C=4: [aca-concurrency-4-af7aa66860534abf9db46cc10d66e6fc.md](aca-concurrency-4-af7aa66860534abf9db46cc10d66e6fc.md)
  - 原始结果 `af7aa66860534abf9db46cc10d66e6fc.json`：`851759fcf57df83038ee89219112b1e3170254f58b0fae2429cb78d2cb5dff93`
  - 安全阶段日志：`8aed61d3f9c69aa1bd46fa997fa853a44c8c656407047399816ddb38785a96d7`
  - 资源指标：`unavailable`
  - 各档位报告：`b35f3c50c86a42bf44b628f8dd952cad79f853dae36ecd846a652fd53920e8f2`
- C=8: [aca-concurrency-8-e8cc8d8da0af49a3a42056e16984a50a.md](aca-concurrency-8-e8cc8d8da0af49a3a42056e16984a50a.md)
  - 原始结果 `e8cc8d8da0af49a3a42056e16984a50a.json`：`9ab20f1c63e9a970be2b62d179669014ca909257b62b7a45d8b45c085258ad31`
  - 安全阶段日志：`8cff52371e86597b91c2551d01df8753a1a66f88efc89d02d8ee592d41ebfc1d`
  - 资源指标：`unavailable`
  - 各档位报告：`a9bfdabe88fafbe2181af82a101fccdede252b06b405f60667f02f4e07f61338`
- C=16: [aca-concurrency-16-d2d5be88042c4b768a983034b4384af1.md](aca-concurrency-16-d2d5be88042c4b768a983034b4384af1.md)
  - 原始结果 `d2d5be88042c4b768a983034b4384af1.json`：`2f5218de2cd08c8e64e11185298fd7161fb69c0d2b33b84d06ca31d5e68be513`
  - 安全阶段日志：`b4bd8b552883ffa209b93a179add58ed9e5f48e86a5b517cdff656c94073b716`
  - 资源指标：`unavailable`
  - 各档位报告：`40e0fc5a4880d0da1a516663a0c6f6b63b777b67b140c36cffead30cb9892f4c`
