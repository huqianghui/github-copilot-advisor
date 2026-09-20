# Benchmark f8de5c62587d4f1395c32f8bf38bda83

**APPLICATION LINK** — not Teams-client latency; not an SLA or an absolute platform maximum.
Profile: `concurrency`; requested outstanding-question limit: 1.
Completion endpoint: complete bounded HTTP reply body received by the observer; accepted only after authentication and complete-message validation.
Observation: 2026-09-19T03:47:54.837686+00:00 to 2026-09-19T03:54:50.939709+00:00.
Actual window: **416.102 seconds**, including drain; submission window: 416.102 seconds.
Stop: `target_reached`. Sampling: **sufficient** (50/50 terminal observations; minimum 180.0 submission seconds).
Attempted: 50; censored: 0; after-window completions: 0.
Full answers (success + degraded)/actual window: **0.120163 answers/s**.
Successes/actual window: **0.11776 successes/s**; successes/attempted: 0.98.

| Outcome | Count | Mean ms | P50 ms | P95 ms | P99 ms | Min ms | Max ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| success | 49 | 8276.615 | 8223.881 | 9796.260 | 10169.091 | 6690.046 | 10349.865 |
| degraded | 1 | 8120.480 | 8120.480 | 8120.480 | 8120.480 | 8120.480 | 8120.480 |
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

Scenario: `real_agent`; profile: `concurrency`; run: `f8de5c62587d4f1395c32f8bf38bda83`.
This section uses only supplied StageObservation values for this run's requests. Synthetic and real-Agent scenarios are not pooled or inferred from stage names.
Coverage is distinct requests with any observation of a stage / all attempted requests in that request-outcome cohort. Missing stage coverage is unknown, not zero or proof that the stage did not run. An observed zero duration is retained.
Latency count is stage observations, not requests: repeated spans are separate observations, not per-request sums. Stage outcomes are split within each request-outcome cohort.
Stage durations overlap and are not additive; cross-process clocks are not directly subtracted. End-to-end remains the observer's complete callback-body reception time, not a sum of stages.
Percentiles use the existing linear interpolation; small stage samples and tail quantiles are exploratory, not an SLA.

### Completed-stage log provenance

