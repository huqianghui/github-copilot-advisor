# ACA cold cohort — partial, validity-limited

> Subsequent authorized closure is complete; see
> [ACA benchmark closure](aca-benchmark-closure.md).
> Target benchmark access is disabled with min0/Teams ingress preserved.
> Observer ingress is disabled, all observer revisions are inactive and actual
> observer replicas are zero. This supersedes the earlier resident-observer
> closure limitation below, but **does not change the 8/10 partial cold result
> or resolve attempt9's missing final**.

Finalized: 2026-09-19T06:57:05.416130+00:00. **8/10 verified natural-zero samples; target not met.** Nine attempt prefixes were observed: eight complete results and one incomplete/unverified checkpoint. No workload rerun was performed.

Artifact `31882ec48e652906874e3d5687bf0b675dfbb841d66531d84ee23a204cd7a4d0`; commit `49100580008a769aafc87c4f12f497e048a7fe6f`.
Base configuration hash `cc0528a1e9ff30b4f86ccf46f5b8b2ece9bd75c461d783c5fc78ccae19497165` is the unchanged deployed baseline. Actual later scale control: `{"at": "2026-09-19T04:54:20.622312+00:00", "kind": "scale-mode", "min_replicas": 0}`. ARM verified min0 with unchanged0.25CPU/0.5Gi/max40/image. This separate control provenance is not a fabricated new config hash.

## Bounds and disposition

- Cold profile, kb-01, real Agent, empty history, concurrency1 and one logical question per attempt; cold target10, maximum15 attempts; zero wait600s; max_submit1800s, request timeout/drain600s; required sampling fields retained.
- Sealed validity expired at `2026-09-19T06:41:13.729465+00:00`. The shorter105-minute window was an operator choice anchored during preparation. Preparation plus roughly9-minute natural cycles left insufficient time for10 samples. The external monitor bound was120minutes from START. The plan was not altered after START.
- The initial START returned actual `starting` on observer revision0000007; it was issued once only. ACR cloud run cmt and guarded Publish/App/Control approvals are preserved under the evidence directory.
- Eight finalized questions all reported success. The ninth checkpoint has zero recorded terminal observations and zero pending intents; its final blob was not exported after the observer session ended. Its latency and terminal stop reason remain unknown, rather than synthesized.
- Final control-plane audit found target min0 and two complete empty active-revision replica listings, with owned incoming-queue approximate count0 twice. No active target replicas were observed; queue counts remain approximate.
- Observer `BENCHMARK_AUTOSTART=false` remains configured. The driver container was **not ready/not started, restartCount4** after expiry. Healthy idle-listener restoration and retrieval of the ninth final require a separately reviewed operator follow-up; no source fix, replacement deployment, cleanup or extra START was attempted.

## Cold evidence and timing method

- Registration precedes zero wait. Proof files were refreshed from actual complete ARM revision/replica lists, with two stable empty observations, original IDs, UTC times and source/proof hashes. Min0 was never used as zero evidence.
- All eight accepted proofs were checked against saved empty-list responses; callback telemetry provided eight distinct boot IDs and instance IDs with first_request=true. No since/boot timestamps were invented.
- The application-link timer ends when the full callback body is received, before authentication; cold idle waiting is excluded from that question latency. This is ACA process/replica cold evidence, not a claim of cold model infrastructure or fresh Azure VMs.
- Driver-MI private Blob reads inspected at most18 top-level prefixes (cap100), selected this exact cold plan/artifact/target, and preserved original final bytes. Exact job/trace safe log projections produced32 completed-stage records; no answer bodies or unrelated log rows were exported.
- Export transport/quoting and KQL timestamp-format errors are retained. A fixed-size compressed bridge and native UTC projection recovered finalized results; only local collectors changed, never repository source or workload START. Observer exec collection can add driver overhead.
- No pooling with prior warm/concurrency cohorts, no platform-capacity/SLA assertion, and no Teams/Functions performance conclusion.

## Measured finalized attempts

| Run | Category | Outcome | Latency ms | Stop |
|---|---|---|---:|---|
| [23c035a024084d909224c6fdd1166aed](aca-cold-23c035a024084d909224c6fdd1166aed.md) | natural_zero | success | 32996.856 | target_reached |
| [180e287d199d4a12ad42de455e59510a](aca-cold-180e287d199d4a12ad42de455e59510a.md) | natural_zero | success | 35250.717 | target_reached |
| [086b724ce3364f48baa4d30351e6be21](aca-cold-086b724ce3364f48baa4d30351e6be21.md) | natural_zero | success | 37787.568 | target_reached |
| [eb8c3ec6194843a686d402fba5b075df](aca-cold-eb8c3ec6194843a686d402fba5b075df.md) | natural_zero | success | 33949.196 | target_reached |
| [585eeefee753449ab9b5700b06705924](aca-cold-585eeefee753449ab9b5700b06705924.md) | natural_zero | success | 41824.085 | target_reached |
| [8ea1af125e674d5789555b2f6ee5f2fe](aca-cold-8ea1af125e674d5789555b2f6ee5f2fe.md) | natural_zero | success | 37857.831 | target_reached |
| [fa2d96f1fd7c434d8637ce757dcc19e4](aca-cold-fa2d96f1fd7c434d8637ce757dcc19e4.md) | natural_zero | success | 38344.275 | target_reached |
| [1cdfb03feb434d519a55533ebcfb2a4e](aca-cold-1cdfb03feb434d519a55533ebcfb2a4e.md) | natural_zero | success | 37938.185 | target_reached |
| [df12a3106f4340ec83c01234be5ada5f](aca-cold-df12a3106f4340ec83c01234be5ada5f.md) | unverified/incomplete | no recorded question intent | n/a | not observed |

