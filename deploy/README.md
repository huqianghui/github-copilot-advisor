# Guarded deployment and cleanup

These scripts are **definitions**, not deployment or application-validation
evidence. Their default is read-only `WhatIf`. They never select another
subscription, register providers, enable keys, relax authentication, or adopt
an unrecorded resource group/application.

For measured results, current resource disposition and remaining blockers, see
the [English experiment archive](reports/2026-09-20-en/summary.md)
([中文](reports/2026-09-20-cn/summary.md)). The
[benchmark closure workflow](bench/CLOSURE.md) disables benchmark access and
suspends the temporary observer without deleting resources or changing Teams
ingress; resource deletion remains a separate confirmed operation.

Prerequisites: PowerShell 7.4+, Azure CLI/Bicep, the configured workspace
`.venv`, and the Task 12 packaging tools. Select the image strategy explicitly:
`ACA_BUILD_MODE=acr` uses guarded ACR Tasks without local Docker;
`ACA_BUILD_MODE=local` (the compatibility default) requires a running local
Linux Docker engine. Neither strategy starts or repairs Docker.

Client `docker --version` output is not daemon readiness. If server-side
requests return HTTP 500 or time out (as observed on the Linux engine), remain
at WhatIf **for local mode**. ACR mode is an independently approved strategy,
not automatic fallback. No daemon restart, context switch, or global
API-version change is performed. Earlier successful artifact checks
are historical evidence, not proof of current daemon availability.
After validating the explicit Apply confirmation, local ACA performs a server-side
Linux-engine readiness check **before bootstrap creates or updates resources**;
it checks again before building. A readiness failure leaves Apply blocked.
The checked CLI resolves the local daemon once: `DOCKER_CONTEXT` takes
precedence over `DOCKER_HOST`, otherwise the saved context is used. Every
subsequent Docker command pins that validated Unix-socket/named-pipe endpoint
with `--host`, including commands using the private credential directory.
Changing `DOCKER_CONFIG` never selects another daemon; remote endpoints and
failed context resolution are rejected without fallback or global changes.

### Historical readiness observations (2026-09-18)

The parent ran read-only readiness probes; these are not deployment evidence:

| Gate | Observed result | Consequence |
| --- | --- | --- |
| Dedicated benchmark identity | `graph_directory_role_unverified` from Register/WhatIf | No directory creation or role grant is authorized by the scripts. |
| Flex regional capacity | AppLens quota diagnosis returned 403; detector GET returned only a form; portal requires sign-in | No measured limit/usage evidence has been accepted. Published defaults remain unusable. |
| Local Linux Docker | Server API returned HTTP 500 | Local-mode Apply remains blocked before bootstrap. Explicit ACR mode avoids this gate; cloud task/probe success is still required. |

On 2026-09-19 the operator explicitly switched to the subscription below.
The new account's directory and Azure role-assignment permissions passed live
checks; the isolated benchmark API registration now exists. ACR mode removes
the local Docker dependency but still requires a successful cloud build/probe.
The actual Flex regional quota remains a separate gate; the operator approved
empty, unpublished Flex bootstrap solely to obtain its diagnostic.

## Fixed boundaries

- Subscription: `18827cdf-a4e8-4316-a7ba-a4499139a3ec`; active CLI context must
  already match, in Azure public cloud.
- Tenant: `4bc8a937-2b77-44c4-8790-dfc0068162f2`.
- Region: `southeastasia`.
- ACA group: `rg-copilot-advisor-aca-exp`.
- Flex group: `rg-copilot-advisor-flex-exp`.
- ACA and Flex main templates are **subscription-scoped**. The benchmark
  template is **resource-group-scoped**, in the owned ACA group.
- Existing OpenAI/Search resources are dependencies, never cleanup targets.
  Existing local Bots and unrelated registries/registrations are not reused.
