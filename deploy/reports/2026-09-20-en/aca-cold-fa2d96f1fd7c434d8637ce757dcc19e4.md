# Benchmark fa2d96f1fd7c434d8637ce757dcc19e4

[简体中文](../2026-09-20-cn/aca-cold-fa2d96f1fd7c434d8637ce757dcc19e4.md) · [Stage definitions and timing boundaries](stage-definitions.md)

**APPLICATION LINK** — not Teams-client latency; not an SLA or an absolute platform maximum.
Profile: `cold`; requested outstanding-question limit: 1.
Completion endpoint: complete bounded HTTP reply body received by the observer; accepted only after authentication and complete-message validation.
Observation: 2026-09-19T06:28:12.227656+00:00 to 2026-09-19T06:28:50.648758+00:00.
Actual window: **38.421 seconds**, including drain; submission window: 38.421 seconds.
Stop: `target_reached`. Sampling: **sufficient** (1/1 terminal observations; minimum 0 submission seconds).
Attempted: 1; censored: 0; after-window completions: 0.
Full answers (success + degraded)/actual window: **0.0260274 answers/s**.
Successes/actual window: **0.0260274 successes/s**; successes/attempted: 1.0.

| Outcome | Count | Mean ms | P50 ms | P95 ms | P99 ms | Min ms | Max ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| success | 1 | 38344.275 | 38344.275 | 38344.275 | 38344.275 | 38344.275 | 38344.275 |
| degraded | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| failed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| unknown | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| expired | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| ignored | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| censored | 0 | n/a | n/a | n/a | n/a | n/a | n/a |

ACKs and typing events are not completions. Censored requests have no fabricated duration.
Failure, degradation, unknown delivery and unfinished work remain separate.
Cold classification: **natural_zero**.
Natural-zero requires supplied control-plane zero proof and first-workload/new-boot evidence for every required scale group. Absence of evidence is unverified.
The first_request marker means first workload on the observed boot, not a platform probe; probe exclusion alone is not cold-start evidence.
Small cold samples and tail quantiles are exploratory; no excluded adverse samples.
Notes: small_tail_sample.

## Observed per-stage latency

Scenario: `real_agent`; profile: `cold`; run: `fa2d96f1fd7c434d8637ce757dcc19e4`.
This section uses only supplied StageObservation values for this run's requests. Synthetic and real-Agent scenarios are not pooled or inferred from stage names.
Coverage is distinct requests with any observation of a stage / all attempted requests in that request-outcome cohort. Missing stage coverage is unknown, not zero or proof that the stage did not run. An observed zero duration is retained.
Latency count is stage observations, not requests: repeated spans are separate observations, not per-request sums. Stage outcomes are split within each request-outcome cohort.
Stage durations overlap and are not additive; cross-process clocks are not directly subtracted. End-to-end remains the observer's complete callback-body reception time, not a sum of stages.
Percentiles use the existing linear interpolation; small stage samples and tail quantiles are exploratory, not an SLA.

### Completed-stage log provenance

Source: [separate stage-log export](../../out/bench/aca-cold/fa2d96f1fd7c434d8637ce757dcc19e4-stages.json).
Source SHA-256 (original bytes): `f20ad10a3e7f3a9607c47083aac536954297ea331ea722f77862a83804432b72`.
Export records: 4; unique event IDs: 4; exact duplicate records: 0.
Log window: 2026-09-19T06:26:12.227656+00:00 to 2026-09-19T06:30:50.648758+00:00 (operator-declared; verified against 4 supplied UTC log-emission timestamps).
Superseded callback observations: 2.
Log identity is exactly (boot_id, segment_id, span_id); differing fields for one identity reject the import. Different identities are distinct observed attempts.
Join is exact job_id AND trace_id from the original callback correspondence. Missing, unmatched or ambiguous correlation rejects the whole import.
Completed logs take precedence only for the matched request/stage. Callback stages have no event IDs: supersession is a source-selection policy, not claimed identity deduplication. Other callback stages are retained. Sources are not pooled in distributions.
Log precedence is restricted to ingress, history_read, history_commit and send; all other stage scopes remain callback-sourced.
A partial export can supersede a more complete callback stage list; export the intended complete log window and inspect counts. Request coverage is not span completeness.
Raw results and the separate import file are unchanged. Source SHA/window are provenance, not Azure attestation. Imported spans may finish after callback reception or the original run window; they never change end-to-end timing, outcomes or cold evidence.
Timestamp window validation does not attest Azure collection or complete attempt coverage.
Observed log-emission range: 2026-09-19T06:28:38.405898+00:00 to 2026-09-19T06:28:51.469685+00:00. Emission timestamps are collection metadata, not stage duration endpoints.

