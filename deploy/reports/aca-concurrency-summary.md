# ACA concurrency cohort B — measured results

Collection ended: 2026-09-19T04:16:50.577985+00:00; disposition: `runner_stopped:request_deadline`.
Artifact `31882ec48e652906874e3d5687bf0b675dfbb841d66531d84ee23a204cd7a4d0`; commit `49100580008a769aafc87c4f12f497e048a7fe6f`.
Real Agent, case kb-01, empty/new-conversation history, sequential planned levels 1/2/4/8/16/32/64/128. This cohort is not pooled with warm cohort A.
Observer revision: ca-bench-cskd5yazfko42--0000006. Collection was read-only through observer MI and CLI-authorized Log Analytics/metrics queries; no START/restart/update/stop/deploy.

| C | Attempted | Success | Degraded | Failed | Unknown | Censored | Mean ms | P50 ms | P95 ms | P99 ms | Answers/s | Stop | Sufficient | Model HTTP / 429 (coverage) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|
| 1 | 50 | 49 | 1 | 0 | 0 | 0 | 8276.615 | 8223.881 | 9796.260 | 10169.091 | 0.120 | target_reached | True | 150 / 0 (50/50) |
| 2 | 51 | 50 | 1 | 0 | 0 | 0 | 8141.066 | 8120.724 | 9109.614 | 9787.614 | 0.242 | target_reached | True | 153 / 0 (51/51) |
| 4 | 76 | 76 | 0 | 0 | 0 | 0 | 9654.781 | 9770.836 | 11154.536 | 11289.485 | 0.410 | target_reached | True | 228 / 0 (76/76) |
| 8 | 154 | 98 | 56 | 0 | 0 | 0 | 10242.556 | 8977.133 | 18010.883 | 21049.372 | 0.817 | target_reached | True | 462 / 0 (154/154) |
| 16 | 320 | 95 | 224 | 0 | 0 | 1 | 9495.446 | 8968.147 | 14626.552 | 16814.165 | 0.517 | request_deadline | True | 977 / 161 (319/320) |

## Interpretation and limits

Latency percentiles are successful-question callback-body observations. Degraded, failed, unknown and censored work remain separate in the per-level reports. Sampling sufficiency uses the runner's terminal-observation/minimum-duration rule and is not an SLA or capacity pass.
Model HTTP/429 totals are only actually observed dependency counters, not inferred model rounds; missing callback counters remain unknown. Resource metrics and safe completed-stage logs are linked per level. No absolute platform maximum is asserted.
A non-target_reached final stops observation of later planned levels; no cloud stop action is issued by this collector. At the time bound, unfinished work is reported from durable checkpoint counts only, without manufacturing a final result.
Latest durable progress: `{"collected_at":"2026-09-19T04:16:20.590360+00:00","new_finals":[{"run_id":"d2d5be88042c4b768a983034b4384af1","level":16,"attempted":320,"stop_reason":"request_deadline","sha256":"2f5218de2cd08c8e64e11185298fd7161fb69c0d2b33b84d06ca31d5e68be513"}],"prefix_count":9,"progress":[]}`.
Safe collection errors: `[]`.

## Evidence and SHA-256

- C=1: [aca-concurrency-1-f8de5c62587d4f1395c32f8bf38bda83.md](aca-concurrency-1-f8de5c62587d4f1395c32f8bf38bda83.md)
  - Raw `f8de5c62587d4f1395c32f8bf38bda83.json`: `f075fc94d4b310d401b5814aad8b441739be75edb811aa097028b791142c1e28`
  - Safe stage logs: `b72a115ee60b2c30aa6e96eabcbbf9f129b48a28e4b13300c1ae0db2a614b54e`
  - Resource metrics: `unavailable`
  - Per-level report: `4ee333720eceef1a9e0e79c549ad3286ce7778d6c122977e8704e216d86cbef7`
- C=2: [aca-concurrency-2-39117b0d856d44598a1ec26042a6a94c.md](aca-concurrency-2-39117b0d856d44598a1ec26042a6a94c.md)
  - Raw `39117b0d856d44598a1ec26042a6a94c.json`: `cb2bd1f11b1403c9c092310cb49b2ab5e2eba62df9d92a15b431abf7eb5b81c2`
  - Safe stage logs: `de59110bdb922118df41ad8fafea45b0d773dafc2444eb5899bb0872bc24f2ba`
  - Resource metrics: `unavailable`
  - Per-level report: `d33aab8ffb4598ece1f711156e1e0cda5cc042156274b32895c8f849dc5aa697`
- C=4: [aca-concurrency-4-af7aa66860534abf9db46cc10d66e6fc.md](aca-concurrency-4-af7aa66860534abf9db46cc10d66e6fc.md)
  - Raw `af7aa66860534abf9db46cc10d66e6fc.json`: `851759fcf57df83038ee89219112b1e3170254f58b0fae2429cb78d2cb5dff93`
  - Safe stage logs: `8aed61d3f9c69aa1bd46fa997fa853a44c8c656407047399816ddb38785a96d7`
  - Resource metrics: `unavailable`
  - Per-level report: `b35f3c50c86a42bf44b628f8dd952cad79f853dae36ecd846a652fd53920e8f2`
- C=8: [aca-concurrency-8-e8cc8d8da0af49a3a42056e16984a50a.md](aca-concurrency-8-e8cc8d8da0af49a3a42056e16984a50a.md)
  - Raw `e8cc8d8da0af49a3a42056e16984a50a.json`: `9ab20f1c63e9a970be2b62d179669014ca909257b62b7a45d8b45c085258ad31`
  - Safe stage logs: `8cff52371e86597b91c2551d01df8753a1a66f88efc89d02d8ee592d41ebfc1d`
  - Resource metrics: `unavailable`
  - Per-level report: `a9bfdabe88fafbe2181af82a101fccdede252b06b405f60667f02f4e07f61338`
- C=16: [aca-concurrency-16-d2d5be88042c4b768a983034b4384af1.md](aca-concurrency-16-d2d5be88042c4b768a983034b4384af1.md)
  - Raw `d2d5be88042c4b768a983034b4384af1.json`: `2f5218de2cd08c8e64e11185298fd7161fb69c0d2b33b84d06ca31d5e68be513`
  - Safe stage logs: `b4bd8b552883ffa209b93a179add58ed9e5f48e86a5b517cdff656c94073b716`
  - Resource metrics: `unavailable`
  - Per-level report: `40e0fc5a4880d0da1a516663a0c6f6b63b777b67b140c36cffead30cb9892f4c`
