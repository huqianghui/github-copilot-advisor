# Policy-compatible Storage connectivity

This bounded helper prepares **one Enforced Network Security Perimeter (NSP)**
in the existing owned ACA resource group, one `storage` profile, one inbound
subscription rule, and exactly two associations: the Storage accounts recorded
in the ACA and Flex ownership ledgers. It creates no Storage account, virtual
network, private endpoint, public IP, policy exemption, identity or role.

The only inbound subscription is
`18827cdf-a4e8-4316-a7ba-a4499139a3ec`. There are no IP-prefix, service-tag or
outbound allow rules. Non-onboarded ACA/Functions hosts use their existing
managed identities; network permission does **not** replace Blob/Queue RBAC.
The subscription rule permits resources in that subscription, not only these
three hosts; the existing narrowly scoped data roles remain the authorization
boundary.

## Plan and approval

Create a non-secret plan under `deploy/out/plans/network.json`:

```json
{
  "format": "advisor-network-plan-v1",
  "aca_manifest": "../runs/aca-ownership.json",
  "functions_manifest": "../runs/functions-ownership.json"
}
```

```powershell
.\deploy\network\Deploy.ps1 -ParametersFile deploy\out\plans\network.json
```

WhatIf validates the active subscription/tenant, both group ownership records,
live Storage security/data roles, Microsoft.Network registration and Southeast
Asia/API availability, and existing associations. It performs local Bicep
compilation plus ARM group validate/what-if and retrieves current public retail
pricing evidence. It changes neither Azure nor either ledger.

Review the returned `pricing` evidence. Add this exact structure to the plan,
using the returned `pricing.sha256` and the actual review time:

```json
"cost_review": {
  "pricing_sha256": "REPLACE-WITH-RETURNED-SHA256",
  "reviewed_at": "REPLACE-WITH-ACTUAL-UTC-ISO-TIMESTAMP",
  "acknowledge_unpublished_meter_risk": true,
  "acknowledge_existing_service_costs": true
}
```

Run WhatIf again. Apply requires a matching, at-most-24-hour-old cost review
and the **new** exact `NETWORK:...` confirmation:

```powershell
.\deploy\network\Deploy.ps1 -ParametersFile deploy\out\plans\network.json `
  -Mode Apply -Confirmation '<exact NETWORK confirmation>'
```

No script selects accounts, registers providers, assigns roles, changes policy
or adds exception tags. A different plan, current ownership record, compiled
template, storage snapshot, pricing result or cost acknowledgement invalidates
the approval.

## Apply and evidence

1. Deploy only the five approved NSP resources in incremental group scope.
2. Append only ARM-receipt-proven network IDs to the ACA ownership ledger.
   Preserve its existing outputs, artifact/configuration digests, summary and
   role records. Do not change the Flex ledger.
3. Verify the exact perimeter, profile, sole subscription rule and two
   `Enforced` associations. Verify Storage's effective NSP configurations,
   including the propagated profile/rule, association name, access mode and
   provisioning result. Bounded propagation waiting never falls back to
   Learning/Transition.
4. PATCH only the two owned accounts' `publicNetworkAccess` to
   `SecuredByPerimeter` and their legacy network ACLs to deny/no bypass/no
   exceptions. This is an explicit separately displayed `storage_patch`, not
   a Storage replacement or tag update. Its server-side policy outcome must
   pass readback; the ARM template preview covers the NSP resources, not this
   subsequent PATCH.
5. Verify the final PNA, authentication controls, network ACLs and effective
   associations. Persist `<aca-ledger-stem>.network.json` with the bound IDs,
   pricing fingerprint and phase.

The shared state-account Bicep module now defaults to
`storagePublicNetworkAccess=SecuredByPerimeter`, with only `Disabled` as the
other supported value. Both target roots inherit this default, so a later
target deployment never resets PNA to Enabled. Before association, this setting
locks public access down; after association, NSP controls access. No
configuration/CLI parameter wiring is required for this default.

Failures retain known receipts and mark the network sidecar failed. They do
not relax controls, delete resources, change main-app deployment status, or
claim data-plane success. `network_verified` means control-plane readback,
not a completed question. The parent must test Blob/Queue from the deployed
managed-identity hosts and then perform the single-question acceptance check.
Local user-token Storage requests need not pass a subscription-source rule.
These deployment-tool source changes also change the strict benchmark package
snapshot. Rebuild/reapprove the observer context/image before a Control or
Publish operation that verifies current source; do not weaken that existing
snapshot guard to reuse stale packaging.

## Availability, permissions and cost

Research on 2026-09-19:

- Microsoft Learn states NSP is generally available in all Azure public
  regions, including Southeast Asia. The helper additionally checks the
  actual subscription provider metadata; it does not register the provider.
- Storage documentation covers HTTPS Blob/Queue operations and explicit
  subscription inbound rules for services not themselves onboarded to NSP.
  In Enforced mode, trusted-service firewall exceptions are not honored.
- A Microsoft Q&A moderator answer dated 2025-12-03 reports no separate NSP
  charge. The retail-pricing lookup returned no dedicated Southeast Asia NSP
  meter during preparation. Neither result is a contractual zero-cost
  guarantee: the helper fetches current evidence and requires explicit cost
  review. Existing Storage/ACA/Functions, transactions, egress and any later
  diagnostics remain billable. This template adds no diagnostic sink or
  extra networking component.

The caller needs deployment write and the four relevant NSP resource write
actions in the ACA group, plus each account's read/write and
`Microsoft.Storage/storageAccounts/joinPerimeter/action`. Existing data roles
are verified, never broadened. ARM validation and final readback must confirm
the management-group policy accepts `SecuredByPerimeter`; no policy bypass is
implemented.

Both experiment accounts depend on the ACA-owned perimeter. Future cleanup
must account for both workloads and still needs a separate explicit approval;
this helper has no delete/detach operation.

References:
- [Storage NSP coverage and subscription rules](https://learn.microsoft.com/azure/storage/common/storage-network-security-perimeter)
- [Secure-by-default PNA and Enforced mode](https://learn.microsoft.com/azure/private-link/network-security-perimeter-transition)
- [Availability and limitations](https://learn.microsoft.com/azure/private-link/network-security-perimeter-concepts)
- [Association schema](https://learn.microsoft.com/azure/templates/microsoft.network/2024-07-01/networksecurityperimeters/resourceassociations)
- [Subscription-rule schema](https://learn.microsoft.com/azure/templates/microsoft.network/2024-07-01/networksecurityperimeters/profiles/accessrules)
- [Effective Storage configuration API](https://learn.microsoft.com/rest/api/storagerp/network-security-perimeter-configurations/list?view=rest-storagerp-2025-01-01)
- [Microsoft Q&A pricing guidance](https://learn.microsoft.com/answers/a/12406324)
