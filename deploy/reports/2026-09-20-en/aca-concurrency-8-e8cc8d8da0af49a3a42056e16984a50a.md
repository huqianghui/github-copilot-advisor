# Benchmark e8cc8d8da0af49a3a42056e16984a50a

[简体中文](../2026-09-20-cn/aca-concurrency-8-e8cc8d8da0af49a3a42056e16984a50a.md) · [Stage definitions and timing boundaries](stage-definitions.md)

**APPLICATION LINK** — not Teams-client latency; not an SLA or an absolute platform maximum.
Profile: `concurrency`; requested outstanding-question limit: 8.
Completion endpoint: complete bounded HTTP reply body received by the observer; accepted only after authentication and complete-message validation.
Observation: 2026-09-19T04:01:27.733001+00:00 to 2026-09-19T04:04:36.115495+00:00.
Actual window: **188.382 seconds**, including drain; submission window: 180.577 seconds.
Stop: `target_reached`. Sampling: **sufficient** (154/50 terminal observations; minimum 180.0 submission seconds).
Attempted: 154; censored: 0; after-window completions: 0.
Full answers (success + degraded)/actual window: **0.817486 answers/s**.
Successes/actual window: **0.520218 successes/s**; successes/attempted: 0.6363636363636364.

| Outcome | Count | Mean ms | P50 ms | P95 ms | P99 ms | Min ms | Max ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| success | 98 | 10242.556 | 8977.133 | 18010.883 | 21049.372 | 6619.093 | 21332.124 |
| degraded | 56 | 8212.618 | 8082.835 | 9903.926 | 10712.906 | 5964.064 | 11649.083 |
| failed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| unknown | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| expired | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| ignored | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| censored | 0 | n/a | n/a | n/a | n/a | n/a | n/a |

ACKs and typing events are not completions. Censored requests have no fabricated duration.
Failure, degradation, unknown delivery and unfinished work remain separate.
Cold classification: **unverified**.
Natural-zero requires supplied control-plane zero proof and first-workload/new-boot evidence for every required scale group. Absence of evidence is unverified.
The first_request marker means first workload on the observed boot, not a platform probe; probe exclusion alone is not cold-start evidence.
Small cold samples and tail quantiles are exploratory; no excluded adverse samples.
Notes: small_tail_sample.

## Observed per-stage latency

Scenario: `real_agent`; profile: `concurrency`; run: `e8cc8d8da0af49a3a42056e16984a50a`.
This section uses only supplied StageObservation values for this run's requests. Synthetic and real-Agent scenarios are not pooled or inferred from stage names.
Coverage is distinct requests with any observation of a stage / all attempted requests in that request-outcome cohort. Missing stage coverage is unknown, not zero or proof that the stage did not run. An observed zero duration is retained.
Latency count is stage observations, not requests: repeated spans are separate observations, not per-request sums. Stage outcomes are split within each request-outcome cohort.
Stage durations overlap and are not additive; cross-process clocks are not directly subtracted. End-to-end remains the observer's complete callback-body reception time, not a sum of stages.
Percentiles use the existing linear interpolation; small stage samples and tail quantiles are exploratory, not an SLA.

### Completed-stage log provenance

