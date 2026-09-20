# Cloud experiment status

Updated: 2026-09-20. **ACA warm, staged-concurrency and partial natural-cold
results are measured. Concurrency stopped at 16; cold collected 8/10 required
samples; guarded benchmark closure is complete; Flex comparison remains blocked.**
This is a partial measured experiment report, not a completed ACA/Flex
comparison. The [approved design](../../docs/superpowers/specs/2026-09-17-cloud-deployment-experiments-design.md)
and [deployment gates](../../.azure/deployment-plan.md) remain in effect.

## Outcomes

| Area | Status | Evidence or limitation |
|---|---|---|
| Local development mode | Retained | Original Teams start command; process-local history is not restart-persistent |
| Durable storage/worker | Local protocol tests passed | Real SDK/Azurite contracts and scoped recovery review; not Azure RBAC proof |
| Native Functions host | Local smoke passed | Six functions discovered; unauthenticated Bot requests rejected; authenticated benchmark registration/message completed through Queue and a real local HTTP callback, followed by a deduplicated Teams fixture turn |
| Model/Teams boundaries in native smoke | Fixtures | JWKS, model and Teams connector were offline substitutes |
| Artifact dataset packaging | Local tests passed | 52 package tests, including locked reproducible ACA contexts; explicit dataset opt-in |
| Final template compilation | Passed | 49 compiler contracts, including separate benchmark identity bootstrap and app deployment |
| Full deployment-tool regression | Passed with skips | 357 passed, 5 skipped; subsequent live Flex metadata parsing fix passed 55 focused tests and reached the correct quota-evidence gate |
| Final metadata/packaging/smoke regression | Passed | 179 tests against the final integrated implementation |
| Current target packages | Built | `deploy/out/aca-key-context` and `deploy/out/functions-key-bootstrap.zip`; three online wheels with full-stage instrumentation |
| Full offline application regression | One existing timing-test failure | 1238 passed, 1 failed, 19 integration tests deselected; the unchanged typing file passed all 21 tests in isolation |
| Existing dependency preflight | Passed actual Key probes | User authorized cross-tenant Key mode; actual model/embedding endpoint and Search schema checks passed, embedding dimension 3072; old-tenant resources unchanged |
| ACA Linux image workflow | Verified in Azure | ACR base run cm3 and initial build/offline-probe/push run cm4 succeeded; subsequent cohorts have separate immutable image proofs; no local Docker required |
| Benchmark Entra identity | Created and exercised | Isolated single-tenant v2 API and service principal; no client secret; driver, ACA and Flex UAMIs assigned Benchmark.Invoke; ACA ingress/callback exercised |
| Flex regional capacity | Unverified | Actual newly created app's quota diagnostic now lists Southeast Asia but still requires region-form selection; no default quota assumed |
| ACA deployment | Succeeded | Immutable image deployed with min replicas 0; health 200, unauthenticated Bot 401, disabled benchmark 404 |
| ACA experiment configuration | Deployed | Subsequently set min replicas 1 and enabled only the authenticated driver/fixed callback for functional smoke |
| Flex empty bootstrap | Infrastructure ready | Recovered via ARM receipts and verified Microsoft Smart Alerts provenance; corrected settings deployed; no code published and actual regional quota still unverified |
| Dedicated observer | Deployed and exercised | Initial evidence-validation crash loop stopped after NSP; functional, warm and concurrency runs exported through its managed identity |
| Storage connectivity | Enforced NSP verified | Only this subscription is permitted inbound; both accounts retain identity-only authorization, disabled anonymous/shared-key access and legacy Deny/no exceptions; no policy bypass |
| ACA full benchmark-link question | Passed, one functional sample | Actual Entra ingress, durable worker/history, KB lookup, two model rounds and rendered callback completed; separate completed-stage logs joined |
| Real Teams acceptance of the new Bots | Not run | ACA Bot provisioned; no side-loaded client acceptance evidence |
| Resident ACA warm latency | Measured | 100/100 success after 3/3 warm-ups; mean 8.177 s, P95 9.732 s at one outstanding question |
| ACA concurrency | Measured through 16 | 651 attempts across five levels; stopped at a request deadline; 32/64/128 not run |
| ACA natural cold | Partial, 8/10 required samples | Eight verified natural-zero successes; mean 36.994 s, P95 40.606 s; ninth attempt has incomplete evidence |
| Flex comparison | Blocked | Actual regional quota still unverified; no code or workload deployed |
| Benchmark closure/scale reset | Verified | Target benchmark disabled/min0/actual0; observer ingress disabled/all revisions inactive/actual0; Teams ingress and resources retained |
| Resource deletion | Not performed | Separate explicit user confirmation required; retained registry/storage/monitoring can still incur cost |

