# Azure Deployment Plan

## Status

Re-preparing for the user's explicitly authorized account change on 2026-09-19.
The intended deployment subscription is now `18827cdf-a4e8-4316-a7ba-a4499139a3ec`
(`MCAP568125-E5-Sub`), tenant `4bc8a937-2b77-44c4-8790-dfc0068162f2`.
Retain Southeast Asia and the two experiment resource-group names. The user
explicitly authorizes Key authentication to the existing model/Search resources
in the former tenant for this experiment. Storage, Bots and benchmark identities
remain in the new tenant; no implicit authentication fallback is allowed.

Current preparation: verify new permissions/capacity, update subscription-bound
guards and cross-tenant Key dependency checks, add ACR cloud build/verification,
and verify stage-by-stage latency reporting before validation and real deployment.
Do not reuse the old subscription's previews as validation for the new target.
The dedicated benchmark API and service principal have been created in the new
tenant. Both experiment resource groups now exist. ACA infrastructure and its
application have deployed successfully through actual ACR cloud build `cm4`;
the initial live probes returned health 200, unauthenticated Bot 401, and disabled
benchmark 404. This is not complete-answer acceptance or a performance result.

Empty Flex bootstrap initially failed on the unsupported worker-process setting.
After fixing it and verifying receipt-backed resources plus Microsoft Smart Alerts
auxiliary provenance, the empty bootstrap retry succeeded with status
`infrastructure_ready`. Actual quota remains unknown/deferred. No Functions code
or pressure-test workload has been published.
Earlier statements below describing an empty cloud are historical.

### Current live connectivity gate

Both storage accounts were actually changed to `publicNetworkAccess=Disabled`
by the inherited management-group `MCAPSGovDeployPolicies` initiative's
`StorageAccount_PublicNetwork_Modify` definition (effect `modify`). Actual
readback differs from the template's Enabled intent. Storage data roles are
correct, but the no-VNet hosts cannot reach Blob/Queue through that network gate.
The observer originally exited during private evidence-container validation,
before opening its listener; the attempted operator exec returned a cluster-exec
error rather than a start confirmation. No question was accepted in that attempt.

No policy, exemption tag, shared-key setting or anonymous-access control has been
weakened. The policy expressly supports `SecuredByPerimeter`. Preparation is now
evaluating Enforced Network Security Perimeter, protecting only the two owned
accounts, with only this subscription allowed inbound and existing narrow MI
RBAC retained. The user was unavailable for the network-option question; this
is a policy-compatible pragmatic preparation choice, not a claim of explicit
approval for a policy exception or deletion. Live policy/region/cost/readback
checks remain required before applying the new network resources.

Those network checks subsequently passed and the Enforced NSP was applied.
Actual five-resource receipts, sole subscription rule, both effective Storage
associations, `SecuredByPerimeter`, legacy Deny/no bypass/no exceptions and unchanged
identity-only data controls were verified. The observer then recovered to Ready/
Running without changing its data roles, confirming its private-container startup
validation can now succeed. Fresh current-source observer publication/deployment
is in progress before retrying the explicitly bounded functional question.

Microsoft.Network is Registered and the actual provider metadata lists Southeast
Asia for NSP. Official Storage documentation covers HTTPS Blob/Queue and
subscription rules for non-onboarded source services. No separate NSP retail
meter was returned; that is not a verified zero-cost claim. Both Log Analytics
workspaces still permit ingestion/query, so completed-stage collection is not
blocked by the same storage-only policy.

### Authorized staged validation (2026-09-19)

#### Scoped benchmark closure — Validated (2026-09-19)

The user authorizes a narrowly guarded, reversible closure without deletion:
preserve the ACA target's image, identities, secrets, other environment settings,
Teams ingress and min0; set only `BENCHMARK_ENABLED=false`. Disable the temporary
observer's ingress and deactivate its explicitly owned active revision(s), retaining
all resources, identities, results and the unresolved ninth cold-attempt evidence.
Do not use maxReplicas=0, alter RBAC/policy/Flex, change business/auth code, or start
another benchmark. Parent-owned summary/cost reports remain out of scope.