Source: [separate stage-log export](../../out/bench/aca-concurrency/e8cc8d8da0af49a3a42056e16984a50a-stages.json).
Source SHA-256 (original bytes): `8cff52371e86597b91c2551d01df8753a1a66f88efc89d02d8ee592d41ebfc1d`.
Export records: 616; unique event IDs: 616; exact duplicate records: 0.
Log window: 2026-09-19T03:59:27.733001+00:00 to 2026-09-19T04:06:36.115495+00:00 (operator-declared; verified against 616 supplied UTC log-emission timestamps).
Superseded callback observations: 308.
Log identity is exactly (boot_id, segment_id, span_id); differing fields for one identity reject the import. Different identities are distinct observed attempts.
Join is exact job_id AND trace_id from the original callback correspondence. Missing, unmatched or ambiguous correlation rejects the whole import.
Completed logs take precedence only for the matched request/stage. Callback stages have no event IDs: supersession is a source-selection policy, not claimed identity deduplication. Other callback stages are retained. Sources are not pooled in distributions.
Log precedence is restricted to ingress, history_read, history_commit and send; all other stage scopes remain callback-sourced.
A partial export can supersede a more complete callback stage list; export the intended complete log window and inspect counts. Request coverage is not span completeness.
Raw results and the separate import file are unchanged. Source SHA/window are provenance, not Azure attestation. Imported spans may finish after callback reception or the original run window; they never change end-to-end timing, outcomes or cold evidence.
Timestamp window validation does not attest Azure collection or complete attempt coverage.
Observed log-emission range: 2026-09-19T04:01:29.039702+00:00 to 2026-09-19T04:04:36.117022+00:00. Emission timestamps are collection metadata, not stage duration endpoints.

### Request outcome: `success`

Attempted requests in cohort: 98.

| Stage | Observations | Requests with stage / cohort | Missing/unknown requests | Coverage |
|---|---:|---:|---:|---:|
| ingress | 98 | 98/98 | 0 | 100.00% |
| queue_wait | 98 | 98/98 | 0 | 100.00% |
| worker_init | 2 | 2/98 | 96 | 2.04% |
| auth | 98 | 98/98 | 0 | 100.00% |
| connection | 2 | 2/98 | 96 | 2.04% |
| history_setup | 0 | 0/98 | 98 | 0.00% |
| history_read | 98 | 98/98 | 0 | 100.00% |
| history_commit | 98 | 98/98 | 0 | 100.00% |
| synthetic | 0 | 0/98 | 98 | 0.00% |
| agent | 98 | 98/98 | 0 | 100.00% |
| kb | 98 | 98/98 | 0 | 100.00% |
| github | 98 | 98/98 | 0 | 100.00% |
| web | 0 | 0/98 | 98 | 0.00% |
| model | 196 | 98/98 | 0 | 100.00% |
| render | 98 | 98/98 | 0 | 100.00% |
| send | 98 | 98/98 | 0 | 100.00% |

| Stage | Callback observations used | Log observations used | Callback requests | Log requests | Callback observations superseded |
|---|---:|---:|---:|---:|---:|
| ingress | 0 | 98 | 0 | 98 | 0 |
| queue_wait | 98 | 0 | 98 | 0 | 0 |
| worker_init | 2 | 0 | 2 | 0 | 0 |
| auth | 98 | 0 | 98 | 0 | 0 |
| connection | 2 | 0 | 2 | 0 | 0 |
| history_setup | 0 | 0 | 0 | 0 | 0 |
| history_read | 0 | 98 | 0 | 98 | 98 |
| history_commit | 0 | 98 | 0 | 98 | 98 |
| synthetic | 0 | 0 | 0 | 0 | 0 |
| agent | 98 | 0 | 98 | 0 | 0 |
| kb | 98 | 0 | 98 | 0 | 0 |
| github | 98 | 0 | 98 | 0 | 0 |
| web | 0 | 0 | 0 | 0 | 0 |
| model | 196 | 0 | 98 | 0 | 0 |
| render | 98 | 0 | 98 | 0 | 0 |
| send | 0 | 98 | 0 | 98 | 0 |

