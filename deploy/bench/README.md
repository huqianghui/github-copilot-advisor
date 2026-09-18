# Bounded application-link benchmark (Task 15)

This tool runs a driver **and** HTTP reply observer in one process/one replica,
separate from the target. It measures submission-to-complete-rendered-reply with the
observer's high-resolution monotonic `perf_counter` clock (resolution is
recorded). Admission HTTP 202 and typing never release a
question slot. A callback can arrive before its ACK.

**Status:** the local synthetic HTTP boundary is runnable. Production target
routes below belong to **Task 11** and must exist before cloud execution.
This directory does not install those routes, provision resources, call a
model, validate cloud capacity, or contain measured cloud reports.

## Run locally

From the repository root with the locked workspace installed:

```powershell
.\deploy\bench\Run.ps1
# Equivalent standalone entry point; the root advisor-deploy CLI is unchanged:
uv run --no-sync --package advisor-deploy python -m advisor_deploy.bench run --plan deploy\bench\run.example.json
```

The example explicitly selects `local_test: true`, accepts only literal
loopback HTTP URLs, and starts two actual aiohttp listeners on ephemeral ports.
The synthetic target waits 1,000 ms and POSTs a complete rendered message in a
real HTTP callback. It does not
exercise the production queue, storage, worker, Agent, Entra service, or Teams.
Its durations/throughput describe **only this local HTTP fixture**, not cloud
or model performance. Short observation limits are permitted only in this
loopback boundary; it cannot be pointed at a cloud endpoint.
The explicit reply text is
`[synthetic benchmark: 1000 ms; no model invocation]`. Its actual async-wait
duration is recorded as stage `synthetic`, never ordinary Agent/model latency.

Results are generated, not checked in, under ignored `deploy\out\bench`.
Convert a specific result without inventing measurements:

```powershell
uv run --no-sync --package advisor-deploy python -m advisor_deploy.bench report --input deploy\out\bench\ACTUAL-RUN-ID.json --output deploy\out\bench\ACTUAL-RUN-ID.md
```

Exit 0 means sampling targets were reached, **not** that all responses
succeeded. Exit 2 means an early stop or configuration/registration failure.
Interruption returns 130 and persists registered unfinished requests as
censored. Interruption during registration/zero waiting also closes the
preparation checkpoint without fabricating a question or duration.

## Version-one shared wire contract

All DTOs live in **`advisor_shared.benchmark`**. The target application must
depend on `advisor-shared`, **never** on deployment tools. JSON objects reject
unknown fields and non-finite numbers. IDs are bounded ASCII identifiers.
Arbitrary callback destinations, question fields, real user identities and
bearer-token fields are rejected. Final replies carry a strictly shaped
rendered message in **`reply` transiently**; it is never copied to observer evidence.
`reply` is the sole canonical wire field; there is no `message` alias.
`WireModel` uses `hide_input_in_errors=True`, and reply/text fields are
excluded from `repr`. HTTP errors contain fixed categories, not raw
validation errors, SDK exceptions or input.

### Target: `POST /bench/runs`

`BenchmarkRunRegistration`:

```json
{
  "run_id": "new-uuid-per-profile-and-level",
  "profile": "concurrency",
  "case_ids": ["kb-01", "web-01"],
  "seed": 20260917,
  "scenario": "synthetic",
  "history_mode": "empty",
  "max_requests": 10000,
  "max_concurrency": 8,
  "expires_at": "2030-01-01T00:00:00+00:00",
  "dataset_sha256": "64-lowercase-hex-characters"
}
```

The digest above is schematic, not a valid registration. The actual dataset
digest is in `run.example.json`. `dataset_digest(dataset)` computes SHA-256
over UTF-8 JSON with `sort_keys=True`, `separators=(",", ":")`,
`ensure_ascii=False`, `allow_nan=False`. Object key ordering/line endings do
not change it; case/history ordering and content do. Install the exact same
approved dataset on **both** targets; registration does not upload questions.

Shared validation is available without importing deployment tools:

* `advisor_shared.benchmark.parse_dataset(content: bytes) -> dict` is the
  common artifact/runtime parser. It enforces the 4 MiB byte limit, strict
  UTF-8/JSON, duplicate-field rejection and the complete v1 fixture schema,
  returning unchanged decoded content. Invalid input, including excessive
  JSON nesting, raises a bounded `ValueError` without fixture bodies.
* `advisor_shared.benchmark.validate_dataset(value: object) -> dict` validates
  the decoded v1 document and returns its content unchanged. It rejects
  unknown fields, invalid/duplicate case IDs, blank or oversized questions,
  and anything other than exactly twenty alternating user/assistant history
  messages. Errors do not include fixture bodies.
* `load_dataset(path: str | Path, *, expected_digest: str | None = None) -> dict`
  reads at most `MAX_DATASET_BYTES + 1` and delegates to `parse_dataset`,
  then optionally checks the canonical digest. The harness's `load_cases`
  reuses this loader and adds selected-case membership.
* `dataset_digest(data: dict) -> str` remains the registration digest helper;
  it is separate from deployment manifests' hashes of the original file bytes.

