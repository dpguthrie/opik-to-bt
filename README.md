# Opik → Braintrust migrator

A resumable Python 3.13 CLI for moving Opik prompts, datasets, experiments,
traces/spans, scorers, online evaluation rules, human-review scores,
annotation queues, and dashboards into Braintrust. It supports Opik Cloud or
self-hosted Opik and Braintrust US, EU, or self-hosted deployments.

## What it migrates

| Opik | Braintrust | Selection |
|---|---|---|
| Projects | Projects | `--projects` |
| Datasets and items | Datasets and records | `--datasets` |
| Experiments and results | Experiments and events | `--experiments`, `--start`, `--end` |
| Traces and spans | Project logs | `--start`, `--end` |
| Prompts | Prompts | `--prompts`, `--prompt-history` |
| Online eval scorers | Scorer functions | `--resources scorers`, `--scorers` |
| Online eval rules | Online scoring automations | `--resources online-evals`, `--online-evals` |
| Feedback definitions | Human-review score widgets | `--resources review-scores`, `--review-scores` |
| Annotation queues | Review views + flagged logs | `--resources annotation-queues`, `--annotation-queues` |
| Dashboards | Monitor views + custom charts | `--resources dashboards`, `--dashboards` |

`--start` is inclusive and `--end` is exclusive. Dates apply to experiment
creation time and root trace start time; all child spans of a selected trace are
preserved. Datasets, prompts, scorers, online evals, review scores, annotation
queues, and dashboards are not inherently time-bounded.

`--resources all` includes scorers, online scoring rules, human-review scores,
annotation queues, and dashboards.

Migrating `online-evals` creates Braintrust automations that score **new
production traffic**. Historical scores already on traces are copied with logs;
the new rules do not rewind those rows. Omit `online-evals` if you want the
scorer definitions without attaching live scoring.

Migrating `annotation-queues` creates Braintrust Review views and flags the
**current queue backlog** as unassigned Awaiting review. It does not assign
reviewers or lock items. Human scores already on traces are copied with logs;
`review-scores` adds the widgets SMEs use to keep labeling. Omit
`annotation-queues` (for example `--resources datasets,experiments,logs,prompts,scorers,review-scores`)
if you want the score widgets without flagging a backlog.

Migrating `dashboards` creates Braintrust Monitor views with generated custom
charts for production widgets (trace volume, latency percentiles, tokens, cost,
scores, errors). Workspace dashboards are copied into each selected project.
Experiment dashboards, radar charts, markdown, and built-in Insights overviews
are inventoried and skipped. Custom charts require a Braintrust Pro or
Enterprise plan. Widget filters are structured Opik predicates, not OQL; they
compile with the same filter-to-SQL helper as online evals.

## How it scales

The migrator does not download a complete row resource before uploading it. It
operates as a bounded pipeline for datasets, experiments, and logs:

```text
Opik pages → incremental transform/staging → bt sync → checkpoint
```

Extraction and upload overlap. Each transformed event is serialized once into
a rolling NDJSON staging file; the file rotates at the partition target even in
the middle of a large Opik page. Ready partitions live on staging disk rather
than in memory while they wait for `bt sync`. Independent projects, datasets,
logs, and prompt jobs run concurrently, while bounded queues prevent memory or
disk usage from growing with the total migration size. Dataset migrations
complete before dependent experiments.

Project-scoped prompts, scorers, online scoring rules, human-review scores,
annotation-queue views, and dashboards use the Braintrust REST API because
`bt sync` operates on data rows, not those definitions. The default `latest` mode retrieves each
Opik prompt's explicit `latest_version` and creates one Braintrust version. The
opt-in `all` mode paginates every Opik version and writes them oldest-to-newest
so the Braintrust prompt ends on the same latest content. Each source version
ID maps to its returned Braintrust `_xact_id` in the checkpoint.

Opik traces and spans are separate resources. The migrator paginates each
project-wide endpoint in bulk: each trace page becomes a bounded chunk, then one
paginated span scan retrieves the children for that entire chunk. A UUIDv7
trace-ID range lets Opik prune the span scan efficiently, and exact membership
is checked client-side. Traces become Braintrust root spans; spans become child
spans using their existing `trace_id` and `parent_span_id`. The migrator never
issues one span request per trace.

