"""FastAPI routes for orchestrator runtime control and task management."""

from __future__ import annotations

import json
import re
from hashlib import sha256
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional, cast

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ...collaboration.modes import MODE_CONFIGS
from ...pipelines.registry import PipelineRegistry
from ...workers.config import WorkerProviderSpec, get_workers_runtime_config, provider_spec_to_dict
from ...workers.diagnostics import test_worker
from ...collaboration.modes import normalize_hitl_mode
from ..domain.models import (
    AgentRecord,
    DependencyPolicy,
    Priority,
    Task,
    TaskStatus,
    now_iso,
)
from ..events.bus import EventBus
from ..orchestrator.live_worker_adapter import get_configurable_step_prompt_defaults
from ..orchestrator.service import OrchestratorService
from ..storage.container import Container
from ..terminal.service import TerminalService


class CreateTaskRequest(BaseModel):
    """Request body for task creation, including optional pipeline overrides.

    Supports classifier metadata fields used when the client requests
    ``task_type="auto"`` and wants the server to persist provenance for the
    selected pipeline.
    """
    title: str
    description: str = ""
    task_type: str = "feature"
    priority: str = "P2"
    status: Optional[str] = None
    labels: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    parent_id: Optional[str] = None
    pipeline_template: Optional[list[str]] = None
    hitl_mode: Optional[str] = None
    dependency_policy: str = ""
    source: str = "manual"
    worker_model: Optional[str] = None
    worker_provider: Optional[str] = None
    step_timeout_seconds: Optional[int] = Field(None, ge=0, le=7200)
    metadata: dict[str, Any] = Field(default_factory=dict)
    project_commands: Optional[dict[str, dict[str, str]]] = None
    classifier_pipeline_id: Optional[str] = None
    classifier_confidence: Optional[Literal["high", "low"]] = None
    classifier_reason: Optional[str] = None
    was_user_override: Optional[bool] = None


class PipelineClassificationRequest(BaseModel):
    """Payload used to classify a task into a pipeline."""
    title: str
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class PipelineClassificationResponse(BaseModel):
    """Normalized pipeline-classification result."""
    pipeline_id: str
    task_type: str
    confidence: Literal["high", "low"]
    reason: str
    allowed_pipelines: list[str]


class SelectPipelineRequest(BaseModel):
    """Payload for manually selecting a pipeline for a task awaiting classification."""
    pipeline_id: str


class UpdateTaskRequest(BaseModel):
    """Patch payload for updating mutable task fields."""
    title: Optional[str] = None
    description: Optional[str] = None
    task_type: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    labels: Optional[list[str]] = None
    blocked_by: Optional[list[str]] = None
    hitl_mode: Optional[str] = None
    dependency_policy: Optional[str] = None
    worker_model: Optional[str] = None
    worker_provider: Optional[str] = None
    step_timeout_seconds: Optional[int] = Field(None, ge=0, le=7200)
    metadata: Optional[dict[str, Any]] = None
    project_commands: Optional[dict[str, dict[str, str]]] = None


class TransitionRequest(BaseModel):
    """Request body for explicit task status transitions."""
    status: str


class AddDependencyRequest(BaseModel):
    """Request body for a single ``depends_on`` relationship."""
    depends_on: str


class PrdPreviewRequest(BaseModel):
    """Request body for parsing PRD text before any task records are written."""
    title: Optional[str] = None
    content: str
    default_priority: str = "P2"


class PrdCommitRequest(BaseModel):
    """Request body selecting a staged PRD preview job to persist as tasks."""
    job_id: str


class StartTerminalSessionRequest(BaseModel):
    """Request body for opening a terminal session with bounded PTY dimensions."""
    cols: Optional[int] = Field(default=120, ge=2, le=500)
    rows: Optional[int] = Field(default=36, ge=2, le=300)
    shell: Optional[str] = None


class TerminalInputRequest(BaseModel):
    """Payload containing terminal input bytes as UTF-8 text."""
    data: str


class TerminalResizeRequest(BaseModel):
    """Request body for updating a terminal PTY size within safety bounds."""
    cols: int = Field(ge=2, le=500)
    rows: int = Field(ge=2, le=300)


class StopTerminalSessionRequest(BaseModel):
    """Request body for graceful or forceful terminal session shutdown."""
    signal: Literal["TERM", "KILL"] = "TERM"


class PlanRefineRequest(BaseModel):
    """Request body for scheduling a plan refinement pass.

    ``feedback`` is required so refinement jobs always include reviewer intent.
    """
    base_revision_id: Optional[str] = None
    feedback: str
    instructions: Optional[str] = None
    priority: Literal["normal", "high"] = "normal"


class CommitPlanRequest(BaseModel):
    """Request body that promotes one plan revision to committed state."""
    revision_id: str


class CreatePlanRevisionRequest(BaseModel):
    """Request body for a user-authored plan revision draft."""
    content: str
    parent_revision_id: Optional[str] = None
    feedback_note: Optional[str] = None


class TaskGenerationPolicyRequest(BaseModel):
    """Optional policy controls for child tasks generated from plans."""
    child_status: Optional[Literal["backlog", "queued"]] = None
    child_hitl_mode: Optional[Literal["inherit_parent", "autopilot", "supervised", "review_only"]] = None
    infer_deps: Optional[bool] = None


class GenerateTasksRequest(BaseModel):
    """Request body for deriving child tasks from committed or ad hoc plan text."""
    source: Optional[Literal["committed", "revision", "override", "latest"]] = None
    revision_id: Optional[str] = None
    plan_override: Optional[str] = None
    policy: Optional[TaskGenerationPolicyRequest] = None
    save_as_default: bool = False
    # Backward-compatibility shim for legacy clients that send infer_deps
    # at the top level rather than under policy.
    infer_deps: bool = True


class ApproveGateRequest(BaseModel):
    """Request body for clearing a pending human gate on the active task."""
    gate: Optional[str] = None
    action: Literal["approve", "request_changes"] = "approve"
    guidance: Optional[str] = None
    generation_policy: Optional[TaskGenerationPolicyRequest] = None
    save_generation_policy_as_default: bool = False
    # Fields for the before_post_review gate only:
    review_decision: Optional[str] = None
    post_comments: Optional[bool] = None


class CommentSelection(BaseModel):
    """Select a generated comment by index, with an optional body override."""
    index: int
    body: Optional[str] = None


class PostReviewCommentsRequest(BaseModel):
    """Optional request body for selective comment posting.

    When ``comments`` is empty or the body is omitted entirely, all
    staged comments are posted (backward-compatible default).
    """
    comments: list[CommentSelection] = Field(default_factory=list)


class OrchestratorControlRequest(BaseModel):
    """Request body for runtime control actions such as pause/resume/stop."""
    action: Literal["pause", "resume", "drain", "stop", "reset", "reconcile"]


class OrchestratorSettingsRequest(BaseModel):
    """Patch payload for orchestrator execution limits and automation policy."""
    concurrency: int = Field(2, ge=1, le=128)
    auto_deps: bool = True
    max_review_attempts: int = Field(10, ge=1, le=50)
    max_merge_conflict_attempts: int = Field(3, ge=1, le=10)
    step_timeout_seconds: int = Field(0, ge=0, le=7200)
    gate_reminder_minutes: int = Field(30, ge=0, le=10080)
    gate_stale_minutes: int = Field(0, ge=0, le=10080)
    gate_max_wait_minutes: int = Field(0, ge=0, le=43200)
    gate_timeout_action: Literal["none", "block"] = "none"
    reliability_mode: Literal["strict", "balanced", "availability"] = "strict"
    reconcile_interval_seconds: int = Field(30, ge=1, le=3600)
    lease_ttl_seconds: int = Field(120, ge=15, le=86400)
    tick_stale_seconds: int = Field(15, ge=5, le=3600)
    tick_failure_threshold: int = Field(5, ge=1, le=1000)
    auto_fix_push: bool = False