`DEFAULT_DATASET_PATH` is `runtime-config/benchmark-cases.json`. Target
ACA/Functions packaging is parent-owned: dataset inclusion requires an
**explicit opt-in package path** (`benchmark_cases_config`,
`--benchmark-cases-config` / `-BenchmarkCasesConfig`), not automatic inclusion. The opted-in
approved file is parsed with `parse_dataset` and copied byte-for-byte to
that relative artifact path, retaining the three-online-wheel contract and
existing byte-hash manifest mechanism. Canonical registration digests do not
replace hashes of the original artifact bytes. Opting into a dataset does not enable an
anonymous benchmark API or relax authentication.
The isolated benchmark context requires a dataset and uses the repository's
`deploy/bench/cases.json` bytes and the same artifact path; its image resolves the default to
`/app/runtime-config/benchmark-cases.json`. The local example explicitly
overrides `cases_path` to `cases.json` for its source-tree execution.
Target and driver datasets must have the same canonical registration digest.
Artifact validators should call `parse_dataset` directly on their bytes;
source packaging/runtime loaders can call `load_dataset`. Do not log the returned
fixture content or copy it into result telemetry.

The target must bind the run to its authenticated driver principal, validate
expiry, known cases, scenario, dataset digest, concurrency and deployment
budget, and persist registration/idempotency state. Re-registering identical
content can return 200; a newly accepted run returns 201. Both return
`BenchmarkRunAccepted`:

```json
{"run_id":"the-run-id","target_id":"the-expected-resource-id","dataset_sha256":"the-actual-digest"}
```

`target_id` must match `RunPlan.expected_target_id`. Mismatch stops execution.
Registration is a control action, not a measured question or a warm sample.
For cold experiments registration happens **before** waiting for zero, since
registration itself can wake the target. Never register or health-check
between zero proof and the measured message.

### Target: `POST /bench/messages`

`BenchmarkRequest` has exactly:

```json
{"run_id":"run-id","request_id":"fresh-logical-query-id","case_id":"kb-01","virtual_user":0,"scenario":"synthetic"}
```

`scenario` is `synthetic` or `real_agent`. `virtual_user` is integer 0–127 and
must fit the run's allowed users. The target looks up the case and derives
isolated synthetic tenant/user/conversation keys from registration:

* `empty`: new conversation for **each request**, no accumulated history.
* `seeded20`: new conversation per request, initialized with the exact twenty
  alternating messages in `seeded_history`, once before processing.
* `same_user`: all driver slots send virtual user 0 into one run-local
  conversation, so same-partition contention/ordering is reported separately.

`conversation_id(request, history_mode)` is the common conversation suffix.
The target must still namespace it using its existing isolation algorithm.
The driver chooses a reproducible case sequence per slot (`seed + slot`);
workload/digest/seed/history snapshots must match across hosts. Wall-time
limits can yield different counts, so compare actual samples, not assumed
equal completed counts.

Admission returns HTTP **202** and `BenchmarkAck`:

```json
{"run_id":"run-id","request_id":"same-request-id","job_id":"durable-job-id"}
```

HTTP retries reuse the same request ID/body; each new logical question gets a
fresh ID. The target must enforce idempotency and reject conflicting reuse.
Known 4xx rejections before any ambiguous attempt are failed admission
observations, not answers. Network timeouts, 5xx, and malformed/mismatched
ACKs may have admitted a job: retain the slot for its callback/deadline.
After an ambiguous attempt, a later 4xx cannot prove the earlier attempt
didn't admit work. HTTP 429 respects numeric or HTTP-date `Retry-After`,
including a shared pause across slots; no identity switching or fallback.

### Observer: `POST /bench/replies`

`BenchmarkReply`:

```json
{
  "run_id": "run-id",
  "request_id": "original-request-id",
  "job_id": "durable-job-id",
  "event": "final",
  "outcome": "success",
  "reply": {
    "type": "message",
    "format": "markdown",
    "text": "The complete rendered test answer."
  },
  "telemetry": {
    "trace_id": "safe-trace-id",
    "stages": [{"name":"agent","duration_ms":123.0,"outcome":"success"}],
    "dependencies": [{"dependency":"model","calls":2,"throttled":0}],
    "boots": [{"group":"worker","boot_id":"boot-id","instance_id":"instance-id","first_request":true}],
    "pending_jobs": 0,
    "host_crashed": false,
    "host_oom": false,
    "paths": ["kb"]
  }
}
```

This is a schema illustration, **not a measurement**. `BenchmarkReply.reply`
has type `RenderedReply | None`. `RenderedReply` has
exactly `type: "message"`, `format: "markdown"`, and nonblank `text` of at
most 32,768 characters. Complete rendered text is required for `success`
and `degraded`. Empty or missing successful answers are rejected, not
counted as full replies. Other final outcomes may omit/null `reply`.
`event: "typing"` requires absent/null outcome **and `reply`**, and never
finishes a request. Only `final`
requires one of `success`, `degraded`, `failed`, `unknown`, `expired`,
`ignored`. Send final outcomes even when no visible message was produced.
`censored` is observer-owned, never a target-reported outcome.

