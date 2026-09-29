# Stage definitions and timing boundaries

[中文](../2026-09-20-cn/stage-definitions.md) · [Experiment summary](summary.md)

This reading guide clarifies the **2026-09-20 experiment archive**. It adds no
measurements and makes no claim about the current cloud state. The relevant
instrumentation matches experiment revision `4910058`.

## The key distinction: Agent turn contains Model calls

**They are separately instrumented, but they are not separate, non-overlapping
phases.** `agent` measures the whole core turn; each `model` observation measures
one chat-completion SDK call **inside that turn**. Tool execution also happens
inside the turn, between model calls. Therefore:

- Do **not** calculate total latency as `Agent turn + Model call + KB + GitHub`.
  That would count nested work more than once.
- A question can produce multiple model-call observations. The model row's
  mean/P95 describes **calls**, not total model time per question.
- The Agent row includes orchestration and waiting, not just local Python CPU
  time and not just the model's answer generation.

### What exactly is inside Agent turn?

The `agent.turn` span starts before the core's per-conversation turn coordinator
and ends after response/event construction, before the event sink runs. It covers:

1. Waiting for and holding the core's turn coordinator, request context and
   query planning (the default planner can be a no-op).
2. Reading the core's session store. In this cloud worker that is an
   **in-memory `BufferedSessionStore`**, populated from history loaded earlier.
3. Preparing model messages and running the backend tool loop: one or more chat
   completions; parsing/dispatching model tool requests; awaiting retrieval or
   other tools; adding tool results to messages; subsequent completions.
4. Answer postprocessing, including citation selection and mention assembly;
   configured evaluation (the default evaluator can be a no-op).
5. Staging the user/assistant turn in the buffered session store on a successful
   answer, or constructing the fallback response on backend failure.

It does **not** include initial HTTP admission, the outer durable context/job
lease acquisition, the worker's archive read or durable history commit, initial
model/client construction, final Teams-format rendering, or callback delivery.
The core's own turn coordinator is different from the worker's earlier durable
context lease. Buffered `session.read`/`session.write` inside Agent are different
from `history_read`/`history_commit` outside Agent.

### Typical real-Agent question in this experiment

```text
Outside Agent: admission, worker scheduling/setup, archive history read
Agent turn
  core turn coordinator -> planner -> buffered session read
  backend: prepare messages
    Model call 1: request tool use
    dispatch search_solutions
      KB: embedding -> Azure AI Search -> result filtering
      GitHub live search (runs concurrently with KB)
      merge results and return them to the backend
    Model call 2: produce answer using tool results
  postprocess -> evaluator -> buffered session write
Outside Agent: render -> durable history commit -> final callback send
```

This is a logical nesting diagram, not a complete serialized trace. The backend
can take other branches/more rounds; setup and queue-wait observations can
overlap. The fixed `kb-01` samples commonly have two model calls; two is not a
general protocol guarantee.

For example, warm cohort A has **100 Agent spans** (mean **6,525.426 ms**) and
**200 Model spans** (mean **3,019.782 ms per call**). The latter is already inside
the former: adding these means does not produce a valid turn or reply latency.
Nor can P95 values from separate distributions be added or subtracted to obtain
the P95 of another interval.

## Every reported stage

Durations are elapsed time, including waits within the instrumented scope.
Not every stage appears for every request. Empty-history, real-Agent questions
do not exercise seeded-history setup or the synthetic workload.

| Report label / wire stage | What is timed | Boundary and interpretation |
|---|---|---|
| Ingress handler / `ingress` | Target host HTTP-handler invocation, through handler return; includes application admission/authentication/validation and durable acceptance work inside that handler. | Excludes time before the application handler starts, such as platform activation and inbound transit. Not the client's full ACK time. |
| Acceptance to validated attempt / `queue_wait` | Persisted `accepted_at` UTC to the worker's first validated observation of that job in the attempt. | Includes admission publication, scheduling, worker setup/validation, and potentially earlier retry delay. Not pure queue-service transit; can overlap `ingress`, `worker_init` or `connection`. Invalid/negative clock intervals are omitted, not clamped to zero. |
| Worker object initialization / `worker_init` | Actual lazy worker-factory construction when no reusable worker exists. | Not complete container/VM/process boot time; warm requests may have no initialization observation. Within a drained batch, initialization is not charged again to every job. |
| Authentication / `auth` | The benchmark ingress `auth.verify(headers)` call. | A sub-operation of ingress. Not all Entra/token work in the system; excludes outbound callback token acquisition and observer callback authentication. |
| Lazy component initialization / `connection` | Observed host/client construction; the real-Agent path includes lazy model/search component creation before core execution. | Not a measured DNS lookup, TCP/TLS handshake, or Azure replica-start interval. Host construction and lazy construction can be separate observations. |
| Seeded history setup / `history_setup` | For `seeded20`, loading or creating and validating the deterministic seed document used as the question's history overlay. | Before core execution, outside Agent; not 20 archive reads and not the normal history-commit operation. |
| History archive read / `history_read` | The worker's `history.get()` against an already leased context before preparing a new answer. | Outside Agent; excludes earlier context-lease/projection acquisition. Very small empty-history timings do not establish general Blob read latency. |
| History commit / `history_commit` | Durable `history.commit(...)` for the prepared turn before delivery, if there is history to commit. | Outside Agent; distinct from the buffered session write. Surrounding job-ledger/checkpoint updates are not all included in this span. |
| Synthetic / `synthetic` | The explicit benchmark synthetic workload, instead of the real Agent. | Not model inference. Synthetic and real-Agent results must not be pooled. |
| Agent turn / `agent` | The complete `agent.turn` scope described above. | Parent interval containing the chat-model/tool/retrieval work, but excluding rendering and durable archive I/O. |
| KB operation / `kb` | One `search.kb` source operation: query embedding, Azure AI Search hybrid/semantic retrieval including pager consumption, result filtering/conversion, within its budget. | Inside Agent's tool execution; includes embedding latency, which is **not** a chat `model` span. May overlap GitHub search. |
| GitHub operation / `github` | One budgeted `search.github-live` source operation, including awaiting/processing its result or failure. | Inside Agent; runs alongside KB in combined search. `empty` means no results, not a failed HTTP request. |
| Web operation / `web` | An observed `search.web` provider operation, within the configured search scope/budget. | Inside Agent when invoked; multiple provider/scope attempts can yield multiple spans. No observation does not mean zero cost or zero duration. |
| Model call / `model` | One `llm.completion` around `chat.completions.create(...)`, through response/usage extraction. | Inside Agent; includes SDK/network/service waiting and any SDK retries within that call. Excludes subsequent tool execution and embeddings. Not pure server-side inference time. |
| Render / `render` | `teams.render`: convert the core response into the outgoing Teams-compatible reply representation. | After Agent returns; no model call, network delivery, or Teams-client display is timed here. |
| Completed callback send / `send` | `benchmark.send`: final callback HTTP request through receipt and validation of the observer's acknowledgement. | Outside Agent; excludes separately instrumented callback-token acquisition and excludes typing sends. Can end **after** the observer's full-body reception endpoint used for end-to-end latency. |