Before mutation, require fresh complete ARM replica snapshots and zero queue
counts, and prove the observer driver is stopped (or already has no replicas).
Preserve the deployed image/context/ACR provenance even though the cold plan has
expired; this is closure authority, not permission to start an expired plan.
Bind the exact intended control changes and active revision IDs to a WhatIf
confirmation. Verify unchanged unrelated settings and actual revision/replica
shutdown afterward. Cloud mutation/polling is bounded to 20 minutes. If proof or
readback fails, stop with current state; do not discard durable state or retry
unboundedly. Focused red/green tests and live validation proof are required.

Closure implementation validation: missing-feature contracts failed first.
The focused closure/observer ownership/checked CLI/wrapper set subsequently
passed **40 tests** using the configured `.venv` interpreter, and
`git diff --check` passed. No business, model, authentication, RBAC or Flex code
was changed. The closure plan is `deploy/out/plans/benchmark-close.json`.
Live ARM revision presentation differences (generated probes/image type,
ephemeral-storage/default fields and generated revision suffix) were reproduced
red, normalized only for revision identity comparison, and verified without
relaxing image/environment/CPU/memory comparisons. The final focused set passed
**42 tests**. Actual
`deploy/bench/Close.ps1 -ParametersFile deploy/out/plans/benchmark-close.json`
WhatIf passed ownership, immutable publication, permissions and two fresh
quiescence observations. It authorizes only observer ingress disable and revision
`ca-bench-cskd5yazfko42--0000007` deactivation, plus target benchmark flag false
with min0 and Teams ingress preserved. Exact confirmation:
`BENCH-CLOSE:aca:8cf1c6ed-743f-4f0b-ac18-df6267235e34:e3d7ca9304a7f283`.
Proof is retained in `deploy/out/bench/aca-cold/guarded-closure-preview-2.txt`.
No closure is claimed until the bounded Apply/readback completes.

Actual closure subsequently completed at `2026-09-19T08:06:45.534051Z`,
1,158.091 seconds from its first mutation (`07:47:27.442982Z`), within the
original 20-minute window. A guarded stale-read stop was followed by independent
exact-resource change-history review and fresh confirmation
`BENCH-CLOSE:aca:8cf1c6ed-743f-4f0b-ac18-df6267235e34:8ce8e2c670bf1861`.
The target benchmark flag is false; min0, image, unrelated settings and Teams
ingress are preserved. Rejection checks returned benchmark404/Teams401 without
running a workload. Observer ingress is disabled, revision0000007 is inactive,
and actual observer replicas are0; its state is `suspended`/autostartfalse.
A later independent ARM read found target replicas0 as well.
No resources, identities, RBAC, policy, Flex code or durable results were deleted
or changed outside the approved control scope. Cold remains8/10 partial and
attempt9's missing final remains unresolved. Details and exact commands are in
`deploy/reports/2026-09-20-en/aca-benchmark-closure.md`.
Final closure and shared-deployment regression command passed **65 tests**;
independent live ARM readback reconfirmed the closed observer and target min0
with actual target replicas0. HEAD was not changed.

- **Validated scope: staged resident-ACA concurrency.** Revision `4910058` passed
  a new real question, run `8bffe5c59e634a9c8dfecd293edb7a28`, including actual
  model HTTP dependency evidence (3 attempts, 0 throttled). The plan now escalates
  1/2/4/8/16/32/64/128 sequentially, with at least 50 terminal observations and
  180 seconds per completed level, bounded submission/drain and automatic stop on
  sustained throttling/backlog, host/driver failures or incomplete work. It does
  not promise that every level will run. Cloud publication `cmr` succeeded;
  App/WhatIf confirmation is
  `BENCH-APP:aca:8cf1c6ed-743f-4f0b-ac18-df6267235e34:2d11820e704b89e5`.
  Normal traffic remains default-off until explicit operator start.
