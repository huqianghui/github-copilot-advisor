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
| Native Functions host | Local smoke passed | Four functions discovered; unauthenticated request rejected; queued fixture completed once and duplicate suppressed |
| Model/Teams boundaries in native smoke | Fixtures | JWKS, model and Teams connector were offline substitutes |
| Artifact dataset packaging | Local tests passed | 52 package tests, including locked reproducible ACA contexts; explicit dataset opt-in |
| Final Linux image verification | Blocked | Docker Linux engine `/version` returned HTTP 500; earlier image checks do not cover the final runtime |
| Benchmark Entra identity permissions | Blocked | Read-only registration preview returned `graph_directory_role_unverified` |
| Flex regional capacity | Unverified | CLI quota detector requires region form input; no actual quota values collected; portal needs sign-in and AppLens returned 403 |
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

## Next execution gates

1. Complete host/benchmark/script integration and rerun the complete offline
   regression against the final source; regenerate both target artifacts.
2. Obtain a working final Linux build/import verification, confirm directory
   permission for the isolated benchmark API/grants, and collect actual fresh
   Flex regional capacity evidence. Do not use published defaults as proof.
3. Validate/preview IaC, then deploy through the guarded scripts. Verify actual
   settings, UAMI data access, full answers and restored history.
4. Run the approved same-region experiments and record measured reports.
   Close benchmark access, restore ACA min 0, and request explicit confirmation
   before deleting the recorded experiment resources.