### Exact observer endpoint and authentication overhead

The observation endpoint is **the instant the observer has consumed the
entire bounded HTTP callback body**, before JSON/schema validation,
Entra/JWKS verification or evidence writes. The same observer `perf_counter`
measures request start and body reception. The received timestamp is accepted
only after the token, run/request/job IDs, outcome, and complete rendered
message pass validation; unauthorized/malformed bodies cannot finish work.

Bodies are limited to 256 KiB and a five-second read. Authentication is
bounded to ten seconds. A fully received, in-deadline callback may hold
deadline classification only for that bounded authentication interval.
Concurrent invalid/duplicate callbacks cannot remove another callback's
in-progress verification. This does not extend the target's 600-second
body-reception deadline. The rendered message and body buffer are discarded
before authentication/evidence processing; only safe metadata survives.
`observations.callback_processing_ms` records observer validation/authentication
overhead separately; duplicates do not overwrite the first completion's
overhead or endpoint. Observer JWKS cold fetches therefore do not count as
target cold latency. No target health/status call is made to warm anything.

Safe telemetry's exact allowlist is `SafeTelemetry`: enum stage/dependency
names; finite local durations; observed calls/throttles; trace/boot/instance
IDs; first-workload markers; pending/instance/worker counts; CPU/memory;
model rounds/token **counts**; attempt counts, cache boolean and actual
retrieval-path enums. Missing data stays unknown. No arbitrary attributes,
error strings, UTC-to-monotonic subtraction, question, reply, token or
conversation bodies. The unchanged wire field `BootObservation.first_request`
means the **first workload invocation on that observed process boot**, not
the first platform probe or the driver's first benchmark question. Trusted
ACA GET/HEAD `/healthz` and `/readyz` and the native health handler are
observed/logged as probes, emit no boot observation, and neither consume nor
reset workload history. Ordinary requests, authentication, registration,
queue and timer work still consume that history. Host total/probe/workload
counters remain separate; snapshot accessors are read-only and no
benchmark-run reset exists.

The observer interprets the marker only alongside genuine host boot/instance
identity and the required external platform-zero/new-boot evidence.
Ignoring a probe does not establish coldness, and `first_request: true`
alone does not establish natural zero. Local sample questions likewise do
not establish that KB/Web was actually used.

The shared enum supports separately measured `seeded20` per-conversation
fixture seeding/overlay preparation as `StageObservation(name="history_setup",
duration_ms=<finite nonnegative float>, outcome=...)` in `telemetry.stages`.
Use the existing stage outcomes (`success`, `degraded`, `failed`, `unknown`,
`empty`). Measure that interval with the target process's local monotonic
clock; keep it distinct from `agent` execution and `worker_init`.
It is a timing observation, not a field containing fixture/history text.
`history_setup` is the canonical seed-stage name; there is no `history_seed`
alias. The Task11 fixture path uses an immutable per-conversation seed record
and overlays the approved history fixture before SDK preparation. Its timing
does **not** represent twenty archive-message reads or measure normal archive
retrieval performance. Report actual seed-record storage operations only when
observed; do not infer read counts from the fixture's twenty messages.
Only work performed for the measured request belongs here: prior run
registration/setup remains outside per-question stage latency. Reused
immutable seed records must not fabricate repeated setup work or reset job
ordering. The observer still measures the full callback body with its own
clock; do not add/subtract target stage times to manufacture end-to-end time.

When the synthetic backend wait itself is measured, use `StageObservation(name="synthetic",
duration_ms=<actual measured local duration>, outcome=...)`. The fixed
1,000-ms sleep is a configured synthetic workload, not an asserted measured
duration: retain actual scheduling/wait time. Do not relabel it `agent` or
`model`, and do not fabricate model-call dependency counts, model rounds or
token usage for this path. Other real pipeline stages, including
`history_setup`, remain separate observations.

Task11 target timing projections use these exact scopes:

| Shared stage | Observed interval |
|---|---|
| `history_setup` | Per-message immutable `seeded20` bootstrap read/conditional-create/overlay, never worker initialization or an assumed twenty archive reads |
| `worker_init` | Host-owned actual worker initialization, only when observed |
| `agent` | The actual Core `agent.turn` span, not the whole SDK prepare wrapper or individual model-call latency |
| `render` | The actual `teams.render` span |
| `synthetic` | The `benchmark.synthetic` span around only `await asyncio.sleep(1.0)`, excluding outer pipeline work |

Neither seed setup nor worker-initialization timing proves platform cold
start or a new boot. Preserve actual Agent/render pipeline observations
alongside the synthetic wait, but do not manufacture missing stages or sum
overlapping component/pipeline durations into observer end-to-end latency.
Synthetic scenario registration and the explicit rendered marker remain
mandatory; neither permits invented model dependency calls or token counts.

Successful callbacks return 200 with disposition `completed`, `typing`,
`duplicate`, or `late`. Identical duplicates never move the completion
timestamp. Late replies never convert censored work into success. Unknown
run/request after an observer restart returns 404; conflicting job/outcome
returns 409. Schema errors return 400. The callback URL is fixed deployment
configuration, must equal the plan/manifest, and never comes from registration
or message request bodies.