- The operator explicitly approved Key access to the two original model/Search
  resources in the former subscription. Only those exact external IDs are
  allowlisted, and only with explicit Key mode. Preflight checks their data-plane
  endpoints, embedding dimension and index schema without old-tenant ARM/RBAC.
  This does not authorize owning, mutating or deleting those dependencies.

`Get-AdvisorDeploymentArguments -Target aca|functions|bench` in
`shared/Azure.ps1` selects the correct ARM scope. Subscription deployments do
not receive a group `--mode` argument. Group create operations are incremental.
What-if uses JSON/resource-ID-only output, which is not echoed verbatim.

## Plan files are not raw ARM parameter files

Create a **non-secret** plan under `deploy/out/plans/`. It references the
explicit local environment file, verified artifact, and ownership ledger.
Paths are relative to the plan file unless absolute. Example `aca.json`:

```json
{
  "format": "advisor-plan-v1",
  "target": "aca",
  "run_id": "REPLACE-WITH-A-NEW-UUID",
  "env_file": "../../../.env",
  "artifact": "../aca-context",
  "manifest": "../runs/aca-ownership.json",
  "overrides": {
    "AZURE_SUBSCRIPTION_ID": "18827cdf-a4e8-4316-a7ba-a4499139a3ec",
    "ACA_BUILD_MODE": "acr",
    "AZURE_OPENAI_RESOURCE_ID": "/subscriptions/adca540e-984c-4cab-bc86-5d38bf979841/resourceGroups/rg-ai-services-all/providers/Microsoft.CognitiveServices/accounts/cdc-ai-foundry-southeastasia-res",
    "AZURE_SEARCH_RESOURCE_ID": "/subscriptions/adca540e-984c-4cab-bc86-5d38bf979841/resourceGroups/rg-playground-aiservices/providers/Microsoft.Search/searchServices/cdc-playground-ai-search",
    "AZURE_OPENAI_AUTH_MODE": "key",
    "AZURE_SEARCH_AUTH_MODE": "key"
  }
}
```

Use `load_deploy_config(..., overrides=...)` semantics: do not rewrite or upload
the complete `.env`. Local Bot settings are ignored; only allowlisted runtime
secrets enter the secure ARM `secretSettings` parameter. Plan overlays cannot
contain credentials. Configured budgets/logging are retained.

Artifacts must match the **current intended** lock, online source files,
workspace dependency metadata, approved YAML files, and host entry files.
Requirements are re-exported with `uv --locked --offline` and compared to the
artifact, including every local wheel hash. A stale image's previous import
flag is not proof about changed application code.

### Explicit benchmark dataset packaging

The parent-owned application packager accepts the optional keyword
`benchmark_cases_config: Path | None`. Supply it explicitly to package the
approved JSON bytes as `runtime-config/benchmark-cases.json`:

```powershell
.\.venv\Scripts\python.exe -m advisor_deploy package `
  --target aca --output deploy\out\aca-context `
  --escalation-config C:\approved\escalation.yaml `
  --diagnostics-config C:\approved\diagnostics.yaml `
  --benchmark-cases-config C:\approved\cases.json

# Equivalent wrapper option:
.\deploy\shared\Package.ps1 -Target aca -Output deploy\out\aca-context `
  -EscalationConfig C:\approved\escalation.yaml `
  -DiagnosticsConfig C:\approved\diagnostics.yaml `
  -BenchmarkCasesConfig C:\approved\cases.json