### Request outcome: `success`

Attempted requests in cohort: 1.

| Stage | Observations | Requests with stage / cohort | Missing/unknown requests | Coverage |
|---|---:|---:|---:|---:|
| ingress | 1 | 1/1 | 0 | 100.00% |
| queue_wait | 1 | 1/1 | 0 | 100.00% |
| worker_init | 1 | 1/1 | 0 | 100.00% |
| auth | 1 | 1/1 | 0 | 100.00% |
| connection | 1 | 1/1 | 0 | 100.00% |
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
| worker_init | 1 | 0 | 1 | 0 | 0 |
| auth | 1 | 0 | 1 | 0 | 0 |
| connection | 1 | 0 | 1 | 0 | 0 |
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
| ingress | completed log | success | 1 | 557.246 | 557.246 | 557.246 | 557.246 | 557.246 | 557.246 |
| queue_wait | callback | success | 1 | 2076.982 | 2076.982 | 2076.982 | 2076.982 | 2076.982 | 2076.982 |
| worker_init | callback | success | 1 | 17.230 | 17.230 | 17.230 | 17.230 | 17.230 | 17.230 |
| auth | callback | success | 1 | 289.539 | 289.539 | 289.539 | 289.539 | 289.539 | 289.539 |
| connection | callback | success | 1 | 3384.225 | 3384.225 | 3384.225 | 3384.225 | 3384.225 | 3384.225 |
| history_setup | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| history_read | completed log | success | 1 | 0.183 | 0.183 | 0.183 | 0.183 | 0.183 | 0.183 |
| history_commit | completed log | success | 1 | 42.818 | 42.818 | 42.818 | 42.818 | 42.818 | 42.818 |
| synthetic | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| agent | callback | success | 1 | 8148.526 | 8148.526 | 8148.526 | 8148.526 | 8148.526 | 8148.526 |
| kb | callback | success | 1 | 428.528 | 428.528 | 428.528 | 428.528 | 428.528 | 428.528 |
| github | callback | empty | 1 | 428.266 | 428.266 | 428.266 | 428.266 | 428.266 | 428.266 |
| web | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| model | callback | success | 2 | 3856.312 | 3856.312 | 4109.361 | 4131.854 | 3575.148 | 4137.477 |
| render | callback | success | 1 | 0.170 | 0.170 | 0.170 | 0.170 | 0.170 | 0.170 |
| send | completed log | success | 1 | 31.612 | 31.612 | 31.612 | 31.612 | 31.612 | 31.612 |

## Configuration and limitations

Dataset SHA-256: `f6c548f30ebd02a7ad2ad897a4eea048a63f4b0103d27b728a82851e04d314ad`; seed: 20260919; history mode: `empty`.
Per-request safe telemetry and supplied cold evidence are in the raw result. Missing host counts, resource utilization, stage timings or dependency calls are unknown, not zero. Target-generated cross-process timings are not subtracted from observer clocks.
Driver utilization is measured separately; a driver stop is not a target capacity limit.
Observer authentication/validation and evidence I/O are recorded separately from body-reception latency. Durable write-ahead intents precede the per-request clock; journal backpressure can limit throughput and must not be attributed to target capacity.
Compare only matching workload/history hashes, model versions, budgets and host settings. Model/Entra/configuration/capacity and cloud functional checks remain deployment gates.
No cloud cost or capacity conclusion is produced by local synthetic tests.