| Stage | Source | Stage outcome | Count | Mean ms | P50 ms | P95 ms | P99 ms | Min ms | Max ms |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| ingress | completed log | success | 98 | 1375.845 | 1180.936 | 2950.335 | 4496.556 | 452.063 | 4822.928 |
| queue_wait | callback | success | 98 | 2499.662 | 1568.662 | 8953.568 | 10911.816 | 443.849 | 11085.703 |
| worker_init | callback | success | 2 | 18.774 | 18.774 | 19.901 | 20.001 | 17.521 | 20.026 |
| auth | callback | success | 98 | 7.811 | 1.214 | 6.592 | 76.978 | 0.576 | 472.312 |
| connection | callback | success | 2 | 831.733 | 831.733 | 859.596 | 862.073 | 800.774 | 862.692 |
| history_setup | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| history_read | completed log | success | 98 | 0.873 | 0.184 | 0.313 | 2.670 | 0.143 | 66.093 |
| history_commit | completed log | success | 98 | 113.051 | 92.049 | 281.951 | 300.467 | 39.968 | 302.779 |
| synthetic | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| agent | callback | success | 98 | 6183.199 | 6152.985 | 7184.197 | 8015.415 | 5123.157 | 8361.196 |
| kb | callback | success | 98 | 334.790 | 304.875 | 509.167 | 609.253 | 234.158 | 1033.860 |
| github | callback | empty | 98 | 500.849 | 450.985 | 828.547 | 923.320 | 406.183 | 1737.944 |
| web | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| model | callback | success | 196 | 2829.211 | 3079.280 | 4331.008 | 4595.695 | 1466.287 | 5989.950 |
| render | callback | success | 98 | 0.190 | 0.168 | 0.382 | 0.548 | 0.081 | 0.626 |
| send | completed log | success | 98 | 49.391 | 34.861 | 95.570 | 185.607 | 25.916 | 452.811 |

### Request outcome: `degraded`

Attempted requests in cohort: 56.

| Stage | Observations | Requests with stage / cohort | Missing/unknown requests | Coverage |
|---|---:|---:|---:|---:|
| ingress | 56 | 56/56 | 0 | 100.00% |
| queue_wait | 56 | 56/56 | 0 | 100.00% |
| worker_init | 0 | 0/56 | 56 | 0.00% |
| auth | 56 | 56/56 | 0 | 100.00% |
| connection | 0 | 0/56 | 56 | 0.00% |
| history_setup | 0 | 0/56 | 56 | 0.00% |
| history_read | 56 | 56/56 | 0 | 100.00% |
| history_commit | 56 | 56/56 | 0 | 100.00% |
| synthetic | 0 | 0/56 | 56 | 0.00% |
| agent | 56 | 56/56 | 0 | 100.00% |
| kb | 56 | 56/56 | 0 | 100.00% |
| github | 56 | 56/56 | 0 | 100.00% |
| web | 0 | 0/56 | 56 | 0.00% |
| model | 112 | 56/56 | 0 | 100.00% |
| render | 56 | 56/56 | 0 | 100.00% |
| send | 56 | 56/56 | 0 | 100.00% |

| Stage | Callback observations used | Log observations used | Callback requests | Log requests | Callback observations superseded |
|---|---:|---:|---:|---:|---:|
| ingress | 0 | 56 | 0 | 56 | 0 |
| queue_wait | 56 | 0 | 56 | 0 | 0 |
| worker_init | 0 | 0 | 0 | 0 | 0 |
| auth | 56 | 0 | 56 | 0 | 0 |
| connection | 0 | 0 | 0 | 0 | 0 |
| history_setup | 0 | 0 | 0 | 0 | 0 |
| history_read | 0 | 56 | 0 | 56 | 56 |
| history_commit | 0 | 56 | 0 | 56 | 56 |
| synthetic | 0 | 0 | 0 | 0 | 0 |
| agent | 56 | 0 | 56 | 0 | 0 |
| kb | 56 | 0 | 56 | 0 | 0 |
| github | 56 | 0 | 56 | 0 | 0 |
| web | 0 | 0 | 0 | 0 | 0 |
| model | 112 | 0 | 56 | 0 | 0 |
| render | 56 | 0 | 56 | 0 | 0 |
| send | 0 | 56 | 0 | 56 | 0 |