```

The same option applies to Functions ZIP packaging. No dataset is read or
included by default, no example-file fallback is permitted, and the online
closure remains exactly the existing **three** workspace wheels. Dataset
validation uses the shared version-1 contract with bounded UTF-8 JSON,
duplicate-key rejection, and the unchanged canonical dataset digest.

When `BENCHMARK_ENABLED=true`, the renderer rejects an artifact lacking the
dataset and includes `BENCHMARK_CASES_PATH` in its protected ARM runtime
settings: `/app/runtime-config/benchmark-cases.json` for ACA, or
`/home/site/wwwroot/runtime-config/benchmark-cases.json` for Flex. The fixed
installed path is not the local source path. Merely including the dataset in
an artifact does not enable benchmark routes. IaC supplies the matching path
and the exact deployed app resource ID as `BENCHMARK_TARGET_ID`.

The separate benchmark observer image is a different packaging contract;
its two-wheel closure must not broaden the application's three-wheel
allowlist.

```powershell
.\deploy\shared\Preflight.ps1 -Target aca -ParametersFile deploy\out\plans\aca.json
.\deploy\aca\Deploy.ps1 -ParametersFile deploy\out\plans\aca.json
.\deploy\functions\Deploy.ps1 -ParametersFile deploy\out\plans\functions.json
```

These examples do not create resource groups, identities, images, or directory
objects. Successful previews return an exact `confirmation` string containing
the target/run and artifact/configuration intent digest. For an operator-
approved execution, pass that value with `-Mode Apply -Confirmation VALUE`.
Changing the intended artifact/configuration requires another preview.

### Protected parameters and diagnostics

The PowerShell boundary creates a unique `deploy/out/.private-<uuid>` directory
with an owner-only DACL **at creation**, before writing any contents. Unix
uses mode 0700. Python rechecks the directory permissions before rendering.
All parameter/header/body files inherit that protection. Azure receives only
`@file` references, never secret command-line arguments.

CLI stdout/stderr are captured separately. Nonzero exit codes, empty/null
responses, malformed JSON, and known service errors fail the operation.
Diagnostics expose phase/service code/exit code, not raw exception bodies,
tokens, settings, or the entire environment. Exact private files/directories
are removed in `finally`; linked/reparse paths are rejected, not traversed.
The scripts do not recursively delete the workspace.

Use `ADVISOR_PYTHON_COMMAND` only to select an explicitly configured interpreter
when it is not the checkout's `.venv`. `ADVISOR_AZ_COMMAND` and
`ADVISOR_DOCKER_COMMAND` are executable-path overrides for isolated testing;
they are not imported from deployment configuration.

## Prerequisite and post-deployment gates

For the management-group Storage policy that accepts `SecuredByPerimeter`,
see the bounded [Enforced NSP preparation and deployment helper](network/README.md).
It protects only the two ledger-owned Storage accounts with one subscription
inbound rule, retains narrow MI data roles, and never reenables unrestricted
public access or adds a policy exception. Shared state Bicep now retains the
policy-compatible PNA intent on later target deployments. Network control-plane
readback remains separate from the parent's actual Blob/Queue and question
acceptance checks.

Preflight checks the active subscription/tenant, provider registration,
ownership collisions, effective ARM actions at required scopes, dependency
existence/region/auth configuration, model deployment metadata, and the Search
index's 3072-dimensional vectors. Search schema access uses the explicitly
selected Key or Entra mode; permission failure never triggers a fallback.
Index-schema access by the deployer does **not** prove deployed-UAMI data access.

Existing groups require the matching ownership manifest and
`advisorDeploymentId` tag. Unrecorded resources in an owned group block reuse
and group cleanup. New resources are recorded from ARM deployment receipts,
not adopted merely because they appear in a group. Partial failures retain
known IDs and pending/unverified role IDs; incomplete discovery blocks cleanup.

### Recovering a failed or partial ARM deployment

Failed deployments can have both `outputResources=null` and `outputs=null`.
Recovery then follows sanitized deployment **operation** targets, traversing
subscription- and group-scoped nested deployments. Only successful `Create`
leaf operations (or ARM output-resource receipts) establish ownership;
`Read`, `Action`, failed leaf writes and unrelated inventory entries do not.
Failed nested deployments are still traversed for successful children.
Partial discoveries persist even if a later receipt read fails.

Apply preserves its original CLI/provider failure. Ownership-tag restoration
or receipt-discovery failures are reported separately and recorded in the
non-secret `<ledger-stem>.recovery.json`, never substituted for that failure.

```powershell
.\deploy\shared\Reconcile.ps1 -OutputsFile deploy\out\runs\functions-ownership.json
# Inspect the preview, then repeat with:
# -Mode Apply -Confirmation <exact RECONCILE token>
```

Reconcile validates the active subscription/tenant, recorded run/group owner
and exact run-bound root deployment name. WhatIf changes neither Azure nor
the ledger. Apply performs the same **read-only Azure requests** and updates
only the local ledger after confirmation. It binds receipt-proven role IDs
to principal IDs read from receipt-owned UAMIs, without requiring root
outputs or synthesizing successful application outputs. Unrelated resources,
foreign scopes/principals and incomplete traversal remain blockers.
When only the inventory comparison finds extra resources, reconciliation can
still record the independently proven subset and validated roles. It lists
the unrecorded IDs, keeps `discovery_complete=false` and
`resume_allowed=false`, and never adopts those extras. An extra platform- or
policy-created action group is not owned merely because it appears in inventory.

One narrowly bounded provider auxiliary is supported: the exact
`microsoft.insights/actiongroups/Application Insights Smart Detection` ID in
the owned **Flex** experiment group and approved subscription. Reconciliation
requires fresh-group and Application Insights resource receipts, the global
enabled `SmartDetect` resource metadata, and an actual successful action-group
write after the ledger's creation time. It reads the bounded Activity Log
creation window and verifies the earliest successful writer through Graph:
application `3af5a1e8-2459-45cb-8683-bcd6cccbcc13`, owned by Microsoft's tenant
`f8cdef31-a31e-4b4a-93e4-5f571e91255a`. Display name alone is never identity proof.

Preview remains read-only. Confirmed local reconciliation atomically persists
`<ledger-stem>.provider-auxiliary.json` **before** adding that ID to the ledger.
The non-secret record binds the run, group, receipt-backed component, Activity
Log event/correlation/time and verified caller. Manifest loads and writes
require that bound record whenever this auxiliary is owned. This does not
permit spaces in other ARM paths or authorize any other action-group name,
group, subscription or caller. Missing/truncated activity, unavailable Graph
proof or unknown resources stay blocked. No generic adoption switch or Azure
mutation is introduced; future cleanup still needs its separate confirmation.

A failed empty bootstrap can resume with its original Functions plan and
`-BootstrapOnly` only after reconciliation completes and pending roles are
resolved. Preview again for a fresh bootstrap confirmation. Status remains
`failed` during reconciliation; resume must finish infrastructure readback
before it becomes `infrastructure_ready`. Full deployment still requires
actual quota evidence, and reconciliation never publishes code.

The existing resource-group Bicep module replaces tags. Without changing IaC,
the scripts restore the guard tag in `finally` after deployments to a group
whose ownership was already established. An abrupt process termination can
require **manual, read-only reconciliation** of ARM receipts and the tag;
missing ownership proof never means automatic adoption.

### Flex capacity

The Python 3.12 regional runtime metadata must admit the requested maximum
instance count and memory size. The guard selects the single top-level
`name=python`, `version=3.12`, `sku.skuCode=FC1` CLI record, checks its nested
runtime agrees, and reads that same SKU's `maximumInstanceCount` bounds and
`instanceMemoryMB` sizes. Nested GitHub metadata and other versions/SKUs
cannot supply capabilities. Malformed, inconsistent or ambiguous metadata
fails closed. This runtime-support check is distinct from regional quota.
Generic `Microsoft.Web` VM quota and the published default
Flex quota are not subscription capacity evidence.

A Functions plan additionally needs fresh, reviewed `capacity_evidence`:

```json
{
  "subscription_id": "adca540e-984c-4cab-bc86-5d38bf979841",
  "location": "southeastasia",
  "kind": "flex-regional-memory",
  "limit_mb": 0,
  "used_mb": 0,
  "observed_at": "REPLACE-WITH-ACTUAL-UTC-OBSERVATION",
  "source": "Flex Consumption Quota diagnostic"
}
```

Zeros are deliberately unusable placeholders, **not** a quota estimate. Obtain
actual values from the Flex Consumption Quota diagnostic or Microsoft support
(`source` may also be `Microsoft support confirmed quota`). Evidence expires
after one hour. Approve its canonical digest using
`advisor_deploy.render.digest(evidence)` and pass
`-CapacityEvidenceHash HASH` to Preflight/Functions Deploy. This is explicit
operator-approved evidence, not a cryptographic attestation or reservation.
An initial `FunctionFlexQuota` detector response containing only a form,
region selector, or submit input is not evidence of quota or usage. The gate
stays blocked until actual selected-region values are returned and reviewed;
a browser sign-in requirement does not justify substituting a default quota.
`maximumInstanceCount` applies **per function scale group**, not to the whole
application. The current entry has three groups: shared HTTP routes,
`process_queue`, and `maintenance`. The guard conservatively requires remaining
capacity for **3 × requested maximum instances × instance memory**. At the
40-instance/512-MB starting configuration this is 61,440 MB (30 equivalent
cores), not an assertion that this subscription has that quota.

Convert measured regional core quota/usage to equivalent MB using 2,048 MB per
core; do not substitute the published 250-core default for actual evidence.
The safe preflight report exposes the group list and projection. Revisit the
configured list whenever another independently scaled trigger is added.
This fresh-evidence headroom gate is conservative admission policy—not a
reservation, guaranteed capacity, or a strict total execution bound.

#### Explicit empty Flex bootstrap for quota diagnostics

If the subscription has no Function App on which to run the quota diagnostic,
the separately approved `functions/Deploy.ps1 -BootstrapOnly` phase can create
empty infrastructure. Preview with `-BootstrapOnly`; Apply must repeat that
flag and supply its exact phase-bound confirmation. Normal/full confirmation
is different and still requires fresh approved regional capacity evidence.

Bootstrap requires Python 3.12/512-MB support, benchmark endpoints disabled,
registered providers, ownership/ARM permissions and ARM validate/what-if.
It creates no code, always-ready instances or benchmark traffic, skips
dependency data-plane/model probes, and stops after receipt/RBAC and
runtime/identity/settings readback. It does **not** call `config-zip`.
Existing recorded Function Apps cannot use this new-empty-app phase.

The ledger ends at `infrastructure_ready`, not `deployed`. Its non-secret
`<ledger-stem>.bootstrap.json` companion records
`regional_capacity=unknown_deferred`, code/dependency probes `not_run`,
artifact/configuration bindings and the app ID for the real quota diagnostic.
This record is **not quota evidence**. Obtain the actual diagnostic, then
preview a normal/full deployment with `-CapacityEvidenceHash` before any code
publication. Bootstrap resources can incur costs and remain ledger-owned.

For a new ACA environment, its consumption-core quota is checked after the
approved bootstrap and before image publication/application startup. This
gate can leave owned bootstrap resources (including billable Basic ACR)
behind; the manifest records them for reviewed cleanup.

### Apply stages

ACA: create only the approved new group → subscription bootstrap with
`deployApp=false` → read back roles/environment quota → rebuild the verified
staged context against a recorded Python 3.12 base digest → verify Linux
imports/configuration rejection → push only to the newly owned registry → retrieve digest →
subscription application deployment using `repository@sha256:...`.

#### ACR cloud image strategy

Put `"ACA_BUILD_MODE": "acr"` in the plan's non-secret `overrides`. It is
approval-bound metadata, never an ARM runtime setting. Both WhatIf and Apply
avoid local Docker in this mode. Only approved Apply schedules quick tasks,
after owned registry bootstrap and capacity checks:

1. A Linux/amd64 quick task pulls official `python:3.12-slim` and inspects its
   resolved digest in ACR. No local engine or unapproved base-image fallback.
2. A second quick task builds from that immutable digest and the sealed
   allowlisted context. Application closure remains three workspace wheels;
   observer closure remains two. Hashed locked requirements are unchanged.
3. A dependent, non-root, **network-disabled** container checks Linux/amd64,
   payload hashes, imports, and rejection of missing configuration. Only its
   explicit success proof permits inspect/push steps. Build success alone is
   not Linux verification.
4. The guard verifies successful run ID/status, proof, image labels/ID,
   run-specific output-image digest and digest-addressed registry readback.
   `<ownership-ledger-stem>.acr.json` records source-file, artifact/lock/config,
   task/probe/log hashes, base/build run IDs, image ID and immutable reference.
   The original package/context metadata remains unchanged: this sidecar,
   not an old local `linux_imports_verified` flag, is cloud verification.

Only copied approved payload files and the generated task/probe definition
enter the private ACR source upload. The repository, environment file, ARM
private files, CLI credentials and runtime secrets never enter that context,
task variables or build arguments. No public pipeline/Git upload, registry
admin login, persistent scheduled task, new identity or role assignment is
introduced. ACR uses its existing quick-task registry access.

The caller needs registry-scoped quick-run/source-upload/run-read/log-read
actions and image readback permission; the guard checks these but grants
nothing. ACR Tasks permissions can confer substantial registry data-plane
power; use an approved least-privilege operator. Basic ACR, stored images,
source/log storage and cloud task compute can incur costs even when tests
fail. Each task has a 1,200-second service timeout; local polling is bounded.
Failure may leave owned bootstrap resources, run records, or an image whose
publication succeeded before readback failed. Reconcile those records; do not
claim deployment/application acceptance or silently retry through local mode.

References: [ACR multi-step YAML](https://learn.microsoft.com/azure/container-registry/container-registry-tasks-reference-yaml),
[quick-run CLI](https://learn.microsoft.com/cli/azure/acr#az-acr-run),
[Tasks permissions](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/containers#container-registry-tasks-contributor).

Flex: approved group/infrastructure → exact role readback → supported
`az functionapp deployment source config-zip --build-remote true` (Flex
OneDeploy/remote build) using the verified ZIP.

Readback checks identity, runtime/memory/scaling/concurrency, immutable image,
binding settings, model references, and forwarded settings. The manifest
always retains `application_validation: "not_run"`: data-plane propagation,
real model/Search/Storage smoke, full Q&A, Teams acceptance, and benchmarking
remain the parent's separate validation gates. No 202/health/IaC response is
treated as complete application acceptance.

## Dedicated benchmark API

`shared/Ensure-BenchmarkIdentity.ps1` defaults to `WhatIf`. Its separate
non-secret plan has this shape:

```json
{
  "format": "advisor-benchmark-plan-v1",
  "run_id": "REPLACE-WITH-A-NEW-UUID",
  "manifest": "../runs/benchmark-api.json",
  "principals": {
    "driver": {"manifest": "../runs/aca-ownership.json", "identity_id": "OWNED-BENCHMARK-UAMI-ARM-ID"},
    "aca": {"manifest": "../runs/aca-ownership.json", "identity_id": "OWNED-ACA-UAMI-ARM-ID"},
    "functions": {"manifest": "../runs/functions-ownership.json", "identity_id": "OWNED-FLEX-UAMI-ARM-ID"}
  }
}
```

1. Preview/apply `-Stage Register` (principals may initially be omitted).
   It creates one single-tenant application and service principal with v2
   tokens and the sole application role `Benchmark.Invoke`. No client secret,
   Microsoft Graph permission grant, or extra API registration is created.
2. `bench/Deploy.ps1 -Stage Bootstrap` provisions only the benchmark UAMI,
   private results container and two resource-scoped roles through the
   group-scoped benchmark template. It appends receipt-proven resources/roles
   to the ACA ownership ledger before requesting grants.
3. Preview/apply `-Stage Grant` with three distinct owned UAMIs. Their purpose
   tags must be benchmark/aca/flex. Exact assignment IDs are recorded.
   Unrecorded preexisting grants are blocked, not silently adopted.

Both stages require `-Mode Apply -Confirmation VALUE` to mutate directory
objects. Reuse is limited to the recorded, tagged application. A same-name
unowned registration is a collision. Graph scopes/application permissions
and, for delegated access, a supported active administrator role must be
provable; otherwise the script stops without relaxing authentication.

**v2 audience is the API application/client GUID**, not its Identifier URI.
The registration report returns `audience=<client-guid>` and
`scope=api://<client-guid>/.default`. Use the audience for both target and
receiver validation. A deployment enabling benchmark routes must reference
the owned API ledger with `benchmark_manifest`, prove all three grants, and
allow only the benchmark driver's principal/client IDs to submit.

