# Benchmark a2a5c67f70554688be1aaff7799a6f43

**APPLICATION LINK** — not Teams-client latency; not an SLA or an absolute platform maximum.
Profile: `warmup`; requested outstanding-question limit: 1.
Completion endpoint: complete bounded HTTP reply body received by the observer; accepted only after authentication and complete-message validation.
Observation: 2026-09-19T01:49:41.678036+00:00 to 2026-09-19T01:50:06.353390+00:00.
Actual window: **24.675 seconds**, including drain; submission window: 24.675 seconds.
Stop: `target_reached`. Sampling: **sufficient** (3/3 terminal observations; minimum 0 submission seconds).
Attempted: 3; censored: 0; after-window completions: 0.
Full answers (success + degraded)/actual window: **0.121579 answers/s**.
Successes/actual window: **0.121579 successes/s**; successes/attempted: 1.0.

| Outcome | Count | Mean ms | P50 ms | P95 ms | P99 ms | Min ms | Max ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| success | 3 | 8164.268 | 8128.248 | 8387.931 | 8411.014 | 7947.771 | 8416.785 |
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

Scenario: `real_agent`; profile: `warmup`; run: `a2a5c67f70554688be1aaff7799a6f43`.
This section uses only supplied StageObservation values for this run's requests. Synthetic and real-Agent scenarios are not pooled or inferred from stage names.
Coverage is distinct requests with any observation of a stage / all attempted requests in that request-outcome cohort. Missing stage coverage is unknown, not zero or proof that the stage did not run. An observed zero duration is retained.
Latency count is stage observations, not requests: repeated spans are separate observations, not per-request sums. Stage outcomes are split within each request-outcome cohort.
Stage durations overlap and are not additive; cross-process clocks are not directly subtracted. End-to-end remains the observer's complete callback-body reception time, not a sum of stages.
Percentiles use the existing linear interpolation; small stage samples and tail quantiles are exploratory, not an SLA.

### Completed-stage log provenance

