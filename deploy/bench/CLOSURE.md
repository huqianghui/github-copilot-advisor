# Guarded benchmark closure (no deletion)

Use `Close.ps1` to close a **quiescent**, owned ACA benchmark experiment while
retaining its normal Teams endpoint, all resources, identities and durable data.
It never sets maxReplicas to zero, starts a workload, changes RBAC/policy/Flex,
or repairs application code.

```json
{
  "format": "advisor-benchmark-close-v1",
  "aca_manifest": "../runs/aca-ownership.json",
  "observer_state": "../runs/observer-deployment.json",
  "observer_context": "../observer-cold-context"
}
```

Save the non-secret plan under `deploy/out/plans/benchmark-close.json`.

```powershell
.\deploy\bench\Close.ps1 -ParametersFile deploy\out\plans\benchmark-close.json
# Inspect the exact actions/quiescence evidence and returned confirmation.
.\deploy\bench\Close.ps1 -ParametersFile deploy\out\plans\benchmark-close.json `
  -Mode Apply -Confirmation '<exact BENCH-CLOSE confirmation>'
```

The guard:

1. Verifies subscription/tenant, ACA group ownership, exact main/observer app and
   identity/image bindings, sealed historical observer files and live ACR
   publication evidence. An expired plan can prove which image is being closed;
   it never authorizes a new run. Current-source rebuild is intentionally not a
   shutdown prerequisite.
2. Requires two fresh complete ARM observations, 30 seconds apart: target
   replicas0, incoming-queue approximate count0, and an observer driver that is
   not started/not ready (or no observer replicas). Healthy/possibly active
   drivers, unknown counts, foreign pagination and changing revisions block
   closure. The queue count alone is not quiescence proof.
3. Disables **only observer ingress**, then changes only the target's literal
   `BENCHMARK_ENABLED` environment value to `false`. It compares the full
   protected configuration/template/identity snapshots in memory, including
   unrelated environment values and secret references; reports contain hashes,
   not those values. Target min0 and Teams ingress remain unchanged.
4. Deactivates only the explicitly owned observer active revision IDs included
   in the approval. Deactivation is reversible and stops revision replicas; no
   app/revision/resource is deleted. Observer AUTOSTART remains false. Its
   configured min1 is not interpreted as residency once all revisions are
   inactive; actual replica lists must show zero.
5. Sends only two unauthenticated empty-object verification requests to the
   target: benchmark messages must return404 and the preserved Teams endpoint
   must return401. These are rejection checks, not benchmark/Teams workload or
   answer-quality claims. They can briefly wake the min0 target. Its actual
   replica count is reported rather than forcibly shutting down the preserved
   Teams service; observer replica shutdown is mandatory.
6. Reads back active revisions, replica lists, feature flags, unchanged settings
   and queue count. Only then records observer state `suspended` and writes
   `<observer-state-stem>.closure.json`. The base deployment configuration digest
   is retained; the sidecar separately binds the actual control changes.

Mutation and polling share a20-minute deadline from the first mutation attempt,
including a guarded resume of a partial closure, and every CLI call is bounded.
Failures retain a blocked closure record/current-state requirement, preserve the
original error, and never delete/discard a pending checkpoint or missing final.
The eighth-success/ninth-unresolved cold result is not repaired or reclassified.

Reopening requires separately approved valid target/observer deployment and
start steps. Do not blindly reactivate an expired observer image. Resource
deletion remains a separate user-confirmed workflow.

Official references:
- [Revision deactivation stops all running replicas](https://learn.microsoft.com/azure/container-apps/revisions-manage#revision-deactivate)
- [Revisions can be reactivated; deactivation shuts containers down](https://learn.microsoft.com/azure/container-apps/application-lifecycle-management#deactivate)
- [Ingress disable CLI](https://learn.microsoft.com/cli/azure/containerapp/ingress#az-containerapp-ingress-disable)
- [Partial environment updates](https://learn.microsoft.com/cli/azure/containerapp#az-containerapp-update)
- [Scale limits: maximum replicas minimum1](https://learn.microsoft.com/azure/container-apps/scale-app#scale-definition)