class AgentRoutingSettingsRequest(BaseModel):
    """Patch payload for mapping task roles to worker roles/providers."""
    default_role: str = "general"
    task_type_roles: dict[str, str] = Field(default_factory=dict)
    role_provider_overrides: dict[str, str] = Field(default_factory=dict)


class WorkerProviderSettingsRequest(BaseModel):
    """Patch payload for one named worker provider backend definition."""
    type: str = "codex"
    command: Optional[str] = None
    reasoning_effort: Optional[str] = None
    execution_mode: Optional[Literal["sandboxed", "host_access"]] = None
    capabilities: Optional[list[str]] = None
    endpoint: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    num_ctx: Optional[int] = None


class WorkerEnvironmentSettingsRequest(BaseModel):
    """Patch payload for worker environment preflight/remediation policy."""
    auto_prepare: bool = True
    max_auto_retries: int = Field(3, ge=0, le=20)
    capability_fallback: bool = True
    required_capabilities_by_step: dict[str, list[str]] = Field(default_factory=dict)
    env_vars: Optional[dict[str, str]] = None


class WorkersSettingsRequest(BaseModel):
    """Patch payload for global worker defaults, routing, and heartbeat checks."""
    default: str = "codex"
    default_model: Optional[str] = None
    heartbeat_seconds: Optional[int] = Field(None, ge=1, le=3600)
    heartbeat_grace_seconds: Optional[int] = Field(None, ge=1, le=7200)
    routing: dict[str, str] = Field(default_factory=dict)
    providers: dict[str, WorkerProviderSettingsRequest] = Field(default_factory=dict)
    environment: Optional[WorkerEnvironmentSettingsRequest] = None


class QualityGateSettingsRequest(BaseModel):
    """Thresholds for severity-based quality gates."""
    critical: int = Field(0, ge=0)
    high: int = Field(0, ge=0)
    medium: int = Field(0, ge=0)
    low: int = Field(0, ge=0)


class TaskGenerationDefaultsRequest(BaseModel):
    """Default policy for generated child tasks."""
    child_status: Literal["backlog", "queued"] = "backlog"
    child_hitl_mode: Literal["inherit_parent", "autopilot", "supervised", "review_only"] = "inherit_parent"
    infer_deps: bool = True


class DefaultsSettingsRequest(BaseModel):
    """Project-wide default policies applied to new tasks."""
    quality_gate: QualityGateSettingsRequest = QualityGateSettingsRequest(
        critical=0,
        high=0,
        medium=0,
        low=0,
    )
    dependency_policy: str = "prudent"
    hitl_mode: str = "supervised"
    task_generation: TaskGenerationDefaultsRequest = TaskGenerationDefaultsRequest()


class LanguageCommandsRequest(BaseModel):
    """Per-language command overrides used in worker prompts."""
    test: Optional[str] = None
    lint: Optional[str] = None
    typecheck: Optional[str] = None
    format: Optional[str] = None


class ProjectSettingsRequest(BaseModel):
    """Patch payload for project-level lint/test/typecheck command overrides."""
    commands: Optional[dict[str, LanguageCommandsRequest]] = None
    disabled_commands: Optional[list[str]] = None
    prompt_overrides: Optional[dict[str, str]] = None
    prompt_injections: Optional[dict[str, str]] = None


class UpdateSettingsRequest(BaseModel):
    """Top-level PATCH payload for runtime settings."""
    orchestrator: Optional[OrchestratorSettingsRequest] = None
    agent_routing: Optional[AgentRoutingSettingsRequest] = None
    defaults: Optional[DefaultsSettingsRequest] = None
    workers: Optional[WorkersSettingsRequest] = None
    project: Optional[ProjectSettingsRequest] = None


class SpawnAgentRequest(BaseModel):
    """Request body for adding a runtime agent with optional provider override."""
    role: str = "general"
    capacity: int = 1
    override_provider: Optional[str] = None


class ReviewActionRequest(BaseModel):
    """Request body for human review decisions, with optional guidance text."""
    guidance: Optional[str] = None


class RetryTaskRequest(BaseModel):
    """Request body for retrying execution from an optional pipeline step."""
    guidance: Optional[str] = None
    start_from_step: Optional[str] = None
    worker_provider: Optional[str] = None


class SkipToPrecommitRequest(BaseModel):
    """Optional request body for blocked->pre-commit skip transitions."""
    guidance: Optional[str] = None


class FinalizeMergeConflictRequest(BaseModel):
    """Optional request body for finalizing manually resolved merge conflicts."""
    guidance: Optional[str] = None


class AddFeedbackRequest(BaseModel):
    """Request body for storing structured reviewer feedback on a task."""
    task_id: str
    feedback_type: str = "general"
    priority: str = "should"
    summary: str
    details: str = ""
    target_file: Optional[str] = None


class AddCommentRequest(BaseModel):
    """Request body for creating a task-scoped threaded code discussion comment."""
    task_id: str
    file_path: str
    line_number: int = 0
    body: str
    line_type: Optional[str] = None
    parent_id: Optional[str] = None


VALID_TRANSITIONS: dict[str, set[str]] = {
    "backlog": {"queued", "cancelled"},
    "queued": {"backlog", "cancelled"},
    "in_progress": {"cancelled"},
    "in_review": {"blocked", "cancelled"},
    "blocked": {"queued", "cancelled"},
    "done": set(),
    "cancelled": {"backlog"},
}

IMPORT_JOB_TTL_SECONDS = 60 * 60 * 24
IMPORT_JOB_MAX_RECORDS = 200
_GATE_DISPLAY_LABELS: dict[str, str] = {
    "before_implement": "Plan ready",
    "before_generate_tasks": "Generate tasks",
    "before_done": "Work complete",
    "before_commit": "Review implementation",
    "human_intervention": "Needs intervention",
    "pipeline_classify": "Classifying\u2026",
    "select_pipeline": "Select pipeline",
    "before_post_review": "Review before posting",
}


def humanize_label(value: str | None) -> str:
    """Convert snake_case identifiers into a title-cased human label."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    return raw.replace("_", " ").strip().capitalize()


def _priority_rank(priority: str) -> int:
    return {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(priority, 9)


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _pruned_import_jobs(items: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    now = datetime.now(timezone.utc)
    kept: list[dict[str, Any]] = []
    for item in items.values():
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("id") or "").strip()
        if not job_id:
            continue
        created_at = _parse_iso_datetime(item.get("created_at"))
        if created_at and (now - created_at).total_seconds() > IMPORT_JOB_TTL_SECONDS:
            continue
        kept.append(item)

    kept.sort(key=lambda job: str(job.get("created_at") or ""), reverse=True)
    trimmed = kept[:IMPORT_JOB_MAX_RECORDS]
    return {str(job.get("id")): job for job in trimmed if str(job.get("id") or "").strip()}


def _coerce_int(value: Any, default: int, *, minimum: int = 0, maximum: Optional[int] = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed < minimum:
        parsed = minimum
    if maximum is not None and parsed > maximum:
        parsed = maximum
    return parsed


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return default


def _normalize_str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, raw in value.items():
        k = str(key or "").strip()
        v = str(raw or "").strip()
        if k and v:
            out[k] = v
    return out


def _normalize_prompt_overrides(value: Any) -> dict[str, str]:
    """Normalize per-step prompt overrides from runtime config."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for raw_step, raw_prompt in value.items():
        step = str(raw_step or "").strip().lower()
        if not step:
            continue
        prompt = raw_prompt if isinstance(raw_prompt, str) else str(raw_prompt)
        if not prompt.strip():
            continue
        out[step] = prompt
    return out


