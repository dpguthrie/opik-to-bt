# Opik → Braintrust migrator

A resumable Python 3.13 CLI for moving Opik datasets, experiments, and
traces/spans into Braintrust. It supports Opik Cloud or self-hosted Opik and
Braintrust US, EU, or self-hosted deployments.

## What it migrates

| Opik | Braintrust | Selection |
|---|---|---|
| Projects | Projects | `--projects` |
| Datasets and items | Datasets and records | `--datasets` |
| Experiments and results | Experiments and events | `--experiments`, `--start`, `--end` |
| Traces and spans | Project logs | `--start`, `--end` |

`--start` is inclusive and `--end` is exclusive. Dates apply to experiment
creation time and root trace start time; all child spans of a selected trace are
preserved. Datasets are not inherently time-bounded.

## How it scales

The migrator does not download a complete resource before uploading it. It
operates as a bounded pipeline:

```text
Opik pages → incremental transform/staging → bt sync → checkpoint
```

Extraction and upload overlap. Each transformed event is serialized once into
a rolling NDJSON staging file; the file rotates at the partition target even in
the middle of a large Opik page. Ready partitions live on staging disk rather
than in memory while they wait for `bt sync`. Independent projects, datasets,
and experiments run concurrently, while bounded queues prevent memory or disk
usage from growing with the total migration size. Dataset migrations complete
before dependent experiments.

Opik traces and spans are separate resources. The migrator paginates each
project-wide endpoint in bulk: each trace page becomes a bounded chunk, then one
paginated span scan retrieves the children for that entire chunk. A UUIDv7
trace-ID range lets Opik prune the span scan efficiently, and exact membership
is checked client-side. Traces become Braintrust root spans; spans become child
spans using their existing `trace_id` and `parent_span_id`. The migrator never
issues one span request per trace.

Runtime settings are automatic. The migrator uses 2,000-record pages—the
maximum Opik documents for its streaming search APIs—to minimize requests, then
considers available CPU, memory, and staging disk to choose partition size,
resource concurrency, upload slots, and `bt sync` workers. Users select *what*
to migrate; the tool manages *how* it moves the data.

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

All uploads go through
[`bt sync`](https://www.braintrust.dev/docs/reference/cli/sync), which provides
parallel, byte-bounded uploads, retries, and resumable upload state.

## Quick start

Install [uv](https://docs.astral.sh/uv/) and `bt >= 0.14.0` using the
[Braintrust CLI](https://www.braintrust.dev/docs/reference/cli/quickstart):

```bash
uv sync
cp .env.example .env
```

Set `OPIK_API_KEY` and `OPIK_WORKSPACE` in `.env`, then authenticate `bt` against
the destination or set `BRAINTRUST_API_KEY`. Change `OPIK_URL` and
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

Resources default to `all`. Optional semantic filters remain available:

```bash
uv run opik-to-bt \
  --projects support-bot \
  --resources datasets,experiments,logs \
  --datasets golden-set,edge-cases \
  --experiments baseline,v2 \
  --start 2026-01-01 \
  --end 2026-02-01
```

No performance flags are required. Keep `.opik-to-bt/` when moving or
restarting the job. Use `--no-resume` only when intentionally ignoring importer
completion markers. This also starts fresh `bt sync` upload state; stable event
IDs make the replay overwrite-safe.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPIK_URL` | `https://www.comet.com/opik/api` | Opik Cloud/self-hosted API |
| `OPIK_API_KEY` | — | Opik API key |
| `OPIK_WORKSPACE` | — | Opik workspace |
| `BRAINTRUST_URL` | `https://api.braintrust.dev` | Braintrust US/EU/self-hosted API |
| `BRAINTRUST_API_KEY` | profile or environment | Braintrust authentication |

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
  spans. Opik LLM/tool/guardrail types map to Braintrust
  `llm`/`tool`/`classifier`; other spans map to `task`.
- Opik tags become native Braintrust tags rather than metadata, mapped one to
  one. Trace tags go on the Braintrust root span, span tags stay on their own
  span, and dataset item tags go on the matching Braintrust dataset record;
  Braintrust aggregates a trace's span tags for display at the trace level.
  Braintrust tags are shared project settings, so add the tag names in the
  destination project's tag settings to control their color and description.
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
| `--resources all\|datasets,experiments,logs` | `all` | Selects resource types. Any comma-separated subset of `datasets`, `experiments`, and `logs` is valid. |
| `--datasets NAME[,NAME...]` | All datasets | Limits datasets by exact name within the selected projects. This does not select experiments that reference an excluded dataset. |
| `--experiments NAME[,NAME...]` | All experiments | Limits experiments by exact name within the selected projects. |
| `--start ISO-8601` | No lower bound | Inclusive UTC lower bound for experiment creation time and root-trace start time. A timezone-free value is interpreted as UTC. It does not filter datasets. |
| `--end ISO-8601` | Run-start snapshot | Exclusive UTC upper bound for experiments and logs. When omitted, the run start is checkpointed and reused on resume so new Opik data cannot move the boundary. |
| `--state-dir PATH` | `.opik-to-bt` | Stores the checkpoint, rolling NDJSON partitions, and `bt sync` state. Preserve this directory to resume, including when running in Docker or on another machine. |
| `--resume` / `--no-resume` | `--resume` | Reuses importer checkpoints and `bt sync` state. `--no-resume` ignores importer completion markers and passes `--fresh` to `bt sync`; stable event IDs keep replay overwrite-safe. |
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
| `BRAINTRUST_API_KEY` | Unset | Braintrust API key inherited by `bt sync`. When unset, `bt` uses its existing authenticated profile. |

### Reliability and performance variables

These are advanced overrides. Leaving them unset lets the migrator size itself
from the runner's CPU, memory, and free staging disk.

| Environment variable | Effective default | Controls |
|---|---:|---|
| `OPIK_TO_BT_TIMEOUT_SECONDS` | `60` | Per-request Opik HTTP timeout in seconds. Must be greater than zero. |
| `OPIK_TO_BT_RETRY_ATTEMPTS` | `8` | Maximum total attempts for a retryable Opik request. The shared request gate honors server reset headers and applies bounded exponential backoff with jitter. |
| `OPIK_TO_BT_PAGE_SIZE` | `2000` | Opik records requested per page (`1`–`2000`). The maximum minimizes API requests. Lower it only when the source response objects themselves are too large for the runner; transformed events are staged incrementally. Do not change it after a stream has checkpointed beyond page 1 unless starting fresh. |
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