### Observer bootstrap, immutable publication, and app deployment

`deploy/bench/Deploy.ps1` uses a separate non-secret plan:

```json
{
  "format": "advisor-bench-deploy-v1",
  "run_id": "SAME-UUID-AS-THE-ACA-OWNERSHIP-LEDGER",
  "environment_name": "copilot-advisor-bench",
  "aca_manifest": "../runs/aca-ownership.json",
  "functions_manifest": "../runs/functions-ownership.json",
  "api_manifest": "../runs/benchmark-api.json",
  "state_file": "../runs/observer-deployment.json",
  "context": "../benchmark-context"
}
```

Omit `context` for the initial bootstrap. The helper reuses only the existing
owned Basic ACR, ACA environment and ACA state account. It never creates
another account, retags the ACA group, or redeploys the main ACA template.
What-if rejects changes to main ACA resources and modifications to unrecorded
benchmark resources. ARM receipts are checked against the approved what-if
IDs. New role records bind to the **driver principal**, not the main app MI.

The stages are separate and default to `-Mode WhatIf`:

1. **Bootstrap**: render `enabled=true, deployApp=false`. Create the one UAMI,
   `benchmark-results` private container and its container-scoped Blob
   Contributor/registry-scoped AcrPull assignments. The results include real
   MI client/principal IDs and a planned stable app name, resource ID and
   endpoint derived from the existing environment's domain. No image or
   driver allowlist placeholder is required, and no app/ingress is created.