def _normalize_prompt_injections(value: Any) -> dict[str, str]:
    """Normalize per-step additive prompt injections from runtime config."""
    return _normalize_prompt_overrides(value)


def _normalize_human_blocking_issues(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            summary = item.strip()
            if summary:
                out.append({"summary": summary})
            continue
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or item.get("issue") or "").strip()
        details = str(item.get("details") or item.get("rationale") or "").strip()
        if not summary and details:
            summary = details.splitlines()[0][:200].strip()
        if not summary:
            continue
        issue: dict[str, str] = {"summary": summary}
        if details:
            issue["details"] = details
        for key in ("category", "action", "blocking_on", "severity"):
            raw = item.get(key)
            if raw is None:
                continue
            text = str(raw).strip()
            if text:
                issue[key] = text
        out.append(issue)
    return out[:20]


def _normalize_wait_state(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind") or "").strip()
    if not kind:
        return None
    wait_state: dict[str, Any] = {"kind": kind}
    for key in ("step", "reason_code", "next_retry_at", "updated_at"):
        text = str(value.get(key) or "").strip()
        if text:
            wait_state[key] = text
    for key in ("recoverable",):
        if key in value:
            wait_state[key] = bool(value.get(key))
    for key in ("attempt", "max_attempts"):
        raw = value.get(key)
        if raw is None:
            continue
        try:
            wait_state[key] = int(raw)
        except (TypeError, ValueError):
            continue
    return wait_state


def _iso_delta_seconds(start: str, end: str) -> Optional[float]:
    """Compute elapsed seconds between two ISO-8601 timestamps."""
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return max((e - s).total_seconds(), 0.0)
    except (ValueError, TypeError):
        return None


def _select_summary_run(task: Task, container: "Container") -> Any:
    """Select the run record that best matches task terminal state."""
    runs: list[Any] = []
    for run_id in reversed(task.run_ids):
        run = container.runs.get(run_id)
        if run:
            runs.append(run)
    if not runs:
        return None

    latest = runs[0]
    preferred_statuses = {
        "done": {"done"},
        "blocked": {"blocked", "interrupted", "cancelled", "error"},
        "in_review": {"in_review"},
    }.get(task.status, set())
    if not preferred_statuses and task.pending_gate:
        preferred_statuses = {"waiting_gate", "in_progress"}
    for run in runs:
        run_status = getattr(run, "status", None)
        run_finished_at = getattr(run, "finished_at", None)
        if run_status in preferred_statuses or run_finished_at:
            return run
    return latest


def _reconcile_summary_run_status(task: Task, run: Any) -> str:
    """Derive summary run status when run/task state are temporarily inconsistent."""
    terminal_task_status = {"done", "blocked", "in_review"}
    stale_run_status = {"in_progress", "running"}
    run_status = str(getattr(run, "status", "") or "")
    run_finished_at = getattr(run, "finished_at", None)
    if run_status == "waiting_gate":
        return run_status
    if task.status in terminal_task_status and run_status in stale_run_status and not run_finished_at:
        return task.status
    return run_status


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string into a datetime value."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _build_task_timing_summary(task: Task, container: "Container") -> Optional[dict[str, Any]]:
    """Build cumulative task timing data across run history.

    Uses ``RunRecord.worker_seconds_accumulated`` (plus any currently-active
    segment) so that only actual worker execution time is counted — time spent
    waiting for human intervention (gates, reviews) is excluded.
    """
    if not task.run_ids:
        return None

    total_completed_seconds = 0.0
    first_started_at: Optional[datetime] = None
    last_finished_at: Optional[datetime] = None
    active_run_started_at: Optional[datetime] = None
    seen_run = False

    for run_id in task.run_ids:
        run = container.runs.get(run_id)
        if run is None:
            continue
        seen_run = True

        started = _parse_iso_timestamp(run.started_at)
        finished = _parse_iso_timestamp(run.finished_at)

        if started is not None and (first_started_at is None or started < first_started_at):
            first_started_at = started

        if finished is not None and (last_finished_at is None or finished > last_finished_at):
            last_finished_at = finished

        if run.status == "in_progress" and started is not None:
            if active_run_started_at is None or started < active_run_started_at:
                active_run_started_at = started
            # Only include prior accumulated worker seconds for in-progress runs;
            # the frontend adds the live segment via (now - active_run_started_at).
            total_completed_seconds += run.worker_seconds_accumulated
            continue

        # For completed/paused runs, use the accumulated worker time
        # (with legacy fallback for runs that pre-date accumulation)
        total_completed_seconds += run.effective_worker_seconds()

    if not seen_run:
        return None

    return {
        "total_completed_seconds": total_completed_seconds,
        "active_run_started_at": active_run_started_at.isoformat() if active_run_started_at else None,
        "is_running": active_run_started_at is not None,
        "first_started_at": first_started_at.isoformat() if first_started_at else None,
        "last_finished_at": last_finished_at.isoformat() if last_finished_at else None,
    }


def _build_execution_summary(task: Task, container: "Container") -> Optional[dict[str, Any]]:
    """Build API-safe execution summary for terminal or in-review tasks.

    The summary collapses run step logs into stable fields consumed by the UI,
    skipping internal-only entries such as skipped steps.
    """
    if task.status not in ("in_review", "blocked", "done"):
        if not (task.status == "in_progress" and task.pending_gate):
            return None
    if not task.run_ids:
        return None
    run = _select_summary_run(task, container)
    if not run or not run.steps:
        return None
    run_status = _reconcile_summary_run_status(task, run)
    steps: list[dict[str, Any]] = []
    for step_data in run.steps:
        if not isinstance(step_data, dict):
            continue
        step_status = str(step_data.get("status") or "unknown")
        if step_status == "skipped":
            continue
        step_summary = str(step_data.get("summary") or "")
        if not step_summary and step_data.get("error"):
            step_summary = str(step_data["error"])
        entry: dict[str, Any] = {
            "step": str(step_data.get("step") or "unknown"),
            "status": step_status,
            "summary": step_summary,
        }
        if step_data.get("open_counts") and isinstance(step_data["open_counts"], dict):
            entry["open_counts"] = step_data["open_counts"]
        if step_data.get("commit"):
            entry["commit"] = str(step_data["commit"])
        started_at_raw = step_data.get("started_at")
        ts_raw = step_data.get("ts")
        if isinstance(started_at_raw, str) and isinstance(ts_raw, str):
            entry["duration_seconds"] = _iso_delta_seconds(started_at_raw, ts_raw)
        steps.append(entry)
    if not steps:
        return None
    # Derive a human-readable run summary
    status_labels = {
        "in_review": "Awaiting human review",
        "blocked": "Blocked",
        "done": "Completed",
        "error": "Failed",
        "interrupted": "Interrupted",
        "cancelled": "Cancelled",
        "running": "Running",
        "waiting_gate": "Awaiting approval",
    }
    run_summary = status_labels.get(run_status) or status_labels.get(task.status) or run_status
    run_duration = _iso_delta_seconds(run.started_at, run.finished_at) if run.started_at and run.finished_at else None
    return {
        "run_id": run.id,
        "run_status": run_status,
        "run_summary": run_summary,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_seconds": run_duration,
        "steps": steps,
    }


def _task_payload(
    task: Task,
    container: Optional["Container"] = None,
    orchestrator: Optional["OrchestratorService"] = None,
) -> dict[str, Any]:
    payload = task.to_dict()
    payload["hitl_mode"] = normalize_hitl_mode(str(payload.get("hitl_mode") or "supervised"))
    metadata = task.metadata if isinstance(task.metadata, dict) else {}
    step_timeout_seconds: int | None = None
    raw_step_timeouts = metadata.get("step_timeouts")
    if isinstance(raw_step_timeouts, dict):
        for key in ("implement", "implement_fix"):
            if key not in raw_step_timeouts:
                continue
            raw_candidate = raw_step_timeouts.get(key)
            if raw_candidate is None:
                continue
            try:
                candidate = int(raw_candidate)
            except (TypeError, ValueError):
                continue
            if candidate > 0:
                step_timeout_seconds = candidate
                break
    payload["step_timeout_seconds"] = step_timeout_seconds
    wait_state = _normalize_wait_state(task.wait_state)
    if wait_state is None:
        pending_gate = str(task.pending_gate or "").strip() or None
        if pending_gate:
            wait_state = {
                "kind": "intervention_wait" if pending_gate == "human_intervention" else "approval_wait",
                "step": str(task.current_step or metadata.get("pipeline_phase") or "").strip() or None,
                "reason_code": pending_gate,
            }
    payload["wait_state"] = wait_state
    pending_gate = str(task.pending_gate or "").strip() or None
    gate_display = _GATE_DISPLAY_LABELS.get(pending_gate or "", humanize_label(pending_gate) if pending_gate else None)
    status_kind = str((wait_state or {}).get("kind") or "").strip()
    if status_kind not in {"approval_wait", "intervention_wait"}:
        status_kind = (
            "intervention_wait"
            if pending_gate == "human_intervention"
            else ("approval_wait" if pending_gate else "")
        )
    payload["gate_context"] = {
        "is_waiting": bool(pending_gate) or status_kind in {"approval_wait", "intervention_wait"},
        "gate": pending_gate,
        "display": gate_display,
        "step": str(task.current_step or metadata.get("pipeline_phase") or "").strip() or None,
        "status_kind": status_kind or None,
    }
    payload["human_blocking_issues"] = _normalize_human_blocking_issues(metadata.get("human_blocking_issues"))
    payload["recommended_action"] = str(metadata.get("recommended_action") or "").strip() or None
    raw_actions = metadata.get("human_review_actions")
    payload["human_review_actions"] = list(raw_actions) if isinstance(raw_actions, list) else []
    can_skip_to_precommit = False
    skip_to_precommit_reason_code: str | None = None
    if orchestrator is not None:
        try:
            can_skip_to_precommit, skip_to_precommit_reason_code = orchestrator.can_skip_to_precommit(task)
        except Exception:
            can_skip_to_precommit = False
            skip_to_precommit_reason_code = "eligibility_unavailable"
    payload["can_skip_to_precommit"] = bool(can_skip_to_precommit)
    payload["skip_to_precommit_reason_code"] = (
        str(skip_to_precommit_reason_code).strip() if skip_to_precommit_reason_code else None
    )
    can_finalize_merge_conflict = False
    finalize_merge_conflict_reason_code: str | None = None
    if orchestrator is not None:
        try:
            can_finalize_merge_conflict, finalize_merge_conflict_reason_code = orchestrator.can_finalize_merge_conflict(task)
        except Exception:
            can_finalize_merge_conflict = False
            finalize_merge_conflict_reason_code = "eligibility_unavailable"
    payload["can_finalize_merge_conflict"] = bool(can_finalize_merge_conflict)
    payload["finalize_merge_conflict_reason_code"] = (
        str(finalize_merge_conflict_reason_code).strip() if finalize_merge_conflict_reason_code else None
    )
    if container is not None:
        timing_summary = _build_task_timing_summary(task, container)
        if timing_summary is not None:
            payload["timing_summary"] = timing_summary
        summary = _build_execution_summary(task, container)
        if summary is not None:
            payload["execution_summary"] = summary
    # Strip bulky source-context metadata keys (diff, plan, description) that are
    # only consumed by the worker adapter.  source_task_id and source_commit_sha
    # are intentionally kept for UI provenance display.
    payload_meta = payload.get("metadata")
    if isinstance(payload_meta, dict):
        for key in ("source_diff", "source_plan", "source_description"):
            payload_meta.pop(key, None)
        # Mask env var values in task payloads (security)
        if "env_vars" in payload_meta and isinstance(payload_meta["env_vars"], dict):
            payload_meta["env_vars"] = {k: "***" for k in payload_meta["env_vars"]}
    return payload


def _gate_display_label(gate: str | None) -> str:
    """Return a user-facing gate label for API/UI messages."""
    raw = str(gate or "").strip()
    if not raw:
        return "approval gate"
    return _GATE_DISPLAY_LABELS.get(raw, humanize_label(raw))


def _safe_state_path(raw_path: Any, state_root: Path) -> Optional[Path]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    try:
        path = Path(raw_path).expanduser().resolve()
        root = state_root.resolve()
    except Exception:
        return None
    if path == root or root in path.parents:
        return path
    return None


def _read_tail(path: Optional[Path], max_chars: int) -> tuple[str, int]:
    """Read the tail of a file.

    Returns ``(text, tail_start_byte)`` where *tail_start_byte* is the byte
    offset in the file where the returned text begins.  When the entire file
    fits within *max_chars*, ``tail_start_byte`` is ``0``.
    """
    if not path or not path.exists() or not path.is_file() or max_chars <= 0:
        return "", 0
    try:
        size = path.stat().st_size
    except Exception:
        return "", 0
    if size <= 0:
        return "", 0

    # Read only a bounded tail window (overshoot bytes for multibyte chars).
    max_bytes = min(size, max_chars * 4)
    start_byte = max(0, size - max_bytes)
    prev_byte = b"\n"
    try:
        with open(path, "rb") as fh:
            if start_byte > 0:
                fh.seek(start_byte - 1)
                prev_byte = fh.read(1) or b"\n"
            fh.seek(start_byte)
            raw = fh.read(max_bytes)
    except Exception:
        return "", 0

    text = raw.decode("utf-8", errors="replace")
    if not text:
        return "", size

    cut_at = max(0, len(text) - max_chars)
    slice_starts_mid_line = False
    if cut_at > 0:
        slice_starts_mid_line = text[cut_at - 1] != "\n"
    elif start_byte > 0 and prev_byte != b"\n":
        slice_starts_mid_line = True

    # If the returned slice starts in the middle of a line, drop the orphaned
    # prefix so NDJSON/text consumers only see full lines.
    if slice_starts_mid_line:
        first_newline = text.find("\n", cut_at)
        if first_newline == -1:
            return "", size
        cut_at = first_newline + 1

    tail = text[cut_at:]
    tail_start_byte = start_byte + len(text[:cut_at].encode("utf-8"))
    return tail, tail_start_byte


def _read_from_offset(
    path: Optional[Path],
    offset: int,
    max_bytes: int,
    read_to: int = 0,
    *,
    align_start_to_line: bool = False,
) -> tuple[str, int, int]:
    """Read file content from *offset* bytes forward.

    Returns ``(text, new_offset, chunk_start_offset)`` where *new_offset* is the
    byte offset after the returned chunk and *chunk_start_offset* is the actual
    byte offset where the returned chunk begins.

    If *read_to* > 0, do not read past that byte position in the file.
    """
    if not path or not path.exists() or not path.is_file():
        return "", 0, 0
    try:
        size = path.stat().st_size
        end = min(size, read_to) if read_to > 0 else size
        start = max(offset, 0)
        if start >= end:
            bounded = min(start, size)
            return "", bounded, bounded

        aligned_start = start
        if align_start_to_line and start > 0:
            lookback = min(start, max(max_bytes, 8192))
            with open(path, "rb") as fh:
                fh.seek(start - lookback)
                prefix = fh.read(lookback)
            newline_idx = prefix.rfind(b"\n")
            if newline_idx >= 0:
                aligned_start = (start - lookback) + newline_idx + 1

        read_limit = end - aligned_start
        if read_limit <= 0:
            bounded = min(aligned_start, size)
            return "", bounded, bounded
        # Allow aligned reads to exceed ``max_bytes`` by at most one chunk size.
        capped = min(read_limit, max_bytes * 2 if align_start_to_line else max_bytes)
        with open(path, "rb") as fh:
            fh.seek(aligned_start)
            raw = fh.read(capped)
        text = raw.decode("utf-8", errors="replace")
        return text, aligned_start + len(raw), aligned_start
    except Exception:
        return "", 0, 0


def _logs_snapshot_id(logs_meta: dict[str, Any]) -> str:
    parts = [
        str(logs_meta.get("run_dir") or ""),
        str(logs_meta.get("stdout_path") or ""),
        str(logs_meta.get("stderr_path") or ""),
        str(logs_meta.get("started_at") or ""),
    ]
    material = "|".join(parts).strip("|")
    if not material:
        return ""
    return sha256(material.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _normalize_workers_environment(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    auto_prepare = _coerce_bool(raw.get("auto_prepare"), True)
    max_auto_retries = _coerce_int(raw.get("max_auto_retries"), 3, minimum=0, maximum=20)
    capability_fallback = _coerce_bool(raw.get("capability_fallback"), True)

    normalized_required: dict[str, list[str]] = {}
    raw_required = raw.get("required_capabilities_by_step")
    if isinstance(raw_required, dict):
        for raw_step, raw_caps in raw_required.items():
            step = str(raw_step or "").strip()
            if not step or not isinstance(raw_caps, list):
                continue
            caps: list[str] = []
            for item in raw_caps:
                cap = str(item or "").strip().lower()
                if cap and cap not in caps:
                    caps.append(cap)
            if caps:
                normalized_required[step] = caps

    normalized_env_vars: dict[str, str] = {}
    raw_env_vars = raw.get("env_vars")
    if isinstance(raw_env_vars, dict):
        for raw_key, raw_val in raw_env_vars.items():
            key = str(raw_key or "").strip()
            if key and isinstance(raw_val, str):
                normalized_env_vars[key] = raw_val

    result: dict[str, Any] = {
        "auto_prepare": auto_prepare,
        "max_auto_retries": max_auto_retries,
        "capability_fallback": capability_fallback,
        "required_capabilities_by_step": normalized_required,
    }
    if normalized_env_vars:
        result["env_vars"] = normalized_env_vars
    return result


def _settings_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    orchestrator = dict(cfg.get("orchestrator") or {})
    routing = dict(cfg.get("agent_routing") or {})
    defaults = dict(cfg.get("defaults") or {})
    quality_gate = dict(defaults.get("quality_gate") or {})
    task_generation = dict(defaults.get("task_generation") or {})
    workers_cfg = dict(cfg.get("workers") or {})
    _runtime = get_workers_runtime_config(config=cfg, codex_command_fallback="codex exec")
    workers_providers = {name: provider_spec_to_dict(spec) for name, spec in _runtime.providers.items()}
    workers_environment = _normalize_workers_environment(workers_cfg.get("environment"))
    workers_default = _runtime.default_worker if _runtime.default_worker in _runtime.providers else "codex"
    workers_default_model = str(workers_cfg.get("default_model") or "").strip()
    workers_heartbeat_seconds = _coerce_int(workers_cfg.get("heartbeat_seconds"), 60, minimum=1, maximum=3600)
    workers_heartbeat_grace_seconds = _coerce_int(
        workers_cfg.get("heartbeat_grace_seconds"), 240, minimum=1, maximum=7200
    )
    if workers_heartbeat_grace_seconds < workers_heartbeat_seconds:
        workers_heartbeat_grace_seconds = workers_heartbeat_seconds
    project_cfg = dict(cfg.get("project") or {})
    prompt_defaults = get_configurable_step_prompt_defaults()
    prompt_overrides = _normalize_prompt_overrides(project_cfg.get("prompt_overrides"))
    prompt_injections = _normalize_prompt_injections(project_cfg.get("prompt_injections"))
    default_hitl_mode = normalize_hitl_mode(str(defaults.get("hitl_mode") or "supervised"))
    task_generation_status = str(task_generation.get("child_status") or "backlog").strip().lower()
    if task_generation_status not in {"backlog", "queued"}:
        task_generation_status = "backlog"
    task_generation_hitl = str(task_generation.get("child_hitl_mode") or "inherit_parent").strip().lower()
    if task_generation_hitl == "collaborative":
        task_generation_hitl = "supervised"
    if task_generation_hitl not in {"inherit_parent", "autopilot", "supervised", "review_only"}:
        task_generation_hitl = "inherit_parent"
    raw_storage_backend = str(cfg.get("storage_backend") or "sqlite").strip().lower()
    storage_backend = raw_storage_backend if raw_storage_backend == "sqlite" else "sqlite"
    return {
        "runtime": {
            "schema_version": _coerce_int(cfg.get("schema_version"), 4, minimum=4),
            "storage_backend": storage_backend,
        },
        "orchestrator": {
            "concurrency": _coerce_int(orchestrator.get("concurrency"), 2, minimum=1, maximum=128),
            "auto_deps": _coerce_bool(orchestrator.get("auto_deps"), True),
            "max_review_attempts": _coerce_int(orchestrator.get("max_review_attempts"), 10, minimum=1, maximum=50),
            "max_merge_conflict_attempts": _coerce_int(
                orchestrator.get("max_merge_conflict_attempts"), 3, minimum=1, maximum=10
            ),
            "step_timeout_seconds": _coerce_int(
                orchestrator.get("step_timeout_seconds"), 0, minimum=0, maximum=7200
            ),
            "gate_reminder_minutes": _coerce_int(
                orchestrator.get("gate_reminder_minutes"), 30, minimum=0, maximum=10080
            ),
            "gate_stale_minutes": _coerce_int(
                orchestrator.get("gate_stale_minutes"), 0, minimum=0, maximum=10080
            ),
            "gate_max_wait_minutes": _coerce_int(
                orchestrator.get("gate_max_wait_minutes"), 0, minimum=0, maximum=43200
            ),
            "gate_timeout_action": (
                str(orchestrator.get("gate_timeout_action") or "none").strip().lower()
                if str(orchestrator.get("gate_timeout_action") or "none").strip().lower() in {"none", "block"}
                else "none"
            ),
            "reliability_mode": (
                str(orchestrator.get("reliability_mode") or "strict").strip().lower()
                if str(orchestrator.get("reliability_mode") or "strict").strip().lower()
                in {"strict", "balanced", "availability"}
                else "strict"
            ),
            "reconcile_interval_seconds": _coerce_int(
                orchestrator.get("reconcile_interval_seconds"), 30, minimum=1, maximum=3600
            ),
            "lease_ttl_seconds": _coerce_int(
                orchestrator.get("lease_ttl_seconds"), 120, minimum=15, maximum=86400
            ),
            "tick_stale_seconds": _coerce_int(
                orchestrator.get("tick_stale_seconds"), 15, minimum=5, maximum=3600
            ),
            "tick_failure_threshold": _coerce_int(
                orchestrator.get("tick_failure_threshold"), 5, minimum=1, maximum=1000
            ),
            "auto_fix_push": _coerce_bool(orchestrator.get("auto_fix_push"), False),
        },
        "agent_routing": {
            "default_role": str(routing.get("default_role") or "general"),
            "task_type_roles": _normalize_str_map(routing.get("task_type_roles")),
            "role_provider_overrides": _normalize_str_map(routing.get("role_provider_overrides")),
        },
        "defaults": {
            "quality_gate": {
                "critical": _coerce_int(quality_gate.get("critical"), 0, minimum=0),
                "high": _coerce_int(quality_gate.get("high"), 0, minimum=0),
                "medium": _coerce_int(quality_gate.get("medium"), 0, minimum=0),
                "low": _coerce_int(quality_gate.get("low"), 0, minimum=0),
            },
            "dependency_policy": str(defaults.get("dependency_policy") or "prudent") if str(defaults.get("dependency_policy") or "prudent") in ("permissive", "prudent", "strict") else "prudent",
            "hitl_mode": default_hitl_mode,
            "task_generation": {
                "child_status": task_generation_status,
                "child_hitl_mode": task_generation_hitl,
                "infer_deps": _coerce_bool(task_generation.get("infer_deps"), True),
            },
        },
        "workers": {
            "default": workers_default,
            "default_model": workers_default_model,
            "heartbeat_seconds": workers_heartbeat_seconds,
            "heartbeat_grace_seconds": workers_heartbeat_grace_seconds,
            "routing": _normalize_str_map(workers_cfg.get("routing")),
            "providers": workers_providers,
            "environment": workers_environment,
        },
        "project": {
            "commands": dict(project_cfg.get("commands") or {}),
            "disabled_commands": list(project_cfg.get("disabled_commands") or []),
            "prompt_overrides": prompt_overrides,
            "prompt_injections": prompt_injections,
            "prompt_defaults": prompt_defaults,
        },
    }


def _mask_settings_env_vars(payload: dict[str, Any]) -> dict[str, Any]:
    """Mask env_vars values in a settings payload for API responses.

    Must be called at the response boundary — never before persistence.
    """
    workers = payload.get("workers")
    if isinstance(workers, dict):
        env = workers.get("environment")
        if isinstance(env, dict) and "env_vars" in env:
            workers["environment"] = dict(env)
            workers["environment"]["env_vars"] = {
                k: "***" for k in env["env_vars"]
            }
    return payload


def _workers_health_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    runtime = get_workers_runtime_config(config=cfg, codex_command_fallback="codex exec")
    known_provider_names: list[str] = ["codex", "claude", "ollama"]
    for name in sorted(runtime.providers.keys()):
        if name not in known_provider_names:
            known_provider_names.append(name)

    providers: list[dict[str, Any]] = []
    checked_at = now_iso()
    for name in known_provider_names:
        spec = runtime.providers.get(name)
        if spec is None:
            # Probe common CLI providers even when not configured, so users can
            # see local availability before adding explicit settings.
            probe_spec: Optional[WorkerProviderSpec] = None
            if name == "codex":
                probe_spec = WorkerProviderSpec(name="codex", type="codex", command="codex exec")
            elif name == "claude":
                probe_spec = WorkerProviderSpec(name="claude", type="claude", command="claude -p")

            if probe_spec is not None:
                healthy, detail = test_worker(probe_spec)
                providers.append(
                    {
                        "name": name,
                        "type": probe_spec.type,
                        "configured": False,
                        "healthy": healthy,
                        "status": "connected" if healthy else "not_configured",
                        "detail": detail if healthy else f"Provider not configured. {detail}",
                        "checked_at": checked_at,
                        "command": probe_spec.command,
                    }
                )
                continue
            providers.append(
                {
                    "name": name,
                    "type": name if name in {"codex", "claude", "ollama"} else "unknown",
                    "configured": False,
                    "healthy": False,
                    "status": "not_configured",
                    "detail": "Provider is not configured.",
                    "checked_at": checked_at,
                }
            )
            continue

        healthy, detail = test_worker(spec)
        item: dict[str, Any] = {
            "name": spec.name,
            "type": spec.type,
            "configured": True,
            "healthy": healthy,
            "status": "connected" if healthy else "unavailable",
            "detail": detail,
            "checked_at": checked_at,
        }
        if spec.command:
            item["command"] = spec.command
        if spec.endpoint:
            item["endpoint"] = spec.endpoint
        if spec.model:
            item["model"] = spec.model
        providers.append(item)

    return {"providers": providers}


def _workers_routing_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    runtime = get_workers_runtime_config(config=cfg, codex_command_fallback="codex exec")
    default_provider = runtime.default_worker if runtime.default_worker in runtime.providers else "codex"
    canonical_steps = ["plan", "analyze", "generate_tasks", "implement", "verify", "review", "commit"]
    ordered_steps = list(dict.fromkeys(canonical_steps + sorted(runtime.routing.keys())))

    rows: list[dict[str, Any]] = []
    for step in ordered_steps:
        provider_name = runtime.routing.get(step) or default_provider
        provider = runtime.providers.get(provider_name)
        rows.append(
            {
                "step": step,
                "provider": provider_name,
                "provider_type": provider.type if provider else None,
                "source": "explicit" if step in runtime.routing else "default",
                "configured": provider is not None,
            }
        )

    return {"default": default_provider, "rows": rows}


def _execution_batches(tasks: list[Task]) -> list[list[str]]:
    by_id = {task.id: task for task in tasks}
    indegree: dict[str, int] = {}
    dependents: dict[str, list[str]] = {task.id: [] for task in tasks}
    for task in tasks:
        refs = [dep_id for dep_id in task.blocked_by if dep_id in by_id]
        indegree[task.id] = len(refs)
        for dep_id in refs:
            dependents.setdefault(dep_id, []).append(task.id)

    ready = sorted([task_id for task_id, degree in indegree.items() if degree == 0], key=lambda tid: (_priority_rank(by_id[tid].priority), by_id[tid].created_at))
    batches: list[list[str]] = []
    while ready:
        batch = list(ready)
        batches.append(batch)
        next_ready: list[str] = []
        for task_id in batch:
            for dep_id in dependents.get(task_id, []):
                indegree[dep_id] -= 1
                if indegree[dep_id] == 0:
                    next_ready.append(dep_id)
        ready = sorted(next_ready, key=lambda tid: (_priority_rank(by_id[tid].priority), by_id[tid].created_at))
    return batches


def _has_unresolved_blockers(container: Container, task: Task) -> Optional[str]:
    for dep_id in task.blocked_by:
        dep = container.tasks.get(dep_id)
        if dep is None or dep.status not in {"done", "cancelled"}:
            return dep_id
    return None


def _normalize_prd_text(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _extract_task_candidates_from_chunk(chunk_text: str) -> list[str]:
    candidates: list[str] = []
    for raw_line in chunk_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("- ") or line.startswith("* "):
            title = re.sub(r"\s+", " ", line[2:].strip()).strip(" -")
            if len(title) >= 4:
                candidates.append(title)
                continue
        numbered = re.match(r"^\d+[\.\)]\s+(.*)$", line)
        if numbered:
            title = re.sub(r"\s+", " ", numbered.group(1).strip()).strip(" -")
            if len(title) >= 4:
                candidates.append(title)
                continue
        section_heading = re.match(r"^#{1,6}\s+(.*)$", line)
        if section_heading:
            title = re.sub(r"\s+", " ", section_heading.group(1).strip()).strip(" -")
            if len(title) >= 4:
                candidates.append(title)
                continue
    return candidates


def _fallback_chunk(text: str, chunk_size: int = 1200, overlap: int = 120) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    if not text.strip():
        return chunks
    idx = 0
    chunk_id = 1
    text_len = len(text)
    while idx < text_len:
        end = min(text_len, idx + chunk_size)
        chunk_text = text[idx:end].strip()
        if chunk_text:
            chunks.append(
                {
                    "id": f"c{chunk_id}",
                    "strategy": "token_window",
                    "section_path": None,
                    "char_start": idx,
                    "char_end": end,
                    "text": chunk_text,
                }
            )
            chunk_id += 1
        if end >= text_len:
            break
        idx = max(end - overlap, idx + 1)
    return chunks


def _ingest_prd(content: str, default_priority: str) -> dict[str, Any]:
    normalized = _normalize_prd_text(content)
    lines = normalized.splitlines(keepends=True)

    chunks: list[dict[str, Any]] = []
    current_heading = "Document"
    current_block: list[str] = []
    block_start = 0
    cursor = 0
    chunk_id = 1
    heading_detected = False

    def flush_block(end_offset: int) -> None:
        nonlocal chunk_id, current_block, block_start
        block_text = "".join(current_block).strip()
        if not block_text:
            current_block = []
            block_start = end_offset
            return
        chunks.append(
            {
                "id": f"c{chunk_id}",
                "strategy": "heading_section",
                "section_path": current_heading,
                "char_start": block_start,
                "char_end": end_offset,
                "text": block_text,
            }
        )
        chunk_id += 1
        current_block = []
        block_start = end_offset

    for line in lines:
        stripped = line.strip()
        is_heading = bool(re.match(r"^#{1,6}\s+\S+", stripped))
        if is_heading:
            heading_detected = True
            flush_block(cursor)
            current_heading = re.sub(r"^#{1,6}\s+", "", stripped).strip() or "Document"
        current_block.append(line)
        cursor += len(line)
    flush_block(cursor)

    if not chunks:
        chunks = _fallback_chunk(normalized)
    elif not heading_detected:
        # Convert no-heading result into paragraph chunking when structure is flat.
        chunks = _fallback_chunk(normalized)
        for item in chunks:
            item["strategy"] = "paragraph_fallback"

    task_candidates: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for chunk in chunks:
        for title in _extract_task_candidates_from_chunk(str(chunk.get("text") or "")):
            key = title.lower().strip()
            if not key or key in seen_titles:
                continue
            seen_titles.add(key)
            task_candidates.append(
                {
                    "title": title[:200],
                    "priority": default_priority,
                    "source_chunk_id": chunk["id"],
                    "source_section_path": chunk.get("section_path"),
                }
            )

    ambiguity_warnings: list[str] = []
    if not task_candidates:
        ambiguity_warnings.append(
            "No explicit task bullets/headings found; generated generic task candidates from document chunks."
        )
        for chunk in chunks[:6]:
            chunk_text = str(chunk.get("text") or "").strip()
            first_sentence = re.split(r"(?<=[\.\!\?])\s+", chunk_text, maxsplit=1)[0].strip()
            if not first_sentence:
                continue
            title = re.sub(r"\s+", " ", first_sentence).strip(" -")
            if len(title) < 4:
                continue
            task_candidates.append(
                {
                    "title": title[:200],
                    "priority": default_priority,
                    "source_chunk_id": chunk["id"],
                    "source_section_path": chunk.get("section_path"),
                    "ambiguity_reason": "derived_from_first_sentence",
                }
            )

    if not task_candidates:
        task_candidates.append(
            {
                "title": "Imported PRD task",
                "priority": default_priority,
                "source_chunk_id": chunks[0]["id"] if chunks else None,
                "source_section_path": chunks[0].get("section_path") if chunks else None,
                "ambiguity_reason": "empty_or_unstructured_document",
            }
        )

    parsed_prd = {
        "strategy": chunks[0]["strategy"] if chunks else "empty",
        "chunk_count": len(chunks),
        "chunks": chunks,
        "task_candidates": task_candidates,
        "ambiguity_warnings": ambiguity_warnings,
    }
    return {
        "original_prd": {
            "content": content,
            "normalized_content": normalized,
            "char_count": len(normalized),
            "checksum_sha256": sha256(normalized.encode("utf-8")).hexdigest(),
        },
        "parsed_prd": parsed_prd,
    }


def _generated_tasks_from_parsed_prd(parsed_prd: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = parsed_prd.get("task_candidates")
    if not isinstance(candidates, list):
        return []
    out: list[dict[str, Any]] = []
    previous_id: Optional[str] = None
    for idx, item in enumerate(candidates, start=1):
        if not isinstance(item, dict):
            continue
        generated_id = f"prd_{idx}"
        title = str(item.get("title") or f"Imported PRD task {idx}").strip() or f"Imported PRD task {idx}"
        priority = str(item.get("priority") or "P2").strip() or "P2"
        depends_on = [previous_id] if previous_id else []
        metadata = {
            "generated_ref_id": generated_id,
            "generated_depends_on": depends_on,
            "source_chunk_id": item.get("source_chunk_id"),
            "source_section_path": item.get("source_section_path"),
            "ingestion_source": "parsed_prd",
        }
        if item.get("ambiguity_reason"):
            metadata["ambiguity_reason"] = item.get("ambiguity_reason")
        out.append(
            {
                "id": generated_id,
                "title": title,
                "description": "",
                "task_type": "feature",
                "priority": priority,
                "depends_on": depends_on,
                "metadata": metadata,
            }
        )
        previous_id = generated_id
    return out


def _apply_generated_dep_links(container: Container, child_ids: list[str]) -> None:
    ref_to_task_id: dict[str, str] = {}
    for child_id in child_ids:
        child = container.tasks.get(child_id)
        if not child or not isinstance(child.metadata, dict):
            continue
        ref_id = str(child.metadata.get("generated_ref_id") or "").strip()
        if ref_id:
            ref_to_task_id[ref_id] = child.id

    for child_id in child_ids:
        child = container.tasks.get(child_id)
        if not child or not isinstance(child.metadata, dict):
            continue
        raw_deps = child.metadata.get("generated_depends_on")
        if not isinstance(raw_deps, list):
            continue
        changed = False
        for dep_ref in raw_deps:
            dep_id = ref_to_task_id.get(str(dep_ref or "").strip())
            if not dep_id or dep_id == child.id:
                continue
            if dep_id not in child.blocked_by:
                child.blocked_by.append(dep_id)
                changed = True
            dep_task = container.tasks.get(dep_id)
            if dep_task and child.id not in dep_task.blocks:
                dep_task.blocks.append(child.id)
                container.tasks.upsert(dep_task)
        if changed:
            container.tasks.upsert(child)


def _canonical_task_type_for_pipeline(registry: PipelineRegistry, pipeline_id: str) -> str:
    template = registry.get(pipeline_id)
    if template.task_types:
        return str(template.task_types[0])
    return "feature"


def _normalize_pipeline_classification_output(
    *,
    summary: str | None,
    allowed_pipelines: list[str],
    registry: PipelineRegistry,
) -> PipelineClassificationResponse:
    allowed_set = set(allowed_pipelines)
    pipeline_id = "feature"
    confidence: Literal["high", "low"] = "low"
    reason = "Pipeline auto-classification was inconclusive."

    if isinstance(summary, str) and summary.strip():
        try:
            payload = json.loads(summary)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            raw_pipeline = str(payload.get("pipeline_id") or "").strip()
            raw_confidence = str(payload.get("confidence") or "").strip().lower()
            raw_reason = str(payload.get("reason") or "").strip()
            if raw_pipeline in allowed_set:
                pipeline_id = raw_pipeline
            if raw_confidence == "high":
                confidence = "high"
            elif raw_confidence == "low":
                confidence = "low"
            if raw_reason:
                reason = raw_reason[:300]

    if pipeline_id not in allowed_set:
        pipeline_id = "feature"
        confidence = "low"
        reason = "Classifier returned an unknown pipeline."
    task_type = _canonical_task_type_for_pipeline(registry, pipeline_id)
    return PipelineClassificationResponse(
        pipeline_id=pipeline_id,
        task_type=task_type,
        confidence=confidence,
        reason=reason,
        allowed_pipelines=allowed_pipelines,
    )


def create_router(
    resolve_container: Any,
    resolve_orchestrator: Any,
    job_store: dict[str, dict[str, Any]],
    terminal_services: dict[str, TerminalService] | None = None,
) -> APIRouter:
    """Create the runtime API router.

    Args:
        resolve_container (Any): Callable that resolves and returns the
            project-scoped ``Container`` for an optional ``project_dir`` value.
        resolve_orchestrator (Any): Callable that resolves and returns the
            project-scoped ``OrchestratorService`` for an optional
            ``project_dir`` value.
        job_store (dict[str, dict[str, Any]]): Shared in-memory map used to keep
            asynchronous import and plan-refinement job state visible across API
            requests.
        terminal_services (dict[str, TerminalService] | None): Shared map of
            per-project terminal service instances.  When ``None`` a new dict is
            created internally.

    Returns:
        APIRouter: Router exposing runtime endpoints for task lifecycle,
        orchestration operations, collaboration metadata, terminal sessions, and
        import workflows.
    """
    router = APIRouter(prefix="/api", tags=["api"])
    if terminal_services is None:
        terminal_services = {}

    def _ctx(project_dir: Optional[str]) -> tuple[Container, EventBus, OrchestratorService]:
        container: Container = resolve_container(project_dir)
        bus = EventBus(container.events, container.project_id)
        orchestrator: OrchestratorService = resolve_orchestrator(project_dir)
        return container, bus, orchestrator

    def _terminal_ctx(project_dir: Optional[str]) -> tuple[Container, EventBus, TerminalService]:
        container: Container = resolve_container(project_dir)
        bus = EventBus(container.events, container.project_id)
        key = str(container.project_dir)
        service = terminal_services.get(key)
        if service is None:
            service = TerminalService(container, bus)
            terminal_services[key] = service
        return container, bus, service

    def _load_feedback_records(container: Container) -> list[dict[str, Any]]:
        cfg = container.config.load()
        raw = cfg.get("collaboration_feedback")
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    def _save_feedback_records(container: Container, items: list[dict[str, Any]]) -> None:
        cfg = container.config.load()
        cfg["collaboration_feedback"] = items
        container.config.save(cfg)

    def _load_comment_records(container: Container) -> list[dict[str, Any]]:
        cfg = container.config.load()
        raw = cfg.get("collaboration_comments")
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    def _save_comment_records(container: Container, items: list[dict[str, Any]]) -> None:
        cfg = container.config.load()
        cfg["collaboration_comments"] = items
        container.config.save(cfg)

    def _load_import_jobs(container: Container) -> dict[str, dict[str, Any]]:
        cfg = container.config.load()
        raw = cfg.get("import_jobs")
        jobs: dict[str, dict[str, Any]] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, dict):
                    job_id = str(value.get("id") or key).strip()
                    if job_id:
                        item = dict(value)
                        item["id"] = job_id
                        jobs[job_id] = item
        elif isinstance(raw, list):
            for value in raw:
                if isinstance(value, dict):
                    job_id = str(value.get("id") or "").strip()
                    if job_id:
                        jobs[job_id] = dict(value)
        return _pruned_import_jobs(jobs)

    def _save_import_jobs(container: Container, jobs: dict[str, dict[str, Any]]) -> None:
        cfg = container.config.load()
        cfg["import_jobs"] = list(_pruned_import_jobs(jobs).values())
        container.config.save(cfg)

    def _upsert_import_job(container: Container, job: dict[str, Any]) -> None:
        jobs = _load_import_jobs(container)
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            return
        jobs[job_id] = job
        _save_import_jobs(container, jobs)

    def _fetch_import_job(container: Container, job_id: str) -> Optional[dict[str, Any]]:
        jobs = _load_import_jobs(container)
        _save_import_jobs(container, jobs)
        return jobs.get(job_id)

    def _prune_in_memory_jobs() -> None:
        pruned = _pruned_import_jobs(dict(job_store))
        job_store.clear()
        job_store.update(pruned)

    from .deps import RouteDeps
    from .routes_agents import register_agent_routes
    from .routes_collab import register_collab_routes
    from .routes_git import register_git_routes
    from .routes_imports import register_import_routes
    from .routes_misc import register_misc_routes
    from .routes_projects import register_project_routes
    from .routes_tasks import register_task_routes
    from .routes_terminal import register_terminal_routes

    deps = RouteDeps(
        resolve_container=resolve_container,
        resolve_orchestrator=resolve_orchestrator,
        job_store=job_store,
        ctx=_ctx,
        terminal_ctx=_terminal_ctx,
        load_feedback_records=_load_feedback_records,
        save_feedback_records=_save_feedback_records,
        load_comment_records=_load_comment_records,
        save_comment_records=_save_comment_records,
        prune_in_memory_jobs=_prune_in_memory_jobs,
        upsert_import_job=_upsert_import_job,
        fetch_import_job=_fetch_import_job,
    )

    register_project_routes(router, deps)
    register_task_routes(router, deps)
    register_import_routes(router, deps)
    register_agent_routes(router, deps)
    register_collab_routes(router, deps)
    register_terminal_routes(router, deps)
    register_misc_routes(router, deps)
    register_git_routes(router, deps)

    return router

def os_access(path: Path) -> bool:
    """Check whether the current process can enumerate a directory path.

    Args:
        path (Path): Filesystem path to validate.

    Returns:
        bool: `True` when the operation succeeds, otherwise `False`.
    """
    try:
        list(path.iterdir())
    except Exception:
        return False
    return True