Runtime settings are automatic. The row-resource streams use 2,000-record
pages—the maximum Opik documents for those search APIs—to minimize requests.
Prompt containers and prompt versions use their endpoint limits of 1,000 and
100 per page, respectively. The migrator then considers available CPU, memory,
and staging disk to choose partition size, resource concurrency, upload slots,
and `bt sync` workers. Users select *what* to migrate; the tool manages *how*
it moves the data.

When `--end` is omitted, the tool records the run's start time as a stable
snapshot boundary for logs and experiments. New Opik activity cannot shift
subsequent pages during a long migration or change the scope of a resumed run.

The terminal shows extraction pages, row counts, upload partitions, and elapsed
time for every active stream. Successful `bt sync` subprocess output is folded
into this display; full output is retained in the error if a push fails.

Each immutable partition has:

- stable Braintrust event IDs;
- a durable source-page and in-page event cursor;
- independent `bt sync` state;
- bounded retry and upload behavior.

Opik requests share an adaptive request gate. When Opik returns `429` or a
transient server error, the migrator honors the server's reset window, adds
jitter, slows concurrent streams, and reports the pause in the terminal before
continuing automatically.

After a successful `bt sync` upload, the temporary NDJSON partition is removed.
Checkpoint and `bt sync` state remain under `.opik-to-bt/`. An interrupted run
restarts after the last uploaded event, including when that position is in the
middle of an Opik page.