## Authentication and cloud execution gates

Cloud uses the existing **`EntraApiValidator`**, RS256 fixed-tenant issuer,
dedicated v2 **API application GUID** as the token-validation audience, and
`Benchmark.Invoke`. No Bot/ARM audience, anonymous cloud mode, default
credential-chain fallback or automatic key fallback is provided.

`AuthPlan` separates token validation from token acquisition:

```json
{
  "audience": "00000000-0000-0000-0000-000000000002",
  "scope": "api://00000000-0000-0000-0000-000000000002/.default"
}
```

The GUID is schematic, not a configured application. For single-tenant v2
tokens `aud` must equal the application GUID; the `api://...` resource URI
is used only in the requested scope and is **not** accepted as token `aud`.
The deployment manifest's `dedicated_benchmark_audience` is also that GUID.
`AuthPlan` explicitly contains tenant, audience, scope,
driver managed-identity client ID, and separate operator and target
client/principal allowlists. Operator identities cannot call replies;
target workload identities cannot start load. Missing/invalid credentials
return 401; identity-service failure returns 503 without downgrading auth.

Cloud receiver binds `PORT` (default 8080), waits for an authorized
`POST /bench/control/start` with body `{}`, and runs **only** its configured
plan. Callers cannot supply a new target, callback or workload.
`GET /bench/control/status` is operator-only. Driver identity/HTTP resources
are prepared without probing the target. Operator control is separate from
target run registration and callback permissions.

### Operator access from the observer container

Use the operator's **ARM-authorized exec access to the existing observer
container**, then run the packaged helper below. Do not try to obtain the
application-only `Benchmark.Invoke` role using an ordinary delegated
`az account get-access-token` session. This helper does not provision
identities, grant roles, execute ARM commands, or collect cloud evidence.
Parent-owned deployment/bootstrap tooling supplies that outer exec operation.

```sh
python -m advisor_deploy.bench.control --plan /app/run.json status
python -m advisor_deploy.bench.control --plan /app/run.json start
```

The helper uses only `ManagedIdentityCredential` selected by
`plan.auth.driver_client_id`, which must come from the driver/receiver
UAMI's `identity_client_id` output (`AZURE_CLIENT_ID`), **not** the API
registration's client ID. The separate API GUID supplies the validation
audience and requested `api://<API-GUID>/.default` scope.
The container's `AZURE_CLIENT_ID` must be present, a valid GUID, and match
the plan's driver identity; all commands reject missing or conflicting
identity configuration before token acquisition, HTTP submission or proof writes.

Before invoking these commands, explicitly include the driver UAMI client
ID in `operator_client_ids` and its object/principal ID in
`operator_principal_ids`. Existing callback target allowlists remain
separate. The helper rejects a missing driver client allowlist entry and
checks the acquired token's driver application ID, allowlisted principal,
tenant, audience, v2 issuer and application role. These local claim checks
are sanity checks, not signature authorization: the receiver independently
verifies the signed token. No delegated-token, Bot-token, environment-secret
or anonymous fallback exists.

`start` POSTs `{}` and `status` GETs only the fixed
`/bench/control/{start,status}` paths on the configured callback **origin**.
There is no URL/token argument and redirects are never followed. Token
acquisition and the complete HTTP exchange each have a ten-second bound.
Responses are limited to 4 KiB and a known status-only JSON shape; raw
upstream bodies, tokens and exception details are never printed. Start's
`starting` response means control admission, not successful benchmark
answers. Non-expected HTTP statuses (including 409 for an already-started
run) are explicit errors; use `status` rather than assuming a second start
created work. Exit 0 reports a successful control operation, exit 2 a bounded
error code, and exit 130 an interruption.

### Supplying real zero observations through exec

Configure a cold plan with the local proof path
`/app/results/zero-evidence.json`. After collecting actual ARM/Azure Monitor
observations using the parent-owned operator procedure, pass their
**nonsecret JSON list** explicitly:

```sh
python -m advisor_deploy.bench.control --plan /app/run.json write-zero --json "$ZERO_OBSERVATIONS_JSON"
# Explicitly clear an older snapshot without claiming zero:
python -m advisor_deploy.bench.control --plan /app/run.json write-zero --json '[]'
```

`ZERO_OBSERVATIONS_JSON` above is supplied operator data, not a generated
example or proof. The helper never creates timestamps, converts a nonzero
replica count to zero, queries application health/status for evidence, or
attests that a supplied observation genuinely came from Azure. ARM/Monitor
collection and interpretation remain the operator's responsibility.

Input is limited to 16 KiB of strict UTF-8 JSON with no duplicate fields.
Every item must satisfy the existing `ZeroProof` schema and match the
plan's expected target ID. Only explicitly supplied `replicas: 0` is
accepted. ACA accepts group `app`; Flex accepts `http` and `worker`, at most
one current observation per group. Timestamps must be timezone-aware, not
future, and no older than `plan.zero_wait_seconds`. Unknown fields,
non-finite values, duplicate groups and unsupported sources are rejected.
Original validated JSON bytes are preserved; no fields are added or
normalized. Partial Flex snapshots are allowed but do not satisfy the
runner's requirement for both scale groups after that attempt's registration.

