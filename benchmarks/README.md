# Partition pipeline benchmark

This benchmark isolates transformation, NDJSON construction, partition
rotation, and local staging. It deliberately excludes Opik network time and
`bt sync` network time so those external systems do not hide pipeline costs.
Every result is the median of three fresh processes on arm64 macOS 26.5.2 with
Python 3.13.13.

## Results

| Workload | Version | Events/s | Peak RSS | Largest partition | Elapsed |
|---|---|---:|---:|---:|---:|
| 3 × 2,000 records, 32 KiB payload | `main` | 9,616 | 323.91 MiB | 62.67 MiB | 0.624 s |
| 3 × 2,000 records, 32 KiB payload | incremental staging | 18,024 | 228.72 MiB | 31.99 MiB | 0.333 s |
| 20 × 2,000 records, 2 KiB payload | `main` | 108,462 | 167.48 MiB | 28.57 MiB | 0.369 s |
| 20 × 2,000 records, 2 KiB payload | incremental staging | 209,643 | 50.45 MiB | 32.00 MiB | 0.191 s |

For the oversized-page workload, incremental staging increased throughput by
87%, reduced peak RSS by 29%, and kept every multi-event partition below the
32 MiB target. For the smaller-record workload, it increased throughput by 93%
and reduced peak RSS by 70%.

The speedup comes from serializing each transformed event once and using the
resulting staging file directly as `bt sync` input. The old path serialized
events once to estimate partition size and again to write NDJSON.

## Reproduce

Oversized source-page case:

```bash
uv run python benchmarks/partition_pipeline.py \
  --pages 3 \
  --records-per-page 2000 \
  --payload-bytes 32768 \
  --partition-mib 32 \
  --buffered-partitions 2
```

Smaller-record throughput case:

```bash
uv run python benchmarks/partition_pipeline.py \
  --pages 20 \
  --records-per-page 2000 \
  --payload-bytes 2048 \
  --partition-mib 32 \
  --buffered-partitions 2
```

Use `--upload-delay-ms` to model a slow uploader and exercise queue
backpressure. Peak RSS is reported by the operating system for the complete
benchmark process.