Dataset records, experiment events, and logs go through
[`bt sync`](https://www.braintrust.dev/docs/reference/cli/sync), which provides
parallel, byte-bounded uploads, retries, and resumable upload state. Prompt
definitions use Braintrust's versioned prompt REST endpoints. Scorer functions
and online scoring rules use Braintrust's function and project-score REST
endpoints. Human-review scores use project-score widgets; annotation queues
become Review views and merge pending-review flags onto already migrated logs.
Dashboards become Monitor views with generated custom charts.

## Quick start

Install [uv](https://docs.astral.sh/uv/) and `bt >= 0.14.0` using the
[Braintrust CLI](https://www.braintrust.dev/docs/reference/cli/quickstart):

```bash
uv sync
cp .env.example .env
```

Set `OPIK_API_KEY` and `OPIK_WORKSPACE` in `.env`, then authenticate `bt` against
the destination or set `BRAINTRUST_API_KEY`. Prompt, scorer, online-eval,
review-score, annotation-queue, dashboard, and object-level dataset/experiment
tag writes specifically require `BRAINTRUST_API_KEY`; a `bt` login profile
alone cannot authenticate those direct REST requests. Change `OPIK_URL` and
`BRAINTRUST_URL` for self-hosted deployments.

Preview the selected scope:

```bash
uv run opik-to-bt \
  --projects support-bot \
  --start 2026-01-01 \
  --end 2026-02-01 \
  --dry-run
```

Run the migration:

```bash
uv run opik-to-bt \
  --projects support-bot \
  --start 2026-01-01 \
  --end 2026-02-01
```

Resources default to `all` (datasets, experiments, logs, prompts, scorers,
online evals, review scores, annotation queues, and dashboards). Optional
semantic filters remain available:

```bash
uv run opik-to-bt \
  --projects support-bot \
  --resources datasets,experiments,logs,prompts,scorers,online-evals,review-scores,annotation-queues,dashboards \
  --datasets golden-set,edge-cases \
  --experiments baseline,v2 \
  --prompts support-answer,route-request \
  --start 2026-01-01 \
  --end 2026-02-01
```

Because `all` includes online scoring, new production logs in Braintrust will be
scored after those rules are created. Omit `online-evals` to copy scorer
definitions without attaching live scoring. Omit `annotation-queues` to copy
human-review score widgets without flagging the current backlog:

```bash
uv run opik-to-bt \
  --projects support-bot \
  --resources datasets,experiments,logs,prompts,scorers,review-scores
```

Prompt history is intentionally opt-in:

```bash
uv run opik-to-bt \
  --projects support-bot \
  --resources prompts \
  --prompt-history all
```

The equivalent persistent setting is `OPIK_TO_BT_PROMPT_HISTORY=all`. Without
either setting, only the latest version of each selected prompt is migrated.

No performance flags are required. Keep `.opik-to-bt/` when moving or
restarting the job. Use `--no-resume` only when intentionally ignoring importer
completion markers. This also starts fresh `bt sync` upload state; stable data
event IDs make those row replays overwrite-safe. Prompt history depends on its
checkpoint for ordering. A prompt already migrated in `latest` mode cannot be
upgraded in place to `all`, because Braintrust cannot insert older versions
before an existing version; use a new state directory and remove or rename the
destination prompt first.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPIK_URL` | `https://www.comet.com/opik/api` | Opik Cloud/self-hosted API |
| `OPIK_API_KEY` | — | Opik API key |
| `OPIK_WORKSPACE` | — | Opik workspace |
| `BRAINTRUST_URL` | `https://api.braintrust.dev` | Braintrust US/EU/self-hosted API |
| `BRAINTRUST_API_KEY` | profile or environment | Braintrust authentication; prompts, scorers, online evals, review scores, annotation queues, dashboards, and object-level tags require an API key |
| `OPIK_TO_BT_PROMPT_HISTORY` | `latest` | Prompt version scope: `latest` or `all` |

Operational overrides exist through `OPIK_TO_BT_*` environment variables for
support and unusual deployments, but are deliberately omitted from the normal
workflow. See [Advanced configuration](#advanced-configuration) for every
selection, connection, retry, and performance control.

## Run in a container

The image includes `bt` 0.14.0:

```bash
docker build -t opik-to-bt .
docker run --rm --env-file .env -v "$PWD/.opik-to-bt:/app/.opik-to-bt" \
  opik-to-bt --projects support-bot --resources all
```

## Run on AWS

The [Terraform example](infra/aws/) creates an outbound-only Graviton EC2 runner
with no inbound SSH rule. It installs Python 3.13, `uv`, and `bt`, and is
accessed through Systems Manager. Its encrypted gp3 root volume holds rolling
partitions and checkpoints; it does not need enough space for the entire source
dataset.

The defaults are intended for sustained migrations and can be changed through
Terraform variables. Stop rather than terminate the instance if the checkpoint
must remain on its root volume.

## Mapping notes

- Opik trace and span relationships become Braintrust root spans and child
  spans. Opik has four span types, and all four are mapped: `llm` and `tool`
  keep their names, `guardrail` becomes `function`, and `general` becomes
  `task`. Anything unrecognized also becomes `task`.
- Opik tags become native Braintrust tags rather than metadata, mapped one to
  one: trace tags onto the Braintrust root span, span tags onto that same child
  span, and dataset item tags onto the matching Braintrust dataset record.
  Braintrust accepts tags on child spans as well as root spans, and stores each
  span's tags on that span. Migrated tag names do not have to exist in the
  destination project beforehand.
- Opik dataset-level and experiment-level tags stay object-level in Braintrust
  rather than being copied onto every row, because Opik has no per-record tags
  on experiment results and stamping the object's tags onto each row would
  invent data Opik does not have. `bt sync` writes only rows, so these tags are
  applied through Braintrust's REST API once the destination object exists,
  which also means they need `BRAINTRUST_API_KEY`: a `bt` login profile alone
  cannot authenticate them. Objects that received no rows are skipped, and the
  tags reapply on a resumed run even when the rows themselves are already
  checkpointed. The patch replaces the object's tag list, so tags added by hand
  in Braintrust on a migrated dataset or experiment are overwritten.
- Opik feedback in Braintrust's score range `[0, 1]` becomes scores. Numeric
  feedback outside that range becomes a custom metric and the original
  feedback objects are retained under `metadata.opik`.
- Test-suite results become one stable `Test suite passed` binary score per
  item (`1` for passed and `0` for failed). Suite-level and item-level
  assertions may differ by row, so assertion text is not used as a Braintrust
  score name. The full assertion breakdown, pass count, Opik status, and
  execution policy are retained on the scorer span metadata. Regular Opik
  feedback remains mapped to separate Braintrust scores.
- Experiment inputs and outputs preserve Opik's existing structured objects
  (for example, `{"question": ...}` and `{"answer": ...}`). If an older Opik
  response omits the explicit input object, all non-reserved dataset fields are
  used as the Braintrust input rather than assuming particular field names.
- Opik duration and time-to-first-token values are converted from milliseconds
  to seconds. Trace and span end times are derived from Opik's measured duration
  when available, avoiding open-ended Braintrust spans when Opik omits
  `end_time`. When a trace has children, its timing uses the child-span envelope
  so a stale reused Opik trace timestamp cannot inflate the Braintrust timeline.
  Token usage is normalized to Braintrust's canonical metric names, and Opik's
  estimated USD cost becomes `metrics.estimated_cost`.
- Trace-level usage and cost are aggregates of their spans. When child spans
  are present, only the span metrics use Braintrust's canonical names so trace
  summaries do not double-count them; the Opik trace aggregates remain under
  `metadata.opik`.
- Cross-object dataset origin IDs are omitted because the destination dataset
  ID is resolved internally by `bt sync`.
- Source identifiers and unmapped context are retained under `metadata.opik`.
- Opik text prompts become Braintrust completion prompts. Opik chat templates
  are decoded from their stored JSON and become Braintrust chat messages.
  Mustache remains Mustache; Jinja2 maps to Nunjucks, which is closely related
  but not perfectly syntax-compatible. Unknown template languages are retained
  as unrendered (`none`) templates.
- Prompt names, descriptions, and tags are preserved. Braintrust slugs are
  readable deterministic values with a suffix derived from the Opik prompt ID,
  avoiding collisions between names that normalize to the same slug.
- Opik's arbitrary per-version prompt metadata, original creator/timestamps,
  commit, version number, and change description do not have corresponding
  writable fields in Braintrust's public prompt-create schema. Source version
  IDs and the returned Braintrust `_xact_id` values remain in the local
  checkpoint. Opik environment assignments are not currently migrated.
- `prompt_data.origin` is deliberately not used for Opik provenance because
  Braintrust reserves it for references to other saved Braintrust prompts.
- Opik online evaluation rules split into two Braintrust objects: an LLM-as-judge
  scorer function and an online `project_score` that binds it to production
  logs. Span, trace, and thread rules become span, trace, and group scope.
  Thread grouping uses `metadata.thread_id`. Sampling and structured filters
  are translated; filters that cannot be compiled to SQL skip the online
  binding and leave the scorer in place. Custom Python metrics, multimodal
  judge messages, disabled rules, and experiment-only triggers are inventoried
  and skipped rather than silently dropped. A dry-run prints translate/skip
  per rule. Creating an online rule does not re-score already migrated logs,
  but it does start scoring **new production traffic** in Braintrust. The CLI
  prints a warning whenever `online-evals` is selected, including via `all`.
- Migrated traces copy Opik `thread_id` onto `metadata.thread_id` so grouped
  online scoring can find the same conversations after logs have been moved.
- Opik feedback definitions become Braintrust human-review project scores.
  Numerical `[0, 1]` ranges become sliders. Other numerical ranges still become
  sliders, with the original min/max recorded in the description; historical
  values outside `[0, 1]` remain metrics from log migration. Categorical maps
  become categorical widgets (values outside `[0, 1]` are rescaled). Booleans
  become two-category widgets using the Opik true/false labels. Workspace-level
  definitions are replicated into each selected Braintrust project. A definition
  that collides with an existing online score name is skipped.
- Opik annotation queues become Braintrust Review views (`for_review_project_log`)
  named after the queue. Current queue members are flagged with
  `~__bt_review_lists.__bt_default_review_list=PENDING` so they appear in
  Awaiting review. Reviewers are not auto-assigned. `annotators_per_item` and
  lock timeout have no Braintrust equivalent and are reported in the dry-run /
  log line. Thread queues group the Review table by `metadata.thread_id` and
  flag each trace in the thread. Selecting `annotation-queues` also writes the
  human-review score widgets those queues need. Flagging runs after logs for
  that project so the destination rows exist. The CLI prints a warning whenever
  `annotation-queues` is selected, including via `all`.
- Opik production dashboards become Braintrust Monitor views. Time-series
  widgets become time-series charts and stat cards become big numbers. Trace
  count, duration percentiles, token usage, estimated cost, feedback scores,
  error rate, and thread count compile to SQL measures against already migrated
  log fields (`metrics.duration` in seconds, `metrics.tokens`,
  `metrics.estimated_cost`, `scores.<Name>`). Widget filters use the same
  structured filter-to-SQL translation as online evals; they are not OQL.
  Breakdowns by name, tags, metadata, model, or provider become `groupBy`.
  Workspace dashboards are replicated into each selected Braintrust project.
  Experiment dashboards, radar charts, markdown, Insights overviews, and
  untranslatable widgets are inventoried and skipped. A dry-run prints
  translate/skip per widget. Custom charts require Braintrust Pro or Enterprise.

Dataset version history is not included. A future opt-in mode could map Opik
item history to Braintrust dataset snapshots.

## Advanced configuration

The automatic runtime is appropriate for most migrations. The controls below
are useful for narrowing scope, integrating self-hosted deployments, or tuning
a constrained or rate-limited runner. Settings are read from the process
environment first, then `.env`, then the defaults shown below.

### CLI flags

| Flag | Default | Controls |
|---|---:|---|
| `--projects NAME[,NAME...]` | All projects | Limits the migration to exact Opik project names. |
| `--resources all\|datasets,experiments,logs,prompts,scorers,online-evals,review-scores,annotation-queues,dashboards` | `all` | Selects resource types. `all` is every supported resource. Migrating `online-evals` starts scoring new production logs. Migrating `annotation-queues` flags the current review backlog. |
| `--datasets NAME[,NAME...]` | All datasets | Limits datasets by exact name within the selected projects. This does not select experiments that reference an excluded dataset. |
| `--experiments NAME[,NAME...]` | All experiments | Limits experiments by exact name within the selected projects. |
| `--prompts NAME[,NAME...]` | All prompts | Limits prompts by exact name within the selected projects. |
| `--scorers NAME[,NAME...]` | All scorers | Limits migrated scorer functions by the source online-eval rule name. |
| `--online-evals NAME[,NAME...]` | All online evals | Limits online scoring automations by the source rule name. Selecting `online-evals` also writes any scorers those rules need. |
| `--review-scores NAME[,NAME...]` | All review scores | Limits migrated human-review score widgets by the source feedback-definition name. |
| `--annotation-queues NAME[,NAME...]` | All annotation queues | Limits Review views and backlog flagging by the source queue name. Selecting `annotation-queues` also writes the review-score widgets those queues need. |
| `--dashboards NAME[,NAME...]` | All dashboards | Limits Monitor views by the source dashboard name. Workspace dashboards are copied into each selected project. |
| `--prompt-history latest\|all` | `OPIK_TO_BT_PROMPT_HISTORY`, then `latest` | Migrates only each prompt's explicit latest version or replays every version oldest-to-newest. The CLI flag overrides the environment setting. |
| `--start ISO-8601` | No lower bound | Inclusive UTC lower bound for experiment creation time and root-trace start time. A timezone-free value is interpreted as UTC. It does not filter datasets. |
| `--end ISO-8601` | Run-start snapshot | Exclusive UTC upper bound for experiments and logs. When omitted, the run start is checkpointed and reused on resume so new Opik data cannot move the boundary. |
| `--state-dir PATH` | `.opik-to-bt` | Stores the checkpoint, prompt-version map, rolling NDJSON partitions, and `bt sync` state. Preserve this directory to resume, including when running in Docker or on another machine. |
| `--resume` / `--no-resume` | `--resume` | Reuses importer checkpoints, prompt-version mappings, and `bt sync` state. `--no-resume` ignores importer completion markers and passes `--fresh` to `bt sync`; stable row event IDs keep row replay overwrite-safe, but prompt history still requires a clean or matching destination. |
| `--dry-run` / `--no-dry-run` | `--no-dry-run` | Inventories the selected scope without checking Braintrust authentication or writing destination data. |

`--help`, `--install-completion`, and `--show-completion` are standard CLI
utility flags and do not affect migration behavior.

### Connection and authentication variables

| Environment variable | Default | Controls |
|---|---:|---|
| `OPIK_URL` | `https://www.comet.com/opik/api` | Opik API base URL. Set this for self-hosted Opik. A trailing slash is removed automatically. |
| `OPIK_API_KEY` | Unset | Opik API key. |
| `OPIK_WORKSPACE` | Unset | Opik workspace used by the SDK. |
| `BRAINTRUST_URL` | `https://api.braintrust.dev` | Braintrust API base URL passed to `bt sync`. Set this for the EU endpoint or a self-hosted deployment. |
| `BRAINTRUST_API_KEY` | Unset | Braintrust API key inherited by `bt sync` and used for direct REST operations. When unset, `bt` can use its existing authenticated profile for row uploads, but prompts, scorers, online evals, review scores, annotation queues, dashboards, and object-level tags cannot be migrated. |

### Reliability and performance variables

These are advanced overrides. Leaving them unset lets the migrator size itself
from the runner's CPU, memory, and free staging disk.

| Environment variable | Effective default | Controls |
|---|---:|---|
| `OPIK_TO_BT_TIMEOUT_SECONDS` | `60` | Per-request Opik HTTP timeout in seconds. Must be greater than zero. |
| `OPIK_TO_BT_RETRY_ATTEMPTS` | `8` | Maximum total attempts for a retryable Opik request. The shared request gate honors server reset headers and applies bounded exponential backoff with jitter. |
| `OPIK_TO_BT_PAGE_SIZE` | `2000` | Opik records requested per row-resource page (`1`–`2000`). Prompt listing and version-history requests are capped at their API limits of 1,000 and 100. Lower this only when source response objects are too large for the runner; transformed row events are staged incrementally. Do not change it after a row stream has checkpointed beyond page 1 unless starting fresh. |
| `OPIK_TO_BT_PROMPT_HISTORY` | `latest` | Default for `--prompt-history`; set to `all` to opt into complete prompt-version replay. |
| `OPIK_TO_BT_PARTITION_BYTES` | Automatic, up to `256 MiB` | Target uncompressed NDJSON bytes per immutable `bt sync` partition; override values are specified in bytes. The automatic value is bounded by memory and free disk; the effective minimum is `16 MiB`. Partitions rotate between events, so only a single event larger than the target can produce an oversized partition. |
| `OPIK_TO_BT_RESOURCE_WORKERS` | `min(8, max(2, CPU/2))` | Maximum concurrent resource jobs and Opik request slots (`1`–`64`). Higher values improve extraction concurrency but increase API pressure and memory use. |
| `OPIK_TO_BT_BUFFERED_PARTITIONS` | `2` | Ready-to-upload partition files buffered per active stream (`1`–`8`). Higher values allow more extraction/upload overlap at the cost of staging disk; partition contents are not held in memory. |
| `OPIK_TO_BT_UPLOAD_PROCESSES` | `min(2, max(1, CPU/4))` | Concurrent `bt sync` subprocesses across the migration (`1`–`16`). |
| `OPIK_TO_BT_BT_WORKERS` | `min(16, max(2, CPU/upload processes))` | Parallel workers passed to each `bt sync push` process (`1`–`64`). Approximate maximum upload concurrency is upload processes multiplied by these workers. |

The CLI prints the resolved resource-worker, upload-slot, and partition-size
values at startup. If a resume fails because `OPIK_TO_BT_PAGE_SIZE` changed,
restore the original value or use a new `--state-dir`.

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
```

The repeatable synthetic partition benchmark and the latest before/after
results are documented in [`benchmarks/README.md`](benchmarks/README.md).