## Actual deployed scope

- Subscription: `18827cdf-a4e8-4316-a7ba-a4499139a3ec`.
- Tenant: `4bc8a937-2b77-44c4-8790-dfc0068162f2`.
- ACA: [experiment endpoint](https://ca-advisor-ttbkwagaatnyc.agreeablebay-c149451d.southeastasia.azurecontainerapps.io/healthz).
- Initial deployment ACR image (historical; use each cohort's bindings for its
  actual artifact):
  `cradvisorttbkwagaatnyc.azurecr.io/advisor@sha256:e61eb688e7beebfaa869651505fbee1024ec35a84b499667d240bd6420661978`.
- Actual ACR proof and ownership are kept under ignored `deploy/out/runs/`.
  The succeeded build/probe/push duration is a build measurement, not a chatbot
  cold-start or request-latency measurement.
- Actual ACA Consumption quota readback: 500 cores, with 0.25 used at observation.
  This is capacity evidence, not a performance result or reservation.

## Latency and concurrency

### Measured functional sample, not a performance conclusion

The [full-stage functional report](aca-functional-smoke.md) records run
`097f2af1d6ef456682431734cd717f1b`, one `real_agent` / `kb-01` request with empty
history, at 2026-09-19 01:17 UTC. It succeeded without retry or throttling reported
in that sample:

| Observation | Measured value |
|---|---:|
| Submission to complete callback body | 14,875.278 ms |
| HTTP admission ACK | 289.546 ms |
| Completed ingress handler | 283.682 ms |
| Acceptance to validated worker attempt | 499.564 ms |
| Worker object initialization | 51.350 ms |
| Lazy model/client component initialization (`connection`, not DNS/TLS) | 4,348.676 ms |
| Agent turn, including nested model/retrieval | 9,668.243 ms |
| Two model calls | 5,254.770 ms and 3,948.743 ms |
| KB operation | 458.737 ms |
| Concurrent GitHub operation (empty result) | 453.925 ms |
| History archive read / commit | 0.178 ms / 45.434 ms |
| Render / completed callback send | 0.173 ms / 43.577 ms |

The sample used 7,575 input and 350 output model tokens. Major spans overlap;
their durations must not be added to reconstruct end-to-end time. This first real
question included lazy component initialization, while its process-first flag
was false. It is **neither verified natural cold nor a warmed average**.
The answer body was validated transiently and is absent from exported evidence.

### Resident ACA warm results

The [measured warm report](aca-warm-92395ebe27bb4a198c542d50ca163884.md) covers
2026-09-19 01:50:06–02:03:49 UTC, after a separate
[three-question warm-up](aca-warmup-a2a5c67f70554688be1aaff7799a6f43.md).
The fixed workload is `real_agent`, only case `kb-01`, empty per-question history,
0.25 vCPU/0.5 GiB, min replicas 1, and one outstanding question.

- 100/100 successful; zero degraded, failed, unknown or censored outcomes.
- Mean **8.177 s**, P50 **8.161 s**, P95 **9.732 s**, P99 **10.137 s**.
- Minimum/maximum: 6.248 s / 10.357 s.
- Actual window 822.486 s; completed-answer throughput **0.12158/s**.
- 400 completed-stage events matched exactly to the 100 callback job/trace pairs.
- Azure Monitor's 14 one-minute bins covering 01:50–02:04 UTC show CPU mean
  17.0% / maximum 57.0%, memory mean 26.39% / maximum 27.0%, one replica and
  zero reported restarts. These are minute-resolution host observations, not
  per-request resource samples; the metric window includes small boundary margins.

| Major stage | Mean ms | P95 ms | Observation count |
|---|---:|---:|---:|
| Ingress handler | 438.856 | 552.796 | 100 |
| Acceptance to validated attempt | 1,263.289 | 2,147.965 | 100 |
| Agent turn | 6,525.426 | 7,479.221 | 100 |
| Model call | 3,019.782 | 4,581.225 | 200 |
| KB operation | 361.066 | 423.967 | 100 |
| Parallel GitHub operation (empty result) | 468.920 | 513.775 | 100 |
| History read / commit | 0.174 / 50.777 | 0.251 / 89.922 | 100 each |
| Render / completed send | 0.203 / 32.022 | 0.500 / 42.251 | 100 each |

These overlapping stage/call observations are not additive. Per-model-call
statistics are not a per-question sum. Results describe this workload only, not
Teams-client latency or an absolute supported-concurrency limit.

| Configuration | Verified natural-cold samples | Cold latency | Warm mean / P95 | Tested sustainable concurrency |
|---|---:|---|---|---|
| ACA 0.25 vCPU / 0.5 GiB, min 1 | Not run | Not measured | 8.177 s / 9.732 s (100 samples) | 1 measured; higher levels pending |
| ACA 0.25 vCPU / 0.5 GiB, min 0 | Not run | Not measured | Not measured | Not measured |
| Flex 512 MB, no Always Ready | Not run | Not measured | Not measured | Not measured |

Do not substitute local synthetic timing, health-check latency, HTTP 202 or
platform scaling limits for these measurements. A natural-cold observation needs
control-plane scale-to-zero evidence and new host boot/first-request evidence;
Flex needs both HTTP and worker scale groups. Failures, degraded replies,
unknown delivery and censored requests remain separate from successful latency.
The boot flag describes the first **workload** invocation, not ACA's mandatory
startup/readiness probes; excluding probes alone does not prove cold startup.
Missing platform instance metadata produces no boot evidence, not a placeholder.
The [benchmark guide](../bench/README.md) describes the bounded sampling and
stopping rules. Results cannot establish an absolute platform maximum or an SLA.

### Resident ACA concurrency cohort B

The [aggregate concurrency report](aca-concurrency-summary.md) and
[actual resource-metric supplement](aca-concurrency-resource-metrics.md) retain
all outcomes. This cohort uses instrumentation revision `4910058`, including
actual model HTTP-attempt/429 counters; it is not pooled with warm cohort A.
The workload remains only `kb-01`, real Agent, empty/new-conversation history.

| Outstanding questions | Success / degraded / censored | Success mean / P95 | Successful answers/s | Recorded stop |
|---:|---:|---:|---:|---|
| 1 | 49 / 1 / 0 | 8.277 s / 9.796 s | 0.118 | Target reached |
| 2 | 50 / 1 / 0 | 8.141 s / 9.110 s | 0.237 | Target reached |
| 4 | 76 / 0 / 0 | 9.655 s / 11.155 s | 0.410 | Target reached |
| 8 | 98 / 56 / 0 | 10.243 s / 18.011 s | 0.520 | Target reached |
| 16 | 95 / 224 / 1 | 9.495 s / 14.627 s | 0.154 | Request deadline |

These latency columns exclude degraded/censored outcomes rather than giving
them zero latency; each per-level report shows those categories separately.
Consequently, the lower successful latency at 16 is not evidence of improvement:
only 95/320 questions were successful and one remained censored. Levels
32/64/128 were not executed after the stop condition.

**Highest observed no-degradation level was 4** (76/76 in that level). This is a
workload-specific observation, not an absolute Azure concurrency ceiling or a
guarantee: even the separately sampled levels 1 and 2 each had one degraded reply.
Without a user-defined SLA, no unconditional production capacity is asserted.

Read-only, trace-correlated diagnostic queries add these observations:

- At level 8, all 56 degraded callbacks coincided with GitHub-stage failures;
  completed-stage logs record **56 HTTP 403s**, while all 154 KB operations
  completed successfully and no model HTTP 429 was observed. The captured safe
  fields do not distinguish a GitHub rate limit from other reasons for 403.
- At level 16, logs record **175 GitHub HTTP 403s** and **61 failed model
  completions with `RateLimitError`**. Callbacks expose **161 model HTTP 429s
  among 977 observed HTTP attempts**, covering 319/320 questions. Counts can
  overlap and are not distinct failed-question counts.
- The runner stopped on `request_deadline`, not `persistent_throttle`. The
  existence of throttling does not establish the cause of the one censored
  request; its full causal path was not proven.
- One-minute metrics at level 16 report up to 20 replicas, maximum CPU 101%,
  maximum memory 27%, and maximum restart metric 0. These do not by themselves
  prove a CPU/memory bottleneck or explain the deadline.

Safe diagnostic categories are retained in
`deploy/out/bench/aca-concurrency/dependency-statuses.json`
(SHA-256 `0b64c0e5d282eabfb9e134a63456ddf1e9d1c9fc71b2f50f362d81db8e504feb`);
raw answers, questions and error bodies were not queried. After the run stopped, two
30-second-separated Queue property reads reported approximate counts 0 and 0.
The cold experiment ran afterward, without overlapping concurrency load.

### ACA natural-cold cohort B: partial result

The [cold report](aca-cold-summary.md) retains all eight complete results and the
[incomplete ninth attempt](aca-cold-df12a3106f4340ec83c01234be5ada5f.md).
The target stayed at 0.25 vCPU / 0.5 GiB and used the same cohort-B artifact.
Only its scale-mode control changed to min replicas 0; that control action is
recorded separately from the original baseline configuration hash.

| Observation | Warm cohort A | Natural-cold cohort B |
|---|---:|---:|
| Complete successful questions | 100 | 8 |
| Mean full-reply latency | 8.177 s | 36.994 s |
| P50 | 8.161 s | 37.823 s |
| P95 | 9.732 s | 40.606 s |
| Minimum / maximum | 6.248 / 10.357 s | 32.997 / 41.824 s |

Cold samples have complete saved ARM empty-replica lists and eight distinct
process boots/instance IDs with first-workload evidence. All eight raw-result,
stage-log and report hashes were rechecked, each report regenerated with only
trailing blank-line differences, and each accepted proof joined by exact
observation timestamp and target ID to the stored ARM responses. The aggregate
is retained in `deploy/out/bench/aca-cold/verified-aggregate.json`.

**The planned minimum was not reached.** The operator set a 105-minute sealed
validity window during preparation, before build/deployment; preparation and
roughly nine-minute natural cold cycles exhausted it. This is a measurement
orchestration shortfall, not evidence that the application failed eight cold
requests. The ninth checkpoint records no submitted-question intent and no
terminal observation; its final result is unavailable. It is not counted as a
success, as a zero-latency sample, or as an invented censored request. START was
not repeated, and no extra cohort was silently substituted.

These are descriptive cohorts, not pooled samples or a controlled estimate of
pure startup overhead: warm A predates the HTTP-counter instrumentation in B.
The cold P95 is especially uncertain with only eight samples. Timings cover the
application/observer link, not the Teams client, and do not include idle waiting
for scale-to-zero.

#### Natural-cold observed stages

All listed stages cover the eight finalized successful questions. Model spans
are individual calls (16 total), not eight per-question totals. GitHub returned
empty results, not errors.

| Boundary / stage | Mean ms | P95 ms | Observation count |
|---|---:|---:|---:|
| Client submission to HTTP ACK | 25,252.022 | 28,238.785 | 8 |
| Ingress handler | 744.904 | 1,054.883 | 8 |
| Acceptance to validated worker attempt | 1,289.923 | 2,237.412 | 8 |
| Worker object initialization | 20.519 | 24.976 | 8 |
| Authentication | 455.935 | 744.055 | 8 |
| Lazy component initialization (`connection`) | 1,849.562 | 3,510.652 | 8 |
| History read | 0.188 | 0.223 | 8 |
| Agent turn | 8,535.391 | 9,523.301 | 8 |
| Model call | 4,018.198 | 4,743.558 | 16 |
| KB operation | 445.148 | 491.525 | 8 |
| GitHub operation (empty) | 488.502 | 555.533 | 8 |
| History commit | 55.070 | 85.042 | 8 |
| Render | 0.182 | 0.240 | 8 |
| Completed callback send | 35.828 | 55.273 | 8 |

Ingress/history/send use 32 exactly correlated completed-stage log events;
other stages remain callback-sourced. Stages overlap and must not be added.
Platform allocation, image startup and ingress routing are not separately
instrumented; the ACK/handler gap is not labeled as a measured pure platform
startup duration. Web search, synthetic execution and seeded-history setup
have no observations in this fixed case, not fabricated zero timings.

**Teams admission risk:** all eight cold benchmark ACKs took 23.346–28.492 s.
[Bot Service guidance](https://learn.microsoft.com/azure/bot-service/bot-builder-howto-long-operations-guidance?view=azure-bot-service-4.0)
describes a 10–15 s channel-dependent timeout for incoming activity processing.
Our endpoint/authentication path is not a real Teams-client test, so this is a
material risk requiring actual Teams verification, not a proven Teams failure.
Asynchronous model processing does not remove host startup time before admission.
Do not treat ACA min0 as production-equivalent to resident mode on this evidence.

At the 2026-09-19 07:02 UTC audit, the target had actual replicas 0, while the
expired-plan observer still had one unready/restarting resident replica. That
condition was resolved by the [guarded closure](aca-benchmark-closure.md):
observer ingress disabled and all its revisions deactivated; target benchmark
flag false, min0 and normal Teams ingress preserved. Independent control-plane
reads on **2026-09-20 02:24–02:25 UTC** again found target replicas0 and observer
replicas0/no active revisions. No application probe was used in that recheck.

The observer's retained revision template still says min1/max1; inactive
revisions are stopped, so this does not mean a resident replica remains.
Its feature flag remains true inside the inactive image, but ingress is null,
all revisions are inactive and AUTOSTART is false. Reopening requires a valid
plan and separate approval, not reactivation of the expired image. No resources
or durable evidence were deleted.

Closure tests passed in the implementation run (65/65). The independent repeat
had 63 passes and two Windows subprocess timeouts after an unusually long
6h57 elapsed runner interval; neither failure was a functional assertion.
Both exact failed tests then passed unchanged in 53.74 seconds. No timeout was
increased or test disabled. Editor diagnostics were clear, and semantic checks
found all 55 `Cli.run` and 21 `Cli.rest` call sites compatible.

## Cost attribution

The [cost evidence report](experiment-costs.md) now includes a **2026-09-20
02:26:23 UTC** Cost Management snapshot: **$0.95806921575 reported subtotal** for
the two experiment resource groups, up from the earlier $0.07103551796 snapshot.
Billing ingestion remains incomplete and no ACA consumption row was returned,
so this is not the full experiment bill. The
report separates reported charges, published retail rates, illustrative
30-day resident scenarios, and unmeasured Flex/external-dependency costs.

Experiment resources and ACR Tasks exist and can incur charges. These verified
API responses provide reported charges, not a final invoice, complete
workload-attributed total or savings claim.

After deployment, report these categories separately: ACA/Flex hosting, the new
Basic ACR fixed charge, each LRS state/queue account, logs, temporary driver,
and model/search/web API usage increments. Existing Search fixed costs are not
new experiment infrastructure costs. Mark unbilled estimates explicitly and
record the pricing region/date and billing-data delay.

The workloads are planned in Southeast Asia. The existing GlobalStandard model
deployments do not guarantee that inference executes in that resource region.

## Local regression limitation

The full offline application run failed only
`test_typing_renews_without_overlapping_or_continuing_after_response`, at its
strictly increasing `asyncio` timestamps assertion. Neither that test nor the
typing implementation changed in this work. The entire typing test file then
passed in isolation (21 tests).

The selected Windows Python 3.12 interpreter reports `GetTickCount64()` with
15.625 ms resolution for the event-loop clock, while that test uses 10 ms typing
intervals. A separate clock probe reproduced distinct asynchronous resumptions
with equal loop timestamps and positive high-resolution elapsed time. This is
recorded as a timing-test portability limitation, not a claim that the complete
suite passed or that cloud timing was measured. Typing behavior was not changed
to make the assertion pass.

## Next execution gates

1. Policy-compatible Blob/Queue connectivity is verified through Enforced NSP.
   Collect actual Flex regional capacity evidence before publishing its code.
   Do not weaken policy or use quota defaults.
2. ACA's functional, warm, concurrency and partial cold evidence is collected.
   Preserve the 8/10 cold shortfall; do not escalate beyond the stopped
   concurrency level, silently replace the cohort or discard adverse results.
3. Benchmark access is closed, ACA min0/actual0 verified, and the observer is
   suspended. Keep evidence and request explicit confirmation before deleting
   recorded resources. After remaining role-assignment work, the operator should
   turn off the manually enabled root-scope "Access management for Azure
   resources" switch; it was not changed by these scripts.

## Saved implementation

The implementation is committed on `dev-dongchen`, without a push:
`8f2b46d` (shared experiment protocol), `b6fa5f3` (cloud hosts),
`baede3a` (benchmark tools), and `ae6cdd3` (guarded deployment and smoke gates),
in addition to the earlier authentication/history/storage commits.
Current telemetry/report additions are committed as `305ae74` and `a433e11`.
Cloud-build/new-context/recovery changes, Enforced NSP and HTTP-attempt counters
are committed through `4910058` and have been exercised in the actual ACA runs.
Closure helpers and measured reports are saved separately from the historical
artifact binding; guarded closure code is committed as `0fe3ec1`. Remaining
work includes the cold sampling shortfall, Flex
code deployment and equivalent workloads after quota verification, final
billing attribution and real Teams client acceptance. The overall ACA/Flex
experiment is not marked fully complete.
