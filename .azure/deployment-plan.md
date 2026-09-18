# Azure Deployment Plan

## Status

Executing the approved implementation plan. Durable storage/worker recovery has
passed scoped review and real SDK/Azurite contracts. Native Functions host smoke
has passed with offline JWKS/model/Teams boundaries. Final benchmark/host/script
integration and whole-workspace verification remain in progress.
Read-only Azure readiness checks have begun and found blockers; the plan is
**not Validated**. Provisioning and cloud load tests have not started.
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
| Application changes and local regressions | Durable recovery accepted through e242443; native Functions offline-boundary smoke passed; final benchmark/runtime integration and full regression pending |
| Infrastructure/package generation | IaC 40 compiler contracts passed; explicit dataset packaging 52 tests passed; final current-source Linux image verification blocked |
| Azure policy, quota and permission validation | Partial read-only checks; directory permission and Flex quota unverified |
| Azure deployment validation | Not completed; no ARM Apply |
| ACA and Flex provisioning | Not started |
| Cloud functional verification | Not started |
| Latency/concurrency experiments and reports | Not started |

## 7. Validation Proof

These observations are partial gates, not deployment approval.

| Command/check | Observed result | Scope |
|---|---|---|
| `az account show` with subscription/tenant/state projection | Expected subscription and tenant, Enabled | Read-only context; no account switch |
| ARG Function App/plan inventory | Existing FC1 app in Southeast Asia identified for quota lookup | Read-only; no existing resource modifications |
| `GET .../sites/cdc-test-dify-feishu/detectors/FunctionFlexQuota?api-version=2024-11-01` | Returned region-selection form only | No numeric quota evidence |
| AppLens read-only quota diagnostic | 403 AccessDenied at 2026-09-18T10:35:17Z | Tool could not obtain quota evidence; not proof quota is zero |
| `Ensure-BenchmarkIdentity.ps1 -Mode WhatIf -Stage Register` | `graph_directory_role_unverified` | No directory registration or role grant created |
| `docker version --format '{{json .Server}}'` | Linux engine HTTP 500 on API v1.56 | Final image build/import proof blocked; no service changes |
| `pytest deploy\tools\tests\test_package.py -q` | 52 passed | Includes current explicit dataset/UTF-8/size/hash rules and real locked context builds, not a Linux image build |
| Native Core Tools + isolated Azurite smoke | Four functions, health 200, unauthenticated messages 401, ignored traffic unstored, one fixture reply, dedupe | JWKS/model/Teams offline; children stopped; not actual Azure/Teams |
| `pytest ...test_quarantine_regressions.py ...test_jobs.py ...test_maintenance.py` | 78 passed memory and 78 actual Azurite after e242443 | Terminal repair scoped review PASS; not managed-identity proof |

Outstanding recipe checks before status can become `Validated`:

- [ ] Final application build, complete offline regression and artifact integrity.
- [ ] Current Bicep compilation and ARM template validation.
- [ ] Current WhatIf preview for the intended parameters.
- [ ] Azure Policy, applicable quota and required permission validation.
- [ ] Static role review for every final identity/data-operation relationship.
- [ ] Resolve the directory-permission, Flex-capacity and Linux-build blockers.

Flex `maximumInstanceCount` applies per function scale group, not to the combined
HTTP/Queue/Timer instance count. Capacity projection must account for these groups
and does not reserve quota. Published regional default quota is not evidence of
the subscription's current remaining capacity.

See the [experiment status report](../deploy/reports/summary.md) for the distinction
between verified local behavior and all still-unmeasured cloud outcomes.
