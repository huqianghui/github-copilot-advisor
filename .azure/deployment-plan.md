# Azure Deployment Plan

## Status

Local implementation, integration and final target packaging are complete.
Durable recovery and guarded deployment scripts passed their scoped reviews;
native Functions/benchmark smoke passed with offline JWKS/model/Teams boundaries.
The full deployment-tool run passed 357 tests with 5 skips; the application run
has one documented, unchanged Windows timing-test failure.
The final metadata/packaging/smoke regression passed 179 tests; the guarded
deployment implementation is committed as `ae6cdd3`.
Actual ACA ARM validation/WhatIf passed. Azure validation remains **blocked and
not Validated**: final Linux image verification, benchmark directory permission,
and fresh Flex capacity evidence are missing. No provisioning or cloud load tests
were executed.
This document must not be interpreted as approval to skip deployment validation.

## Approved design

- [Cloud deployment and experiments design](../docs/superpowers/specs/2026-09-17-cloud-deployment-experiments-design.md).
- Mode: MODIFY an existing Python/uv workspace.
- Recipe: Bicep and PowerShell; deployment artifacts belong under `deploy/`.
- Region: `southeastasia`.
- Confirmed subscription: `adca540e-984c-4cab-bc86-5d38bf979841`.
- Confirmed tenant: `16b3c013-d300-468d-ac64-7eda0820b6d3`.
- Proposed resource groups: `rg-copilot-advisor-aca-exp` and
  `rg-copilot-advisor-flex-exp`.

## Existing resources to reuse

- Model account: `cdc-ai-foundry-southeastasia-res` in `rg-ai-services-all`.
- Search: `cdc-playground-ai-search` in `rg-playground-aiservices`.
- Chat and same-name `text-embedding-3-large` deployments are present.
- Existing Search vector schema has 3072 dimensions.
- Initial checks established metadata compatibility. A later local read-only
  data-plane smoke using the configured credentials passed: embedding output
  3072 dimensions, a hybrid/semantic Search result from the 1639-document index,
  and a nonempty chat completion. This does not validate cloud managed identities
  or the deployed application.
- Do not modify models, indexes, existing local Bot configuration or `.env`.

## Execution boundaries

- Preserve the original local process and memory mode.
- Both cloud hosts use durable task queues and shared per-environment state.
- Preserve tenant/conversation/user isolation; retain messages for 30 days and
  select only the latest 20 history messages per partition for each model turn.
- Cloud Azure dependencies use managed identity for the main experiments and
  also support explicit key mode. No silent authentication fallback.
- Separate experimental Bots and identities; no production-group load traffic.
- No monetary ceiling was set, but experiments must remain bounded and staged.
- Inspect policy, quota, RBAC, artifact integrity and IaC changes before deploying.
- Run Azure validation before deployment; cloud smoke tests precede benchmarks.
- Cleanup requires explicit scope confirmation; retain unrelated resources.

## Implementation plan

- [Task-by-task implementation plan](../docs/superpowers/plans/2026-09-17-cloud-deployment-experiments.md).
- Tasks 1-7: explicit credentials, history retention, leased Blob state, jobs,
  recoverable queue notifications and ordered workers.
- Tasks 8-14: Teams preparation/delivery, native Functions and ACA lifecycles,
  authenticated benchmark entry, packaging, IaC and guarded scripts.
- Tasks 15-18: load generation, local/cloud verification, measured experiments,
  reports and confirmed cleanup.
- This is an implementation plan, not a completed Azure capacity validation.

## Planned artifacts and resource inventory

| Artifact | Purpose |
|---|---|
| `deploy/shared/Preflight.ps1` | Config, artifact, quota, RBAC and policy checks |
| `deploy/shared/Package.ps1` | Locked online-only packages |
| `deploy/aca/Deploy.ps1` | ACA WhatIf/Apply |
| `deploy/functions/Deploy.ps1` | Flex WhatIf/Apply and supported zip deployment |
| `deploy/aca/Set-Mode.ps1` | Resident versus scale-to-zero |
| `deploy/bench/Run.ps1` | Bounded same-region experiments |
| `deploy/Cleanup.ps1` | Preview and confirm exact owned-resource deletion |

