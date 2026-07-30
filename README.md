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
Opik pages → transform → immutable partition → bt sync → checkpoint
```

Extraction and upload overlap. Independent projects, datasets, and experiments
run concurrently, while bounded queues prevent memory or disk usage from
growing with the total migration size. Dataset migrations complete before
dependent experiments.

Opik traces and spans are separate resources. The migrator paginates each
project-wide endpoint in bulk: each trace page becomes a bounded chunk, then one
paginated span scan retrieves the children for that entire chunk. A UUIDv7
trace-ID range lets Opik prune the span scan efficiently, and exact membership
is checked client-side. Traces become Braintrust root spans; spans become child
spans using their existing `trace_id` and `parent_span_id`. The migrator never
issues one span request per trace.

Runtime settings are automatic. At startup the migrator considers available
CPU, memory, and staging disk to choose page size, partition size, resource
concurrency, upload slots, and `bt sync` workers. Users select *what* to migrate;
the tool manages *how* it moves the data.

When `--end` is omitted, the tool records the run's start time as a stable
snapshot boundary for logs and experiments. New Opik activity cannot shift
subsequent pages during a long migration or change the scope of a resumed run.

The terminal shows extraction pages, row counts, upload partitions, and elapsed
time for every active stream. Successful `bt sync` subprocess output is folded
into this display; full output is retained in the error if a push fails.

Each immutable partition has:

- stable Braintrust event IDs;
- a durable source-page cursor;
- independent `bt sync` state;
- bounded retry and upload behavior.

Opik requests share an adaptive request gate. When Opik returns `429` or a
transient server error, the migrator honors the server's reset window, adds
jitter, slows concurrent streams, and reports the pause in the terminal before
continuing automatically.

After a successful `bt sync` upload, the temporary NDJSON partition is removed.
Checkpoint and `bt sync` state remain under `.opik-migrate/`. An interrupted run
restarts at the last uploaded source page rather than the beginning.

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
uv run opik-migrate migrate \
  --projects support-bot \
  --start 2026-01-01 \
  --end 2026-02-01 \
  --dry-run
```

Run the migration:

```bash
uv run opik-migrate migrate \
  --projects support-bot \
  --start 2026-01-01 \
  --end 2026-02-01
```

Resources default to `all`. Optional semantic filters remain available:

```bash
uv run opik-migrate migrate \
  --projects support-bot \
  --resources datasets,experiments,logs \
  --datasets golden-set,edge-cases \
  --experiments baseline,v2 \
  --start 2026-01-01 \
  --end 2026-02-01
```

No performance flags are required. Keep `.opik-migrate/` when moving or
restarting the job. Use `--no-resume` only when intentionally ignoring importer
completion markers; stable IDs and `bt sync` state still make replay safe.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPIK_URL` | `https://www.comet.com/opik/api` | Opik Cloud/self-hosted API |
| `OPIK_API_KEY` | — | Opik API key |
| `OPIK_WORKSPACE` | — | Opik workspace |
| `BRAINTRUST_URL` | `https://api.braintrust.dev` | Braintrust US/EU/self-hosted API |
| `BRAINTRUST_API_KEY` | profile or environment | Braintrust authentication |

Operational overrides exist through `OPIK_MIGRATE_*` environment variables for
support and unusual deployments, but are deliberately omitted from the normal
workflow. See the fields in `config.py` when diagnosing a constrained or
rate-limited environment.

## Run in a container

The image includes `bt` 0.14.0:

```bash
docker build -t opik-migrate .
docker run --rm --env-file .env -v "$PWD/.opik-migrate:/app/.opik-migrate" \
  opik-migrate migrate --projects support-bot --resources all
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
- Experiment feedback in Braintrust's score range `[0, 1]` becomes scores.
  Other numeric feedback is retained in metadata.
- Cross-object dataset origin IDs are omitted because the destination dataset
  ID is resolved internally by `bt sync`.
- Source identifiers and unmapped context are retained under `metadata.opik`.

Dataset version history is not included. A future opt-in mode could map Opik
item history to Braintrust dataset snapshots.

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
```
