# Benchmark af7aa66860534abf9db46cc10d66e6fc

[简体中文](../2026-09-20-cn/aca-concurrency-4-af7aa66860534abf9db46cc10d66e6fc.md) · [Stage definitions and timing boundaries](stage-definitions.md)

**APPLICATION LINK** — not Teams-client latency; not an SLA or an absolute platform maximum.
Profile: `concurrency`; requested outstanding-question limit: 4.
Completion endpoint: complete bounded HTTP reply body received by the observer; accepted only after authentication and complete-message validation.
Observation: 2026-09-19T03:58:22.181254+00:00 to 2026-09-19T04:01:27.613785+00:00.
Actual window: **185.432 seconds**, including drain; submission window: 183.980 seconds.
Stop: `target_reached`. Sampling: **sufficient** (76/50 terminal observations; minimum 180.0 submission seconds).
Attempted: 76; censored: 0; after-window completions: 0.
Full answers (success + degraded)/actual window: **0.409853 answers/s**.
Successes/actual window: **0.409853 successes/s**; successes/attempted: 1.0.

| Outcome | Count | Mean ms | P50 ms | P95 ms | P99 ms | Min ms | Max ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| success | 76 | 9654.781 | 9770.836 | 11154.536 | 11289.485 | 7844.123 | 11522.235 |
| degraded | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
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

Scenario: `real_agent`; profile: `concurrency`; run: `af7aa66860534abf9db46cc10d66e6fc`.
This section uses only supplied StageObservation values for this run's requests. Synthetic and real-Agent scenarios are not pooled or inferred from stage names.
Coverage is distinct requests with any observation of a stage / all attempted requests in that request-outcome cohort. Missing stage coverage is unknown, not zero or proof that the stage did not run. An observed zero duration is retained.
Latency count is stage observations, not requests: repeated spans are separate observations, not per-request sums. Stage outcomes are split within each request-outcome cohort.
Stage durations overlap and are not additive; cross-process clocks are not directly subtracted. End-to-end remains the observer's complete callback-body reception time, not a sum of stages.
Percentiles use the existing linear interpolation; small stage samples and tail quantiles are exploratory, not an SLA.

### Completed-stage log provenance

Source: [separate stage-log export](../../out/bench/aca-concurrency/af7aa66860534abf9db46cc10d66e6fc-stages.json).
Source SHA-256 (original bytes): `8aed61d3f9c69aa1bd46fa997fa853a44c8c656407047399816ddb38785a96d7`.
Export records: 304; unique event IDs: 304; exact duplicate records: 0.
Log window: 2026-09-19T03:56:22.181254+00:00 to 2026-09-19T04:03:27.613785+00:00 (operator-declared; verified against 304 supplied UTC log-emission timestamps).
Superseded callback observations: 152.
Log identity is exactly (boot_id, segment_id, span_id); differing fields for one identity reject the import. Different identities are distinct observed attempts.
Join is exact job_id AND trace_id from the original callback correspondence. Missing, unmatched or ambiguous correlation rejects the whole import.
Completed logs take precedence only for the matched request/stage. Callback stages have no event IDs: supersession is a source-selection policy, not claimed identity deduplication. Other callback stages are retained. Sources are not pooled in distributions.
Log precedence is restricted to ingress, history_read, history_commit and send; all other stage scopes remain callback-sourced.
A partial export can supersede a more complete callback stage list; export the intended complete log window and inspect counts. Request coverage is not span completeness.
Raw results and the separate import file are unchanged. Source SHA/window are provenance, not Azure attestation. Imported spans may finish after callback reception or the original run window; they never change end-to-end timing, outcomes or cold evidence.
Timestamp window validation does not attest Azure collection or complete attempt coverage.
Observed log-emission range: 2026-09-19T03:58:23.122833+00:00 to 2026-09-19T04:01:27.615349+00:00. Emission timestamps are collection metadata, not stage duration endpoints.

### Request outcome: `success`

Attempted requests in cohort: 76.

| Stage | Observations | Requests with stage / cohort | Missing/unknown requests | Coverage |
|---|---:|---:|---:|---:|
| ingress | 76 | 76/76 | 0 | 100.00% |
| queue_wait | 76 | 76/76 | 0 | 100.00% |
| worker_init | 0 | 0/76 | 76 | 0.00% |
| auth | 76 | 76/76 | 0 | 100.00% |
| connection | 0 | 0/76 | 76 | 0.00% |
| history_setup | 0 | 0/76 | 76 | 0.00% |
| history_read | 76 | 76/76 | 0 | 100.00% |
| history_commit | 76 | 76/76 | 0 | 100.00% |
| synthetic | 0 | 0/76 | 76 | 0.00% |
| agent | 76 | 76/76 | 0 | 100.00% |
| kb | 76 | 76/76 | 0 | 100.00% |
| github | 76 | 76/76 | 0 | 100.00% |
| web | 0 | 0/76 | 76 | 0.00% |
| model | 152 | 76/76 | 0 | 100.00% |
| render | 76 | 76/76 | 0 | 100.00% |
| send | 76 | 76/76 | 0 | 100.00% |

