# Cloud experiment status

Updated: 2026-09-18. **No cloud performance measurements are available yet.**
This is an implementation/readiness record, not a completed latency or capacity
experiment. The [approved design](../../docs/superpowers/specs/2026-09-17-cloud-deployment-experiments-design.md)
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
| Final target packages | Built, not deployed | `deploy/out/aca-final-context` and `deploy/out/functions-final.zip`; exact current-source/lock/configuration checks passed |
| Full offline application regression | One existing timing-test failure | 1238 passed, 1 failed, 19 integration tests deselected; the unchanged typing file passed all 21 tests in isolation |
| Existing dependency preflight | Passed read-only checks | Actual config with Entra overlays: resource/region/endpoints, permission checks, model metadata and 3072-dimensional Search schema; not cloud application identity proof |
| Final Linux image verification | Blocked | Docker Linux engine `/version` returned HTTP 500; earlier image checks do not cover the final runtime |
| Benchmark Entra identity permissions | Blocked | Read-only registration preview returned `graph_directory_role_unverified` |
| Flex regional capacity | Unverified | CLI quota detector requires region form input; no actual quota values collected; portal needs sign-in and AppLens returned 403 |
| ACA ARM validation / WhatIf | Passed | Real read-only preview against the final artifact; no Apply |
| ACA/Flex deployment | Not run | No experiment resource groups, images, identities, grants or Bots provisioned |
| Cloud managed-identity dependency access | Not run | Local configured-credential model/Search smoke is not cloud UAMI evidence |
| Real Teams acceptance of the two new Bots | Not run | No experimental Bots deployed or side-loaded |
| Cloud latency/concurrency runs | Not run | No cloud samples, success rates or capacity conclusions |
| Cloud cleanup/scale reset | Not applicable yet | No experiment resources created; existing resources were not modified |

## Latency and concurrency

| Configuration | Verified natural-cold samples | Cold latency | Warm mean / P95 | Tested sustainable concurrency |
|---|---:|---|---|---|
| ACA 0.25 vCPU / 0.5 GiB, min 1 | Not run | Not measured | Not measured | Not measured |
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

## Cost attribution

No deployment-attributable bill or measured run duration exists yet, so no
currency totals or savings claims are reported. This does not mean the user's
subscription or existing services cost zero.

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

1. Obtain a working final Linux build/import verification, confirm directory
   permission for the isolated benchmark API/grants, and collect actual fresh
   Flex regional capacity evidence. Do not use published defaults as proof.
2. Rerun readiness checks against the intended source/artifacts, complete Flex
   ARM validation/preview, then deploy through the guarded scripts. Verify actual
   settings, UAMI data access, full answers and restored history.
3. Run the approved same-region experiments and record measured reports.
   Close benchmark access, restore ACA min 0, and request explicit confirmation
   before deleting the recorded experiment resources.

## Saved implementation

The implementation is committed on `dev-dongchen`, without a push:
`8f2b46d` (shared experiment protocol), `b6fa5f3` (cloud hosts),
`baede3a` (benchmark tools), and `ae6cdd3` (guarded deployment and smoke gates),
in addition to the earlier authentication/history/storage commits.
The remaining work is actual cloud validation, deployment, measurements and
the measured cost report. Those tasks are blocked, not marked complete.