Source: [separate stage-log export](file:///C:/Codes/github-copilot-advisor/deploy/out/bench/aca-warm/a2a5c67f70554688be1aaff7799a6f43-stages.json).
Source SHA-256 (original bytes): `de7b18c387be8a510e723eb96df7a94922a4b3bcde0c99c3e85ef924822693ba`.
Export records: 12; unique event IDs: 12; exact duplicate records: 0.
Log window: 2026-09-19T01:47:41.678036+00:00 to 2026-09-19T01:52:06.353390+00:00 (operator-declared; verified against 12 supplied UTC log-emission timestamps).
Superseded callback observations: 6.
Log identity is exactly (boot_id, segment_id, span_id); differing fields for one identity reject the import. Different identities are distinct observed attempts.
Join is exact job_id AND trace_id from the original callback correspondence. Missing, unmatched or ambiguous correlation rejects the whole import.
Completed logs take precedence only for the matched request/stage. Callback stages have no event IDs: supersession is a source-selection policy, not claimed identity deduplication. Other callback stages are retained. Sources are not pooled in distributions.
Log precedence is restricted to ingress, history_read, history_commit and send; all other stage scopes remain callback-sourced.
A partial export can supersede a more complete callback stage list; export the intended complete log window and inspect counts. Request coverage is not span completeness.
Raw results and the separate import file are unchanged. Source SHA/window are provenance, not Azure attestation. Imported spans may finish after callback reception or the original run window; they never change end-to-end timing, outcomes or cold evidence.
Timestamp window validation does not attest Azure collection or complete attempt coverage.
Observed log-emission range: 2026-09-19T01:49:41.969275+00:00 to 2026-09-19T01:50:06.358369+00:00. Emission timestamps are collection metadata, not stage duration endpoints.

### Request outcome: `success`

Attempted requests in cohort: 3.

| Stage | Observations | Requests with stage / cohort | Missing/unknown requests | Coverage |
|---|---:|---:|---:|---:|
| ingress | 3 | 3/3 | 0 | 100.00% |
| queue_wait | 3 | 3/3 | 0 | 100.00% |
| worker_init | 0 | 0/3 | 3 | 0.00% |
| auth | 3 | 3/3 | 0 | 100.00% |
| connection | 0 | 0/3 | 3 | 0.00% |
| history_setup | 0 | 0/3 | 3 | 0.00% |
| history_read | 3 | 3/3 | 0 | 100.00% |
| history_commit | 3 | 3/3 | 0 | 100.00% |
| synthetic | 0 | 0/3 | 3 | 0.00% |
| agent | 3 | 3/3 | 0 | 100.00% |
| kb | 3 | 3/3 | 0 | 100.00% |
| github | 3 | 3/3 | 0 | 100.00% |
| web | 0 | 0/3 | 3 | 0.00% |
| model | 6 | 3/3 | 0 | 100.00% |
| render | 3 | 3/3 | 0 | 100.00% |
| send | 3 | 3/3 | 0 | 100.00% |

| Stage | Callback observations used | Log observations used | Callback requests | Log requests | Callback observations superseded |
|---|---:|---:|---:|---:|---:|
| ingress | 0 | 3 | 0 | 3 | 0 |
| queue_wait | 3 | 0 | 3 | 0 | 0 |
| worker_init | 0 | 0 | 0 | 0 | 0 |
| auth | 3 | 0 | 3 | 0 | 0 |
| connection | 0 | 0 | 0 | 0 | 0 |
| history_setup | 0 | 0 | 0 | 0 | 0 |
| history_read | 0 | 3 | 0 | 3 | 3 |
| history_commit | 0 | 3 | 0 | 3 | 3 |
| synthetic | 0 | 0 | 0 | 0 | 0 |
| agent | 3 | 0 | 3 | 0 | 0 |
| kb | 3 | 0 | 3 | 0 | 0 |
| github | 3 | 0 | 3 | 0 | 0 |
| web | 0 | 0 | 0 | 0 | 0 |
| model | 6 | 0 | 3 | 0 | 0 |
| render | 3 | 0 | 3 | 0 | 0 |
| send | 0 | 3 | 0 | 3 | 0 |

| Stage | Source | Stage outcome | Count | Mean ms | P50 ms | P95 ms | P99 ms | Min ms | Max ms |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| ingress | completed log | success | 3 | 375.897 | 375.618 | 496.785 | 507.556 | 241.826 | 510.248 |
| queue_wait | callback | success | 3 | 1028.945 | 1017.032 | 1190.691 | 1206.128 | 859.815 | 1209.987 |
| worker_init | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| auth | callback | success | 3 | 0.837 | 0.716 | 1.105 | 1.140 | 0.648 | 1.148 |
| connection | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| history_setup | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| history_read | completed log | success | 3 | 0.225 | 0.221 | 0.279 | 0.285 | 0.169 | 0.286 |
| history_commit | completed log | success | 3 | 45.665 | 46.811 | 49.490 | 49.728 | 40.396 | 49.788 |
| synthetic | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| agent | callback | success | 3 | 6729.088 | 6590.223 | 7001.609 | 7038.177 | 6549.721 | 7047.319 |
| kb | callback | success | 3 | 377.072 | 383.794 | 395.517 | 396.559 | 350.603 | 396.820 |
| github | callback | empty | 3 | 443.841 | 439.019 | 456.852 | 458.437 | 433.672 | 458.833 |
| web | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| model | callback | success | 6 | 3138.175 | 3070.524 | 4529.153 | 4646.847 | 1903.097 | 4676.270 |
| render | callback | success | 3 | 0.184 | 0.198 | 0.210 | 0.211 | 0.142 | 0.211 |
| send | completed log | success | 3 | 38.146 | 34.551 | 48.712 | 49.971 | 29.602 | 50.286 |

## Configuration and limitations

Dataset SHA-256: `f6c548f30ebd02a7ad2ad897a4eea048a63f4b0103d27b728a82851e04d314ad`; seed: 20260919; history mode: `empty`.
Per-request safe telemetry and supplied cold evidence are in the raw result. Missing host counts, resource utilization, stage timings or dependency calls are unknown, not zero. Target-generated cross-process timings are not subtracted from observer clocks.
Driver utilization is measured separately; a driver stop is not a target capacity limit.
Observer authentication/validation and evidence I/O are recorded separately from body-reception latency. Durable write-ahead intents precede the per-request clock; journal backpressure can limit throughput and must not be attributed to target capacity.
Compare only matching workload/history hashes, model versions, budgets and host settings. Model/Entra/configuration/capacity and cloud functional checks remain deployment gates.
No cloud cost or capacity conclusion is produced by local synthetic tests.

## Raw result provenance

Source: [original raw result](file:///C:/Codes/github-copilot-advisor/deploy/out/bench/aca-warm/a2a5c67f70554688be1aaff7799a6f43.json).
Raw-result SHA-256 (original bytes): `7b4d0a7b799199725a2011b02036785a90d7bd087fe9d5d313b71ebe78119274`.
