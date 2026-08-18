# Feasibility: online evals, annotation queues, and dashboards

This note assesses whether the Opik → Braintrust migrator can also move
**online evaluation config**, **annotation queues / human review scores**, and
**dashboards**. It is an analysis, not an implementation plan with a ship date.

The current CLI already migrates projects, datasets, experiments, traces/spans,
and prompts. Historical score *values* on those rows already come along. The
open question is whether we can also recreate the **live configuration** that
keeps scoring, review, and monitoring running after cutover.

Autodesk raised these three (plus alerts) as remaining migration items after
the Aug 17 Construction AI discussion, and asked again on Aug 18 whether they
are feasible. Dashboards were the main concern on that call because Opik’s
query language is not SQL.

## Verdict

| Resource | Historical data | Live config | Overall | Notes |
|---|---|---|---|---|
| Online eval scores already on traces | Already migrated | n/a | **Done** | Values in `[0, 1]` become Braintrust scores; other numerics become metrics. |
| Online eval **rules** (LLM-as-judge + sampling + filters) | n/a | Automatable with translation | **High**, with gaps | Public APIs exist on both sides. Prompts, models, sampling, and span/trace/thread scope map. Python metrics and multi-score judge schemas need extra work. |
| Feedback definitions / human review score types | Partial (values only) | Automatable | **High** | Opik numerical/categorical/boolean definitions map cleanly onto Braintrust slider/categorical scores. |
| Annotation queues + membership | Scores on items yes; queue membership no | Automatable as review views | **Medium** | Recreate queues as Braintrust Review views and flag the already-migrated traces. Locks, per-queue SME UX, and `annotators_per_item` are not 1:1. |
| Dashboards | n/a | Heuristic mapper, not pixel-perfect | **Medium** | Both sides expose dashboard APIs. Common volume/latency/cost/score charts can be generated. Layout, radar, experiment leaderboards, and free-form OQL are not a lossless translate. |
| Alerts (asked alongside these) | n/a | Partial | **Medium** | Event types map to Braintrust SQL alerts. Slack channels and webhook destinations are environment-specific and should not be copied blindly. |

Recommended order if we build this: **feedback definitions → queues → online
eval rules → dashboards → alerts**. Queues and online rules depend on projects
and logs already existing. Dashboards and alerts are more useful once scores
have names in the destination.

These should be **opt-in resources**, not part of default `all`, until they
have been run against a real Autodesk workspace.

## What already migrates today

Trace, span, and experiment `feedback_scores` are already mapped in
`src/opik_to_bt/mapping.py`:

- Values in `[0, 1]` become Braintrust `scores`.
- Other finite numerics become custom `metrics`.
- The original Opik objects are retained under `metadata.opik`.
- Experiment scores also become child `score` spans.

What is **not** treated as first-class Braintrust data today:

- `source` (`ui` / `sdk` / `online_scoring`)
- `source_queue_id`
- `created_by` / `value_by_author` (multi-reviewer)
- `reason` / comments as Braintrust comments
- Feedback *definitions* (the score type catalog)
- The automation rules that produced online scores
- Queue membership and review status

So a customer who only cares about “did these traces already get scored?” is
covered. A customer who needs “keep scoring new production traffic the same
way, and keep SMEs reviewing the same backlog” is not.

---

## 1. Online evals

Two different jobs:

1. **Historical scores** on traces/spans/threads that online rules already
   wrote. The migrator already copies these as numbers.
2. **The rule config** so Braintrust keeps scoring *new* traffic after cutover.

(2) is the remaining work.

### Opik model

Online evaluation is an **automation rule evaluator**, not a row resource.

