# API Reference

Base path: `/api`

This document describes the HTTP and WebSocket contract exposed by the local
Overdrive server (`overdrive server`).

## Conventions

- Most endpoints accept `project_dir` as an optional query parameter.
- If `project_dir` is omitted, the server uses its startup project (or current
  working directory).
- JSON responses use object envelopes (for example `{ "task": ... }` or
  `{ "tasks": [...] }`).
- Error responses are standard FastAPI errors with HTTP status and `detail`.

## Root and Health Endpoints

These routes are outside `/api`.

### `GET /`
Returns runtime metadata for the active project context.

Response fields:
- `name`
- `version`
- `project`
- `project_id`
- `schema_version`

### `GET /healthz`
Liveness endpoint.

### `GET /readyz`
Readiness endpoint with project and orchestrator cache details.

## Projects

### `GET /api/projects`
List discovered and pinned projects.

Query:
- `include_non_git` (boolean, default `false`)

Response:
- `projects[]` with `id`, `path`, `source` (`discovered|pinned`), `is_git`

### `GET /api/projects/pinned`
Returns pinned projects from config.

Response:
- `items[]`

### `POST /api/projects/pinned`
Pin a project path.

Request body:
- `path` (required)
- `project_id` (optional)
- `allow_non_git` (optional, default `false`)

Notes:
- Returns `400` if path is invalid or non-git without `allow_non_git=true`.

### `DELETE /api/projects/pinned/{project_id}`
Remove a pinned project.

Response:
- `removed` (`true` if an entry was deleted)

### `GET /api/projects/browse`
Browse directories for project selection.