2. Use those actual identity values and planned endpoint to construct the
   bounded cloud RunPlan. Its callback is the planned endpoint plus
   `/bench/replies`. Set its operator client/principal allowlists to the
   **driver MI itself**; callback allowlists contain the two target app MIs.
   Grant the existing `Benchmark.Invoke` role to those three identities with
   `Ensure-BenchmarkIdentity.ps1 -Stage Grant`.
3. Build the isolated benchmark context using the harness's
   `bench/Build.ps1` / `advisor_deploy.bench.build` interfaces. The deployment
   helper verifies its exact two-wheel closure, current source/lock snapshot,
   dataset/Dockerfile bytes, intended target/config hashes, MI bindings,
   approved API audience/scope, existing storage and bounded RunPlan.
4. **Publish**: set `"build_mode": "acr"` in the benchmark deployment plan to
   preview the exact unbuilt context and cloud strategy. Apply performs the
   same cloud build/probe/push sequence above; no local image ID is required
   beforehand. The observer's cloud proof is saved alongside its deployment
   state as `<state-stem>.acr.json`. App rechecks that proof and live run/image
   readback. For explicit `"build_mode": "local"` (default), build the image
   first and preview its context digest and local image ID. Apply in local
   mode requires the returned confirmation, current Linux daemon readiness,
   image-ID/platform/entrypoint readback, and in-image hashes of the locked
   requirements, two wheels, RunPlan and dataset. Only then does it authenticate
   to the owned ACR via stdin and push that image ID. The digest emitted by
   push is checked using a digest-addressed registry query and recorded.