Source: [separate stage-log export](file:///C:/Codes/github-copilot-advisor/deploy/out/bench/aca-concurrency/f8de5c62587d4f1395c32f8bf38bda83-stages.json).
Source SHA-256 (original bytes): `b72a115ee60b2c30aa6e96eabcbbf9f129b48a28e4b13300c1ae0db2a614b54e`.
Export records: 200; unique event IDs: 200; exact duplicate records: 0.
Log window: 2026-09-19T03:45:54.837686+00:00 to 2026-09-19T03:56:50.939709+00:00 (operator-declared; verified against 200 supplied UTC log-emission timestamps).
Superseded callback observations: 100.
Log identity is exactly (boot_id, segment_id, span_id); differing fields for one identity reject the import. Different identities are distinct observed attempts.
Join is exact job_id AND trace_id from the original callback correspondence. Missing, unmatched or ambiguous correlation rejects the whole import.
Completed logs take precedence only for the matched request/stage. Callback stages have no event IDs: supersession is a source-selection policy, not claimed identity deduplication. Other callback stages are retained. Sources are not pooled in distributions.
Log precedence is restricted to ingress, history_read, history_commit and send; all other stage scopes remain callback-sourced.
A partial export can supersede a more complete callback stage list; export the intended complete log window and inspect counts. Request coverage is not span completeness.
Raw results and the separate import file are unchanged. Source SHA/window are provenance, not Azure attestation. Imported spans may finish after callback reception or the original run window; they never change end-to-end timing, outcomes or cold evidence.
Timestamp window validation does not attest Azure collection or complete attempt coverage.
Observed log-emission range: 2026-09-19T03:47:55.188993+00:00 to 2026-09-19T03:54:50.941439+00:00. Emission timestamps are collection metadata, not stage duration endpoints.

### Request outcome: `success`

Attempted requests in cohort: 49.

| Stage | Observations | Requests with stage / cohort | Missing/unknown requests | Coverage |
|---|---:|---:|---:|---:|
| ingress | 49 | 49/49 | 0 | 100.00% |
| queue_wait | 49 | 49/49 | 0 | 100.00% |
| worker_init | 0 | 0/49 | 49 | 0.00% |
| auth | 49 | 49/49 | 0 | 100.00% |
| connection | 0 | 0/49 | 49 | 0.00% |
| history_setup | 0 | 0/49 | 49 | 0.00% |
| history_read | 49 | 49/49 | 0 | 100.00% |
| history_commit | 49 | 49/49 | 0 | 100.00% |
| synthetic | 0 | 0/49 | 49 | 0.00% |
| agent | 49 | 49/49 | 0 | 100.00% |
| kb | 49 | 49/49 | 0 | 100.00% |
| github | 49 | 49/49 | 0 | 100.00% |
| web | 0 | 0/49 | 49 | 0.00% |
| model | 98 | 49/49 | 0 | 100.00% |
| render | 49 | 49/49 | 0 | 100.00% |
| send | 49 | 49/49 | 0 | 100.00% |

| Stage | Callback observations used | Log observations used | Callback requests | Log requests | Callback observations superseded |
|---|---:|---:|---:|---:|---:|
| ingress | 0 | 49 | 0 | 49 | 0 |
| queue_wait | 49 | 0 | 49 | 0 | 0 |
| worker_init | 0 | 0 | 0 | 0 | 0 |
| auth | 49 | 0 | 49 | 0 | 0 |
| connection | 0 | 0 | 0 | 0 | 0 |
| history_setup | 0 | 0 | 0 | 0 | 0 |
| history_read | 0 | 49 | 0 | 49 | 49 |
| history_commit | 0 | 49 | 0 | 49 | 49 |
| synthetic | 0 | 0 | 0 | 0 | 0 |
| agent | 49 | 0 | 49 | 0 | 0 |
| kb | 49 | 0 | 49 | 0 | 0 |
| github | 49 | 0 | 49 | 0 | 0 |
| web | 0 | 0 | 0 | 0 | 0 |
| model | 98 | 0 | 49 | 0 | 0 |
| render | 49 | 0 | 49 | 0 | 0 |
| send | 0 | 49 | 0 | 49 | 0 |

| Stage | Source | Stage outcome | Count | Mean ms | P50 ms | P95 ms | P99 ms | Min ms | Max ms |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| ingress | completed log | success | 49 | 450.646 | 432.880 | 629.187 | 795.166 | 281.581 | 802.323 |
| queue_wait | callback | success | 49 | 1456.643 | 1407.383 | 2319.461 | 2519.715 | 451.607 | 2628.931 |
| worker_init | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| auth | callback | success | 49 | 1.152 | 0.781 | 1.916 | 6.152 | 0.584 | 9.888 |
| connection | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| history_setup | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| history_read | completed log | success | 49 | 0.190 | 0.170 | 0.280 | 0.530 | 0.115 | 0.605 |
| history_commit | completed log | success | 49 | 52.002 | 45.007 | 95.043 | 101.818 | 37.610 | 102.317 |
| synthetic | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| agent | callback | success | 49 | 6416.159 | 6341.253 | 7437.732 | 7599.943 | 5456.219 | 7655.879 |
| kb | callback | success | 49 | 352.665 | 333.405 | 461.088 | 577.144 | 296.806 | 644.940 |
| github | callback | empty | 49 | 492.888 | 453.754 | 826.978 | 891.848 | 414.152 | 940.145 |
| web | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| model | callback | success | 98 | 2953.478 | 2904.477 | 4545.790 | 4778.465 | 1684.262 | 4956.321 |
| render | callback | success | 49 | 0.165 | 0.165 | 0.224 | 0.263 | 0.082 | 0.296 |
| send | completed log | success | 49 | 31.193 | 29.695 | 46.054 | 49.188 | 24.864 | 49.390 |

### Request outcome: `degraded`

Attempted requests in cohort: 1.

| Stage | Observations | Requests with stage / cohort | Missing/unknown requests | Coverage |
|---|---:|---:|---:|---:|
| ingress | 1 | 1/1 | 0 | 100.00% |
| queue_wait | 1 | 1/1 | 0 | 100.00% |
| worker_init | 0 | 0/1 | 1 | 0.00% |
| auth | 1 | 1/1 | 0 | 100.00% |
| connection | 0 | 0/1 | 1 | 0.00% |
| history_setup | 0 | 0/1 | 1 | 0.00% |
| history_read | 1 | 1/1 | 0 | 100.00% |
| history_commit | 1 | 1/1 | 0 | 100.00% |
| synthetic | 0 | 0/1 | 1 | 0.00% |
| agent | 1 | 1/1 | 0 | 100.00% |
| kb | 1 | 1/1 | 0 | 100.00% |
| github | 1 | 1/1 | 0 | 100.00% |
| web | 0 | 0/1 | 1 | 0.00% |
| model | 2 | 1/1 | 0 | 100.00% |
| render | 1 | 1/1 | 0 | 100.00% |
| send | 1 | 1/1 | 0 | 100.00% |

| Stage | Callback observations used | Log observations used | Callback requests | Log requests | Callback observations superseded |
|---|---:|---:|---:|---:|---:|
| ingress | 0 | 1 | 0 | 1 | 0 |
| queue_wait | 1 | 0 | 1 | 0 | 0 |
| worker_init | 0 | 0 | 0 | 0 | 0 |
| auth | 1 | 0 | 1 | 0 | 0 |
| connection | 0 | 0 | 0 | 0 | 0 |
| history_setup | 0 | 0 | 0 | 0 | 0 |
| history_read | 0 | 1 | 0 | 1 | 1 |
| history_commit | 0 | 1 | 0 | 1 | 1 |
| synthetic | 0 | 0 | 0 | 0 | 0 |
| agent | 1 | 0 | 1 | 0 | 0 |
| kb | 1 | 0 | 1 | 0 | 0 |
| github | 1 | 0 | 1 | 0 | 0 |
| web | 0 | 0 | 0 | 0 | 0 |
| model | 2 | 0 | 1 | 0 | 0 |
| render | 1 | 0 | 1 | 0 | 0 |
| send | 0 | 1 | 0 | 1 | 0 |

| Stage | Source | Stage outcome | Count | Mean ms | P50 ms | P95 ms | P99 ms | Min ms | Max ms |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| ingress | completed log | success | 1 | 388.866 | 388.866 | 388.866 | 388.866 | 388.866 | 388.866 |
| queue_wait | callback | success | 1 | 1678.618 | 1678.618 | 1678.618 | 1678.618 | 1678.618 | 1678.618 |
| worker_init | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| auth | callback | success | 1 | 1.216 | 1.216 | 1.216 | 1.216 | 1.216 | 1.216 |
| connection | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| history_setup | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| history_read | completed log | success | 1 | 0.271 | 0.271 | 0.271 | 0.271 | 0.271 | 0.271 |
| history_commit | completed log | success | 1 | 84.680 | 84.680 | 84.680 | 84.680 | 84.680 | 84.680 |
| synthetic | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| agent | callback | success | 1 | 6014.523 | 6014.523 | 6014.523 | 6014.523 | 6014.523 | 6014.523 |
| kb | callback | success | 1 | 307.725 | 307.725 | 307.725 | 307.725 | 307.725 | 307.725 |
| github | callback | empty | 1 | 454.970 | 454.970 | 454.970 | 454.970 | 454.970 | 454.970 |
| web | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| model | callback | success | 2 | 2776.808 | 2776.808 | 3717.249 | 3800.843 | 1731.874 | 3821.742 |
| render | callback | success | 1 | 0.154 | 0.154 | 0.154 | 0.154 | 0.154 | 0.154 |
| send | completed log | success | 1 | 50.458 | 50.458 | 50.458 | 50.458 | 50.458 | 50.458 |

## Configuration and limitations

Dataset SHA-256: `f6c548f30ebd02a7ad2ad897a4eea048a63f4b0103d27b728a82851e04d314ad`; seed: 20260919; history mode: `empty`.
Per-request safe telemetry and supplied cold evidence are in the raw result. Missing host counts, resource utilization, stage timings or dependency calls are unknown, not zero. Target-generated cross-process timings are not subtracted from observer clocks.
Driver utilization is measured separately; a driver stop is not a target capacity limit.
Observer authentication/validation and evidence I/O are recorded separately from body-reception latency. Durable write-ahead intents precede the per-request clock; journal backpressure can limit throughput and must not be attributed to target capacity.
Compare only matching workload/history hashes, model versions, budgets and host settings. Model/Entra/configuration/capacity and cloud functional checks remain deployment gates.
No cloud cost or capacity conclusion is produced by local synthetic tests.

## Raw result provenance

Source: [original raw result](file:///C:/Codes/github-copilot-advisor/deploy/out/bench/aca-concurrency/f8de5c62587d4f1395c32f8bf38bda83.json).
Raw-result SHA-256 (original bytes): `f075fc94d4b310d401b5814aad8b441739be75edb811aa097028b791142c1e28`.

## Measured cohort and resource evidence

Target artifact: `31882ec48e652906874e3d5687bf0b675dfbb841d66531d84ee23a204cd7a4d0`; commit: `49100580008a769aafc87c4f12f497e048a7fe6f`.
This is cohort B, not pooled with the earlier warm cohort A.
Observed model HTTP attempts: 150; observed HTTP 429s: 0; counter coverage: 50/50 requests. No counts inferred from model rounds.
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