- **Validated scope: model-throttle observation revision before concurrency.**
  The 100-question warm run finished 100/100 successfully (mean 8.177 s,
  P95 9.732 s) and remains a separate versioned cohort. Instrumentation commit
  `4910058` now records actual chat/embedding HTTP attempts and 429 responses,
  including retries, on the originating trace; no inference/request behavior
  changed. Parent verification passed 83 focused tests. Actual ACA WhatIf passed
  for artifact `31882ec48e652906874e3d5687bf0b675dfbb841d66531d84ee23a204cd7a4d0`,
  confirmation `APPLY:aca:8cf1c6ed-743f-4f0b-ac18-df6267235e34:80011333e7bc8aee`.
  Revalidate one full question on this revision before escalating load; do not
  silently pool its results with the earlier warm instrumentation revision.
- **Actual ACA functional gate passed.** Run
  `097f2af1d6ef456682431734cd717f1b` completed one real Agent/KB/model/history/
  rendered-callback question successfully. End-to-end was 14,875.278 ms, with
  two actual model rounds; raw private Blob evidence and four exact-correlated
  completion logs were exported without answer bodies. This is diagnostic smoke,
  not a warmed average or cold-start/capacity conclusion.
- **Validated scope: warmed resident-ACA sampling.** Operator-reviewed smoke
  evidence now permits the normal warm profile. The sealed plan uses three
  warm-ups, 100 warm observations, one outstanding question, only `kb-01`,
  empty history, and finite submission/request/drain limits. Cloud publication
  `cmj` passed and App/WhatIf returned
  `BENCH-APP:aca:8cf1c6ed-743f-4f0b-ac18-df6267235e34:86e5ca4cbd7988d4`.
  App deployment is in progress, with autostart still false.
- **Validated scope: policy-compatible Enforced NSP connectivity.** Actual
  network WhatIf passed current subscription/tenant, both ownership ledgers,
  storage/RBAC readback, Southeast Asia support, permissions, compilation, ARM
  validation and exact five-resource preview. Cost evidence fingerprint
  `e2aacb2a358609226898292866e324738c20d95aae9a5f453c97f268d1777863`
  was reviewed at `2026-09-19T00:35:10.7039778Z`; no separate meter was returned
  and zero cost is **not** claimed. Existing-service and unpublished-meter risk
  remain explicit. This adds no VNet, private endpoint, identity or broad RBAC.
  The subsequent displayed patch affects only the two owned accounts: NSP-secured
  access plus legacy Deny/no bypass/no IP or service exceptions. No policy or
  exemption tag is changed. Confirmation:
  `NETWORK:aca:8cf1c6ed-743f-4f0b-ac18-df6267235e34:84ba8f6f4f9b2095`.
  Parent tightened unknown-resource collision checks (3 red cases, then 22
  network template/workflow tests passed). Data-plane validation is still
  `not_run`; association/rule/effective-configuration readback is mandatory.
- **Validated scope: host the default-off observer.** Revised current-source
  image cloud run `cma` succeeded, including offline Linux probe and digest
  readback. Actual App/WhatIf passed; existing observer resources were reported
  as `Deploy` by Azure ResourceIdOnly WhatIf, now accepted only for previously
  recorded observer IDs (14 guard tests passed). Main ACA resources remain Ignore.
  App confirmation:
  `BENCH-APP:aca:8cf1c6ed-743f-4f0b-ac18-df6267235e34:9b21d192356aa390`.
  Image:
  `cradvisorttbkwagaatnyc.azurecr.io/advisor-bench@sha256:dcf69b00ff1729aac76f2010f67c2eb2a4dca2dac274f797048d2394eae8b00c`.
  No autostart is enabled. A harmless actual ARM-authorized exec of
  `python --version` on the owned ACA target succeeded in this environment;
  the documented exec command is usable here without additional privileges.