Query:
- `path` (optional; defaults to current user's home)
- `include_hidden` (boolean, default `false`)
- `limit` (integer, default `200`, range `1..1000`)

Response:
- `path`, `parent`, `current_is_git`
- `directories[]` with `name`, `path`, `is_git`
- `truncated` (`true` if results hit `limit`)

## Tasks

### `POST /api/tasks`
Create a task.

Request fields include:
- `title` (required)
- `description`, `task_type`, `priority`, `status`
- `labels[]`, `blocked_by[]`, `parent_id`
- `pipeline_template[]` (optional override)
- `hitl_mode`
- `dependency_policy` (`permissive|prudent|strict`)
- `source`, `worker_model`
- `metadata` (object)
- `project_commands` (per-task command overrides)
- optional classifier fields used by `task_type="auto"`

Behavior:
- `status` is only honored for `backlog` and `queued` at create time.
- `task_type="auto"` requires a high-confidence classifier result.
- If dependency policy is invalid/missing, server falls back to settings default,
  then `prudent`.

### `POST /api/tasks/classify-pipeline`
Classify free-form task text into a pipeline.

Request:
- `title`
- `description` (optional)
- `metadata` (optional)

Response:
- `pipeline_id`
- `task_type`
- `confidence` (`high|low`)
- `reason`
- `allowed_pipelines[]`

### `GET /api/tasks`
List tasks.

Query filters:
- `status`
- `task_type`
- `priority`

Response:
- `tasks[]`
- `total`

### `GET /api/tasks/board`
Returns board columns keyed by status.

Columns:
- `backlog`, `queued`, `in_progress`, `in_review`, `blocked`, `done`, `cancelled`

Column ordering:
- `backlog`: priority (`P0` first), then oldest `created_at`
- `queued`: priority (`P0` first), then oldest `created_at`
- `in_progress`: priority (`P0` first), then most recently updated
- `in_review`: priority (`P0` first), then oldest `updated_at`
- `blocked`: priority (`P0` first), then most recently updated
- `done`: most recently updated first (newest completed first), then priority, then oldest `created_at`
- `cancelled`: most recently updated first

Notes:
- `done` recency is based on task `updated_at`.
- Missing or malformed timestamps are sorted last with deterministic ID tie-breakers.

### `POST /api/tasks/queue-backlog`
Transition all backlog tasks to queued status in a single bulk operation.

Query parameters:
- `project_dir` (optional): project directory to resolve runtime state.

Response:
- `queued_count` (number of tasks transitioned)
- `task_ids` (list of transitioned task IDs)
- `message` (user-facing summary)

### `POST /api/tasks/clear`
Clear all board tasks by archiving runtime state and reinitializing empty state.

Query parameters:
- `force` (`false` by default): when `true`, cancel active tasks and wait for quiescence before clear.
- `timeout_seconds` (`10.0` by default, `0..120`): max wait for shutdown/quiescence.

Response:
- `cleared` (`true` on success)
- `archived_to` (absolute archive directory path; empty when no prior state existed)
- `message` (user-facing archive/clear summary)
- `cleared_at` (ISO timestamp)
- `force`
- `timeout_seconds`
- `active_execution_before`
- `active_execution_after`
- `force_cancel_result`
- `quiescence_result`

Conflict responses (`409`):
- `detail.detail` (human-readable message)
- `detail.code` (`active_execution` or `active_execution_timeout`)
- `detail.data` (structured diagnostics)

### `GET /api/tasks/execution-order`
Returns dependency-aware batches for non-terminal tasks.

Response:
- `batches` (list of task-id lists)
- `completed` (recently completed/cancelled task IDs)

### `GET /api/tasks/{task_id}`
Fetch one task. Returns `404` if not found.

Additive timing fields in task payload:
- `timing_summary.total_completed_seconds`: Sum of completed run durations in seconds.
- `timing_summary.active_run_started_at`: ISO timestamp of current in-progress run start, when present.
- `timing_summary.is_running`: `true` when an active run is in progress.
- `timing_summary.first_started_at`: Earliest valid run start timestamp in run history.
- `timing_summary.last_finished_at`: Latest valid run finish timestamp in run history.

### `DELETE /api/tasks/{task_id}`
Delete a task only when it is terminal (`done` or `cancelled`).

Behavior:
- Returns `400` for non-terminal statuses.
- Cleans references from remaining tasks (`blocked_by`, `blocks`, `parent_id`, `children_ids`).

### `PATCH /api/tasks/{task_id}`
Patch mutable fields.

Important constraints:
- Status changes are rejected (`400`).
- Use transition/retry/cancel/review actions for status updates.
- Empty `project_commands` clears per-task command overrides.

### `POST /api/tasks/{task_id}/transition`
Manual status transition with guardrails.

Valid transitions:
- `backlog -> queued|cancelled`
- `queued -> backlog|cancelled`
- `in_progress -> cancelled`
- `in_review -> blocked|cancelled`
- `blocked -> queued|cancelled`
- `cancelled -> backlog`

Notes:
- Transitioning to `queued` is allowed even when dependencies are unresolved.
- Execution is still guarded: `POST /api/tasks/{task_id}/run` returns `400`
  while blockers remain unresolved.
- Use `POST /api/review/{task_id}/approve` (not `/transition`) to finalize
  `in_review` tasks.
- Blocked tasks cannot be forced into `in_review` via `/transition`; use
  `POST /api/tasks/{task_id}/skip-to-precommit` when eligible.

### `POST /api/tasks/{task_id}/run`
Queue/execute a task through the orchestrator.

### `POST /api/tasks/{task_id}/select-pipeline`
Manually select a pipeline for a task awaiting classification.

Request:
- `pipeline_id` — the pipeline to assign.

Returns 400 if `pending_gate` is not `select_pipeline` or `pipeline_id` is invalid.

### `POST /api/tasks/{task_id}/retry`
Reset task for another run.

Request (optional):
- `guidance`
- `start_from_step`

Behavior:
- Clears `error` and `pending_gate`.
- Increments `retry_count`.
- Stores retry guidance metadata/history when provided.

### `POST /api/tasks/{task_id}/skip-to-precommit`
Move an eligible blocked task to pre-commit review without rerunning earlier steps.

Request (optional):
- `guidance`

Behavior:
- Only allowed for blocked tasks that satisfy backend eligibility checks
  (pipeline capability, blocked step compatibility, and retry context availability).
- Transitions task to `in_review` with `pending_precommit_approval=true`.

### `POST /api/tasks/{task_id}/finalize-merge-conflict`
Finalize a blocked merge-conflict task after manual Git resolution.

Request (optional):
- `guidance`

Behavior:
- Only allowed for `blocked` tasks at `commit` where `metadata.merge_conflict=true`.
- Verifies Git has no unresolved index entries and that task work is already
  integrated into the active base branch.
- Marks task `done` and records an audit action in `human_review_actions`.

### `POST /api/tasks/{task_id}/cancel`
Set task status to `cancelled`.

### `POST /api/tasks/{task_id}/approve-gate`
Handle a pending human gate action.

Request:
- `gate` (optional; if provided must match `task.pending_gate`)
- `action` (optional; `approve` or `request_changes`, default `approve`)
- `guidance` (optional; used with `request_changes`)

### `POST /api/tasks/{task_id}/dependencies`
Add dependency edge.

Request:
- `depends_on` (task id)

### `DELETE /api/tasks/{task_id}/dependencies/{dep_id}`
Remove dependency edge.

### `POST /api/tasks/analyze-dependencies`
Run dependency inference across tasks and return inferred edges.

### `POST /api/tasks/{task_id}/reset-dep-analysis`
Remove inferred dependency metadata and inferred blocker links for a task.

### `POST /api/tasks/{task_id}/review-commit`
Create a commit-review task from a completed task's commit.

Behavior:
- Validates source task exists and has status `done`.
- Extracts commit SHA from the latest run's commit step.
- Fetches the git diff and source task plan/description.
- Creates a new `commit_review` task pre-loaded with source context.

Response:
- `task` — the newly created review task payload.

### `POST /api/tasks/{task_id}/review-mr`
Create an MR-review task from a GitLab merge request.

Query parameters:
- `mr_number` (required) — GitLab merge request number.

Behavior:
- Validates source task exists and `glab` CLI is installed.
- Fetches MR metadata, diff, and diff stat via `glab` / `git`.
- Creates a new `mr_review` task pre-loaded with MR context.

Response:
- `task` — the newly created review task payload.

### `POST /api/tasks/{task_id}/review-pr`
Create a PR-review task from a GitHub pull request.

Query parameters:
- `pr_number` (required) — GitHub pull request number.

Behavior:
- Validates source task exists and `gh` CLI is installed.
- Fetches PR metadata, diff, and diff stat via `gh` / `git`.
- Creates a new `pr_review` task pre-loaded with PR context.

Response:
- `task` — the newly created review task payload.

### `GET /api/tasks/{task_id}/workdoc`
Return task work document payload.

Behavior:
- Reads the canonical orchestrator workdoc for the task.
- Returns `409 Conflict` when the canonical workdoc is missing (`Missing required workdoc for task {task_id}`).

Response:
- `task_id`
- `content` (markdown text)
- `exists` (`true` when returned)

UI usage:
- Task detail exposes a **Workdoc** panel that renders `content` as markdown.

### `GET /api/tasks/{task_id}/plan`
Return iterative plan document, including revisions and active refine job.

### `POST /api/tasks/{task_id}/plan/refine`
Queue async refinement of a plan revision.

Request:
- `feedback` (required)
- `base_revision_id` (optional)
- `instructions` (optional)
- `priority` (`normal|high`)

### `GET /api/tasks/{task_id}/plan/jobs`
List refine jobs for a task.

### `GET /api/tasks/{task_id}/plan/jobs/{job_id}`
Get one refine job.

### `POST /api/tasks/{task_id}/plan/commit`
Commit a chosen revision.

Request:
- `revision_id`

### `POST /api/tasks/{task_id}/plan/revisions`
Create a manual revision.

Request:
- `content` (required)
- `parent_revision_id` (optional)
- `feedback_note` (optional)

### `POST /api/tasks/{task_id}/post-review-comments`
Post dry-run review comments for a task.

Validates task is a dry-run with generated comments. Posts comments via the configured platform and updates metadata.

Response:
- `posted_count`, `failed_count`, `results[]`, `task`

Errors: 404 (not found), 409 (not dry-run or no generated comments).

### `POST /api/tasks/{task_id}/generate-tasks`
Generate child tasks from plan content.

Request:
- `source`: `latest|committed|revision|override`
- `revision_id` (required when `source=revision`)
- `plan_override` (required when `source=override`)
- `infer_deps` (default `true`)

Validation:
- `revision_id` is only valid with `source=revision`.
- `plan_override` is only valid with `source=override`.

### `GET /api/tasks/{task_id}/diff`
Return latest commit diff/stat for the task.

Response:
- `commit` (or `null`)
- `files[]` (`path`, `changes`)
- `diff` (full unified diff)
- `stat` (git stat text)

### `GET /api/tasks/{task_id}/changes`
Return best-available task changes:
- committed diff when commit evidence exists
- otherwise working-tree changes (including pre-commit/unborn-HEAD states)
- otherwise preserved-branch changes when available

Additive response fields:
- `base_ref` (`string|null`): base reference used for diff reconstruction.
- `base_sha` (`string|null`): resolved base commit SHA.
- `head_ref` (`string|null`): head reference used for diff reconstruction.
- `head_sha` (`string|null`): resolved head commit SHA.
- `base_source`:
  - `metadata_sha`
  - `metadata_branch`
  - `review_context_sha`
  - `review_context_branch`
  - `heuristic`
  - `none`
- `confidence`: `high|medium|low`
- `warnings[]`: machine-readable warning codes (for example `heuristic_base_inferred`, `large_file_count`, `large_line_churn`, `base_not_ancestor`)

Notes:
- Preserved-branch diffs are SHA-anchored when metadata/review context provides anchors.
- Legacy tasks can fall back to inferred base resolution (`base_source=heuristic`) and may return `confidence=low` plus warnings.

### `GET /api/tasks/{task_id}/logs`
Read current or historical step logs.

Query:
- `max_chars` (`200..2000000`, default `12000`)
- `stdout_offset`, `stderr_offset` (for incremental reads)
- `backfill` (align chunk to line starts)
- `stdout_read_to`, `stderr_read_to` (optional byte limits)
- `step` (optional, fetch logs for a specific step attempt)
- `run_id` (optional, used with `step` to select a specific run attempt)

Response includes:
- `mode` (`active|last|history|none`)
- `stdout`, `stderr`
- offsets and chunk metadata
- `available_steps[]`, `step_execution_counts`, `step_latest_run`
- `step_history[]` (most-recent-first step attempts across runs)
- `progress` JSON payload when present

## PRD Import

### `POST /api/import/prd/preview`
Parse PRD text and return a preview graph.

Request:
- `content` (required)
- `title` (optional)
- `default_priority` (default `P2`)

Response:
- `job_id`
- `preview.nodes[]`, `preview.edges[]`
- parsing metadata (`chunk_count`, `strategy`, `ambiguity_warnings`)

### `POST /api/import/prd/commit`
Commit previewed import and create tasks via initiative pipeline.

Request:
- `job_id`

Response:
- `job_id`
- `created_task_ids[]`
- `parent_task_id`

### `GET /api/import/{job_id}`
Fetch stored import job state.

## Pull Requests

> **Prerequisites:** These endpoints require the `gh` CLI (GitHub) or `glab` CLI
> (GitLab) to be installed and authenticated, and an `origin` remote pointing to
> GitHub or GitLab. See [USER_GUIDE.md — GitHub & GitLab Integration](USER_GUIDE.md#github--gitlab-integration)
> for setup instructions.

### `GET /api/pull-requests`
List open pull requests (GitHub) or merge requests (GitLab).

Detects the git platform from the remote URL and fetches open PRs/MRs via the
appropriate CLI (`gh` or `glab`). Each item is annotated with whether a review
task already exists.

Response:
- `platform` (`github`, `gitlab`, or `null`)
- `error` (string or `null`)
- `error_code` (`no_remote`, `cli_not_installed`, `cli_not_authenticated`, `cli_error`, or `null`)
- `items[]` — each with `number`, `title`, `author`, `head_ref`, `base_ref`,
  `url`, `has_review_task`, `review_task_id`

### `GET /api/git-platform-status`
Check GitHub/GitLab integration status in a single call.

Reports whether a git remote is configured, the platform CLI is installed,
and the CLI is authenticated. Used by the frontend to show a setup checklist
proactively before errors occur.

Response:
- `platform` (`github`, `gitlab`, or `null`)
- `remote_url` (string or `null`)
- `cli_installed` (boolean)
- `cli_authenticated` (boolean)
- `cli_name` (`gh`, `glab`, or `null`)
- `setup_steps[]` — each with `step` (number), `label` (string), `done` (boolean)

### `POST /api/pull-requests/{number}/review`
Create a review task for a pull request or merge request.

Request:
- `guidance` (optional review instructions)

Response:
- `task` (the newly created review task payload)

Errors:
- `400` if platform detection or CLI check fails

## Git

### `GET /api/git/status`
Returns current branch name, remote tracking info, ahead/behind counts, and commit list.

Response:
- `branch` (string) — current branch name (or `"HEAD"` if detached)
- `remote_branch` (string | null) — upstream tracking branch (e.g. `"origin/main"`) or `null` if none
- `ahead_count` (int) — number of local commits not yet on the remote
- `behind_count` (int) — number of remote commits not yet pulled locally
- `commits` (array of `{sha, message}`) — list of ahead commits (newest first)
- `has_remote` (bool) — whether an `origin` remote is configured

### `POST /api/git/push`
Push commits to the remote.

Request:
- `target_branch` (string, optional) — remote branch name to push to; omit to push to current upstream
- `auto_name` (bool, default `false`) — auto-generate a remote branch name (e.g. `push/main-20260319-091742`)

Response:
- `success` (bool) — whether the push succeeded
- `error` (string | null) — error message on failure
- `remote_branch` (string) — the remote branch that was pushed to
- `pushed_commits` (int) — number of commits pushed

Errors:
- `400` — no remote configured, or push rejected by remote (e.g. non-fast-forward)

On success, emits a `git.pushed` WebSocket event on the `system` channel.

### `POST /api/git/push/cancel`
Cancel a running push operation.

Response:
- `cancelled` (bool) — whether a push was cancelled
- `detail` (string | null) — explanation when no push was in progress

### `POST /api/git/suggest-branch-name`
Use an LLM worker to suggest a branch name from commits ahead of the remote.

Query parameters:
- `project_dir` (string, optional) — project directory override

Response:
- `branch_name` (string | null) — suggested branch name prefixed with `push/`, or null on failure
- `error` (string | null) — error description when branch name generation fails

Never raises HTTP errors; all failure modes return `branch_name: null` with an `error` message.

## Review and Collaboration

### `GET /api/review-queue`
List tasks in `in_review`.

### `POST /api/review/{task_id}/approve`
Approve review; marks task `done` (and merges preserved branch when applicable).

Body:
- `guidance` (optional)

### `POST /api/review/{task_id}/request-changes`
Send task back to `queued` with review guidance.

Body:
- `guidance` (optional)

### `GET /api/collaboration/modes`
Returns available HITL mode definitions.

### `GET /api/git/status`
Returns current branch name, remote tracking info, ahead/behind counts, and commit list.

Response:
- `branch` (string) — current branch name (or `"HEAD"` if detached)
- `remote_branch` (string | null) — upstream tracking branch (e.g. `"origin/main"`) or `null` if none
- `ahead_count` (int) — number of local commits not yet on the remote
- `behind_count` (int) — number of remote commits not yet pulled locally
- `commits` (array of `{sha, message}`) — list of ahead commits (newest first)
- `has_remote` (bool) — whether an `origin` remote is configured

### `POST /api/git/push`
Push commits to the remote.

Request:
- `target_branch` (string, optional) — remote branch name to push to; omit to push to current upstream
- `auto_name` (bool, default `false`) — auto-generate a remote branch name (e.g. `push/main-20260319-091742`)

Response:
- `success` (bool) — whether the push succeeded
- `error` (string | null) — error message on failure
- `remote_branch` (string) — the remote branch that was pushed to
- `pushed_commits` (int) — number of commits pushed

Errors:
- `400` — no remote configured, or push rejected by remote (e.g. non-fast-forward)

On success, emits a `git.pushed` WebSocket event on the `system` channel.

### `GET /api/collaboration/presence`
Presence feed (currently empty list).

### `GET /api/collaboration/timeline/{task_id}`
Task timeline combining status, events, feedback, comments, and human blockers.

### `GET /api/collaboration/feedback/{task_id}`
List feedback records for a task.

### `POST /api/collaboration/feedback`
Add a feedback item.

Required fields:
- `task_id`
- `summary`

Optional fields:
- `feedback_type`, `priority`, `details`, `target_file`

### `POST /api/collaboration/feedback/{feedback_id}/dismiss`
Mark feedback as addressed.

### `GET /api/collaboration/comments/{task_id}`
List comments for a task.

Query:
- `file_path` (optional exact match filter)

### `POST /api/collaboration/comments`
Create a threaded file comment.

Required fields:
- `task_id`, `file_path`, `body`

Optional fields:
- `line_number`, `line_type`, `parent_id`

### `POST /api/collaboration/comments/{comment_id}/resolve`
Mark a comment resolved.

## Terminal

### `POST /api/terminal/session`
Start a PTY session for the active project.

Request:
- `cols` (default `120`, range `2..500`)
- `rows` (default `36`, range `2..300`)
- `shell` (optional)

### `GET /api/terminal/session`
Get active session for project, if any.

### `POST /api/terminal/session/{session_id}/input`
Write UTF-8 input to session.

Request:
- `data`

### `POST /api/terminal/session/{session_id}/resize`
Resize PTY.

Request:
- `cols` (`2..500`)
- `rows` (`2..300`)

### `POST /api/terminal/session/{session_id}/stop`
Stop session with signal.

Request:
- `signal`: `TERM|KILL` (default `TERM`)

### `GET /api/terminal/session/{session_id}/logs`
Read terminal output.

Query:
- `offset` (default `0`)
- `max_bytes` (default `65536`)

Response:
- `output`
- `offset` (next cursor)
- `status`
- `finished_at`

## Orchestrator and Runtime Metrics

### `GET /api/orchestrator/status`
Return orchestrator state (queue, in-progress, control mode, etc.).

### `POST /api/orchestrator/control`
Control orchestrator state.

Request:
- `action`: `pause|resume|drain|stop|reset|reconcile`

### `POST /api/orchestrator/reconcile`
Run one explicit runtime reconciliation pass and return repair summary + status.

### `GET /api/metrics`
Aggregated runtime metrics (API calls, wall time, queue depth, progress counters).

### `GET /api/phases`
Task progress snapshot used for execution phase visualization.

## Settings and Workers

### `GET /api/settings`
Read normalized runtime settings payload.

Top-level sections:
- `orchestrator`
- `agent_routing`
- `defaults`
- `workers`
- `project`

Project section fields:
- `project.commands`: language command overrides.
- `project.prompt_overrides`: per-step prompt text overrides.
- `project.prompt_injections`: per-step additive prompt snippets.
- `project.prompt_defaults`: server-provided default prompt text by step
  (read-only, for Settings UI review).

### `PATCH /api/settings`
Patch one or more settings sections.

Merge semantics:
- Omitted section: unchanged.
- Included section: shallow-merged by section rules.
- In `project.commands`, empty string removes a command.
- In `project.prompt_overrides`, empty string removes that step override.
- In `project.prompt_injections`, empty string removes that step injection.

Workers settings notes:
- Provider types: `codex`, `claude`, `ollama`.
- `workers.default` falls back to `codex` if invalid.
- `heartbeat_grace_seconds` is clamped to be at least
  `heartbeat_seconds`.

### `GET /api/workers/health`
Health check results for configured and common known providers.

### `GET /api/workers/routing`
Resolved step-to-provider routing table.

## Agents

### `GET /api/agents`
List agent records.

### `GET /api/agents/types`
List built-in agent role types and their capabilities.

Query parameters:
- `project_dir` (optional): project path used to resolve `task_type_affinity`
  from active `agent_routing.task_type_roles` settings.

Response:
- `types`: array of role descriptors.
  - `role`: machine role name.
  - `display_name`: human-friendly role label.
  - `description`: short role description.
  - `task_type_affinity`: sorted task types mapped to this role in settings.
  - `allowed_steps`: workflow steps the role can execute (`plan`,
    `implement`, `verify`, `review`).
  - `limits`: current runtime limits object.
    - `max_tokens`
    - `max_time_seconds`
    - `max_cost_usd`

### `POST /api/agents/spawn`
Create an agent record.

Request:
- `role` (default `general`)
- `capacity` (default `1`)
- `override_provider` (optional)

### `POST /api/agents/{agent_id}/pause`
Pause an agent.

### `POST /api/agents/{agent_id}/resume`
Resume an agent.

### `POST /api/agents/{agent_id}/terminate`
Mark an agent terminated.

### `DELETE /api/agents/{agent_id}`
Remove agent record.

### `POST /api/agents/{agent_id}/remove`
Legacy-compatible remove endpoint (same behavior as `DELETE`).

## WebSocket

Endpoint: `/ws`

Channels used by UI:
- `tasks`
- `queue`
- `agents`
- `review`
- `terminal`
- `notifications`
- `system`

Event envelope fields:
- `id`
- `ts`
- `channel`
- `type`
- `entity_id`
- `payload`
- `project_id`