The PowerShell entry points in this table now exist and are undergoing integration
verification. The target and shared Bicep modules passed local compiler-contract
tests. Their existence or test results are not authorization/capacity evidence.

| Resource | Planned count | Initial configuration |
|---|---|---|
| Experimental resource groups | 2 | Named above, Southeast Asia |
| ACA managed environment | 1 | Consumption workload profile |
| Advisor Container App | 1 | 0.25 vCPU/0.5 GiB, min replicas 0 or 1 |
| Flex plan and Function App | 1 each | FC1, Python 3.12, 512 MB, no Always Ready |
| Storage accounts | 2 | Standard LRS, private Hot Blob and Queue |
| Experimental Azure Bots | 2 | F0, independent identities |
| Application user-assigned identities | 2 | One per target |
| Basic ACR | 1 | New experiment-owned registry, not an existing registry |
| Temporary load-driver Container App/identity | 1 each | Isolated from target replicas |
| Benchmark Entra API registration | 1 | Separate audience and application role |
| Monitoring | Minimal per-host resources | Explicit retention and no payload capture |

An existing registry was found, but reuse was not authorized while the user was
unavailable. Use the already-approved new Basic ACR fallback; do not modify
existing registries. Its fixed cost must appear separately in the report.

## Capacity observations, not deployment approval

Read-only observations on 2026-09-17:

| Resource metric | Current / limit | Planned result |
|---|---|---|
| Southeast Asia ACA environment count | 0 / 50 | 1, within the observed count limit |
| Southeast Asia Storage account count | 4 / 250 | 6, within the observed count limit |
| Generic Microsoft.Web quota | Inapplicable Total Regional VMs metric | Does not validate Flex memory |
| ACA environment consumption cores | New environment does not exist | Must inspect before scale tests |

The missing Azure CLI quota extension was installed after the initial quota
command failed. No quota increase or provider registration was requested.
Flex-specific capacity, policies, directory permissions and all data-plane RBAC
remain explicit execution gates, rather than assumed passes.

## Validation sequence

1. Run the task-specific tests and the final local workspace regression.
2. Compile the two target IaC templates and benchmark template.
3. Validate packages and inspect the credential-free artifact manifest.
4. Validate current subscription, region, policy, quota and required permissions.
5. Preview the resource-group shell and target templates without applying them.
6. Mark preparation `Ready for Validation` only after artifacts exist; invoke
   Azure validation and resolve blockers before deployment.
7. Deploy through the deployment workflow, then verify actual settings and data
   plane behavior. Only then run bounded benchmarks.
8. Publish measured reports, close benchmark access, restore ACA min replicas 0,
   and request confirmation for deletion of specific experimental resources.

## Verification state

| Gate | State |
|---|---|
| Design approval | Complete |
| Implementation plan | Execution authorized on 2026-09-18 |
| Application changes and local regressions | Hosts/benchmark integration committed as b6fa5f3; 268 focused host/telemetry tests passed; full application run has the recorded existing typing timing failure |
| Infrastructure/package generation | Final ACA context/Functions ZIP built and checked against current source/lock; 49 compiler contracts passed; final Linux image verification blocked |
| Azure policy, quota and permission validation | ACA preview and existing dependency checks passed; benchmark directory permission and Flex capacity remain unverified |
| Azure deployment validation | ACA ARM validation/WhatIf passed; Flex preflight stops at the genuine regional-capacity evidence gate; no ARM Apply |
| ACA and Flex provisioning | Not started |
| Cloud functional verification | Not started |
| Latency/concurrency experiments and reports | Not started |

## 7. Validation Proof

These observations are partial gates, not deployment approval.