- REST: `GET/POST /v1/private/automations/evaluators`
- SDK: `client.rest_client.automation_rule_evaluators`
- Docs: [Online Evaluation rules](https://www.comet.com/docs/opik/production/online-evaluation/rules)

Each rule has:

| Field | Role |
|---|---|
| `name`, `enabled`, `sampling_rate` | Identity and how much traffic to score |
| `type` | `llm_as_judge`, `user_defined_metric_python`, plus `span_*` and `trace_thread_*` variants |
| `filters` | Structured field / operator / value predicates |
| `trigger_scope` | `production`, `experiment`, or `both` |
| LLM judge: model, prompt (`{{variable}}`), `variable_mapping`, score `schema` | How the judge runs |
| Python: `code.metric` + `arguments` | Custom metric class to instantiate |
| Thread cooldown | Workspace setting, default 15 minutes |

Built-in judge templates: Hallucination, Moderation, Answer Relevance;
thread templates: Conversation Coherence, User Frustration.

Rules created after a given time only score *new* data unless someone
re-runs them from the UI. Migrating config therefore wires the future; it
does not by itself re-score history. Braintrust can rewind trace/group
automations after cutover if that is desired.

### Braintrust model

Online scoring is two objects:

1. A **scorer function** (`POST /v1/function`, `function_type=scorer`), either
   LLM-as-judge or custom code. Same pattern as prompt migration, which
   already uses Braintrust REST.
2. A **project score** of `score_type=online` (`POST /v1/project_score`) that
   binds the function to logs.

Docs: [Score production traces](https://www.braintrust.dev/docs/evaluate/score-online),
[create project score](https://www.braintrust.dev/docs/api-reference/projectscores/create-project_score).

| Opik | Braintrust |
|---|---|
| Trace rule | Scope `trace` + idle timeout (default 30s vs Opik thread cooldown 15m) |
| Span rule | Scope `span` + filter by root / name / SQL |
| Thread rule | Scope `group`, `group_by` a session key, idle timeout |
| `sampling_rate` | `config.online.sampling_rate` |
| Structured filters | SQL filter (`IS NOT` instead of `!=`) |
| LLM judge prompt | LLM-as-judge scorer with `{{input}}`, `{{output}}`, `{{expected}}`, `{{metadata}}`, `{{thread}}` |
| Python metric | Custom Python/TypeScript scorer via `bt functions push` or `POST /v1/function` |

### What maps well

- **LLM-as-judge rules** are the high-value, high-feasibility path. Name,
  enabled flag, sampling, model, and the judge prompt can be copied. Span vs
  trace vs thread has a real Braintrust equivalent.
- **Variable mapping** can be rewritten onto Braintrust template variables for
  the common cases (`input`, `output`, metadata paths). Unmapped custom
  paths can be documented and left in `metadata.opik`.
- **Built-in templates** can either copy the stored prompt text (preferred;
  no semantic drift) or offer an optional remap onto Braintrust Autoevals
  (`Factuality`, moderation-style judges) when the customer wants the
  native scorer.
- **Filters** with `contains`, `=`, `>`, `is_empty`, tags, and metadata keys
  can be compiled to SQL. This is the same class of translation we would
  need for dashboards and alerts.
- Implementation shape matches prompts: REST in, REST out, checkpointed by
  source rule ID, **not** `bt sync`.

### Gaps that are not 1:1

- **Score schema.** One Opik judge can return several named scores
  (`BOOLEAN` / `INTEGER` / `DOUBLE`) via structured outputs. Braintrust
  LLM-as-judge scorers typically return a single numeric score through
  **choice scores** (`A`/`B`/`C` or `correct`/`incorrect`). A multi-score
  Opik rule should become **one Braintrust scorer per schema field**, or one
  custom-code scorer that writes multiple scores. That is mechanical, not a
  blocker, but it is not a copy-paste of the prompt.
- **Python metrics.** Opik stores a metric class name plus constructor
  arguments, executed against Opik’s `Conversation` / trace objects. That
  code will not run unchanged. Feasible approaches: emit a Braintrust custom
  scorer stub that wraps the original source under `metadata.opik` and
  requires a human to adapt the body; or skip Python rules and report them
  in the dry-run inventory. Do not silently drop them.
- **Thread judge tools.** Opik can inject truncated full-thread structure and
  let the judge call `read` / `jq` / `search`. Braintrust group/trace scoring
  exposes `{{thread}}` / `trace.getSpans()`, not that tool loop. Full-thread
  prompts still migrate; the tool-calling enrichment does not.
- **Images.** Opik wraps image variables in hidden `<< >>` tags. Braintrust
  multimodal judge support needs an explicit mapping of those fields.
- **`trigger_scope=experiment`.** Braintrust online scoring runs on project
  logs. Experiment scoring is a different workflow (scorers on evals). Rules
  that only fire on experiments should be inventoried, not attached to logs.
- **Idle timeout.** Opik thread cooldown is workspace-wide (often 15 minutes).
  Braintrust idle timeout is per rule (default 30 seconds). Copying 15 minutes
  onto thread/group rules is the conservative default; do not silently use 30s.
- **Model catalog.** Provider/model slugs may differ. Resolve what we can,
  fail the rule with a clear skip reason when we cannot.
- **Historical backfill.** Creating the Braintrust rule does not re-score
  migrated logs. If Autodesk wants continuity of *new* traffic only, that is
  enough. If they want the same rule on the last N days of already-copied
  logs, use Braintrust rewind after cutover (trace/group scopes only).

### Suggested migrator behavior

New resource, for example `--resources online-evals`:

1. List evaluators per selected project.
2. Dry-run prints type, sampling, filter, and whether the rule is
   auto-translatable / needs a stub / skipped.
3. For each LLM-as-judge rule: create a scorer function, then a
   `score_type=online` project score with the translated filter and sampling.
4. Checkpoint source rule ID → Braintrust function ID + project_score ID.
5. Leave original Opik rule JSON under function metadata / description.

Python rules: inventory + optional stub in v1; full translation only after we
see real Autodesk rule bodies.

---

## 2. Annotation queues and human review scores

Again, two layers:

1. **Score types** (what SMEs fill in).
2. **Queues** (which items, which instructions, which definitions, progress).

### Opik model

Feedback definitions are workspace-level:

- REST: `/v1/private/feedback-definitions`
- Types: `numerical` (min/max), `categorical` (name → double), `boolean`
  (`trueLabel` / `falseLabel`)

Annotation queues:

- REST: `/v1/private/annotation-queues`
- SDK: `create_traces_annotation_queue`, `get_items()`, `add_traces`, …
- Fields: `name`, `scope` (`trace` | `thread`), `description`, `instructions`,
  `feedback_definition_names`, `comments_enabled`, `annotators_per_item`,
  `lock_timeout_seconds`
- Items are traces or threads; `get_items()` reuses search filtered by
  `annotation_queue_ids`
- Human scores on those items have `source=ui`, optional `source_queue_id`,
  `created_by`, and `value_by_author`

Docs: [Annotation Queues](https://www.comet.com/docs/opik/evaluation/advanced/annotation_queues),
[Feedback definitions](https://www.comet.com/docs/opik/administration/workspace-settings/feedback_definitions).

### Braintrust model

- Human review scores: `POST /v1/project_score` with `score_type` of
  `slider`, `categorical`, or `free-form`. Categorical options are
  `{name, value}` with values in `[0, 1]`.
- Review work: the **Review** page, custom views
  (`view_type=for_review_project_log`), assignment, and Kanban
  (backlog / pending / complete).
- Flagging a log for review is a metadata merge:

  ```json
  {
    "metadata": {
      "~__bt_review_lists": {
        "__bt_default_review_list": { "status": "PENDING" }
      }
    },
    "_is_merge": true
  }
  ```

- Assignments: `metadata.~__bt_assignments` as Braintrust user IDs.
- Score descriptions support Markdown (a place to put queue instructions).
- Multiple reviewers on one span are a first-class Braintrust feature.

Docs: [Human review](https://www.braintrust.dev/docs/annotate/human-review),
[Manage review work](https://www.braintrust.dev/docs/annotate/human-review/manage-review-work),
[Flagging for review via API](https://www.braintrust.dev/docs/kb/flagging-users-for-review-via-api).

### What maps well

**Feedback definitions → project human-review scores (high).**

| Opik | Braintrust |
|---|---|
| Numerical, range `[0, 1]` | `slider` |
| Numerical, other range | `slider` if we rescale to `[0, 1]` and keep the original range in description; otherwise skip with a warning (Braintrust review scores are 0–1) |
| Categorical name → double | `categorical` categories |
| Boolean true/false labels | Two-category `categorical` (1 and 0) |

This is small, REST-only, and unblocks both review UX and dashboards that
chart those score names.

**Queue → Review view + flagged logs (medium).**

For each Opik queue:

1. Create a `for_review_project_log` view named after the queue.
2. Put `instructions` on the view description and/or the related human-review
   scores.
3. Restrict visible scores to `feedback_definition_names` using Braintrust
   score visibility (display filter, not ACL).
4. After logs have been migrated, `get_items()` and merge
   `~__bt_review_lists` onto the matching Braintrust root spans so they
   appear in **Awaiting review**.

Thread queues: flag the thread’s traces (or the latest trace) and optionally
set a default Review grouping by the same session key used for online thread
scoring. Braintrust review is span-based; it does not have a separate
thread-queue object.

### Gaps that are not 1:1

- **SME-only annotation UI.** Opik’s queue is a distraction-free annotator
  surface. Braintrust’s analog is the Review page (keyboard scoring, Kanban,
  assignments). Functionally equivalent for Autodesk’s “SMEs review traces”
  workflow; not the same chrome. Gainsight’s HTML-rendering complaint is a
  Braintrust product advantage, not a migrator problem.
- **`annotators_per_item` and lock timeout.** No Braintrust equivalent.
  Multiple reviewers exist, but there is no “require N annotators then
  lock the row for K seconds.” Capture those fields in the view description
  / dry-run report.
- **Queue comments vs trace comments.** Opik comments_enabled is per queue.
  Braintrust comments live on the span. We can copy comment text onto
  migrated spans if we add a comments extract; that is extra source work,
  not blocked by APIs.
- **Reviewer identity.** `created_by` / `value_by_author` are Opik user
  strings. Braintrust assignments need Braintrust user IDs. Unless both
  orgs share emails we can resolve, **do not auto-assign**. Flag as
  unassigned PENDING and preserve the Opik author under `metadata.opik`.
- **Historical human scores.** Values already migrate. We should additionally
  create the human-review *score configs* so the Review UI shows the same
  widgets, and optionally distinguish `source=ui` in metadata so Autodesk
  can filter SME labels vs online judges.
- **Completed vs remaining work.** Opik queue progress is “items still in
  the queue.” Braintrust Kanban is metadata status. Items still in
  `get_items()` → `PENDING`. Items that were annotated and removed from the
  queue will not appear unless we also scan scores with `source_queue_id`
  and mark those traces `COMPLETE`. Worth doing if Autodesk cares about
  audit of finished review, not only the live backlog.

### Suggested migrator behavior

New resources, for example `--resources feedback-definitions,annotation-queues`:

1. Migrate definitions into each destination project (Braintrust scores are
   per-project; Opik definitions are workspace-level — replicate into every
   selected project, or only projects that use them).
2. Create one Review view per queue.
3. After the logs stream for that project finishes, flag current queue
   members.
4. Dry-run: queue name, item count, definition names, unmapped fields.

Do not put this in default `all` until logs migration + flagging has been
tested together (queues are useless if the traces are not there yet).

---

## 3. Dashboards

This is the item Autodesk flagged as the hard one: “Opik uses OQL, Braintrust
uses SQL.” That is true for **search**, and less true for **dashboard
widgets**.

### Opik model

- REST: `GET/POST /v1/private/dashboards`
- SDK: typed `Dashboard` / `DashboardWidget` wrappers (Opik ≥ recent 2.x)
- Two dashboard types: `multi_project` (prod monitoring) and `experiments`
- Two surfaces: workspace dashboards vs project **Insights** views
- Widget types:
  - Multi-project: time series, single metric (stat card), markdown
  - Experiments: metrics chart (line/bar/radar), leaderboard, markdown
- Widget metrics are **enums**, not free-form queries: trace count, duration
  percentiles, token usage, estimated cost, feedback scores, thread counts,
  guardrail failures, etc.
- Filters on widgets are structured Opik filters (field/operator/value), the
  same family as online-eval filters. OQL is the SDK search language
  (`filter_string`), not the dashboard storage format.
- `config` is a JSON blob (sections, widgets, layout). The OpenAPI schema
  types it as `JsonNode`; the Python SDK has the real widget models.

Docs: [Dashboards](https://www.comet.com/docs/opik/tracing/dashboards/dashboards).

### Braintrust model

- Monitor **views** via `GET/POST /v1/view` with `view_type=monitor`
- Chart types: time series, top list, big number, plus presets
- Each chart is a **SQL measure** + optional trace/span filters + group-by
- First-class copy/import of view JSON is documented
- Experiment comparison lives on the experiments UI, not Monitor
- Custom charts are Pro/Enterprise

Docs: [Monitor with dashboards](https://www.braintrust.dev/docs/observe/dashboards),
[create view](https://www.braintrust.dev/docs/api-reference/views/create-view).

### What maps well (heuristic, not lossless)

A widget-enum → SQL table covers the charts Autodesk described (volume,
latency, cost, TTFT, feedback scores over time):

| Opik widget metric | Braintrust Monitor chart |
|---|---|
| Number of traces / threads | Time series or big number on `count(id)` |
| Trace duration p50/p90/p99 | Percentile of `metrics.duration` |
| Token usage / estimated cost | `metrics.tokens` / `metrics.estimated_cost` (already normalized by the log migrator) |
| Trace / thread feedback scores | `avg(scores.<Name>)` |
| Errors | Filter `error IS NOT NULL` |
| Breakdown by name, tags, metadata, model, provider | `GROUP BY` the mapped field |
| Single-metric stat card | Big number |
| Time series line/bar | Time series line/bar |
| Markdown | Skip, or fold into the view description |

Widget filters compile with the same filter→SQL helper as online evals.

Workspace multi-project dashboards can become one Monitor view per project,
or an org-level Monitor view where Braintrust supports it. Do not pretend a
single Opik workspace dashboard is one Braintrust project view.

### What does not map 1:1

- **No OQL parser is required for v1 dashboards.** Widgets are not stored as
  OQL. The Aug 17 “parser would be non-trivial” concern applies if we tried
  to translate arbitrary saved *searches*. Dashboard widgets are a closed
  metric catalog plus structured filters.
- **Layout.** Opik sections + grid vs Braintrust Monitor layout will not be
  pixel-identical. Aim for “same charts, readable default layout.”
- **Radar charts and experiment leaderboards.** No Monitor equivalent.
  Experiment dashboards should be inventoried and, at most, turned into
  saved experiment table views — or left for a manual rebuild in the
  experiments UI.
- **Insights built-in Project Overview.** Skip; Braintrust’s built-in “All
  data view” already covers the same health metrics.
- **Click-through filters, date-picker presets, share URLs.** Destination UX.
- **Guardrail-failed count.** Only if guardrail spans were migrated (they
  are mapped to `function` spans); a dedicated metric may need a SQL filter
  on span type/name rather than a preset.

### Suggested migrator behavior

New resource `--resources dashboards`:

1. List dashboards (and optionally Insights custom views) for selected
   projects / workspace.
2. Dry-run: widget-by-widget translate / skip / needs-review.
3. For each translatable multi-project dashboard, `POST /v1/view` with
   `view_type=monitor` and generated `view_data`.
4. Keep the original Opik `config` JSON in the checkpoint or as view
   metadata so a human can rebuild anything the mapper skipped.
5. Never claim experiment dashboards are done unless we also generate a
   documented rebuild checklist.

If Autodesk’s actual dashboards are mostly “traces, latency, cost, score X
over time,” this is a good automatic migration. If they are heavily custom
experiment leaderboards, the honest deliverable is inventory + rebuild
guide, not a parser.

---

## Related: alerts

Not in the original three, but Autodesk asked in the same breath.

Opik `GET /v1/private/alerts`: Slack / PagerDuty / general webhooks, triggers
such as `trace:errors`, `trace:feedback_score`, `trace:cost`, `trace:latency`,
`trace:guardrails_triggered`, `experiment:finished`, prompt lifecycle events.

Braintrust `POST /v1/project_automation`: SQL `btql_filter` on logs, notify
interval, Slack or webhook. Separate environment-update alerts for prompt
promotions.

| Opik trigger | Braintrust |
|---|---|
| `trace:errors` | `error IS NOT NULL` |
| `trace:latency` / `trace:cost` / `trace:feedback_score` | SQL on `metrics.duration`, `metrics.estimated_cost`, `scores.<Name>` plus threshold |
| Prompt created/committed/deleted | Environment-update alerts cover promotions, not every prompt CRUD event |
| `experiment:finished` | No direct log alert; skip or document |
| Slack / PagerDuty destination | Recreate only if the destination org’s Slack workspace is connected; never copy a customer webhook URL into another tenant without confirmation |

Feasible as a follow-on to dashboards, using the same filter compiler.
Destination credentials make this the worst candidate for a silent default.

---

## Implementation shape (if we build it)

All three (plus alerts) are **config objects**, not row streams:

- Extract via Opik `rest_client` (same pattern as prompts).
- Write via Braintrust REST (`/v1/function`, `/v1/project_score`, `/v1/view`,
  `/v1/project_automation`). Do not use `bt sync`.
- Checkpoint by source ID. Idempotent `PUT`/`create-or-replace` where the
  Braintrust API supports it.
- Opt-in `--resources` values so a logs-only run cannot create half-wired
  automations.
- Dry-run must print **translate / stub / skip** per object. That report is
  what Autodesk can review before a real cutover.
- Shared helper: Opik structured filter → Braintrust SQL. Used by online
  evals, dashboards, and alerts.

Dependencies:

| New resource | Requires already migrated |
|---|---|
| Feedback definitions | Projects |
| Annotation queues | Projects, logs, feedback definitions |
| Online eval rules | Projects, and ideally feedback/score names |
| Dashboards | Projects, logs, score names |
| Alerts | Projects, score names, destination Slack/webhook |

Python 3.13 CLI + `opik>=2.2.11` already exposes the REST clients behind
`OpikSource`. Confirm the installed SDK has
`automation_rule_evaluators`, `annotation_queues`, `dashboards`,
`feedback_definitions`, and `alerts` before coding; bump the pin if a
workspace is on a newer Opik that added thread/span rule types.

---

## What to tell Autodesk

**Yes, these are feasible to migrate as configuration, with a clear quality
bar per resource.**

- **Online evals:** historical scores already move. We can recreate LLM-as-judge
  production rules (prompt, model, sampling, span/trace/thread scope, filters)
  so new traffic keeps getting scored. Custom Python metrics and multi-score
  judges need a stub or a split into multiple Braintrust scorers, not a silent
  copy. Creating a rule does not re-score old logs unless they rewind it.
- **Annotation queues:** we can recreate the score rubrics SMEs use, recreate
  each queue as a Braintrust Review view, and flag the current backlog on the
  migrated traces. We cannot recreate Opik’s lock / N-annotators-per-item
  mechanics or auto-assign reviewers without a user-ID map. The Review page
  is the destination workflow.
- **Dashboards:** we can generate Braintrust Monitor views for the common
  production charts (volume, latency, cost, scores). This is a widget-catalog
  translation, not an OQL→SQL compiler. Experiment leaderboards and custom
  layout will need a rebuild pass. We should inventory their real dashboards
  in dry-run before promising coverage.

If they need a single next step from us: run a **dry-run inventory** against
one Autodesk project that lists every online rule, queue, dashboard widget,
and alert with a translate/skip reason. That inventory, not more API
research, is what determines how much is automatic vs a guided rebuild.