5. **App**: preview/apply only the recorded `repository@sha256:...`, with
   `deployApp=true`. It verifies one container/process/replica, identity,
   resources, endpoint, API auth/allowlists, storage settings and
   `BENCHMARK_AUTOSTART=false`. Receipt-backed additions are merged into the
   ACA ledger while its original app outputs/config summary remain unchanged.

Each Apply needs the exact confirmation returned by the corresponding
WhatIf. A sidecar stores bootstrap identity/endpoint and immutable image/plan
provenance; deletion authority remains in the ACA ledger. Partial or uncertain
receipt discovery is recorded and blocks reuse/cleanup until reconciled.
Docker failure leaves local Publish blocked. Cloud mode never falls back to
local publication, and its failed/missing Linux proof always blocks App.

### Starting the default-off observer without another app registration

`deploy/bench/Control.ps1 -ParametersFile PLAN -Action start|status` previews
the command; `-Mode Apply -Confirmation VALUE` validates approval and returns
`awaiting_interactive_exec`, not a fabricated remote execution result.
The operator needs ARM `Microsoft.App/containerApps/exec/action` on this owned
observer app. Azure CLI documents `containerapp exec` as an interactive
console. Open the exact owned app using the returned `exec_arguments` (which
include the approved subscription/group/app/container and `--command sh`),
then run the returned fixed command inside `driver-receiver`:

```text
python -m advisor_deploy.bench.control --plan /app/run.json start
```

Use `status` instead of `start` for its control status. The helper obtains the
driver UAMI's token for `api://<API-client-ID>/.default` and calls the existing
authenticated control route. The operator's Azure CLI token authorizes ARM
exec only; it is **not** treated as an application-role token. This uses the
existing driver role grant, with no fourth registration, MI, or app-role
grant. Control acceptance/status does not establish workload completion or
application acceptance. The guard always reports `control_invoked=false`;
the in-container helper's bounded JSON response is the evidence that control
was accepted. No unsupported unattended exec path, shell-input automation,
automatic retry, extra registration or additional MI is introduced.

## Mode changes and cleanup

`aca/Set-Mode.ps1 -OutputsFile OWNERSHIP.json -Mode Resident|ScaleToZero`
previews by default. Add `-Apply -Confirmation VALUE` only after reviewing the
token. It changes only this owned app's `minReplicas` (1 or 0), verifies other
template fields did not change, and records the control action for cold-start
evidence. A minimum of zero is not proof that the app actually reached zero.

`Cleanup.ps1 -OutputsFile OWNERSHIP.json` (or `-WhatIf`) previews exact scopes.
`OutputsFile` means the validated **ownership manifest**, not raw ARM outputs.
Deletion requires `-Mode Apply -Confirmation VALUE -TrafficStopped` and
`-InFlightDecision drain|abandon`. It deletes only recorded, verified role
assignments, directory objects/grants, and owned experiment groups. Foreign
resources/grants, changed roles, malformed ledgers, or incomplete discovery
block deletion. Existing model/Search resources are never deleted.

Ownership manifests are non-secret local records, **not signed attestations**.
Protect them as operational records and inspect previews. Loss or uncertain
partial directory-operation outcomes require explicit reconciliation; do not
invent missing ownership IDs or reuse another registration to continue.

## Offline verification

```powershell
.\.venv\Scripts\python.exe -m pytest deploy\tools\tests\test_deploy_manifest.py `
  deploy\tools\tests\test_powershell_contracts.py deploy\tools\tests\test_deploy_workflows.py -q
```

Tests execute real PowerShell entrypoints with fake Azure/Docker executables,
record argv/effects, inspect private-file cleanup, and exercise malformed
ownership, permission/capacity gates, subscription/group scope, WhatIf,
confirmation, publishing definitions, and deletion boundaries. No test
performs a real deployment, registration, grant, push, or deletion.