## Distribution and observed dependencies

Natural-zero **successful questions only**: count8, mean36993.589ms, P5037822.700ms, P9540606.151ms, P9941580.498ms. Eight samples are below the requested minimum; tail estimates are small-sample diagnostics.
Observed question outcomes: `{"success": 8}`. The incomplete attempt is not inserted into success or censored latency distributions.
Actual callback dependency counters: model HTTP attempts24, HTTP429 observations0, counter coverage8/8 observed questions. These are observed counters, not inferred from model rounds.

## Evidence locations and hashes

All raw evidence is under ignored `deploy/out/bench/aca-cold`: ARM observations, zero DTO/source hashes, deployment/control outputs, original result bytes, versioned checkpoints, safe logs/KQL and end-state readbacks.

The report hashes below identify the original exports, preserved byte-for-byte
under `report-exports/<run-id>.md` in that evidence directory. Published copies
remove one redundant blank line at EOF only; no measured content changed.

- `23c035a024084d909224c6fdd1166aed` raw `cc7e6dd951b6c8e0a0d1fc0226781e46b3ae3befa83e370daaa43e15cdb09ae1`; safe logs `66ad817d7b7894b3970a99121e4763822690dac17b766cd3d9efaab96b580dc1`; report `c94a02d311bea4d27f0e4608bc2efc6403ea30ec01b5ddb0ee6d9aa665edcfee`.
- `180e287d199d4a12ad42de455e59510a` raw `aa28401f2d91a5327bd5d115c0f67e16f5db5200a429ff647f91bb51a98bae70`; safe logs `e4cfa4c365a56721f41d75900c28446ef9c2054537d3d469cd14563ef2ae1244`; report `b68377d0ec4f3e8480294dc9f8f3e6e0dc90cb6a1ad7fee562caeeaaa7f1a951`.
- `086b724ce3364f48baa4d30351e6be21` raw `1f37cc369aa1761d00ae067d9397b943f250a5993d940c7792d494bda126974e`; safe logs `879eb486292e21ef5bbf5bb917f0f0633db6f2723027bd96ae97a93ddc448064`; report `c7cf5917fed791ff7028839d63692c240b70a2a0d78469ee07e7d2971044d6a1`.
- `eb8c3ec6194843a686d402fba5b075df` raw `b345d45ba9225fdfb697eae9e3fbd722c33616083828cf61d23b7b2bed3569e8`; safe logs `6e85230a29e9d64d5c45ab50605602cd1aeedfe983f1d05c14f300d08f4e0f5e`; report `bbd3af7bf03faf5b86460725dc6b4d1f037c1aef4bf48d4fcd863ef396ef8e60`.
- `585eeefee753449ab9b5700b06705924` raw `b4796ace3b651d12daa5c617b01d90862c1da64f00b75324c69ca514685889ef`; safe logs `f9884a0c432782a757a250bc121a27b2f8a8c44ca43e2ff90a23c525a60773d9`; report `3f1ecd71c7a1c3bd3eddb7fc2be76376547aaf0b54c3540258713b184783c13e`.
- `8ea1af125e674d5789555b2f6ee5f2fe` raw `6fd0e578b1fb91850539781c8b0aeea23fcc90cc07b956660d81b601d02521d9`; safe logs `cda8563441f5d19495f196e8969f261af1a0c87369f9a8b26bc716d222e03258`; report `0fb8649d6b0cf000dab4fc24fb9016f1dbc0f5020569a2a0087184a11d812bc6`.
- `fa2d96f1fd7c434d8637ce757dcc19e4` raw `d38ffc519a1fd25e8340f5ff0e367c5af51a127ce5c4db687891d284102752e3`; safe logs `f20ad10a3e7f3a9607c47083aac536954297ea331ea722f77862a83804432b72`; report `740d8d3a8a089d0dff6102e5ca759ccc259888fe083e905b6bf499258f690ffa`.
- `1cdfb03feb434d519a55533ebcfb2a4e` raw `99a84db594388257437cad61015de9a5790a69eb744958a25dbea04762f77cf4`; safe logs `86f8e086bdb07b7e03a40f420c4540f1957aecd3d8fa05fbe91c5555d8df4e07`; report `3c65be2ad9a8cc10682b929aa00ce843907db62aab91eb8d35f002d1f09f7e76`.
- Incomplete checkpoint `914d098f9c7a83dcd8fc1b0bdb8b4301c3afcb7bf2da241b9d6521a8429c6836`; no final-result hash is claimed.

## Additional closure assessment

Observed 2026-09-19T07:02:26.771617+00:00.

- Target ACA: min replicas 0, actual active-revision replicas 0; external HTTPS ingress and `BENCHMARK_ENABLED=True` remain configured. No endpoint probe or disabling mutation was performed.
- Temporary observer: min replicas 1, actual active-revision replicas 1; external HTTPS ingress and benchmark feature remain enabled; `BENCHMARK_AUTOSTART=false`. Driver container remains not ready/not started, so live control state is unavailable—not verified healthy waiting.
- Existing approved helpers do not provide observer min0: Set-Mode addresses the main ACA app, Control offers start/status only, and observer infrastructure fixes one resident replica. No direct CLI/source workaround was used. **Observer idle/scale-down is a remaining closure action for the parent/operator**, to avoid leaving the resident resource indefinitely.
- No deletion, policy, RBAC, business-setting or target scaling change was made during this assessment. Future deletion still requires explicit user confirmation. Parent-owned summary and cost reports were not edited. Raw closure metadata: `deploy/out/bench/aca-cold/closure-assessment.json`.