| Command/check | Observed result | Scope |
|---|---|---|
| `az account show` with subscription/tenant/state projection | Expected subscription and tenant, Enabled | Read-only context; no account switch |
| Existing `dependency_checks` helper with actual `.env`, explicit ARM IDs and Entra overlays | Passed dependency identity/region/endpoint, role-write permission, model metadata and Search 3072-dimensional schema checks | CLI user's credentials, not application UAMIs; private scratch removed; no resources changed |
| ARG Function App/plan inventory | Existing FC1 app in Southeast Asia identified for quota lookup | Read-only; no existing resource modifications |
| `GET .../sites/cdc-test-dify-feishu/detectors/FunctionFlexQuota?api-version=2024-11-01` | Returned region-selection form only | No numeric quota evidence |
| AppLens read-only quota diagnostic | 403 AccessDenied at 2026-09-18T10:35:17Z | Tool could not obtain quota evidence; not proof quota is zero |
| `Ensure-BenchmarkIdentity.ps1 -Mode WhatIf -Stage Register` | `graph_directory_role_unverified` | No directory registration or role grant created |
| `docker version --format '{{json .Server}}'` | Linux engine HTTP 500 on API v1.56 | Final image build/import proof blocked; no service changes |
| `pytest deploy\tools\tests\test_package.py -q` | 52 passed | Includes current explicit dataset/UTF-8/size/hash rules and real locked context builds, not a Linux image build |
| Native Core Tools + isolated Azurite final smoke | Six functions; health 200, unauthenticated messages 401, ignored traffic unstored, benchmark 201/202 and full local callback without model calls, then one Teams fixture reply/dedupe | JWKS/model/Teams offline; unknown platform identity omitted; children stopped; not actual Azure/Teams |
| `pytest ...test_quarantine_regressions.py ...test_jobs.py ...test_maintenance.py` | 78 passed memory and 78 actual Azurite after e242443 | Terminal repair scoped review PASS; not managed-identity proof |
| `pytest deploy\tests -q` | 49 passed | All three template entries compiled, including benchmark bootstrap/app split |
| Full offline shared/agent/Teams/ingestion regression | 1238 passed, 1 failed, 19 integration deselected, 19 existing warnings | Unchanged typing timestamp assertion; isolated typing file 21 passed; Windows loop clock 15.625 ms versus test's 10 ms interval; not recorded as all-green |
| `pytest deploy\tools\tests -q` | 357 passed, 5 skipped | Full tool regression before the subsequent real-CLI metadata shape fix |
| Flex metadata/workflow regression after the live-shape fix | 55 passed | Python 3.12 FC1 bounds and memory read from the same `sku` record; no regional quota default substituted |
| Final metadata/workflow/package/smoke regression | 179 passed | Covers the current metadata repair, dataset packaging and root smoke CLI together |
| `Deploy.ps1 -Mode WhatIf` against the final ACA artifact | Passed ARM validation and preview | Planned bootstrap/build/push/app stages only; no mutations |
| Final Functions `Preflight.ps1` against live runtime metadata | `flex_capacity_evidence_unconfirmed` | Runtime parsing corrected; actual remaining regional capacity still not supplied |
| `az group exists` for each named experiment group | Both false | No experimental resource groups created by these validations |

Final local artifacts, generated from the explicitly supplied `.env` with Entra
metadata overlays and approved dataset, are under ignored `deploy/out/`:

- `aca-final-context`: manifest digest
  `b15fb62d09124aa7941176d88809a450042b0d9dfaa5d48cb0856fe5c926596e`.
- `functions-final.zip`: manifest digest
  `04e5d0da5bdb25b1b4d71a1f51f0778d498040ed1c6b3ca856f3ae5a942d6b5c`.
- Both contain exactly the three online wheels. Current source, dependency
  metadata, locked requirements, approved configuration and dataset hashes match.
- The ACA base digest is not resolved and Linux import verification is false.
  Functions has not undergone Azure remote build or deployed-runtime verification.

Before status can become `Validated`, resolve the three live blockers, preserve
the documented regression limitation, rerun the relevant current-source checks
and complete Flex ARM validation/WhatIf. After approved bootstrap, inspect the new
ACA environment's actual core quota before publication. Deployed UAMI data access,
full answers, restored history and actual Teams clients are later acceptance
gates, not implied by a template preview.

Flex `maximumInstanceCount` applies per function scale group, not to the combined
HTTP/Queue/Timer instance count. Capacity projection must account for these groups
and does not reserve quota. Published regional default quota is not evidence of
the subscription's current remaining capacity.

See the [experiment status report](../deploy/reports/summary.md) for the distinction
between verified local behavior and all still-unmeasured cloud outcomes.