The sole destination is `plan.zero_evidence_path`: a direct regular file in
the existing `results` directory beside the plan (`/app/results` for
`/app/run.json`). No output-path argument exists. Traversal, nested/escaped
destinations, symbolic links/junctions and existing hard-linked destination
files are rejected. A private temporary file is flushed and atomically
replaces the destination; rejected input or failed replacement preserves
the previous snapshot and removes the temporary file. This command acquires
no token and makes no network call. It updates only the proof snapshot read
by the existing cold-wait loop; it neither starts load nor claims a verified
cold result.

Create an explicit cloud plan, not a modified anonymous example:

* `local_test: false`, HTTPS target/callback, actual expected target resource ID.
* `auth` as above and a verified `deployment` (`DeploymentBounds` in `plan.py`).
* `results.account_url` points to the already-provisioned observer Blob account;
  `results.container` is exactly `benchmark-results`. The manifest's
  `results_account_url` must match. No SAS, account key, connection string,
  public container or container creation fallback is accepted.
* Deployment snapshot includes target/endpoint/callback, region, host/mode,
  commit and artifact/config/dataset hashes, Python/model/SKU/version/retrieval
  snapshot, memory, workers/processes, instance limits, queue batch/polling,
  min replicas/Always Ready, approved max concurrency/requests/submission
  duration, driver CPU/memory and exactly one driver process/replica.
* Nonexpired manifest with explicit verified config, cloud smoke, capacity
  and Entra registration gates. The harness checks declarations/bounds;
  it does **not** independently attest that cloud validation happened.
* Deploy Task11 routes and the fixed callback configuration; grant the
  benchmark role to the driver and target identities; validate real model,
  search, queue/storage and Teams paths before experiments.
* Use `-AllowCloud` / `--allow-cloud`. Run host profiles sequentially,
  rotating their order; don't contend for the same model quota concurrently.

Cloud provisioning/configuration/Entra registration/capacity remain external
deployment gates. Merely supplying a manifest is not proof of functionality.

## Limits, monitoring and cold evidence

Defaults: warm target 100, cold target 10, max 15 cold attempts, max 3,600 s
waiting for zero each attempt. Concurrency ladder 1,2,4,8,16,32,64,128.
Warm profiles first run three separately persisted `warmup` requests using
a distinct run ID; these are never pooled into the measured warm samples.
Observed new-boot/retry/cache/degradation labels remain in all raw samples,
including during nominally warm load.
Each cloud concurrency level requires at least 50 terminal observations and
180 s submissions, with 1,800 s maximum submission time. Single requests
and post-stop drain are bounded by 600 s each. Max logical requests per run
and max HTTP attempts additionally bound work. No next profile/level starts
after an adverse stop.

Every 60 s: stop for the same dependency's >=5% actual throttles in two
successive windows (each must contain throttles), or three increasing
window-end backlog counts after skipping the initial ramp window.
Absence of observations is **not** zero backlog/calls. Host crash/OOM,
overdue requests, excessive observer loop lag, or sustained observer CPU/
memory saturation stop further submissions. Driver CPU capacity ratio,
peak process memory where available, and maximum event-loop lag are stored
separately. Windows memory is marked unavailable rather than fabricated.

Cold runs accept supplied **real control-plane** observations from
`zero_evidence_path`, a JSON array:

```json
[{"target_id":"expected-resource-id","group":"worker","observed_at":"2030-01-01T00:00:00+00:00","replicas":0,"source":"azure_monitor","evidence_id":"control-plane-record-id"}]
```

The illustration is not evidence. The authorized collector must replace it
with actual ARM/Azure Monitor observations; this tool never invents one or
polls application health/status. Only observations after that attempt's
registration and not in the future qualify. ACA requires group `app`;
Flex requires **both `http` and `worker`** zero proof and first-workload boot
evidence. Supplied evidence without observed first-workload/new-boot data
stays `unverified`; waiting alone does not count. Missing proof times out
without a fabricated question or cold sample. Restart/deployment/scale-out
are not substituted for natural-zero samples.

All results retain actual counts/window, success/degraded/failed/unknown/
expired/ignored/censored splits, separate latency distributions/ranges and
safe per-request telemetry. Throughput denominator includes drain. Small
tail samples are labeled exploratory. Reports never infer an SLA or
absolute platform maximum, and contain no fabricated cost results.

### Per-stage latency reports and missing coverage

The generated report includes an **Observed per-stage latency** section for
all existing stage names: `ingress`, `queue_wait`, `worker_init`, `auth`,
`connection`, `history_setup`, `synthetic`, `agent`, `kb`, `github`, `web`,
`model`, `render`, `send`, `history_read`, and `history_commit`. By default it
uses supplied `telemetry[].stages[]` observations, correlated by `request_id`
to recorded samples; duplicate telemetry records, missing request IDs and
orphan records are rejected rather than double-counted. The optional strict
completion-log import below supplies post-callback observations without
changing the original result.

