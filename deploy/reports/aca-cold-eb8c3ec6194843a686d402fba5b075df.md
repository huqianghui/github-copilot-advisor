# Benchmark eb8c3ec6194843a686d402fba5b075df

**APPLICATION LINK** — not Teams-client latency; not an SLA or an absolute platform maximum.
Profile: `cold`; requested outstanding-question limit: 1.
Completion endpoint: complete bounded HTTP reply body received by the observer; accepted only after authentication and complete-message validation.
Observation: 2026-09-19T06:01:42.106184+00:00 to 2026-09-19T06:02:16.135473+00:00.
Actual window: **34.029 seconds**, including drain; submission window: 34.029 seconds.
Stop: `target_reached`. Sampling: **sufficient** (1/1 terminal observations; minimum 0 submission seconds).
Attempted: 1; censored: 0; after-window completions: 0.
Full answers (success + degraded)/actual window: **0.0293865 answers/s**.
Successes/actual window: **0.0293865 successes/s**; successes/attempted: 1.0.

| Outcome | Count | Mean ms | P50 ms | P95 ms | P99 ms | Min ms | Max ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| success | 1 | 33949.196 | 33949.196 | 33949.196 | 33949.196 | 33949.196 | 33949.196 |
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

Scenario: `real_agent`; profile: `cold`; run: `eb8c3ec6194843a686d402fba5b075df`.
This section uses only supplied StageObservation values for this run's requests. Synthetic and real-Agent scenarios are not pooled or inferred from stage names.
Coverage is distinct requests with any observation of a stage / all attempted requests in that request-outcome cohort. Missing stage coverage is unknown, not zero or proof that the stage did not run. An observed zero duration is retained.
Latency count is stage observations, not requests: repeated spans are separate observations, not per-request sums. Stage outcomes are split within each request-outcome cohort.
Stage durations overlap and are not additive; cross-process clocks are not directly subtracted. End-to-end remains the observer's complete callback-body reception time, not a sum of stages.
Percentiles use the existing linear interpolation; small stage samples and tail quantiles are exploratory, not an SLA.

### Completed-stage log provenance

Source: [separate stage-log export](file:///C:/Codes/github-copilot-advisor/deploy/out/bench/aca-cold/eb8c3ec6194843a686d402fba5b075df-stages.json).
Source SHA-256 (original bytes): `6e85230a29e9d64d5c45ab50605602cd1aeedfe983f1d05c14f300d08f4e0f5e`.
Export records: 4; unique event IDs: 4; exact duplicate records: 0.
Log window: 2026-09-19T05:59:42.106184+00:00 to 2026-09-19T06:04:16.135473+00:00 (operator-declared; verified against 4 supplied UTC log-emission timestamps).
Superseded callback observations: 2.
Log identity is exactly (boot_id, segment_id, span_id); differing fields for one identity reject the import. Different identities are distinct observed attempts.
Join is exact job_id AND trace_id from the original callback correspondence. Missing, unmatched or ambiguous correlation rejects the whole import.
Completed logs take precedence only for the matched request/stage. Callback stages have no event IDs: supersession is a source-selection policy, not claimed identity deduplication. Other callback stages are retained. Sources are not pooled in distributions.
Log precedence is restricted to ingress, history_read, history_commit and send; all other stage scopes remain callback-sourced.
A partial export can supersede a more complete callback stage list; export the intended complete log window and inspect counts. Request coverage is not span completeness.
Raw results and the separate import file are unchanged. Source SHA/window are provenance, not Azure attestation. Imported spans may finish after callback reception or the original run window; they never change end-to-end timing, outcomes or cold evidence.
Timestamp window validation does not attest Azure collection or complete attempt coverage.
Observed log-emission range: 2026-09-19T06:02:06.619807+00:00 to 2026-09-19T06:02:17.510657+00:00. Emission timestamps are collection metadata, not stage duration endpoints.

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
| ingress | completed log | success | 1 | 762.994 | 762.994 | 762.994 | 762.994 | 762.994 | 762.994 |
| queue_wait | callback | success | 1 | 519.940 | 519.940 | 519.940 | 519.940 | 519.940 | 519.940 |
| worker_init | callback | success | 1 | 20.361 | 20.361 | 20.361 | 20.361 | 20.361 | 20.361 |
| auth | callback | success | 1 | 480.514 | 480.514 | 480.514 | 480.514 | 480.514 | 480.514 |
| connection | callback | success | 1 | 756.299 | 756.299 | 756.299 | 756.299 | 756.299 | 756.299 |
| history_setup | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| history_read | completed log | success | 1 | 0.186 | 0.186 | 0.186 | 0.186 | 0.186 | 0.186 |
| history_commit | completed log | success | 1 | 47.124 | 47.124 | 47.124 | 47.124 | 47.124 | 47.124 |
| synthetic | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| agent | callback | success | 1 | 9063.620 | 9063.620 | 9063.620 | 9063.620 | 9063.620 | 9063.620 |
| kb | callback | success | 1 | 426.882 | 426.882 | 426.882 | 426.882 | 426.882 | 426.882 |
| github | callback | empty | 1 | 523.440 | 523.440 | 523.440 | 523.440 | 523.440 | 523.440 |
| web | none | not observed | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| model | callback | success | 2 | 4266.832 | 4266.832 | 4287.047 | 4288.844 | 4244.370 | 4289.293 |
| render | callback | success | 1 | 0.134 | 0.134 | 0.134 | 0.134 | 0.134 | 0.134 |
| send | completed log | success | 1 | 40.565 | 40.565 | 40.565 | 40.565 | 40.565 | 40.565 |

## Configuration and limitations

Dataset SHA-256: `f6c548f30ebd02a7ad2ad897a4eea048a63f4b0103d27b728a82851e04d314ad`; seed: 20260919; history mode: `empty`.
Per-request safe telemetry and supplied cold evidence are in the raw result. Missing host counts, resource utilization, stage timings or dependency calls are unknown, not zero. Target-generated cross-process timings are not subtracted from observer clocks.
Driver utilization is measured separately; a driver stop is not a target capacity limit.
Observer authentication/validation and evidence I/O are recorded separately from body-reception latency. Durable write-ahead intents precede the per-request clock; journal backpressure can limit throughput and must not be attributed to target capacity.
Compare only matching workload/history hashes, model versions, budgets and host settings. Model/Entra/configuration/capacity and cloud functional checks remain deployment gates.
No cloud cost or capacity conclusion is produced by local synthetic tests.