- **Validated scope: observer cloud publication for one functional question.**
  Actual Publish/WhatIf verified the owned registry/environment/storage/API,
  current two-wheel source/lock, three MI bindings, and resident ACA artifact/
  configuration. Plan profile is `cloud-smoke`, concurrency 1, one `real_agent`
  request, no prior smoke claim, max submission 600 seconds. Confirmation:
  `BENCH-PUBLISH:aca:8cf1c6ed-743f-4f0b-ac18-df6267235e34:d0a3c29a2ffe843f`.
  Context digest `bbb52677f9aca54fe0f7cab12db722cb65e7a3fefba89a890317dd61b463893e`.
  The observer still requires separate App deployment and explicit start;
  publication itself does not start traffic.
- **Validated scope: resident ACA with isolated benchmark access.** The actual
  three MI grants succeeded and were read back. The updated ACA WhatIf passed
  with min replicas 1, only the driver principal/client allowed to submit, and
  the fixed planned observer `/bench/replies` callback. Confirmation:
  `APPLY:aca:8cf1c6ed-743f-4f0b-ac18-df6267235e34:c3799e89b66d826f`.
  This prepares functional smoke; it is not permission to claim measured answers
  or run the normal stress profiles before actual smoke succeeds.
- **Validated scope: three benchmark workload grants.** Actual
  `Ensure-BenchmarkIdentity.ps1 -Stage Grant -Mode WhatIf` verified the created
  driver/ACA/Flex UAMIs against ownership ledgers and purpose tags, the API's
  v2 role configuration and current directory permissions. No unrelated grants
  were present. Confirmation:
  `APPLY:benchmark:66891dbb-d7a7-4dfa-84a7-9dcbcefca117:9aa91bfeb5bff228`.
  Integrated deployment/report/IaC regression now passes 623 tests with 5 skips.
- **Validated scope: observer identity/bootstrap only.** Actual
  `bench/Deploy.ps1 -Stage Bootstrap -Mode WhatIf` passed ownership, permission,
  ARM validation and exact-resource preview. Creates one driver UAMI, the private
  `benchmark-results` container in existing ACA storage, AcrPull and container
  Blob Contributor assignments; no application/ingress or extra storage account.
  Confirmation:
  `BENCH-BOOTSTRAP:aca:8cf1c6ed-743f-4f0b-ac18-df6267235e34:a0d9d1e7397657de`.
- **Validated scope: ACA bootstrap and guarded ACR publication.** The current
  new-subscription ACA WhatIf passed after explicit Key dependency probes,
  provider/permission checks and ARM validation. Cloud publication contracts
  passed; source is sealed from commit `305ae74` (323 focused host/worker tests).
  Artifact digest `b05a18399304d07e1606a7d23f8975ccda3593dd17e003017b54c335906e311a`;
  confirmation `APPLY:aca:8cf1c6ed-743f-4f0b-ac18-df6267235e34:df669bee926503fb`.
  Apply must stop before publication if the newly created environment's core
  quota is insufficient, and must not deploy an app image unless the actual ACR
  task, offline Linux probe and immutable-digest readback all succeed.
  These gates subsequently passed: environment consumption quota is 500 cores
  (0.25 currently observed), base run `cm3` and build/probe/push run `cm4`
  succeeded. The immutable image is
  `cradvisorttbkwagaatnyc.azurecr.io/advisor@sha256:e61eb688e7beebfaa869651505fbee1024ec35a84b499667d240bd6420661978`.
  Actual deployment endpoint:
  `https://ca-advisor-ttbkwagaatnyc.agreeablebay-c149451d.southeastasia.azurecontainerapps.io`.
  Source/artifact/base/task/probe provenance is in
  `deploy/out/runs/aca-ownership.acr.json`, separate from the package manifest.
- **Validated scope: empty Flex bootstrap.** On the new subscription,
  `deploy/functions/Deploy.ps1 -Mode WhatIf -BootstrapOnly` passed live runtime,
  provider, ARM permission, ARM validation and WhatIf checks. The phase is
  `empty-infrastructure`, quota is explicitly `unknown_deferred`, and no code
  will be published. Confirmation:
  `APPLY:functions:aadc7e74-614d-47ea-9675-5dee5fcebee1:9c2aa4256b44c1ec`.
  Artifact manifest digest:
  `643155d276c41c57e618caf35c1b8a40c694fbf606b155a0ed8e07391224f764`.
  Cloud-publication/bootstrap/new-Key contracts passed 37 focused tests.
  The prior directory-registration scope is also Validated; full application
  publication remains unvalidated until actual capacity and image gates pass.