Each report is scoped to its run, profile and explicit plan scenario
(`synthetic` or `real_agent`). A missing legacy scenario is labeled
`unknown`, never inferred from a stage name. Results from different
scenarios/runs are not pooled. Within that scope, request outcomes are
separate cohorts: success, degraded, failed, unknown, expired, ignored and
censored. Each cohort has two tables:

* **Coverage:** stage observation count, distinct requests with at least one
  observation / total cohort requests, missing/unknown request count, and
  coverage percentage. Repeated spans for one request increase observation
  count but count only once toward request coverage. Requests with no
  telemetry or an empty stage list remain in the denominator. Coverage is
  for the stage overall, not conditioned on a particular stage outcome.
* **Latency:** count, mean, P50/P95/P99, minimum and maximum in milliseconds,
  separately for every observed stage outcome (`success`, `degraded`,
  `failed`, `unknown`, `empty`). The distribution consists of individual
  observed spans, including repeated calls, not summed per-request stage
  time. Quantiles use the existing linearly interpolated percentile helper.

An absent stage is shown as `not observed`, count 0 and `n/a` timings.
Missing means unknown coverage, not measured zero, success, failure, or
proof that a stage did not execute. A genuine observed `0 ms` is retained
in its distribution. An empty run shows `0/0` coverage with `n/a`, not a
fabricated percentage. ACK durations, token counts, dependency call counts,
observer processing overhead and bootstrap flags are never converted into
missing stage durations. Small stage samples and tail quantiles remain
exploratory; coverage does not establish instrumentation completeness or
an SLA.

Stages can overlap or nest and are **not additive**. In particular, do not
sum synthetic/model subspans with Agent/pipeline spans to manufacture total
latency. Cross-process monotonic clocks are not directly subtracted.
End-to-end statistics still use the same observer's complete callback-body
reception timestamp after eventual authentication/validation, not any
stage sum. Stage coverage includes all recorded requests in the run,
including drain and censored work; stage observations do not turn a
censored request into a completed answer.

Production coverage remains the host/telemetry integration's responsibility.
If ingress, queue waiting, initialization, auth/connection or full send
duration is not carried in the existing safe stage list, the report exposes
that gap as missing coverage rather than estimating it. Only measured local
durations with the existing stage names/outcomes belong in those fields;
no raw question, reply or token content is needed for this report.

#### Post-run completed-stage log import

Full ingress, final callback HTTP/ACK completion, and durable history commit
can finish after the original callback has already been serialized. Import
their **actual completed observations** from a separately collected safe
export; do not estimate them from callback ACK time or missing values:

```powershell
uv run --no-sync --package advisor-deploy python -m advisor_deploy.bench report `
  --input deploy\out\bench\ACTUAL-RUN-ID.json `
  --stage-logs deploy\out\bench\completed-stages.json `
  --output deploy\out\bench\ACTUAL-RUN-ID-with-stages.md
