# Cloud experiment cost evidence

[简体中文](../2026-09-20-cn/experiment-costs.md) · [Stage definitions and timing boundaries](stage-definitions.md)

Updated: 2026-09-20, Southeast Asia, USD. This is a partial billing snapshot
plus explicitly labeled retail-price scenarios, not the final experiment bill.

## Reported costs so far

At **2026-09-20 02:26:23 UTC**, a successful Cost Management `ActualCost` query
covered 2026-09-18 00:00 UTC through observation time. It filtered only
`rg-copilot-advisor-aca-exp` and `rg-copilot-advisor-flex-exp`, grouped by resource
group and service, and summed `PreTaxCost`. The response had no continuation page.

| Resource group | Service | Reported USD |
|---|---|---:|
| ACA experiment | Container Registry | 0.3536506134 |
| ACA experiment | Log Analytics | 0.32984264235 |
| ACA experiment | Microsoft Defender for Cloud | 0 |
| ACA experiment | Storage | 0.27449276 |
| Flex experiment | Azure App Service | 0 |
| Flex experiment | Microsoft Defender for Cloud | 0 |
| Flex experiment | Storage | 0.0000832 |
| **Reported subtotal** | | **0.95806921575** |

**This subtotal is not the complete cost of the completed workloads.** Billing
ingestion is delayed: no ACA consumption row was returned, which does not prove
that ACA compute was free or unused. Keep reported zero rows distinct from
missing services. Later costs, credits, adjustments and taxes are not captured.
Requery after billing data catches up and after the experiment is closed.

The ignored evidence file
`deploy/out/bench/experiment-cost-snapshot-20260920.json` contains the exact request,
observation time, response columns and rows. No other resource-group charges
were queried. SHA-256:
`f37f22e342dfad80cda586b33dae123ce3d64656702369856afd6502022d7456`.

The initial snapshot at 2026-09-19 04:39:53 UTC reported $0.07103551796.
It remains unchanged at `deploy/out/bench/experiment-cost-snapshot.json`;
SHA-256:
`d4a5fe4629734861a5680ee36abc229ffba719bdbea88f0af680289801939670`.

## Published retail prices, not billed quantities

The following Southeast Asia USD rates were retrieved on 2026-09-19 from the
[Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices).
Paid tiers are shown; free allocations, negotiated discounts and remaining
subscription-level grants are not assumed.

| Service / meter | Paid rate | Meter ID |
|---|---:|---|
| Basic ACR registry | $0.1666/day | `5c9e7a65-5784-494c-9718-7749d4075dd9` |
| ACR Task vCPU duration | $0.0001/vCPU-second | `bb93fa13-a4e2-4027-85b9-b06e3353ffc3` |
| ACA active vCPU | $0.000034/vCPU-second | `5345ba68-7e2a-5eac-b829-98a6329c2272` |
| ACA idle vCPU | $0.000004/vCPU-second | `72c036f2-6920-5fae-8459-96038d34e82e` |
| ACA active memory | $0.000004/GiB-second | `5d2b5080-8fa7-581a-b8ae-fec637063514` |
| ACA idle memory | $0.000004/GiB-second | `375db157-0f59-520a-be46-d449cd6797ab` |
| ACA requests | $0.40/million | `bdb6456b-7218-5020-bde6-f3268dc3f6b2` |
| Flex On Demand execution time | $0.000037/GB-second | `9d043203-101b-507d-9f78-e2c8fa27f5bd` |
| Flex On Demand executions | $0.000004/10 executions | `e796b62c-4d21-5f05-b759-669864e7d41f` |

### Illustrative 30-day resident ACA scenarios

For exactly one 0.25-vCPU / 0.5-GiB replica for 2,592,000 seconds:

- If the platform classifies every second as eligible idle:
  `(0.25 * 0.000004 + 0.5 * 0.000004) * 2592000 = $7.776`.
- If every second is active:
  `(0.25 * 0.000034 + 0.5 * 0.000004) * 2592000 = $27.216`.
- A retained Basic registry adds `$0.1666 * 30 = $4.998`, before build tasks.

These are arithmetic scenarios, **not a measured monthly forecast or a cost
ceiling**. Background queue polling, reconciliation and network traffic affect
active/idle classification. Scale-out adds replicas; the concurrency experiment
observed up to 20. Storage, operations, logs, requests, egress, the separate
observer, network-governance-related charges and external dependencies are
excluded. The observer is an experiment cost, not a necessary production
component. No independent NSP meter was found in the initial price lookup;
that is not evidence that all networking/governance costs are zero.

### Flex comparison remains unmeasured

The empty Flex infrastructure is provisioned, but application code and workloads
are not deployed pending actual regional quota evidence. There is therefore no
measured per-question Flex cost or cost/performance comparison.

With no Always Ready instances, the published paid-meter formula is
`0.000037 * billable_GB_seconds + 0.0000004 * billable_executions`, before grants
and other services. Billable function execution time is not benchmark
end-to-end latency; HTTP, queue worker and reconciliation executions must all be
accounted for. Apply the platform's duration rounding rules rather than simply
multiplying the ACA reply latency by 512 MB.

See [Flex billing](https://learn.microsoft.com/azure/azure-functions/flex-consumption-plan#billing)
and [ACA billing](https://learn.microsoft.com/azure/container-apps/billing).

## Attribution and closure

- The existing OpenAI and Search resources are in the old subscription and
  excluded from the new-resource subtotal. Their model/API incremental usage,
  existing Search baseline, and any GitHub/web-search charges must be reported
  separately. Their final billed increments were not obtained.
- Retaining the registry, Storage, monitoring and a resident observer can incur
  costs even when no chatbot questions are submitted. ACA `minReplicas=0` is not
  proof of actual zero replicas or zero total cost.
- [Guarded closure](aca-benchmark-closure.md) disabled target benchmark access
  and observer ingress, preserved target min0/Teams ingress, and deactivated all
  observer revisions. Independent 2026-09-20 02:24–02:25 UTC reads found actual
  replicas0 for both. The observer's retained min1 template is inactive, not
  resident. Retained registry/storage/monitoring still prevent a zero-total-cost
  claim.
- Resource deletion requires separate explicit user confirmation; no resources
  were deleted to produce this report.