| Stage | Callback observations used | Log observations used | Callback requests | Log requests | Callback observations superseded |
|---|---:|---:|---:|---:|---:|
| ingress | 0 | 76 | 0 | 76 | 0 |
| queue_wait | 76 | 0 | 76 | 0 | 0 |
| worker_init | 0 | 0 | 0 | 0 | 0 |
| auth | 76 | 0 | 76 | 0 | 0 |
| connection | 0 | 0 | 0 | 0 | 0 |
| history_setup | 0 | 0 | 0 | 0 | 0 |
| history_read | 0 | 76 | 0 | 76 | 76 |
| history_commit | 0 | 76 | 0 | 76 | 76 |
| synthetic | 0 | 0 | 0 | 0 | 0 |
| agent | 76 | 0 | 76 | 0 | 0 |
| kb | 76 | 0 | 76 | 0 | 0 |
| github | 76 | 0 | 76 | 0 | 0 |
| web | 0 | 0 | 0 | 0 | 0 |
| model | 152 | 0 | 76 | 0 | 0 |
| render | 76 | 0 | 76 | 0 | 0 |
| send | 0 | 76 | 0 | 76 | 0 |

| Stage | Source | Stage outcome | Count | Mean ms | P50 ms | P95 ms | P99 ms | Min ms | Max ms |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| ingress | completed log | success | 76 | 1297.989 | 1340.822 | 1770.673 | 1858.655 | 448.106 | 2004.165 |
| queue_wait | callback | success | 76 | 1875.346 | 1824.452 | 2652.224 | 2887.622 | 997.105 | 2993.732 |
| worker_init | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| auth | callback | success | 76 | 3.129 | 2.557 | 5.057 | 19.709 | 0.517 | 48.878 |
| connection | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| history_setup | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| history_read | completed log | success | 76 | 0.372 | 0.175 | 0.229 | 3.965 | 0.101 | 15.097 |
| history_commit | completed log | success | 76 | 154.723 | 151.440 | 291.137 | 302.646 | 40.834 | 311.240 |
| synthetic | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| agent | callback | success | 76 | 6330.877 | 6328.762 | 7009.223 | 7215.795 | 5396.756 | 7436.712 |
| kb | callback | success | 76 | 495.533 | 469.373 | 792.833 | 888.752 | 235.204 | 993.719 |
| github | callback | empty | 76 | 537.009 | 508.567 | 773.892 | 826.847 | 412.170 | 829.633 |
| web | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| model | callback | success | 152 | 2861.495 | 2758.421 | 4327.875 | 4621.063 | 1501.815 | 4767.036 |
| render | callback | success | 76 | 0.184 | 0.171 | 0.253 | 0.445 | 0.089 | 0.636 |
| send | completed log | success | 76 | 46.604 | 42.840 | 85.878 | 97.645 | 25.136 | 102.509 |

## Configuration and limitations

Dataset SHA-256: `f6c548f30ebd02a7ad2ad897a4eea048a63f4b0103d27b728a82851e04d314ad`; seed: 20260919; history mode: `empty`.
Per-request safe telemetry and supplied cold evidence are in the raw result. Missing host counts, resource utilization, stage timings or dependency calls are unknown, not zero. Target-generated cross-process timings are not subtracted from observer clocks.
Driver utilization is measured separately; a driver stop is not a target capacity limit.
Observer authentication/validation and evidence I/O are recorded separately from body-reception latency. Durable write-ahead intents precede the per-request clock; journal backpressure can limit throughput and must not be attributed to target capacity.
Compare only matching workload/history hashes, model versions, budgets and host settings. Model/Entra/configuration/capacity and cloud functional checks remain deployment gates.
No cloud cost or capacity conclusion is produced by local synthetic tests.

## Raw result provenance

Source: [original raw result](../../out/bench/aca-concurrency/af7aa66860534abf9db46cc10d66e6fc.json).
Raw-result SHA-256 (original bytes): `851759fcf57df83038ee89219112b1e3170254f58b0fae2429cb78d2cb5dff93`.

## Measured cohort and resource evidence

Target artifact: `31882ec48e652906874e3d5687bf0b675dfbb841d66531d84ee23a204cd7a4d0`; commit: `49100580008a769aafc87c4f12f497e048a7fe6f`.
This is cohort B, not pooled with the earlier warm cohort A.
Observed model HTTP attempts: 228; observed HTTP 429s: 0; counter coverage: 76/76 requests. No counts inferred from model rounds.
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