```

This command performs no Azure query. Parent/operator collection must select
exactly the fixed `StageCompletion` fields from
`teams_adapter.cloud.benchmark_observations` and save a UTF-8 **JSON array**:

| Required field | Contract |
|---|---|
| `event` | Exactly `benchmark_stage_completed` |
| `version` | Integer `1`, not a boolean |
| `job_id`, `trace_id` | Exact canonical callback correlation identifiers |
| `boot_id`, `segment_id`, `span_id` | Existing bounded identifiers identifying the completed process span |
| `stage` | Existing `StageObservation`: allowed `name`, finite nonnegative `duration_ms`, allowed `outcome` |

This completed-log import is restricted to `ingress`, `history_read`,
`history_commit`, and `send`, matching the authorized completed-phase
producer. All other stages remain callback-sourced; attempting to import
another stage scope is rejected.

Do not pass ordinary log lines, Azure query/table envelopes, JSONL, or the
logger's additional `timestamp`, `level`, `logger`, or `message` fields.
Question/reply/token/URL fields and all unknown fields are rejected, not
silently stripped. The import is bounded to 32 MiB and 100,000 input records;
duplicate JSON fields, non-finite numbers and malformed encodings are rejected.
The report validates this exact export contract with shared types, without
adding a Teams/runtime dependency to the two-wheel observer package.

Log identity is exactly **`(boot_id, segment_id, span_id)`**. Identical
repeated records are deduplicated; any differing duration, outcome, stage,
job, or trace under one identity rejects the import. Different identities
remain real observed attempts even if their stage names/durations coincide.
Each unique event must match exactly one original callback record by
**both `job_id` and `trace_id`**. Missing callback correspondence, missing
trace/job identifiers, unmatched pairs, or two requests claiming one pair
reject the entire import. Request IDs, run labels and timing proximity are
not substitutes for this correspondence. In particular, a request with no
canonical callback trace/job cannot be reconstructed from these logs alone.

Callback stage records do not carry process span IDs. To avoid guessing
cross-source identity or double-counting, completed logs take precedence
for a matched **request/stage** in the four supported phases: only its unique log spans are used for that
stage, and its callback observations are explicitly counted as superseded.
Other callback stages remain. This is a visible source-selection policy,
**not** a claim that callback/log identities were matched heuristically.
A partial export can supersede a larger callback list; collect the intended
complete window and inspect supersession/counts. The original callback
evidence remains intact for comparison.

With an import, the report adds per-stage callback/log observation and request
coverage, and keeps their outcome distributions separate. Overall request
coverage uses the selected observations; it does not certify that every span
or retry was collected. Missing stages remain unknown. Completed
`history_read`/`history_commit` observations are not aliases for the fixture's
`history_setup`. Imported ingress/send/history intervals can extend beyond
callback reception or the original run window; they never change sample
outcomes, callback-body end-to-end latency, throughput denominators, or cold
classification. Process boot/segment/span IDs are correlation keys, not
new platform boot/instance evidence.

Provenance includes links to the original result and separate import file,
their **original-byte SHA-256** hashes, input/unique/duplicate event counts,
and superseded callback count. The CLI refuses to overwrite either input,
including through a hard-link alias. It does not rewrite or append timings
to the raw result JSON.

If the collector recorded an actual query window, pass both optional
`--stage-log-window-start` and `--stage-log-window-end` as timezone-aware
ISO-8601 values. Partial, reversed or malformed windows are rejected.
There are two strictly separated collection formats:

* A JSON array of the exact DTOs above. Since the DTO has no timestamp, the
  supplied window is **operator-declared and not independently checked
  against event times**.
* A JSON array of exact two-field collection wrappers:
  `{"timestamp": "<actual UTC TelemetryFormatter emission time>", "completion": <exact DTO>}`.
  The wrapper is report-collection metadata, not a change to the runtime DTO.
  The exporter must copy the original log envelope's UTC emission timestamp
  and select only the exact completion DTO fields. No `level`, `logger`,
  `message`, body or other envelope fields are accepted.

Do not mix the two formats in one file. In the timestamped format every
emission timestamp must be UTC and, when a window is supplied, lie within
its inclusive bounds. This check happens **before deduplication**, so an
out-of-window duplicate is not hidden. Identical completion DTOs emitted at
different times remain one observation: emission time is not part of the
process-span identity. The report records the observed min/max emission
range and distinguishes timestamp-checked windows from declarations.
Emission timestamps are not used to calculate stage durations.

Without window arguments, the query window remains explicitly unknown,
even when actual emission timestamps are available; it is not manufactured
from the run or event range. File hashes and timestamp checks do not attest
Azure collection or establish complete attempt coverage.

## Packaging, persistence and shutdown

Use the separate benchmark helper; **do not** extend the application
packager's three-online-wheel allowlist:

```powershell
.\deploy\bench\Build.ps1 -Plan .\deploy\bench\run.example.json -Output .\deploy\out\bench-context
uv run --no-sync --package advisor-deploy python -m advisor_deploy.bench.build verify deploy\out\bench-context
# Optional local Linux image build and network-disabled import probe:
.\deploy\bench\Build.ps1 -Plan .\deploy\bench\run.example.json -Output .\deploy\out\bench-context -ImageTag advisor-bench:local
```

`advisor_deploy.bench.build` performs a `uv export --locked --package
advisor-deploy --no-dev --no-editable --no-emit-workspace` and checks the
export's complete registry package/version/hash set against the selected
lock closure, including requested extras. It builds **exactly**
`advisor-shared` and `advisor-deploy` pure-Python workspace wheels, adds their
SHA-256 requirements, and rejects other workspace/development distributions.
The application package validator remains unchanged and still rejects a
deployment-tools wheel. The direct `aiohttp` and `azure-storage-blob`
dependencies must be declared and locked by the package owner; missing
declarations fail before packaging rather than being appended ad hoc.

Only the two wheels, hash-complete requirements, benchmark Dockerfile,
allowlist `.dockerignore`, `runtime-config/benchmark-cases.json`, sanitized
`run.json`, and `benchmark-manifest.json` enter the context. The dataset
always comes from the repository's `deploy/bench/cases.json`, not an
alternative plan-selected source. The plan's canonical dataset digest is
verified and its packaged/default path becomes
`runtime-config/benchmark-cases.json`. Secrets, `.env`,
development environments, Bot/application/ingestion wheels and the rest of
the workspace are not copied. Source/lock changes during packaging abort.
The manifest records source and lock hashes, artifact hashes, exact exported
dependencies, the uv version, Linux/amd64 + Python 3.12, and verification
state. Builds use the existing sanitized tool environment and fixed
`SOURCE_DATE_EPOCH`; repeated builds from unchanged inputs are tested for
identical payload hashes.

The optional image command requires an existing **local Linux Docker
engine**. It never starts/restarts Docker, changes services, pushes images,
or connects to a remote engine. It resolves the official Python 3.12 slim
image, records the actual immutable base reference/digest, supplies that
reference as `PYTHON_IMAGE`, installs binary locked dependencies with
`--require-hashes`, and imports the harness in a `--network none` container.
It verifies that only the two intended advisor distributions are installed
and that Agent/Teams/ingestion modules are absent. Successful import probing
records the local image ID and `linux_imports_verified: true`.

For a reproducible repeat image build, pass the **actual recorded**
`base_image_reference` using `-BaseImage` (or the Python image command's
`--base-image`), rather than resolving the mutable tag again. An explicit
base reference must be digest-pinned official Python. `-DockerContext`
selects an existing local context without changing the global context.
Both flags require `-ImageTag`. A context-only build intentionally records
`base_image_digest: null` and `linux_imports_verified: false`; that is not
evidence of an image build. Engine/network/build failures never set a
verified flag or invent a digest.

One driver process/replica is mandatory. **Cloud evidence is automatically
persisted to the existing private `benchmark-results` container**, using the
same explicitly selected driver `ManagedIdentityCredential` and
`azure-storage-blob` async SDK. Startup checks container existence/private
access; it never creates an account/container or changes its policy.
`ADVISOR_BLOB_SERVICE_URL` and `BENCHMARK_RESULTS_CONTAINER`, when configured
by the provisioned observer, must match the plan. Driver storage permissions
remain a deployment gate, not something this tool grants.

Per run, the sink writes:

* `<run_id>/events/<sequence>.json`: immutable allowlisted events.
* `<run_id>/checkpoint.json`: current committed sequence, plan, outstanding
  request intents/starts and completion count; never reply/question/token text.
  Its intended terminal state alone is **not** a completion commit.
* `<run_id>/final.json`: authoritative **last commit**, written only after
  the final checkpoint attempt. It contains the validated safe result,
  including right-censored work and interruption/evidence-failure status.
  If the checkpoint fails, any final file published reports
  `observer_evidence_failure`, never a clean `target_reached`. Missing final
  means completion was not committed; retain safe earlier events/checkpoints.

A durable request intent is written **before** registering the per-request
start clock or submitting HTTP. A crash after intent but before a start record
is explicitly unknown/unstarted, not a fabricated measured duration.
Start/ACK/final/censor events use a bounded queue; final callbacks are acknowledged
only after their safe completion evidence is durable. The callback timestamp
remains full-body reception, not storage completion.

`ResultsPlan` defaults: 256 pending events, ten-second operation deadline,
30-second bounded flush, periodic checkpoint every ten events plus critical
intent/completion checkpoints, and 32 MiB maximum record. SDK retries are
disabled; there is no unbounded hidden retry loop. Queue overflow, storage
loss/timeouts, or oversized evidence stops new
submissions and reports **`observer_evidence_failure`**, never a target capacity
limit. Driver I/O count/seconds/maximum are recorded separately; durable
write-ahead work and backpressure can limit throughput, so assess observer
overhead before attributing throughput to the target.
These counters are the pre-finalization snapshot; they do not claim to
measure the checkpoint/final upload that publishes the counters themselves.
Unknown payload fields and non-finite evidence are rejected before persistence.

Previously committed events/checkpoints survive process loss. Handled
interruption flushes censored records and writes an interrupted final result.
If storage itself is unavailable, already committed evidence is retained,
final upload is attempted within bounds, and an atomic local result is kept
in the configured output directory for operator recovery. Recover/export
that fallback before deleting/replacing the observer; its ephemeral container
filesystem is **not** claimed durable. Local mode explicitly uses atomic
filesystem events/checkpoints/results, not Azure or anonymous cloud storage.
There is no public results route.

Finalized level snapshots are written even on handled interruption.
Sudden process loss cannot reconstruct the original monotonic clock:
keep the previous results, classify unobserved work as unknown/censored
from independent evidence, and start a **new run ID**; never fabricate
missing latency or resume it with a new observer clock.

Stopping submissions/censoring is **not cancellation of target jobs**.
Review and explicitly wait for or cancel unresolved target jobs before
closing experiment resources. The harness does not delete target state,
disable production routes, restore replicas or silently discard server
work. Those operations remain the guarded deployment/cleanup workflow.

## Local verification

Dedicated tests use real aiohttp client/server boundaries and signed v2 JWTs
with an injected local signing-key provider (no Entra service calls). Durable
tests use the real Azure Blob SDK and **local Azurite** at
`http://127.0.0.1:11000/devstoreaccount1`; their isolated containers are created
and deleted only by the test fixture using Azurite's published development
credential, never by the production sink:

```powershell
$env:ADVISOR_BENCH_AZURITE = '1'
uv run --no-sync pytest shared\tests\test_benchmark_protocol.py deploy\tools\tests\test_bench_receiver.py deploy\tools\tests\test_bench_runner.py deploy\tools\tests\test_bench_report.py -q
```

The real tests cover final/checkpoint/event persistence, interrupted load and
zero-wait preparation, missing-container rejection, storage-loss stop and
local fallback, bounded queue overflow, and payload exclusion. They do not
establish cloud managed-identity/RBAC or production target functionality.

Packaging tests exercise real locked export/wheel builds, payload-hash
reproducibility, extra-file/tamper rejection and the unchanged app-wheel
allowlist. To explicitly attempt the real Docker build/import test with an
already-running Linux engine:

```powershell
$env:ADVISOR_BENCH_DOCKER = '1'
uv run --no-sync pytest deploy\tools\tests\test_bench_build.py -q
```

An unavailable engine is an explicit verification blocker, not a passing
Linux check. The tests/helper do not repair or restart shared services.
