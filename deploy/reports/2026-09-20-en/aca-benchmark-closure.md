# ACA benchmark closure — completed without deletion

[简体中文](../2026-09-20-cn/aca-benchmark-closure.md) · [Stage definitions and timing boundaries](stage-definitions.md)

The user authorized reversible, guarded closure of the temporary observer and
target benchmark feature, preserving normal Teams ingress and all durable state.

## Actual operations and readback

The first mutation attempt began at **2026-09-19T07:47:27.442982Z**. It disabled
observer ingress and changed the target's `BENCHMARK_ENABLED` flag, then stopped
at the settings-readback guard. No deletion or rollback was attempted.

Read-only Resource Graph change history for the exact target showed only:

- the authorized environment value changing from `True` to `false`;
- provisioning state and generated latest-revision metadata.

Old/new revision templates independently differed only in that environment
value. A regression test added bounded polling for an unchanged stale read
after `--no-wait`; unrelated configuration drift still fails. A fresh exact
WhatIf approval was obtained for the remaining closure. The original mutation
window was retained, not renewed.

Guarded resume completed at **2026-09-19T08:06:45.534051Z**:
**1,158.091 seconds (19m18s)** from the first mutation, within the 20-minute bound.

Actual final readback:

| Surface | Final evidence |
|---|---|
| ACA target benchmark feature | `BENCHMARK_ENABLED=false` |
| ACA target scaling | min0; independent subsequent ARM readback found replicas0 |
| Normal Teams ingress | Preserved external HTTPS ingress and original configuration |
| Benchmark access rejection check | Unauthenticated empty POST `/bench/messages` returned404 |
| Teams route preservation check | Unauthenticated empty POST `/api/messages` returned401 |
| Temporary observer ingress | Disabled (`ingress=null`) |
| Temporary observer active revisions | None |
| Temporary observer replicas | 0, including the explicitly deactivated revision |
| Temporary observer state | `suspended`, `BENCHMARK_AUTOSTART=false` |
| Resources, identities, role records, result data | Retained; no deletion/RBAC/policy/Flex operations |

The rejection probes were not benchmark questions or Teams functional tests.
They briefly woke the target: it had one replica at probe completion, with min0
unchanged; a later complete control-plane readback found zero. The helper never
forced the normal Teams service down to achieve a replica count.

Observer suspension used the documented reversible deactivation of
`ca-bench-cskd5yazfko42--0000007`. Configured min1/max1 on its retained template
was not changed or interpreted as max0; inactive revisions had actual zero
replicas. Reopening requires separate approved operations and a valid observer
plan, not blind reactivation of the expired image.

## Exact guarded commands

```powershell
.\deploy\bench\Close.ps1 -ParametersFile deploy\out\plans\benchmark-close.json

.\deploy\bench\Close.ps1 -ParametersFile deploy\out\plans\benchmark-close.json `
  -Mode Apply `
  -Confirmation 'BENCH-CLOSE:aca:8cf1c6ed-743f-4f0b-ac18-df6267235e34:e3d7ca9304a7f283'
```

After the guarded stop and independent change-history review, the same preview
command produced the fresh remaining-action confirmation:

```powershell
.\deploy\bench\Close.ps1 -ParametersFile deploy\out\plans\benchmark-close.json `
  -Mode Apply `
  -Confirmation 'BENCH-CLOSE:aca:8cf1c6ed-743f-4f0b-ac18-df6267235e34:8ce8e2c670bf1861'
```

## Evidence

Under ignored `deploy/out/bench/aca-cold/`:

- `guarded-closure-preview-2.txt`, `guarded-closure-apply.txt`;
- `guarded-closure-first-blocked.json`, `closure-change-history-paths.json`,
  `closure-safe-change-detail.json`, `closure-drift-diagnostic.json`;
- `guarded-closure-resume-preview.txt`, `guarded-closure-resume-apply.txt`;
- `guarded-closure-command.json`, `guarded-closure-resume-command.json`;
- `guarded-closure-final-readback.json`.

The durable control record is
`deploy/out/runs/observer-deployment.closure.json`. It records the original
deadline, quiescence evidence, intended actions, protected configuration hashes,
route checks and final revision/replica observations. It does not echo secrets
or unrelated environment values, and it retains the base configuration digest
instead of inventing a new deployment artifact/configuration.

## Cold result remains partial

Closure does **not** repair or reclassify the cold cohort: **8/10** verified
natural-zero successful questions remain the measured result. Attempt
`df12a3106f4340ec83c01234be5ada5f` still has an exported checkpoint but no exported
final result. Its durable state was neither deleted nor discarded. No load or
START was repeated.

At closure execution time HEAD remained
`49100580008a769aafc87c4f12f497e048a7fe6f`. Only closure-related deployment
helpers/tests/docs were edited; no application business/model/auth code was
changed. After validation, closure code was committed as `0fe3ec1`; this does
not change the historical workload artifact binding.

## Code and verification

Code files:

- `deploy/tools/src/advisor_deploy/benchmark_close.py`
- `deploy/tools/src/advisor_deploy/operations.py` — closure dispatch and optional
  shorter checked-command timeout only
- `deploy/tools/src/advisor_deploy/benchmark_deploy.py` — suspended-state support
- `deploy/bench/Close.ps1`
- `deploy/tools/tests/test_benchmark_close.py`

Red/green coverage includes quiescence refusal, historical immutable artifact
binding, exact confirmation, unchanged environment/secret references and Teams
configuration, output redaction, real ARM default-field presentation, bounded
stale reads, deactivation drain, refusal to renew the mutation window, and
truthful target replica reporting after rejection probes.

Final command:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  deploy\tools\tests\test_benchmark_close.py `
  deploy\tools\tests\test_observer_deployment.py `
  deploy\tools\tests\test_docker_routing.py `
  deploy\tools\tests\test_powershell_contracts.py `
  deploy\tools\tests\test_deploy_workflows.py -q --tb=short
```

**65 passed.** `git diff --check` passed. Independent live readback confirmed
target benchmark false/min0/replicas0 and preserved Teams ingress, observer
ingress null/no active revisions/replicas0/autostartfalse/state suspended.

## Independent follow-up

On 2026-09-20 02:24–02:25 UTC, fresh CLI control-plane reads again confirmed
target benchmark false/min0/active revision scaled to zero with no replicas;
observer ingress null/AUTOSTART false/all revisions stopped with no replicas.
No endpoint was called, so the verification did not wake either app.

The independent repeat of the 65-test selection produced 63 passes and two
Windows subprocess timeouts (negative remaining wait values after a 6h57
elapsed run), not functional assertion failures. The two exact failed tests
then passed unchanged in 53.74 seconds; no timeout or behavior was altered.
Editor diagnostics found no errors, and all 55 `Cli.run` / 21 `Cli.rest` call
sites passed semantic signature-compatibility checks.