- **Validated scope: dedicated benchmark API registration only.** Actual
  `Ensure-BenchmarkIdentity.ps1 -Mode WhatIf -Stage Register` passed for the new
  tenant. Microsoft Graph reports the active Global Administrator role; after
  the user's portal elevation, ARM reports effective `Microsoft.Authorization/*`.
  The registration creates only the tagged single-tenant API/service principal
  with `Benchmark.Invoke`, no client secret or Graph permission grant to the app.
  Its returned confirmation is
  `APPLY:benchmark:66891dbb-d7a7-4dfa-84a7-9dcbcefca117:d8ea50b3e785dea7`.
  Apply subsequently succeeded: API client `7e23d083-9182-48ea-aa97-37aaa5d34164`,
  service principal `6c8a1f0a-b3c5-49a5-a669-a53fd04f8730`; ownership is in
  `deploy/out/runs/benchmark-ownership.json`. No workload grants exist yet.
  Readback confirms single-tenant/v2, exactly `Benchmark.Invoke`, mandatory
  app-role assignment on the service principal, and no password credentials.
- New-context configuration/ownership/key guards passed 69 focused tests.
  Actual cross-tenant Key probes passed: embedding 3072 dimensions, model
  completion endpoint, and the Search index's 3072-dimensional schema. This is
  not new-host application verification; old-tenant ARM/RBAC is not accessed.
- **Application deployment remains preparing**, not globally Validated:
  ACR remote build and final artifact/stage telemetry changes are in progress.
- The user explicitly approved empty Flex bootstrap to obtain actual quota:
  512 MB, no Always Ready, no code publication and no benchmark traffic. The
  bootstrap phase must still pass runtime/ARM/policy/ownership validation;
  actual regional quota is deferred, never treated as passed. Full publication
  and load tests remain gated by the real quota diagnostic.
- Required provider readiness: Microsoft.App/Web/Storage/ContainerRegistry/
  BotService/ManagedIdentity/Authorization/Resources/Insights are registered.
  Microsoft.OperationalInsights was initially unregistered and was explicitly
  registered in the approved new subscription; actual readback is Registered.
  Guarded scripts still do not automatically register providers.

### Prior verified baseline

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
- Current subscription: `18827cdf-a4e8-4316-a7ba-a4499139a3ec`.
- Current tenant: `4bc8a937-2b77-44c4-8790-dfc0068162f2`.
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
- Existing cross-tenant OpenAI/Search dependencies use explicitly approved Key
  mode for this experiment. New storage/Bot/benchmark identities remain managed
  identities. No silent authentication fallback or old-tenant RBAC mutation.
- ACA and observer images are to be built and verified in ACR Tasks rather than
  requiring a local Docker daemon; only approved artifact inputs may be uploaded.
- Reports must include actual full-link stage latency, with missing observations
  explicit and overlapping spans never summed into a fabricated end-to-end total.
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

New-tenant scoped validation on 2026-09-19:

| Scope | Actual check | Result |
|---|---|---|
| Directory registration | Register/WhatIf, then approved Apply/readback | Passed; isolated API/SP created, no secrets or workload grants |
| External dependencies | Explicit Key embedding/chat/index schema calls | Passed 3072 dimensions and reachable configured model/Search; no old-tenant ARM |
| Empty Flex bootstrap only | `Deploy.ps1 -Mode WhatIf -BootstrapOnly` | ARM validate/WhatIf and prerequisites passed; quota remains unknown/deferred; no publication authorized |
| Changed guard workflows | `test_remote_acr`, `test_cloud_publication`, `test_flex_bootstrap`, `test_cross_tenant_keys` | 37 passed; actual ACR runs still pending |

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

See the [experiment status report](../deploy/reports/2026-09-20-en/summary.md) for the distinction
between verified local behavior and all still-unmeasured cloud outcomes.