## End-to-end latency, ACK and evidence sources

- **Full-reply latency:** from registration of a logical question immediately
  before submission to complete callback-body reception at the observer, using
  that process's monotonic clock. The reply is accepted only after authentication
  and complete-message validation. This is an **application-link** measurement,
  not time to first token or Teams-client display. Preparation, warm-up and
  idle waiting for the cold zero-proof are outside that question's timer.
- **ACK latency:** elapsed submission/admission response time, not completion.
  `202` and typing events do not release an outstanding-question slot. A valid
  callback may arrive before the ACK.
- **Completed logs:** ingress/history/send logs are joined to callbacks by
  exact `job_id` **and** `trace_id`; their matching stage observations replace,
  rather than duplicate, callback observations. Full ingress and send may
  finish too late to be present in the original callback snapshot.
- **Coverage/count:** coverage counts distinct requests with a stage;
  observation count counts spans. Retries/multiple calls can increase spans
  without increasing the number of questions. Missing coverage is unknown,
  not a zero-duration sample.
- **Clock boundary:** local spans use their own elapsed clocks; `queue_wait`
  is the specific persisted-UTC interval described above. Do not subtract
  arbitrary cross-process timestamps or sum overlapping spans to reconstruct
  end-to-end latency.

Platform allocation/image startup, inbound routing, individual network
handshakes, some durable lease/ledger operations and observer work are not
independently decomposed by this stage table. The gap between ACK and ingress
cannot be called a measured pure cold-start duration.

### Model call count is not model HTTP-attempt count

In cohort B, the dependency counter `model.calls` records observed HTTP-client
attempts for **chat plus embeddings**, including SDK retries/redirects.
`model_rounds` and the `model` latency rows instead come from chat-completion
spans. Thus two chat calls plus one embedding can produce **2 Model spans but
3 model HTTP attempts** even without retries. A `RateLimitError` completion is
not one-to-one with an HTTP 429 attempt. Unavailable attempt evidence remains
unknown; older cohort A is not retroactively treated as having these counters.

## How to read concurrency results

`Concurrency = N` is the limit on **outstanding logical questions** in the
driver, not N HTTP connections, N model calls, N worker processes or N Azure
replicas. Each slot waits for a terminal reply/outcome or its deadline, not just
the admission ACK. Independent conversations can run concurrently; same-context
work can serialize through durable coordination.

- Successful latency distributions include only `success`. `degraded` has a
  completed but degraded reply; `failed`, `unknown` delivery, `expired`,
  `ignored` and unfinished `censored` remain separate outcomes.
- **Answers/s** counts `success + degraded` over the actual measurement window,
  including drain; **successes/s** counts only `success` over that same window.
  Neither is HTTP-ACK throughput or the configured concurrency value.
- A lower successful-only mean at higher load can hide worse degradation or
  censoring. Read outcome counts, stage failures, dependency throttles, window
  duration and replica metrics together.
- Here C4 had 76/76 successes without degradation; C16 stopped on
  `request_deadline`, so C32/C64/C128 were not run. This establishes neither an
  SLA nor the platform's absolute maximum. One-minute resource metrics are not
  per-request stage measurements.

## Source boundaries

- [Core turn and buffered sessions](../../../agent/src/advisor_agent/core.py),
  [session-store implementations](../../../agent/src/advisor_agent/sessions.py).
- [Chat/tool loop](../../../agent/src/advisor_agent/maf_backend.py),
  [parallel combined search](../../../agent/src/advisor_agent/search/combined.py),
  [KB embedding/retrieval](../../../agent/src/advisor_agent/search/knowledge.py).
- [Durable history/worker boundaries](../../../agent/src/advisor_agent/durable/worker.py),
  [host timing](../../../channels/teams/src/teams_adapter/cloud/observability.py),
  [stage projection](../../../channels/teams/src/teams_adapter/cloud/benchmark_observations.py).
- [Ingress authentication](../../../channels/teams/src/teams_adapter/cloud/benchmark.py),
  [callback delivery and seed setup](../../../channels/teams/src/teams_adapter/cloud/benchmark_connector.py),
  [rendering](../../../channels/teams/src/teams_adapter/processing.py).

These links explain implementation boundaries; the archived raw evidence and
cohort bindings remain the authority for what was actually observed.