| Stage | Source | Stage outcome | Count | Mean ms | P50 ms | P95 ms | P99 ms | Min ms | Max ms |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| ingress | completed log | success | 56 | 921.762 | 817.661 | 1599.487 | 1857.419 | 430.201 | 2028.585 |
| queue_wait | callback | success | 56 | 1171.210 | 1081.543 | 2219.646 | 2353.924 | 437.425 | 2460.562 |
| worker_init | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| auth | callback | success | 56 | 12.944 | 0.875 | 43.727 | 250.850 | 0.598 | 496.387 |
| connection | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| history_setup | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| history_read | completed log | success | 56 | 0.841 | 0.188 | 0.324 | 16.575 | 0.110 | 36.185 |
| history_commit | completed log | success | 56 | 102.551 | 81.135 | 221.233 | 325.319 | 41.217 | 393.537 |
| synthetic | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| agent | callback | success | 56 | 5899.003 | 5802.057 | 6818.075 | 8307.061 | 5028.589 | 8780.645 |
| kb | callback | success | 56 | 351.586 | 313.245 | 516.677 | 684.028 | 251.643 | 773.298 |
| github | callback | failed | 56 | 238.567 | 217.901 | 314.671 | 395.998 | 202.622 | 398.926 |
| web | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| model | callback | success | 112 | 2766.094 | 2926.170 | 4229.028 | 5343.603 | 1562.278 | 5479.215 |
| render | callback | success | 56 | 0.180 | 0.163 | 0.305 | 0.509 | 0.058 | 0.601 |
| send | completed log | success | 56 | 50.940 | 35.296 | 96.412 | 177.333 | 26.557 | 197.862 |

## Configuration and limitations

Dataset SHA-256: `f6c548f30ebd02a7ad2ad897a4eea048a63f4b0103d27b728a82851e04d314ad`; seed: 20260919; history mode: `empty`.
Per-request safe telemetry and supplied cold evidence are in the raw result. Missing host counts, resource utilization, stage timings or dependency calls are unknown, not zero. Target-generated cross-process timings are not subtracted from observer clocks.
Driver utilization is measured separately; a driver stop is not a target capacity limit.
Observer authentication/validation and evidence I/O are recorded separately from body-reception latency. Durable write-ahead intents precede the per-request clock; journal backpressure can limit throughput and must not be attributed to target capacity.
Compare only matching workload/history hashes, model versions, budgets and host settings. Model/Entra/configuration/capacity and cloud functional checks remain deployment gates.
No cloud cost or capacity conclusion is produced by local synthetic tests.

## Raw result provenance

Source: [original raw result](../../out/bench/aca-concurrency/e8cc8d8da0af49a3a42056e16984a50a.json).
Raw-result SHA-256 (original bytes): `9ab20f1c63e9a970be2b62d179669014ca909257b62b7a45d8b45c085258ad31`.

## Measured cohort and resource evidence

Target artifact: `31882ec48e652906874e3d5687bf0b675dfbb841d66531d84ee23a204cd7a4d0`; commit: `49100580008a769aafc87c4f12f497e048a7fe6f`.
This is cohort B, not pooled with the earlier warm cohort A.
Observed model HTTP attempts: 462; observed HTTP 429s: 0; counter coverage: 154/154 requests. No counts inferred from model rounds.
| Azure metric | Reported minute averages | Mean of those averages | Maximum reported |
|---|---:|---:|---:|
| CpuPercentage | unknown | unknown | unknown |
| MemoryPercentage | unknown | unknown | unknown |
| Replicas | unknown | unknown | unknown |
| RestartCount | unknown | unknown | unknown |
Metrics are the actual target resource's PT1M samples over the run window; means above are means of reported minute averages, not invented per-request utilization. Missing data is unknown. RestartCount is the reported metric, not a sum invented from samples.
Metric source SHA-256: `unavailable`.
Collection limitations: metric_collection:command_failed.
Finite sampled load and dependency quotas do not establish an absolute platform maximum.
