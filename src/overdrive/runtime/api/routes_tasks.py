"""Task-focused route registration for the runtime API."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, cast

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, model_validator

from ...collaboration.modes import normalize_hitl_mode
from ...pipelines.registry import PipelineRegistry
from ..orchestrator.human_guidance import (
    clear_active_human_guidance,
    set_active_human_guidance,
)
from ..domain.models import (
    DependencyPolicy,
    Priority,
    ReviewMode,
    Task,
    TaskStatus,
    now_iso,
)
from ..domain.scope_contract import normalize_scope_contract
from ..storage.bootstrap import (
    archive_state_root,
    archive_task_context_manifest,
    ensure_state_root,
)
from .deps import RouteDeps
from . import router_impl as impl


AddDependencyRequest = impl.AddDependencyRequest
ApproveGateRequest = impl.ApproveGateRequest
CommitPlanRequest = impl.CommitPlanRequest
CreatePlanRevisionRequest = impl.CreatePlanRevisionRequest
CreateTaskRequest = impl.CreateTaskRequest
GenerateTasksRequest = impl.GenerateTasksRequest
FinalizeMergeConflictRequest = impl.FinalizeMergeConflictRequest
PipelineClassificationRequest = impl.PipelineClassificationRequest
PipelineClassificationResponse = impl.PipelineClassificationResponse
PlanRefineRequest = impl.PlanRefineRequest
RetryTaskRequest = impl.RetryTaskRequest
SkipToPrecommitRequest = impl.SkipToPrecommitRequest
TransitionRequest = impl.TransitionRequest
UpdateTaskRequest = impl.UpdateTaskRequest
VALID_TRANSITIONS = impl.VALID_TRANSITIONS
_canonical_task_type_for_pipeline = impl._canonical_task_type_for_pipeline
_execution_batches = impl._execution_batches
_gate_display_label = impl._gate_display_label
_has_unresolved_blockers = impl._has_unresolved_blockers
_logs_snapshot_id = impl._logs_snapshot_id
_normalize_pipeline_classification_output = impl._normalize_pipeline_classification_output
_priority_rank = impl._priority_rank
_read_from_offset = impl._read_from_offset
_read_tail = impl._read_tail
_safe_state_path = impl._safe_state_path
_task_payload = impl._task_payload


class CreatePullRequestReviewRequest(BaseModel):
    """Request body for creating a pull request review task."""

    guidance: str = ""
    review_mode: ReviewMode = "fix_only"


# ---------------------------------------------------------------------------
# Review mode → pipeline mapping
# ---------------------------------------------------------------------------

_REVIEW_MODE_TO_PIPELINE_GITHUB: dict[str, tuple[str, str]] = {
    # review_mode → (task_type, pipeline_id)
    "fix_only": ("pr_review_fix_only", "pr_review_fix_only"),
    "review_comment": ("pr_review_comment", "pr_review_comment"),
    "summarize": ("pr_review_summarize", "pr_review_summarize"),
    "fix_respond": ("pr_review_fix_respond", "pr_review_fix_respond"),
}

_REVIEW_MODE_TO_PIPELINE_GITLAB: dict[str, tuple[str, str]] = {
    # review_mode → (task_type, pipeline_id)
    "fix_only": ("mr_review", "mr_review"),
    "review_comment": ("mr_review_comment", "mr_review_comment"),
    "summarize": ("mr_review_summarize", "mr_review_summarize"),
    "fix_respond": ("mr_review_fix_respond", "mr_review_fix_respond"),
}

# Backward-compatible alias retained for tests and existing imports. This maps
# review modes to the GitHub/default task types.
_REVIEW_MODE_TO_PIPELINE = _REVIEW_MODE_TO_PIPELINE_GITHUB

_MODES_NEEDING_COMMENTS: set[str] = {"review_comment", "summarize", "fix_respond"}

_MAX_FETCHED_COMMENTS = 200
_MAX_COMMENT_BODY_CHARS = 4000

_CHANGES_TELEMETRY_RECENT: dict[tuple[str, str, str, str], float] = {}
_CHANGES_TELEMETRY_TTL_SECONDS = 900.0
_CHANGES_TELEMETRY_MAX_KEYS = 2048
_LOW_CONFIDENCE_FILE_THRESHOLD = 120
_LOW_CONFIDENCE_LINE_THRESHOLD = 4000
_INTERNAL_TASK_METADATA_KEYS = {
    "worktree_dir",
    "task_context",
    "preserved_branch",
    "preserved_base_branch",
    "preserved_base_sha",
    "preserved_head_sha",
    "preserved_merge_base_sha",
    "preserved_at",
    "review_context",
    "execution_checkpoint",
    "pending_precommit_approval",
    "review_stage",
    "active_human_guidance",
    "retry_guidance",
    "requested_changes",
    "pipeline_phase",
    "step_outputs",
    "task_generation_defaults",
    "task_generation_override",
    "source_task_id",
    "source_commit_sha",
    "source_description",
    "source_plan",
    "source_diff",
}


def _error_detail_envelope(
    *,
    detail: str,
    code: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a backward-compatible error envelope for API responses."""
    payload: dict[str, Any] = {"detail": detail}
    if code:
        payload["code"] = code
    if data:
        payload["data"] = data
    return payload


def _gate_approved_message(gate: str | None, *, will_resume: bool) -> str:
    normalized = str(gate or "").strip()
    if will_resume:
        if normalized == "before_implement":
            return "Plan approved. Task will resume shortly."
        if normalized == "before_generate_tasks":
            return "Task generation approved. Task will resume shortly."
        if normalized == "before_done":
            return "Marked done. Task will finalize shortly."
        if normalized == "before_commit":
            return "Approved. Task will resume shortly."
        if normalized == "before_post_review":
            return "Review approved. Proceeding to post."
        if normalized == "human_intervention":
            return "Intervention acknowledged. Task will resume shortly."
        gate_label = _gate_display_label(normalized)
        if gate_label:
            return f"{gate_label} approved. Task will resume shortly."
        return "Gate approved. Task will resume shortly."

    if normalized == "human_intervention":
        return "Intervention acknowledged. Task is still blocked; retry when ready."
    gate_label = _gate_display_label(normalized)
    if gate_label:
        return f"{gate_label} approved."
    return "Gate approved."


def _gate_changes_requested_message(gate: str | None, *, start_from_step: str | None) -> str:
    normalized = str(gate or "").strip()
    if normalized == "before_implement":
        return "Changes requested. Task resumed from planning."
    if normalized == "before_generate_tasks":
        return "Changes requested. Task resumed before task generation."
    if normalized == "before_done":
        return "Refinement requested. Task will re-run analysis."
    if normalized == "before_post_review":
        return "Review refinement requested. Task will re-run the review step."
    if start_from_step:
        return f"Changes requested. Task resumed from {start_from_step}."
    return "Changes requested. Task resumed."


def _resume_step_for_gate(gate: str | None) -> str | None:
    mapping = {
        "before_plan": "plan",
        "before_implement": "implement",
        "before_generate_tasks": "generate_tasks",
        "before_done": "__before_done__",
        "before_commit": "commit",
        "before_post_review": "post_comments",
    }
    return mapping.get(str(gate or "").strip())


def _pipeline_steps(task: Task) -> list[str]:
    if task.pipeline_template:
        return [str(step).strip() for step in task.pipeline_template if str(step).strip()]
    try:
        return PipelineRegistry().resolve_for_task_type(task.task_type).step_names()
    except Exception:
        return []


def _task_context_manifest_entry(task: Task) -> dict[str, Any] | None:
    metadata = task.metadata if isinstance(task.metadata, dict) else {}
    context_raw = metadata.get("task_context")
    context = context_raw if isinstance(context_raw, dict) else {}
    retained_worktree = str(context.get("worktree_dir") or metadata.get("worktree_dir") or "").strip()
    task_branch = str(context.get("task_branch") or "").strip()
    preserved_branch = str(metadata.get("preserved_branch") or "").strip()
    has_context = bool(retained_worktree or task_branch or preserved_branch)
    if not has_context:
        return None
    return {
        "task_id": task.id,
        "status": task.status,
        "run_ids": list(task.run_ids or []),
        "retained_worktree_dir": retained_worktree or None,
        "task_branch": task_branch or None,
        "preserved_branch": preserved_branch or None,
        "preserved_base_branch": str(metadata.get("preserved_base_branch") or "").strip() or None,
        "preserved_base_sha": str(metadata.get("preserved_base_sha") or "").strip() or None,
        "preserved_head_sha": str(metadata.get("preserved_head_sha") or "").strip() or None,
        "preserved_merge_base_sha": str(metadata.get("preserved_merge_base_sha") or "").strip() or None,
        "retained_reason": str(context.get("retained_reason") or "").strip() or None,
        "retained_at": str(context.get("retained_at") or "").strip() or None,
        "expected_on_retry": bool(context.get("expected_on_retry")),
        "archived_at": now_iso(),
    }


def _collect_task_context_manifest_entries(tasks: list[Task]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for task in tasks:
        entry = _task_context_manifest_entry(task)
        if entry is not None:
            entries.append(entry)
    return entries


def _sanitize_client_task_metadata(raw: dict[str, Any] | None) -> dict[str, Any]:
    metadata = dict(raw or {})
    sanitized: dict[str, Any] = {}
    scope_contract_raw = metadata.get("scope_contract")
    normalized_scope_contract = normalize_scope_contract(scope_contract_raw)
    for key, value in metadata.items():
        normalized = str(key)
        if normalized in _INTERNAL_TASK_METADATA_KEYS or normalized.startswith("preserved_"):
            continue
        if normalized == "scope_contract":
            continue
        sanitized[normalized] = value
    if normalized_scope_contract is not None:
        sanitized["scope_contract"] = normalized_scope_contract
    return sanitized


def _generation_policy_overrides(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Extract optional generated-task policy overrides from request payloads."""
    source = dict(raw) if isinstance(raw, dict) else {}
    overrides: dict[str, Any] = {}
    if "child_status" in source and source.get("child_status") is not None:
        overrides["child_status"] = source.get("child_status")
    if "child_hitl_mode" in source and source.get("child_hitl_mode") is not None:
        overrides["child_hitl_mode"] = source.get("child_hitl_mode")
    if "infer_deps" in source and source.get("infer_deps") is not None:
        overrides["infer_deps"] = source.get("infer_deps")
    return overrides


def _previous_step(steps: list[str], target_step: str) -> str | None:
    if target_step not in steps:
        return None
    index = steps.index(target_step)
    if index <= 0:
        return target_step
    return steps[index - 1]


def _request_changes_step_for_gate(task: Task, gate: str | None) -> str | None:
    normalized = str(gate or "").strip()
    steps = _pipeline_steps(task)
    if normalized == "before_implement":
        return _previous_step(steps, "implement")
    if normalized == "before_generate_tasks":
        return _previous_step(steps, "generate_tasks")
    if normalized == "before_done":
        # For early-completed tasks, retry from the step that triggered
        # early completion (stored in task.current_step by the executor).
        # For non-early-completed tasks, retry from the last pipeline step.
        if isinstance(task.metadata, dict) and task.metadata.get("early_complete"):
            return task.current_step or (steps[0] if steps else None)
        return steps[-1] if steps else task.current_step
    if normalized == "before_commit":
        return _previous_step(steps, "commit") or "implement"
    if normalized == "before_plan":
        return _previous_step(steps, "plan") or "plan"
    if normalized == "before_post_review":
        # Re-run from the review step that precedes the posting step.
        for posting_step in ("post_comments", "post_comment_responses"):
            prev = _previous_step(steps, posting_step)
            if prev and prev != posting_step:
                return prev
        return task.current_step or None
    return task.current_step or None


def _guidance_fallback_step(task: Task, *, target_step: str | None) -> str | None:
    if str(target_step or "").strip() == "implement_fix":
        return None
    pipeline_steps = set(_pipeline_steps(task))
    if any(step in pipeline_steps for step in {"implement", "prototype", "verify", "benchmark", "review", "commit"}):
        return "implement_fix"
    return None


def _effective_retry_target_step(task: Task) -> str | None:
    if isinstance(task.metadata, dict):
        retry_from = str(task.metadata.get("retry_from_step") or "").strip()
        if retry_from:
            return retry_from
    pipeline_steps = _pipeline_steps(task)
    return pipeline_steps[0] if pipeline_steps else None


def _mark_latest_run_interrupted(container: Any, task: Task, *, summary: str, ts: str) -> None:
    latest_run = None
    for run_id in reversed(task.run_ids):
        latest_run = container.runs.get(run_id)
        if latest_run:
            break
    if latest_run and latest_run.status in {"in_review", "in_progress", "waiting_gate"}:
        latest_run.status = "interrupted"
        latest_run.finished_at = latest_run.finished_at or ts
        latest_run.summary = latest_run.summary or summary
        container.runs.upsert(latest_run)


def _parse_stat_files(stat_text: str) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for line in str(stat_text or "").strip().splitlines():
        text = line.strip()
        if not text or text.startswith("Showing") or " | " not in text:
            continue
        parts = text.split(" | ", 1)
        files.append({"path": parts[0].strip(), "changes": parts[1].strip() if len(parts) > 1 else ""})
    return files


def _is_within_project(candidate: Path, project_root: Path) -> bool:
    try:
        resolved_candidate = candidate.resolve()
        resolved_root = project_root.resolve()
    except Exception:
        return False
    return resolved_candidate == resolved_root or resolved_root in resolved_candidate.parents


def _is_valid_task_worktree_path(
    *,
    task_id: str,
    candidate: Path,
    project_root: Path,
    state_root: Path,
    strict_retained: bool,
) -> bool:
    try:
        resolved_candidate = candidate.resolve()
        resolved_root = project_root.resolve()
        resolved_state = state_root.resolve()
    except Exception:
        return False
    if strict_retained:
        expected = (resolved_state / "worktrees" / str(task_id)).resolve()
        if resolved_candidate != expected:
            return False
    else:
        if not (resolved_candidate == resolved_root or resolved_root in resolved_candidate.parents):
            return False
    if not resolved_candidate.exists() or not resolved_candidate.is_dir():
        return False
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=resolved_candidate,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception:
        return False
    return inside.returncode == 0 and str(inside.stdout or "").strip().lower() == "true"


def _resolve_worktree_diff_base_ref(
    *,
    task: Task,
    metadata: dict[str, Any],
    git_dir: Path,
    head_ref: str | None,
) -> tuple[str | None, str]:
    task_context_raw = metadata.get("task_context")
    task_context = task_context_raw if isinstance(task_context_raw, dict) else {}
    preferred_base = str(task_context.get("base_branch") or metadata.get("preserved_base_branch") or "").strip()
    if preferred_base and _resolve_commit_sha(git_dir, preferred_base):
        return preferred_base, "task_context"
    review_context_raw = metadata.get("review_context")
    review_context = review_context_raw if isinstance(review_context_raw, dict) else {}
    review_base = str(review_context.get("base_branch") or "").strip()
    if review_base and _resolve_commit_sha(git_dir, review_base):
        return review_base, "review_context"

    # Prefer the snapshot of HEAD at worktree dispatch time — this excludes
    # dependency-task commits that were merged before this task started.
    initial_head = str(task_context.get("initial_head_sha") or "").strip()
    if initial_head and _resolve_commit_sha(git_dir, initial_head):
        return initial_head, "initial_head"

    heuristic = _resolve_base_branch(git_dir, avoid_branch=str(head_ref or "").strip() or None)
    if heuristic and _resolve_commit_sha(git_dir, heuristic):
        # Guard against including dependency-task commits in the diff.
        # If every commit between the heuristic base and HEAD belongs to
        # other tasks (not this one), the base is too broad — fall back to
        # HEAD so only this task's uncommitted work is shown.
        task_id = str(task.id)
        try:
            log_result = subprocess.run(
                ["git", "log", "--oneline", f"{heuristic}..HEAD"],
                cwd=git_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if log_result.returncode == 0:
                commits = log_result.stdout.strip().splitlines()
                if commits and not any(task_id in line for line in commits):
                    return "HEAD", "head_inferred"
        except Exception:
            pass
        return heuristic, "heuristic"
    return None, "none"


def _is_runtime_state_path(path_text: str) -> bool:
    normalized = str(path_text or "").replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized == ".overdrive" or normalized.startswith(".overdrive/")


def _short_sha(value: str | None) -> str:
    raw = str(value or "").strip()
    return raw[:12] if raw else ""


def _resolve_commit_sha(git_dir: Path, ref: str | None) -> str | None:
    normalized = str(ref or "").strip()
    if not normalized:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{normalized}^{{commit}}"],
            cwd=git_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception:
        return None
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        return None
    return sha


def _merge_base_sha(git_dir: Path, left_sha: str | None, right_sha: str | None) -> str | None:
    left = str(left_sha or "").strip()
    right = str(right_sha or "").strip()
    if not left or not right:
        return None
    try:
        result = subprocess.run(
            ["git", "merge-base", left, right],
            cwd=git_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception:
        return None
    merge_sha = result.stdout.strip()
    if result.returncode != 0 or not merge_sha:
        return None
    return merge_sha


def _parse_shortstat_counts(text: str) -> tuple[int, int, int]:
    raw = str(text or "").strip()
    if not raw:
        return 0, 0, 0
    files = 0
    additions = 0
    deletions = 0
    file_match = re.search(r"(\d+)\s+files?\s+changed", raw)
    if file_match:
        files = int(file_match.group(1))
    add_match = re.search(r"(\d+)\s+insertions?\(\+\)", raw)
    if add_match:
        additions = int(add_match.group(1))
    del_match = re.search(r"(\d+)\s+deletions?\(-\)", raw)
    if del_match:
        deletions = int(del_match.group(1))
    return files, additions, deletions


def _emit_changes_telemetry_once(
    bus: Any,
    *,
    task_id: str,
    event_type: str,
    base_sha: str | None,
    head_sha: str | None,
    payload: dict[str, Any],
) -> None:
    if bus is None:
        return
    key = (
        str(event_type).strip(),
        str(task_id).strip(),
        str(base_sha or "").strip(),
        str(head_sha or "").strip(),
    )
    now_monotonic = time.monotonic()

    if _CHANGES_TELEMETRY_RECENT:
        stale_keys = [
            existing
            for existing, emitted_at in _CHANGES_TELEMETRY_RECENT.items()
            if now_monotonic - emitted_at > _CHANGES_TELEMETRY_TTL_SECONDS
        ]
        for stale in stale_keys:
            _CHANGES_TELEMETRY_RECENT.pop(stale, None)

    existing_ts = _CHANGES_TELEMETRY_RECENT.get(key)
    if existing_ts is not None and (now_monotonic - existing_ts) <= _CHANGES_TELEMETRY_TTL_SECONDS:
        return

    _CHANGES_TELEMETRY_RECENT[key] = now_monotonic
    if len(_CHANGES_TELEMETRY_RECENT) > _CHANGES_TELEMETRY_MAX_KEYS:
        oldest = sorted(_CHANGES_TELEMETRY_RECENT.items(), key=lambda item: item[1])[: max(1, _CHANGES_TELEMETRY_MAX_KEYS // 8)]
        for old_key, _ in oldest:
            _CHANGES_TELEMETRY_RECENT.pop(old_key, None)

    try:
        bus.emit(
            channel="tasks",
            event_type=event_type,
            entity_id=task_id,
            payload=payload,
        )
    except Exception:
        # Telemetry must never fail the changes endpoint.
        return


_UNTRACKED_DIFF_FILE_CAP = 60


def _diff_untracked_files(
    git_dir: Path,
    status_entries: list[dict[str, str]],
) -> tuple[str, list[dict[str, str]]]:
    """Generate unified diff text and file entries for untracked (``??``) files.

    Returns ``(diff_text, file_entries)`` where *diff_text* is concatenated
    unified diff output for all untracked files and *file_entries* are
    ``{"path": ..., "changes": ...}`` dicts suitable for the files list.
    """
    has_untracked = any(entry.get("code") == "??" for entry in status_entries)
    if not has_untracked:
        return "", []

    try:
        ls_result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=git_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return "", []

    untracked_paths = [
        p.strip()
        for p in ls_result.stdout.splitlines()
        if p.strip() and not _is_runtime_state_path(p.strip())
    ]

    if not untracked_paths:
        return "", []

    capped = untracked_paths[:_UNTRACKED_DIFF_FILE_CAP]
    diff_parts: list[str] = []
    file_entries: list[dict[str, str]] = []

    for fpath in capped:
        try:
            result = subprocess.run(
                ["git", "diff", "--no-index", "/dev/null", fpath],
                cwd=git_dir,
                capture_output=True,
                text=True,
                check=False,  # exit-code 1 is normal for diffs with changes
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        raw = result.stdout or ""
        if raw:
            diff_parts.append(raw)
            file_entries.append({"path": fpath, "changes": "new file"})

    return "\n".join(diff_parts), file_entries


def _resolve_base_branch(git_dir: Path, *, avoid_branch: str | None = None) -> str | None:
    candidates: list[str] = []
    try:
        remote_head = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            cwd=git_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if remote_head.returncode == 0:
            value = remote_head.stdout.strip()
            if value.startswith("origin/"):
                value = value[len("origin/") :]
            if value:
                candidates.append(value)
    except Exception:
        pass

    candidates.extend(["main", "master", "develop", "trunk"])
    try:
        configured_default = subprocess.run(
            ["git", "config", "--get", "init.defaultBranch"],
            cwd=git_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        ).stdout.strip()
        if configured_default:
            candidates.append(configured_default)
    except Exception:
        pass
    try:
        current = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=git_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        current_name = current.stdout.strip()
        if (
            current.returncode == 0
            and current_name
            and current_name != "HEAD"
            and current_name != str(avoid_branch or "").strip()
        ):
            candidates.append(current_name)
    except Exception:
        pass
    try:
        local_heads = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
            cwd=git_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if local_heads.returncode == 0:
            for raw in local_heads.stdout.splitlines():
                name = raw.strip()
                if not name or name == str(avoid_branch or "").strip():
                    continue
                if name.startswith("task-"):
                    continue
                candidates.append(name)
    except Exception:
        pass

    seen: set[str] = set()
    for name in candidates:
        if not name or name in seen or name == str(avoid_branch or "").strip():
            continue
        seen.add(name)
        try:
            verify = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/heads/{name}"],
                cwd=git_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if verify.returncode == 0:
                return name
        except Exception:
            continue
    return None


def _preserved_branch_changes(
    *,
    task_id: str,
    metadata: dict[str, Any] | None,
    git_dir: Path,
    bus: Any,
) -> dict[str, Any] | None:
    metadata_obj = metadata if isinstance(metadata, dict) else {}
    review_context_raw = metadata_obj.get("review_context")
    review_context: dict[str, Any] = review_context_raw if isinstance(review_context_raw, dict) else {}

    preserved_branch = str((metadata_obj or {}).get("preserved_branch") or "").strip()
    if not preserved_branch:
        return None

    head_sha = _resolve_commit_sha(git_dir, str(metadata_obj.get("preserved_head_sha") or "").strip())
    if not head_sha:
        head_sha = _resolve_commit_sha(git_dir, f"refs/heads/{preserved_branch}")
    if not head_sha:
        return None

    base_ref: str | None = None
    base_sha: str | None = None
    base_source = "none"
    confidence: str = "low"

    metadata_base_sha = _resolve_commit_sha(git_dir, str(metadata_obj.get("preserved_base_sha") or "").strip())
    metadata_base_branch = str(metadata_obj.get("preserved_base_branch") or "").strip()
    review_base_sha = _resolve_commit_sha(git_dir, str(review_context.get("base_sha") or "").strip())
    review_base_branch = str(review_context.get("base_branch") or "").strip()

    if metadata_base_sha:
        base_sha = metadata_base_sha
        base_ref = metadata_base_branch or metadata_base_sha
        base_source = "metadata_sha"
        confidence = "high"

    if not base_sha and metadata_base_branch:
        resolved = _resolve_commit_sha(git_dir, metadata_base_branch)
        if resolved:
            base_sha = resolved
            base_ref = metadata_base_branch
            base_source = "metadata_branch"
            confidence = "high"

    if not base_sha and review_base_sha:
        base_sha = review_base_sha
        base_ref = review_base_branch or review_base_sha
        base_source = "review_context_sha"
        confidence = "medium"

    if not base_sha and review_base_branch:
        resolved = _resolve_commit_sha(git_dir, review_base_branch)
        if resolved:
            base_sha = resolved
            base_ref = review_base_branch
            base_source = "review_context_branch"
            confidence = "medium"

    if not base_sha:
        heuristic_base_branch = _resolve_base_branch(git_dir, avoid_branch=preserved_branch)
        if heuristic_base_branch:
            resolved = _resolve_commit_sha(git_dir, heuristic_base_branch)
            if resolved:
                base_sha = resolved
                base_ref = heuristic_base_branch
                base_source = "heuristic"
                confidence = "low"

    if not base_sha:
        return None

    # Use the merge-base (not the raw base tip) so the diff only contains
    # the task's own changes and excludes work merged into the base branch
    # after the task branched off.
    explicit_merge_base_sha = _resolve_commit_sha(git_dir, str(metadata_obj.get("preserved_merge_base_sha") or "").strip())
    computed_merge_base_sha = explicit_merge_base_sha or _merge_base_sha(git_dir, base_sha, head_sha)
    effective_base = computed_merge_base_sha or base_sha

    diff_range = f"{effective_base}..{head_sha}"
    try:
        branch_stat = subprocess.run(
            ["git", "diff", "--stat", diff_range],
            cwd=git_dir,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        branch_diff = subprocess.run(
            ["git", "diff", diff_range],
            cwd=git_dir,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        shortstat_result = subprocess.run(
            ["git", "diff", "--shortstat", diff_range],
            cwd=git_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    branch_files = _parse_stat_files(branch_stat.stdout)
    branch_diff_text = branch_diff.stdout or ""
    branch_stat_text = branch_stat.stdout or ""
    if not branch_diff_text and not branch_files and not branch_stat_text.strip():
        return None

    warnings: list[str] = []

    if base_source == "heuristic":
        warnings.append("heuristic_base_inferred")
        files_changed, additions, deletions = _parse_shortstat_counts(shortstat_result.stdout if shortstat_result else "")
        if files_changed <= 0:
            files_changed = len(branch_files)
        if files_changed >= _LOW_CONFIDENCE_FILE_THRESHOLD:
            warnings.append("large_file_count")
        if (additions + deletions) >= _LOW_CONFIDENCE_LINE_THRESHOLD:
            warnings.append("large_line_churn")
        if computed_merge_base_sha and computed_merge_base_sha != base_sha:
            warnings.append("base_not_ancestor")
        confidence = "low"

        _emit_changes_telemetry_once(
            bus,
            task_id=task_id,
            event_type="task.changes_heuristic_base_used",
            base_sha=base_sha,
            head_sha=head_sha,
            payload={
                "base_source": base_source,
                "base_ref": base_ref,
                "base_sha": base_sha,
                "head_ref": preserved_branch,
                "head_sha": head_sha,
                "warnings": list(warnings),
            },
        )

    if warnings:
        _emit_changes_telemetry_once(
            bus,
            task_id=task_id,
            event_type="task.changes_low_confidence",
            base_sha=base_sha,
            head_sha=head_sha,
            payload={
                "base_source": base_source,
                "confidence": confidence,
                "warnings": list(warnings),
                "base_ref": base_ref,
                "base_sha": base_sha,
                "head_ref": preserved_branch,
                "head_sha": head_sha,
            },
        )

    display_base_branch = str(base_ref or "").strip() or None
    return {
        "mode": "preserved_branch",
        "context_source": "preserved_branch",
        "commit": None,
        "branch": preserved_branch,
        "base_branch": display_base_branch,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "head_ref": preserved_branch,
        "head_sha": head_sha,
        "base_source": base_source,
        "confidence": confidence,
        "warnings": warnings,
        "files": branch_files,
        "diff": branch_diff_text,
        "stat": branch_stat_text,
    }


def _timestamp_sort_value(value: Any, *, descending: bool) -> float:
    """Convert ISO timestamps into sortable numeric values."""
    raw = str(value or "").strip()
    if not raw:
        return float("inf")
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return float("inf")
    stamp = parsed.timestamp()
    return -stamp if descending else stamp


def _board_item_sort_key(status: str, item: dict[str, Any]) -> tuple[Any, ...]:
    """Return status-aware sort keys for Kanban board columns."""
    priority = _priority_rank(str(item.get("priority") or "P3"))
    created_at = _timestamp_sort_value(item.get("created_at"), descending=False)
    updated_at_asc = _timestamp_sort_value(item.get("updated_at"), descending=False)
    updated_at_desc = _timestamp_sort_value(item.get("updated_at"), descending=True)
    task_id = str(item.get("id") or "")
    if status in {"backlog", "queued"}:
        return (priority, created_at, updated_at_asc, task_id)
    if status == "in_progress":
        return (priority, updated_at_desc, created_at, task_id)
    if status == "in_review":
        return (priority, updated_at_asc, created_at, task_id)
    if status == "blocked":
        return (priority, updated_at_desc, created_at, task_id)
    if status == "done":
        return (updated_at_desc, priority, created_at, task_id)
    if status == "cancelled":
        return (updated_at_desc, priority, created_at, task_id)
    return (priority, created_at, updated_at_asc, task_id)


def _remove_task_relationship_refs(*, task_id: str, container: Any) -> None:
    """Remove references to a deleted task from all remaining tasks."""
    for existing in container.tasks.list():
        changed = False
        if task_id in existing.blocked_by:
            existing.blocked_by = [dep_id for dep_id in existing.blocked_by if dep_id != task_id]
            changed = True
        if task_id in existing.blocks:
            existing.blocks = [dep_id for dep_id in existing.blocks if dep_id != task_id]
            changed = True
        if existing.parent_id == task_id:
            existing.parent_id = None
            changed = True
        if task_id in existing.children_ids:
            existing.children_ids = [child_id for child_id in existing.children_ids if child_id != task_id]
            changed = True
        if changed:
            existing.updated_at = now_iso()
            container.tasks.upsert(existing)


def _set_task_step_timeout(task: Task, timeout_seconds: int | None) -> None:
    """Set or clear per-task implement timeout metadata."""
    if not isinstance(task.metadata, dict):
        task.metadata = {}
    metadata = task.metadata
    existing = metadata.get("step_timeouts")
    step_timeouts: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    if timeout_seconds is None:
        step_timeouts.pop("implement", None)
        step_timeouts.pop("implement_fix", None)
        if step_timeouts:
            metadata["step_timeouts"] = step_timeouts
        else:
            metadata.pop("step_timeouts", None)
        return
    step_timeouts["implement"] = int(timeout_seconds)
    metadata["step_timeouts"] = step_timeouts


_MAX_DIFF_CHARS = 100_000


def _truncate_diff(diff_text: str) -> str:
    """Truncate diff at line boundary with notice."""
    if len(diff_text) <= _MAX_DIFF_CHARS:
        return diff_text
    truncated = diff_text[:_MAX_DIFF_CHARS].rsplit("\n", 1)[0]
    return truncated + "\n\n[DIFF TRUNCATED — exceeded 100K character limit. Consult the --stat summary above and use `git diff` to read remaining files individually.]"


def _detect_git_platform(project_dir: Path) -> str | None:
    """Detect whether the project uses GitHub or GitLab from the git remote URL."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=project_dir, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
        if "github.com" in url:
            return "github"
        if "gitlab" in url:
            return "gitlab"
        return None
    except Exception:
        return None


def _fetch_github_pr_context(
    git_dir: Path, pr_number: int, *, fetch_comments: bool = False,
) -> dict[str, Any]:
    """Fetch GitHub PR metadata, diff, and stat via the ``gh`` CLI.

    Args:
        git_dir: Path to the git repository.
        pr_number: GitHub pull request number.
        fetch_comments: When True, also fetch PR comments and review comments.

    Returns:
        A dict with keys: title, body, head_ref, base_ref, url, diff, stat,
        and optionally ``comments`` (list of dicts) when *fetch_comments* is True.

    Raises:
        HTTPException: If metadata fetch fails.
    """
    # Fetch PR metadata.
    try:
        meta_result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "title,body,headRefName,baseRefName,url"],
            cwd=git_dir, capture_output=True, text=True, check=True, timeout=30,
        )
        pr_meta = json.loads(meta_result.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch PR metadata: {exc}")

    title = str(pr_meta.get("title") or "").strip()
    body = str(pr_meta.get("body") or "").strip()
    head_ref = str(pr_meta.get("headRefName") or "").strip()
    base_ref = str(pr_meta.get("baseRefName") or "").strip()
    url = str(pr_meta.get("url") or "").strip()

    # Fetch PR diff.
    try:
        diff_result = subprocess.run(
            ["gh", "pr", "diff", str(pr_number)],
            cwd=git_dir, capture_output=True, text=True, check=True, timeout=30,
        )
        diff_text = _truncate_diff(diff_result.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        diff_text = ""

    # Fetch diff stat.
    stat_text = ""
    try:
        stat_result = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "--stat"],
            cwd=git_dir, capture_output=True, text=True, timeout=15,
        )
        if stat_result.returncode == 0:
            stat_text = stat_result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    if not stat_text and base_ref and head_ref:
        try:
            stat_result = subprocess.run(
                ["git", "diff", "--stat", f"{base_ref}...{head_ref}"],
                cwd=git_dir, capture_output=True, text=True, timeout=15,
            )
            if stat_result.returncode == 0:
                stat_text = stat_result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            pass

    result: dict[str, Any] = {
        "title": title,
        "body": body,
        "head_ref": head_ref,
        "base_ref": base_ref,
        "url": url,
        "diff": diff_text,
        "stat": stat_text,
    }

    if fetch_comments:
        result["comments"] = _fetch_github_pr_comments(git_dir, pr_number)

    return result


def _fetch_github_pr_comments(git_dir: Path, pr_number: int) -> list[dict[str, Any]]:
    """Fetch PR comments and review comments via the ``gh`` CLI.

    Returns a list of comment dicts, each with keys: id, author, body,
    created_at, type ("comment" or "review"), path (str | None),
    line (int | None).  On failure returns an empty list (non-fatal).
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "comments,reviews"],
            cwd=git_dir, capture_output=True, text=True, check=True, timeout=30,
        )
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return []

    comments: list[dict[str, Any]] = []

    for c in data.get("comments", []):
        comments.append({
            "id": str(c.get("id", "")),
            "author": str((c.get("author") or {}).get("login", "")),
            "body": str(c.get("body", ""))[:_MAX_COMMENT_BODY_CHARS],
            "created_at": str(c.get("createdAt", "")),
            "type": "comment",
            "path": None,
            "line": None,
        })

    for r in data.get("reviews", []):
        comments.append({
            "id": str(r.get("id", "")),
            "author": str((r.get("author") or {}).get("login", "")),
            "body": str(r.get("body", ""))[:_MAX_COMMENT_BODY_CHARS],
            "created_at": str(r.get("submittedAt", "")),
            "type": "review",
            "path": None,
            "line": None,
        })

    return comments[:_MAX_FETCHED_COMMENTS]


def _fetch_gitlab_mr_context(
    git_dir: Path, mr_number: int, *, fetch_comments: bool = False,
) -> dict[str, Any]:
    """Fetch GitLab MR metadata, diff, and stat via the ``glab`` CLI.

    Args:
        git_dir: Path to the git repository.
        mr_number: GitLab merge request number.
        fetch_comments: When True, also fetch MR comments and include both raw
            and formatted comment context.

    Returns:
        A dict with keys: title, body, head_ref, base_ref, url, diff, stat,
        and optionally ``comments`` when *fetch_comments* is True.

    Raises:
        HTTPException: If metadata fetch fails.
    """
    # Fetch MR metadata.
    try:
        meta_result = subprocess.run(
            ["glab", "mr", "view", str(mr_number), "--output", "json"],
            cwd=git_dir, capture_output=True, text=True, check=True, timeout=30,
        )
        mr_meta = json.loads(meta_result.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch MR metadata: {exc}")

    title = str(mr_meta.get("title") or "").strip()
    body = str(mr_meta.get("description") or "").strip()
    head_ref = str(mr_meta.get("source_branch") or "").strip()
    base_ref = str(mr_meta.get("target_branch") or "").strip()
    url = str(mr_meta.get("web_url") or "").strip()

    # Fetch MR diff.
    try:
        diff_result = subprocess.run(
            ["glab", "mr", "diff", str(mr_number)],
            cwd=git_dir, capture_output=True, text=True, check=True, timeout=30,
        )
        diff_text = _truncate_diff(diff_result.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        diff_text = ""

    # Fetch diff stat via git.
    stat_text = ""
    if base_ref and head_ref:
        try:
            stat_result = subprocess.run(
                ["git", "diff", "--stat", f"{base_ref}...{head_ref}"],
                cwd=git_dir, capture_output=True, text=True, timeout=15,
            )
            if stat_result.returncode == 0:
                stat_text = stat_result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            pass

    result: dict[str, Any] = {
        "title": title,
        "body": body,
        "head_ref": head_ref,
        "base_ref": base_ref,
        "url": url,
        "diff": diff_text,
        "stat": stat_text,
    }

    if fetch_comments:
        from ...comments.formatter import format_comments_for_prompt
        from ...comments.reader import CommentFetchError, fetch_mr_comments
        from ...comments.writer import parse_source_url

        try:
            project_id = str(parse_source_url(url)["project_id"])
        except (ValueError, KeyError, TypeError):
            project_id = ""

        result["project_id"] = project_id
        if project_id:
            try:
                comments = fetch_mr_comments(project_id, mr_number, cwd=git_dir)
            except CommentFetchError:
                comments = []
            result["comments"] = [c.to_dict() for c in comments]
            result["comments_formatted"] = format_comments_for_prompt(comments)
        else:
            result["comments"] = []
            result["comments_formatted"] = ""
    elif url:
        try:
            from ...comments.writer import parse_source_url

            result["project_id"] = str(parse_source_url(url)["project_id"])
        except (ValueError, KeyError, TypeError):
            pass

    return result


SelectPipelineRequest = impl.SelectPipelineRequest

_bg_classify_logger = logging.getLogger(__name__ + ".bg_classify")


async def _run_background_classification(
    task_id: str,
    project_dir: str | None,
    allowed_pipelines: list[str],
    deps: RouteDeps,
) -> None:
    """Classify a task's pipeline in the background and update the gate."""
    try:
        container, bus, orchestrator = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task or task.pending_gate != "pipeline_classify":
            return
        registry = PipelineRegistry()
        synthetic = Task(
            title=task.title,
            description=task.description,
            task_type="feature",
            status="queued",
            metadata={"classification_allowed_pipelines": allowed_pipelines},
        )
        try:
            result = await asyncio.to_thread(
                orchestrator.worker_adapter.run_step_ephemeral,
                task=synthetic, step="pipeline_classify", attempt=1,
            )
        except Exception:
            _bg_classify_logger.warning("Background classification failed for %s", task_id, exc_info=True)
            result = None

        # Re-fetch task to avoid overwriting concurrent edits
        task = container.tasks.get(task_id)
        if not task:
            return
        if task.pending_gate != "pipeline_classify":
            return  # Gate was already resolved manually

        if result and result.status == "ok":
            classified = _normalize_pipeline_classification_output(
                summary=result.summary,
                allowed_pipelines=allowed_pipelines,
                registry=registry,
            )
            if classified.confidence == "high":
                resolved_type = _canonical_task_type_for_pipeline(registry, classified.pipeline_id)
                template = registry.resolve_for_task_type(resolved_type)
                task.task_type = resolved_type
                task.pipeline_template = template.step_names()
                task.pending_gate = None
                task.metadata["classifier_pipeline_id"] = classified.pipeline_id
                task.metadata["classifier_confidence"] = "high"
                task.metadata["classifier_reason"] = classified.reason
                task.metadata["final_pipeline_id"] = template.id
            else:
                task.pending_gate = "select_pipeline"
                task.metadata["classifier_pipeline_id"] = classified.pipeline_id
                task.metadata["classifier_confidence"] = "low"
                task.metadata["classifier_reason"] = classified.reason
                task.metadata["classification_allowed_pipelines"] = allowed_pipelines
        else:
            task.pending_gate = "select_pipeline"
            task.metadata["classifier_confidence"] = "failed"
            task.metadata["classifier_reason"] = "Auto-classification failed."
            task.metadata["classification_allowed_pipelines"] = allowed_pipelines

        container.tasks.upsert(task)
        bus.emit(
            channel="tasks", event_type="task.updated",
            entity_id=task.id, payload={"status": task.status},
        )
    except Exception:
        _bg_classify_logger.error("Unhandled error in background classification for %s", task_id, exc_info=True)
        try:
            container, bus, _ = deps.ctx(project_dir)
            task = container.tasks.get(task_id)
            if task and task.pending_gate == "pipeline_classify":
                task.pending_gate = "select_pipeline"
                task.metadata["classifier_confidence"] = "failed"
                task.metadata["classifier_reason"] = "Auto-classification failed."
                task.metadata["classification_allowed_pipelines"] = allowed_pipelines
                container.tasks.upsert(task)
                bus.emit(
                    channel="tasks", event_type="task.updated",
                    entity_id=task.id, payload={"status": task.status},
                )
        except Exception:
            _bg_classify_logger.error("Failed to recover task %s after classification error", task_id, exc_info=True)


def register_task_routes(router: APIRouter, deps: RouteDeps) -> None:
    """Register task, planning, execution, and logs routes."""
    @router.post("/tasks")
    async def create_task(body: CreateTaskRequest, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Create and persist a task with resolved pipeline configuration.
        
        Args:
            body: Request payload describing task metadata and pipeline preferences.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload containing the created task details.
        
        Raises:
            HTTPException: If task type auto-resolution inputs are invalid.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        registry = PipelineRegistry()
        allowed_pipelines = sorted(template.id for template in registry.list_templates())
        classifier_pipeline_id = str(body.classifier_pipeline_id or "").strip()
        classifier_confidence = str(body.classifier_confidence or "").strip().lower()
        classifier_reason = str(body.classifier_reason or "").strip()
        classifier_pipeline_valid = classifier_pipeline_id in allowed_pipelines
        classifier_confidence_valid = classifier_confidence in {"high", "low"}
        requested_task_type = str(body.task_type or "feature").strip() or "feature"

        needs_background_classification = False
        if requested_task_type == "auto":
            if classifier_confidence == "high" and classifier_pipeline_valid:
                resolved_task_type = _canonical_task_type_for_pipeline(registry, classifier_pipeline_id)
            else:
                # Defer classification to background — use feature as placeholder
                resolved_task_type = "feature"
                needs_background_classification = True
        else:
            resolved_task_type = requested_task_type

        template = registry.resolve_for_task_type(resolved_task_type)
        final_pipeline_id = template.id
        pipeline_steps = body.pipeline_template
        if pipeline_steps is None:
            pipeline_steps = template.step_names()
        dep_policy = body.dependency_policy.strip() if body.dependency_policy else ""
        if dep_policy not in ("permissive", "prudent", "strict"):
            cfg = container.config.load()
            dep_policy = str((cfg.get("defaults") or {}).get("dependency_policy") or "prudent")
            if dep_policy not in ("permissive", "prudent", "strict"):
                dep_policy = "prudent"
        cfg = container.config.load()
        configured_hitl_mode = normalize_hitl_mode((cfg.get("defaults") or {}).get("hitl_mode"))
        requested_hitl_mode = normalize_hitl_mode(body.hitl_mode, default="")
        hitl_mode = requested_hitl_mode or configured_hitl_mode
        priority = body.priority if body.priority in ("P0", "P1", "P2", "P3") else "P2"
        task = Task(
            title=body.title,
            description=body.description,
            task_type=resolved_task_type,
            priority=cast(Priority, priority),
            labels=body.labels,
            blocked_by=body.blocked_by,
            parent_id=body.parent_id,
            pipeline_template=pipeline_steps,
            hitl_mode=hitl_mode,
            dependency_policy=cast(DependencyPolicy, dep_policy),
            source=body.source,
            worker_model=(str(body.worker_model).strip() if body.worker_model else None),
            worker_provider=(str(body.worker_provider).strip() if body.worker_provider else None),
            metadata=_sanitize_client_task_metadata(body.metadata),
            project_commands=(body.project_commands if body.project_commands else None),
        )
        if not isinstance(task.metadata, dict):
            task.metadata = {}
        if body.step_timeout_seconds is not None:
            _set_task_step_timeout(task, body.step_timeout_seconds)
        if classifier_pipeline_valid and classifier_confidence_valid:
            task.metadata["classifier_pipeline_id"] = classifier_pipeline_id
            task.metadata["classifier_confidence"] = classifier_confidence
            task.metadata["classifier_reason"] = classifier_reason
            task.metadata["was_user_override"] = bool(body.was_user_override)
        task.metadata["final_pipeline_id"] = final_pipeline_id
        if needs_background_classification:
            task.pending_gate = "pipeline_classify"
        requested_status = str(body.status or "").strip()
        valid_statuses = {"backlog", "queued", "in_progress", "in_review", "done", "blocked", "cancelled"}
        if requested_status in valid_statuses:
            task.status = cast(TaskStatus, requested_status)
        elif not requested_status:
            # Keep API create deterministic by default; callers can opt into
            # immediate scheduler pickup by explicitly passing status=queued.
            task.status = "backlog"
        if task.parent_id:
            parent = container.tasks.get(task.parent_id)
            if parent and task.id not in parent.children_ids:
                parent.children_ids.append(task.id)
                container.tasks.upsert(parent)
        container.tasks.upsert(task)
        bus.emit(channel="tasks", event_type="task.created", entity_id=task.id, payload={"status": task.status})
        if needs_background_classification:
            asyncio.ensure_future(_run_background_classification(
                task.id, project_dir, allowed_pipelines, deps,
            ))
        return {"task": _task_payload(task, orchestrator=orchestrator)}

    @router.post("/tasks/classify-pipeline", response_model=PipelineClassificationResponse)
    async def classify_pipeline(
        body: PipelineClassificationRequest,
        project_dir: Optional[str] = Query(None),
    ) -> PipelineClassificationResponse:
        """Classify the best pipeline template for a proposed task.
        
        Args:
            body: Request payload with title, description, and optional metadata.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A normalized pipeline classification result with confidence and rationale.
        """
        container, _, orchestrator = deps.ctx(project_dir)
        registry = PipelineRegistry()
        allowed_pipelines = sorted(template.id for template in registry.list_templates())
        synthetic = Task(
            title=str(body.title or "").strip(),
            description=str(body.description or "").strip(),
            task_type="feature",
            status="queued",
            metadata=_sanitize_client_task_metadata(body.metadata),
        )
        if not isinstance(synthetic.metadata, dict):
            synthetic.metadata = {}
        synthetic.metadata["classification_allowed_pipelines"] = allowed_pipelines
        try:
            result = orchestrator.worker_adapter.run_step_ephemeral(task=synthetic, step="pipeline_classify", attempt=1)
        except Exception:
            result = None
        if result is None or result.status != "ok":
            return PipelineClassificationResponse(
                pipeline_id="feature",
                task_type="feature",
                confidence="low",
                reason="Pipeline auto-classification failed. Please select a pipeline.",
                allowed_pipelines=allowed_pipelines,
            )
        return _normalize_pipeline_classification_output(
            summary=result.summary,
            allowed_pipelines=allowed_pipelines,
            registry=registry,
        )

    @router.post("/tasks/{task_id}/select-pipeline")
    async def select_pipeline(
        task_id: str,
        body: SelectPipelineRequest,
        project_dir: Optional[str] = Query(None),
    ) -> dict[str, Any]:
        """Manually select a pipeline for a task awaiting classification."""
        container, bus, orchestrator = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        if task.pending_gate != "select_pipeline":
            raise HTTPException(400, "Task is not awaiting pipeline selection")
        registry = PipelineRegistry()
        allowed = sorted(t.id for t in registry.list_templates())
        if body.pipeline_id not in allowed:
            raise HTTPException(400, f"Unknown pipeline: {body.pipeline_id}")
        resolved_type = _canonical_task_type_for_pipeline(registry, body.pipeline_id)
        template = registry.resolve_for_task_type(resolved_type)
        task.task_type = resolved_type
        task.pipeline_template = template.step_names()
        task.pending_gate = None
        task.metadata["final_pipeline_id"] = template.id
        task.metadata["was_user_override"] = True
        task.metadata.pop("classification_allowed_pipelines", None)
        container.tasks.upsert(task)
        bus.emit(
            channel="tasks", event_type="task.updated",
            entity_id=task.id, payload={"status": task.status},
        )
        return {"task": _task_payload(task, orchestrator=orchestrator)}

    @router.get("/tasks")
    async def list_tasks(
        project_dir: Optional[str] = Query(None),
        status: Optional[str] = Query(None),
        task_type: Optional[str] = Query(None),
        priority: Optional[str] = Query(None),
    ) -> dict[str, Any]:
        """List tasks filtered by lifecycle, type, or priority.

        Args:
            project_dir: Optional project directory used to resolve runtime state.
            status: Optional status filter.
            task_type: Optional task type filter.
            priority: Optional priority filter.
        
        Returns:
            A payload with sorted task summaries and total count.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        tasks = container.tasks.list()
        filtered = []
        for task in tasks:
            if status and task.status != status:
                continue
            if task_type and task.task_type != task_type:
                continue
            if priority and task.priority != priority:
                continue
            filtered.append(task)
        filtered.sort(key=lambda t: (_priority_rank(t.priority), t.created_at))
        return {
            "tasks": [_task_payload(task, orchestrator=orchestrator) for task in filtered],
            "total": len(filtered),
        }

    @router.get("/tasks/board")
    async def board(project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Return task data grouped by Kanban-style status columns.
        
        Args:
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload keyed by task status with sorted task cards.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        columns: dict[str, list[dict[str, Any]]] = {
            name: [] for name in ["backlog", "queued", "in_progress", "in_review", "blocked", "done", "cancelled"]
        }
        for task in container.tasks.list():
            columns.setdefault(task.status, []).append(_task_payload(task, orchestrator=orchestrator))
        for status, items in columns.items():
            items.sort(key=lambda item: _board_item_sort_key(status, item))
        return {"columns": columns}

    @router.post("/tasks/queue-backlog")
    async def queue_backlog(
        project_dir: Optional[str] = Query(None),
    ) -> dict[str, Any]:
        """Transition all backlog tasks to queued status.

        Args:
            project_dir: Optional project directory used to resolve runtime state.

        Returns:
            A payload with the count of queued tasks and their IDs.
        """
        container, bus, _orchestrator = deps.ctx(project_dir)
        tasks = container.tasks.list()
        backlog_tasks = [t for t in tasks if t.status == "backlog"]

        queued_ids: list[str] = []
        for task in backlog_tasks:
            task.status = "queued"
            task.updated_at = now_iso()
            container.tasks.upsert(task)
            queued_ids.append(task.id)
            bus.emit(
                channel="tasks",
                event_type="task.updated",
                entity_id=task.id,
                payload={"status": "queued"},
            )

        if queued_ids:
            bus.emit(
                channel="queue",
                event_type="queue.changed",
                entity_id="",
                payload={"queued_count": len(queued_ids)},
            )

        return {
            "queued_count": len(queued_ids),
            "task_ids": queued_ids,
            "message": f"Queued {len(queued_ids)} backlog task(s)." if queued_ids else "No backlog tasks to queue.",
        }

    @router.post("/tasks/clear")
    async def clear_tasks(
        project_dir: Optional[str] = Query(None),
        force: bool = Query(False),
        timeout_seconds: float = Query(10.0, ge=0.0, le=120.0),
    ) -> dict[str, Any]:
        """Clear all tasks by archiving runtime state and reinitializing storage.

        Args:
            project_dir: Optional project directory used to resolve runtime state.
            force: Whether to force-cancel active tasks before clear.
            timeout_seconds: Maximum wait time for shutdown/quiescence.

        Returns:
            A payload indicating clear status and archive destination path.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        pre_clear_status = str(orchestrator.status().get("status") or "running")
        clear_succeeded = False
        clear_timeout = max(float(timeout_seconds), 0.0)
        force_cancel_result: dict[str, Any] = {
            "cancelled_task_ids": [],
            "already_cancelled_task_ids": [],
            "terminal_skipped_task_ids": [],
            "missing_task_ids": [],
            "failed": {},
        }

        orchestrator.control("pause")
        try:
            # Pause intake and stop scheduler/workers before mutating state files.
            orchestrator.shutdown(timeout=clear_timeout)
            blockers_before = orchestrator.active_execution_blockers()

            if int(blockers_before.get("count") or 0) > 0 and not force:
                message = "Cannot clear tasks while execution is still active. Retry with force=true."
                raise HTTPException(
                    status_code=409,
                    detail=_error_detail_envelope(
                        detail=message,
                        code="active_execution",
                        data={"active_execution": blockers_before},
                    ),
                )

            blockers_after = blockers_before
            quiescence_result: dict[str, Any] | None = None
            if int(blockers_before.get("count") or 0) > 0 and force:
                force_cancel_result = orchestrator.force_cancel_tasks(
                    list(blockers_before.get("task_ids") or []),
                    source="api_clear_force",
                )
                quiescence_result = orchestrator.wait_for_execution_quiescence(timeout=clear_timeout)
                blockers_after = dict(quiescence_result.get("blockers") or {})
                if not bool(quiescence_result.get("quiescent")):
                    message = "Forced clear timed out waiting for active execution to stop."
                    raise HTTPException(
                        status_code=409,
                        detail=_error_detail_envelope(
                            detail=message,
                            code="active_execution_timeout",
                            data={
                                "active_execution": blockers_after,
                                "force_cancel_result": force_cancel_result,
                                "waited_seconds": quiescence_result.get("waited_seconds"),
                            },
                        ),
                    )

            context_entries = _collect_task_context_manifest_entries(container.tasks.list())
            context_manifest = archive_task_context_manifest(container.project_dir, context_entries)
            archived_to = archive_state_root(container.project_dir)
            ensure_state_root(container.project_dir)
            deps.job_store.clear()

            archive_path = str(archived_to) if archived_to else ""
            message = (
                f"Cleared all tasks. Archived previous runtime state to {archive_path}."
                if archive_path
                else "Cleared all tasks. No existing runtime state archive was needed."
            )
            payload = {
                "archived_to": archive_path,
                "message": message,
                "cleared_at": now_iso(),
                "archived_context_count": len(context_entries),
                "archived_context_manifest_path": str(context_manifest) if context_manifest else "",
                "force": force,
                "timeout_seconds": clear_timeout,
                "active_execution_before": blockers_before,
                "active_execution_after": blockers_after,
                "force_cancel_result": force_cancel_result,
                "quiescence_result": quiescence_result or {"quiescent": True, "waited_seconds": 0.0},
            }
            bus.emit(channel="tasks", event_type="tasks.cleared", entity_id=container.project_id, payload=payload)
            bus.emit(channel="notifications", event_type="tasks.cleared", entity_id=container.project_id, payload=payload)
            clear_succeeded = True
            return {"cleared": True, **payload}
        finally:
            # On success, return intake to running mode after storage reinit.
            # On failure, restore previous status to avoid side-effecting control state.
            if clear_succeeded:
                orchestrator.control("resume")
            elif pre_clear_status == "paused":
                orchestrator.control("pause")
            elif pre_clear_status == "stopped":
                orchestrator.control("stop")
            else:
                orchestrator.control("resume")

    @router.get("/tasks/execution-order")
    async def execution_order(project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Compute execution batches for non-terminal tasks.
        
        Args:
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload containing ready-to-run batches and recently completed tasks.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        tasks = container.tasks.list()
        terminal = {"done", "cancelled"}
        pending = [t for t in tasks if t.status not in terminal]
        completed = [t for t in tasks if t.status in terminal]
        completed.sort(key=lambda t: t.updated_at, reverse=True)
        return {
            "batches": _execution_batches(pending),
            "completed": [t.id for t in completed],
        }

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Load one task and return its expanded payload.
        
        Args:
            task_id: Identifier of the task to fetch.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload containing the requested task.
        
        Raises:
            HTTPException: If the task does not exist.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return {"task": _task_payload(task, container, orchestrator)}

    @router.delete("/tasks/{task_id}")
    async def delete_task(task_id: str, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Delete a terminal task and clean stale task relationship references.

        Args:
            task_id: Identifier of the task to delete.
            project_dir: Optional project directory used to resolve runtime state.

        Returns:
            A payload indicating successful deletion.

        Raises:
            HTTPException: If the task is missing or non-terminal.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status not in {"done", "cancelled"}:
            raise HTTPException(status_code=400, detail="Only terminal tasks (done/cancelled) can be deleted.")

        context_entry = _task_context_manifest_entry(task)
        context_manifest = (
            archive_task_context_manifest(container.project_dir, [context_entry])
            if context_entry is not None
            else None
        )
        _remove_task_relationship_refs(task_id=task_id, container=container)
        if not container.tasks.delete(task_id):
            raise HTTPException(status_code=404, detail="Task not found")
        bus.emit(channel="tasks", event_type="task.deleted", entity_id=task_id, payload={"status": task.status})
        return {
            "deleted": True,
            "task_id": task_id,
            "archived_context_count": 1 if context_entry is not None else 0,
            "archived_context_manifest_path": str(context_manifest) if context_manifest else "",
        }

    @router.get("/tasks/{task_id}/diff")
    async def get_task_diff(task_id: str, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Load git diff metadata for the most recent commit step on a task.

        Args:
            task_id: Identifier of the task whose commit diff should be loaded.
            project_dir: Optional project directory used to resolve runtime
                state and git context.

        Returns:
            A payload with ``commit``, ``files``, ``diff``, and ``stat`` keys.
            When no commit step exists, returns
            ``{"commit": None, "files": [], "diff": "", "stat": ""}``.

        Raises:
            HTTPException: If the task does not exist (404) or git diff/stat
                retrieval fails (500).
        """
        container, _, _ = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        # Find the commit SHA from the latest run's steps.
        commit_sha: str | None = None
        for run_id in reversed(task.run_ids):
            for run in container.runs.list():
                if run.id == run_id:
                    for step in reversed(run.steps or []):
                        if isinstance(step, dict) and step.get("step") == "commit" and step.get("commit"):
                            commit_sha = str(step["commit"])
                            break
                    if commit_sha:
                        break
            if commit_sha:
                break

        if not commit_sha:
            return {"commit": None, "files": [], "diff": "", "stat": ""}

        git_dir = container.project_dir
        try:
            stat_result = subprocess.run(
                ["git", "show", "--stat", "--format=", commit_sha],
                cwd=git_dir, capture_output=True, text=True, check=True, timeout=10,
            )
            diff_result = subprocess.run(
                ["git", "diff", f"{commit_sha}~1..{commit_sha}"],
                cwd=git_dir, capture_output=True, text=True, check=True, timeout=10,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            raise HTTPException(status_code=500, detail=f"Failed to read diff: {exc}") from exc

        files = _parse_stat_files(stat_result.stdout)

        return {
            "commit": commit_sha,
            "files": files,
            "diff": diff_result.stdout,
            "stat": stat_result.stdout,
        }

    @router.get("/tasks/{task_id}/changes")
    async def get_task_changes(task_id: str, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Return best-available task change evidence.

        Prefers committed diff when present; otherwise returns current working tree
        changes for task worktree/repo context.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        task_metadata = task.metadata if isinstance(task.metadata, dict) else {}
        worktree_dir = None
        worktree_candidate_present = False
        if isinstance(task_metadata, dict):
            candidate = str(task_metadata.get("worktree_dir") or "").strip()
            if candidate:
                worktree_candidate_present = True
                worktree_dir = candidate
        git_dir: Path | None = None
        project_root = container.project_dir
        has_task_worktree = False
        strict_retained_context = task.status == "blocked"
        if worktree_dir:
            try:
                worktree_path = Path(worktree_dir).expanduser().resolve()
            except Exception:
                worktree_path = None
            if (
                worktree_path
                and _is_valid_task_worktree_path(
                    task_id=task.id,
                    candidate=worktree_path,
                    project_root=project_root,
                    state_root=container.state_root,
                    strict_retained=strict_retained_context,
                )
            ):
                git_dir = worktree_path
                has_task_worktree = True
        prefer_retained_worktree = bool(task.status == "blocked" and has_task_worktree)

        if not prefer_retained_worktree:
            commit_payload: dict[str, Any] | None = None
            try:
                commit_payload = await get_task_diff(task_id, project_dir)
            except HTTPException as exc:
                if exc.status_code != 500:
                    raise
            if commit_payload and commit_payload.get("commit"):
                return {
                    "mode": "committed",
                    "context_source": "committed",
                    "commit": commit_payload.get("commit"),
                    "base_ref": None,
                    "base_sha": None,
                    "head_ref": None,
                    "head_sha": None,
                    "base_source": "none",
                    "confidence": "high",
                    "warnings": [],
                    "files": commit_payload.get("files") or [],
                    "diff": commit_payload.get("diff") or "",
                    "stat": commit_payload.get("stat") or "",
                }

        if has_task_worktree and git_dir is not None:
            try:
                status_result = subprocess.run(
                    ["git", "status", "--short"],
                    cwd=git_dir, capture_output=True, text=True, check=True, timeout=10,
                )
                head_ref_result = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=git_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                head_ref: str | None = str(head_ref_result.stdout or "").strip()
                if head_ref_result.returncode != 0 or not head_ref or head_ref == "HEAD":
                    head_ref = None
                has_head = (
                    subprocess.run(
                        ["git", "rev-parse", "--verify", "HEAD"],
                        cwd=git_dir,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=10,
                    ).returncode
                    == 0
                )
                base_ref = None
                base_source = "none"
                effective_base_sha: str | None = None
                if prefer_retained_worktree and has_head:
                    base_ref, base_source = _resolve_worktree_diff_base_ref(
                        task=task,
                        metadata=task_metadata,
                        git_dir=git_dir,
                        head_ref=head_ref,
                    )
                if base_ref and has_head:
                    # Use the merge-base so the diff only shows the task's
                    # own changes, excluding work merged into the base branch
                    # after this task branched off.
                    worktree_merge_base = _merge_base_sha(git_dir, base_ref, "HEAD")
                    effective_base_sha = worktree_merge_base or _resolve_commit_sha(git_dir, base_ref)
                    effective_worktree_base = effective_base_sha or base_ref
                    stat_cmd = ["git", "diff", "--stat", effective_worktree_base]
                    diff_cmd = ["git", "diff", effective_worktree_base]
                else:
                    stat_cmd = ["git", "diff", "--stat", "HEAD"] if has_head else ["git", "diff", "--stat"]
                    diff_cmd = ["git", "diff", "HEAD"] if has_head else ["git", "diff"]
                stat_result = subprocess.run(
                    stat_cmd,
                    cwd=git_dir, capture_output=True, text=True, check=True, timeout=10,
                )
                diff_result = subprocess.run(
                    diff_cmd,
                    cwd=git_dir, capture_output=True, text=True, check=True, timeout=10,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
                raise HTTPException(status_code=500, detail=f"Failed to read working-tree changes: {exc}") from exc

            raw_status_lines = [line.rstrip() for line in status_result.stdout.splitlines() if line.strip()]
            status_entries: list[dict[str, str]] = []
            for line in raw_status_lines:
                code = line[:2].strip() or "??"
                path_fragment = line[3:].strip() if len(line) > 3 else line.strip()
                logical_paths = [path_fragment]
                if " -> " in path_fragment:
                    logical_paths = [p.strip() for p in path_fragment.split(" -> ", 1)]
                if logical_paths and all(_is_runtime_state_path(p) for p in logical_paths):
                    continue
                status_entries.append({"code": code, "path": path_fragment})
            status_lines = [f"{entry['code']} {entry['path']}".strip() for entry in status_entries]

            files = [entry for entry in _parse_stat_files(stat_result.stdout) if not _is_runtime_state_path(entry.get("path", ""))]
            if not files and status_entries:
                parsed: list[dict[str, str]] = []
                for entry in status_entries:
                    parsed.append({"path": entry["path"], "changes": entry["code"]})
                files = parsed

            diff_text = diff_result.stdout or ""

            # git diff does not include untracked files — generate diffs for
            # them separately so reviewers can see new-file content.
            untracked_diff, untracked_file_entries = _diff_untracked_files(git_dir, status_entries)
            if untracked_diff:
                diff_text = (diff_text + "\n" + untracked_diff) if diff_text else untracked_diff
            if untracked_file_entries:
                tracked_paths = {entry.get("path") for entry in files}
                for uf_entry in untracked_file_entries:
                    if uf_entry["path"] not in tracked_paths:
                        files.append(uf_entry)
            if files or status_lines:
                stat_text = stat_result.stdout or ("\n".join(status_lines) if status_lines else "")
                return {
                    "mode": "working_tree",
                    "context_source": "retained_worktree",
                    "commit": None,
                    "base_ref": base_ref,
                    "base_sha": effective_base_sha or (_resolve_commit_sha(git_dir, base_ref) if base_ref else None),
                    "head_ref": head_ref,
                    "head_sha": _resolve_commit_sha(git_dir, head_ref) if head_ref else None,
                    "base_source": base_source,
                    "confidence": "high",
                    "warnings": [],
                    "files": files,
                    "diff": diff_text,
                    "stat": stat_text,
                }

        if prefer_retained_worktree:
            commit_payload = None
            try:
                commit_payload = await get_task_diff(task_id, project_dir)
            except HTTPException as exc:
                if exc.status_code != 500:
                    raise
            if commit_payload and commit_payload.get("commit"):
                return {
                    "mode": "committed",
                    "context_source": "committed",
                    "commit": commit_payload.get("commit"),
                    "base_ref": None,
                    "base_sha": None,
                    "head_ref": None,
                    "head_sha": None,
                    "base_source": "none",
                    "confidence": "high",
                    "warnings": [],
                    "files": commit_payload.get("files") or [],
                    "diff": commit_payload.get("diff") or "",
                    "stat": commit_payload.get("stat") or "",
                }

        preserved_payload = _preserved_branch_changes(
            task_id=task.id,
            metadata=task_metadata,
            git_dir=container.project_dir,
            bus=bus,
        )
        if preserved_payload:
            return preserved_payload

        if has_task_worktree:
            return {
                "mode": "none",
                "context_source": "none",
                "commit": None,
                "base_ref": None,
                "base_sha": None,
                "head_ref": None,
                "head_sha": None,
                "base_source": "none",
                "confidence": "high",
                "warnings": [],
                "files": [],
                "diff": "",
                "stat": "",
            }

        reason = "task_context_missing"
        if worktree_candidate_present and not has_task_worktree:
            reason = "invalid_worktree_context"
        elif str(task_metadata.get("preserved_branch") or "").strip():
            reason = "preserved_branch_missing"
        return {
            "mode": "none",
            "reason": reason,
            "context_source": "none",
            "commit": None,
            "base_ref": None,
            "base_sha": None,
            "head_ref": None,
            "head_sha": None,
            "base_source": "none",
            "confidence": "low",
            "warnings": [],
            "files": [],
            "diff": "",
            "stat": "",
        }

    @router.get("/tasks/{task_id}/logs")
    async def get_task_logs(
        task_id: str,
        project_dir: Optional[str] = Query(None),
        max_chars: int = Query(12000, ge=200, le=2000000),
        stdout_offset: int = Query(0, ge=0),
        stderr_offset: int = Query(0, ge=0),
        backfill: bool = Query(False),
        stdout_read_to: int = Query(0, ge=0),
        stderr_read_to: int = Query(0, ge=0),
        step: Optional[str] = Query(None),
        run_id: Optional[str] = Query(None),
    ) -> dict[str, Any]:
        """Return active, historical, or incremental task log output.
        
        Args:
            task_id: Identifier of the task whose logs are requested.
            project_dir: Optional project directory used to resolve runtime state.
            max_chars: Maximum number of characters returned from each stream read.
            stdout_offset: Byte offset for incremental stdout reads.
            stderr_offset: Byte offset for incremental stderr reads.
            backfill: Whether to align incremental reads to full-line boundaries.
            stdout_read_to: Optional upper byte boundary for stdout reads.
            stderr_read_to: Optional upper byte boundary for stderr reads.
            step: Optional step name to load logs from historical run metadata.
            run_id: Optional run identifier used with ``step`` to select a specific
                historical step attempt.
        
        Returns:
            A payload containing log text, offsets, and progress metadata.
        
        Raises:
            HTTPException: If the task does not exist.
        """
        container, _, _ = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        metadata = task.metadata if isinstance(task.metadata, dict) else {}

        active_meta = metadata.get("active_logs") if isinstance(metadata.get("active_logs"), dict) else None
        last_meta = metadata.get("last_logs") if isinstance(metadata.get("last_logs"), dict) else None

        requested_step = str(step or "").strip()
        requested_run_id = str(run_id or "").strip()

        all_runs = container.runs.list()
        runs_by_id = {str(run.id): run for run in all_runs if str(run.id).strip()}
        task_run_ids = [str(run_id_value).strip() for run_id_value in list(task.run_ids or []) if str(run_id_value).strip()]
        if task_run_ids:
            known_run_ids = set(task_run_ids)
            for run in all_runs:
                run_id_value = str(run.id).strip()
                if not run_id_value:
                    continue
                if str(run.task_id or "").strip() != task.id:
                    continue
                if run_id_value in known_run_ids:
                    continue
                task_run_ids.append(run_id_value)
                known_run_ids.add(run_id_value)
        else:
            task_run_ids = [
                str(run.id).strip()
                for run in all_runs
                if str(run.id).strip() and str(run.task_id or "").strip() == task.id
            ]
        # Normalize run ordering using run timestamps so latest-log selection does
        # not depend on historical run_ids list direction.
        indexed_run_ids = {run_id_value: idx for idx, run_id_value in enumerate(task_run_ids)}

        def _run_sort_key(run_id_value: str) -> tuple[float, int, str]:
            run = runs_by_id.get(run_id_value)
            fallback_idx = indexed_run_ids.get(run_id_value, 10_000_000)
            if not run:
                return float("-inf"), fallback_idx, run_id_value
            started_at_raw = str(getattr(run, "started_at", "") or "").strip()
            finished_at_raw = str(getattr(run, "finished_at", "") or "").strip()
            candidate = started_at_raw or finished_at_raw
            if not candidate:
                return float("-inf"), fallback_idx, run_id_value
            normalized = candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
            try:
                parsed = datetime.fromisoformat(normalized)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                parsed_epoch = parsed.astimezone(timezone.utc).timestamp()
            except ValueError:
                parsed_epoch = float("-inf")
            return parsed_epoch, fallback_idx, run_id_value

        task_run_ids = sorted(task_run_ids, key=_run_sort_key)
        step_execution_counts: dict[str, int] = {}
        step_latest_run: dict[str, str] = {}
        available_steps: list[str] = []
        seen_steps: set[str] = set()
        # Most-recent-first list of step executions with available stdout logs.
        step_history_entries: list[dict[str, Any]] = []
        step_attempt_counts: dict[str, int] = {}
        for task_run_id in reversed(task_run_ids):
            selected_run = runs_by_id.get(task_run_id)
            if not selected_run:
                continue
            for entry in reversed(selected_run.steps or []):
                if not isinstance(entry, dict) or not entry.get("stdout_path"):
                    continue
                step_name = str(entry.get("step") or "").strip()
                if not step_name:
                    continue
                step_execution_counts[step_name] = step_execution_counts.get(step_name, 0) + 1
                if step_name not in seen_steps:
                    available_steps.append(step_name)
                    seen_steps.add(step_name)
                if step_name not in step_latest_run:
                    step_latest_run[step_name] = task_run_id
                step_attempt_counts[step_name] = step_attempt_counts.get(step_name, 0) + 1
                step_history_entries.append(
                    {
                        "step": step_name,
                        "run_id": task_run_id,
                        # Temporary recent-first ordinal; normalized below so
                        # attempt numbers are chronological (oldest=1, newest=N).
                        "attempt": step_attempt_counts[step_name],
                        "started_at": entry.get("started_at"),
                        "finished_at": entry.get("ts"),
                        "entry": entry,
                    }
                )

        # Normalize per-step attempt numbering so higher attempt means more recent.
        # `step_history_entries` is recent-first, while `step_execution_counts`
        # carries total executions for each step.
        for item in step_history_entries:
            step_name = str(item.get("step") or "").strip()
            if not step_name:
                continue
            recent_first_attempt = int(item.get("attempt") or 0)
            total_for_step = int(step_execution_counts.get(step_name) or 0)
            if recent_first_attempt > 0 and total_for_step > 0:
                item["attempt"] = total_for_step - recent_first_attempt + 1

        # When a specific step is requested, resolve logs strictly for that step.
        step_logs_meta: Optional[dict[str, Any]] = None
        selected_run_id: str | None = None
        mode = "none"
        if requested_step:
            strict_run_selection = bool(requested_run_id)
            if requested_run_id:
                direct_run = runs_by_id.get(requested_run_id)
                if direct_run:
                    for entry in reversed(direct_run.steps or []):
                        if (
                            isinstance(entry, dict)
                            and str(entry.get("step") or "").strip() == requested_step
                            and entry.get("stdout_path")
                        ):
                            step_logs_meta = entry
                            selected_run_id = requested_run_id
                            break
            for item in step_history_entries:
                if step_logs_meta:
                    break
                if item.get("step") != requested_step:
                    continue
                if requested_run_id and str(item.get("run_id") or "") != requested_run_id:
                    continue
                selected_run_id = str(item.get("run_id") or "") or None
                history_entry = item.get("entry")
                step_logs_meta = history_entry if isinstance(history_entry, dict) else None
                if step_logs_meta:
                    break

            if step_logs_meta:
                logs_meta = step_logs_meta
                mode = "history"
            elif strict_run_selection:
                logs_meta = {"step": requested_step}
            elif active_meta and str(active_meta.get("step") or "").strip() == requested_step:
                logs_meta = active_meta
                selected_run_id = str(active_meta.get("run_id") or "").strip() or None
                mode = "active"
            elif last_meta and str(last_meta.get("step") or "").strip() == requested_step:
                logs_meta = last_meta
                selected_run_id = str(last_meta.get("run_id") or "").strip() or None
                mode = "last"
            else:
                logs_meta = {"step": requested_step}
        else:
            # Keep active stream selection first for in-progress tasks, but for
            # non-scoped requests fall back to the latest historical execution
            # before considering stale `last_logs` metadata.
            latest_history = step_history_entries[0] if step_history_entries else None
            latest_history_entry = latest_history.get("entry") if isinstance(latest_history, dict) else None
            if active_meta:
                logs_meta = active_meta
                selected_run_id = str(logs_meta.get("run_id") or "").strip() or None
                mode = "active"
            elif isinstance(latest_history_entry, dict):
                logs_meta = latest_history_entry
                latest_history_map = latest_history if isinstance(latest_history, dict) else {}
                selected_run_id = str(latest_history_map.get("run_id") or "").strip() or None
                mode = "history"
            elif last_meta:
                logs_meta = last_meta
                selected_run_id = str(logs_meta.get("run_id") or "").strip() or None
                mode = "last"
            else:
                logs_meta = {}
                mode = "none"

        stdout_path = _safe_state_path(logs_meta.get("stdout_path"), container.state_root)
        stderr_path = _safe_state_path(logs_meta.get("stderr_path"), container.state_root)
        progress_path = _safe_state_path(logs_meta.get("progress_path"), container.state_root)

        progress_payload: dict[str, Any] = {}
        if progress_path and progress_path.exists():
            try:
                raw = json.loads(progress_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    progress_payload = raw
            except Exception:
                progress_payload = {}

        use_incremental = backfill or stdout_offset > 0 or stderr_offset > 0
        stdout_tail_start = 0
        stderr_tail_start = 0
        stdout_chunk_start = 0
        stderr_chunk_start = 0
        if use_incremental:
            stdout_text, new_stdout_offset, stdout_chunk_start = _read_from_offset(
                stdout_path,
                stdout_offset,
                max_chars,
                read_to=stdout_read_to,
                align_start_to_line=backfill,
            )
            stderr_text, new_stderr_offset, stderr_chunk_start = _read_from_offset(
                stderr_path,
                stderr_offset,
                max_chars,
                read_to=stderr_read_to,
                align_start_to_line=backfill,
            )
        else:
            stdout_text, stdout_tail_start = _read_tail(stdout_path, max_chars)
            stderr_text, stderr_tail_start = _read_tail(stderr_path, max_chars)
            new_stdout_offset = stdout_path.stat().st_size if stdout_path and stdout_path.exists() else 0
            new_stderr_offset = stderr_path.stat().st_size if stderr_path and stderr_path.exists() else 0
            stdout_chunk_start = stdout_tail_start
            stderr_chunk_start = stderr_tail_start

        # Include the currently active log step for live switching support.
        active_step = active_meta.get("step") if isinstance(active_meta, dict) else None
        if active_step and active_step not in seen_steps:
            available_steps.append(str(active_step))
        if active_step and str(active_step) not in step_execution_counts:
            step_execution_counts[str(active_step)] = 1

        return {
            "mode": mode,
            "task_status": task.status,
            "step": str(logs_meta.get("step") or task.current_step or ""),
            "run_id": selected_run_id,
            "current_step": str(task.current_step or ""),
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_offset": new_stdout_offset,
            "stderr_offset": new_stderr_offset,
            "stdout_chunk_start": stdout_chunk_start,
            "stderr_chunk_start": stderr_chunk_start,
            "stdout_tail_start": stdout_tail_start,
            "stderr_tail_start": stderr_tail_start,
            "started_at": logs_meta.get("started_at"),
            "finished_at": logs_meta.get("finished_at"),
            "log_id": _logs_snapshot_id(logs_meta),
            "progress": progress_payload,
            "available_steps": available_steps,
            "step_execution_counts": step_execution_counts,
            "step_latest_run": step_latest_run,
            "step_history": [
                {
                    "step": str(item.get("step") or ""),
                    "run_id": str(item.get("run_id") or ""),
                    "attempt": int(item.get("attempt") or 0),
                    "started_at": item.get("started_at"),
                    "finished_at": item.get("finished_at"),
                }
                for item in step_history_entries[:200]
            ],
        }

    @router.patch("/tasks/{task_id}")
    async def patch_task(task_id: str, body: UpdateTaskRequest, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Apply mutable field updates to an existing task.
        
        Args:
            task_id: Identifier of the task to update.
            body: Partial task update payload.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload containing the updated task.
        
        Raises:
            HTTPException: If the task is missing or if status mutation is requested.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if "step_timeout_seconds" in body.model_fields_set:
            _set_task_step_timeout(task, body.step_timeout_seconds)
        updates = body.model_dump(exclude_none=True, exclude={"step_timeout_seconds"})
        if "status" in updates:
            raise HTTPException(
                status_code=400,
                detail="Task status cannot be changed via PATCH. Use /transition, /retry, /cancel, or review actions.",
            )
        if "metadata" in updates:
            incoming_metadata = updates.get("metadata")
            if not isinstance(incoming_metadata, dict):
                raise HTTPException(status_code=400, detail="Task metadata must be an object.")
            invalid_keys = [
                str(key)
                for key in incoming_metadata.keys()
                if str(key) != "user" and not str(key).startswith("user.")
            ]
            if invalid_keys:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Only metadata.user.* keys are mutable via PATCH. "
                        f"Unsupported keys: {', '.join(sorted(invalid_keys))}"
                    ),
                )
            existing_metadata = task.metadata if isinstance(task.metadata, dict) else {}
            merged_metadata = dict(existing_metadata)
            for key in list(merged_metadata.keys()):
                if str(key) == "user" or str(key).startswith("user."):
                    merged_metadata.pop(key, None)
            for key, value in incoming_metadata.items():
                merged_metadata[str(key)] = value
            updates["metadata"] = merged_metadata
        if "project_commands" in updates and not updates["project_commands"]:
            updates["project_commands"] = None
        for key, value in updates.items():
            if key == "hitl_mode":
                value = normalize_hitl_mode(value)
            setattr(task, key, value)
        task.updated_at = now_iso()
        container.tasks.upsert(task)
        bus.emit(channel="tasks", event_type="task.updated", entity_id=task.id, payload={"status": task.status})
        return {"task": _task_payload(task, container, orchestrator)}

    @router.post("/tasks/{task_id}/transition")
    async def transition_task(task_id: str, body: TransitionRequest, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Transition a task to a valid next status.
        
        Args:
            task_id: Identifier of the task to transition.
            body: Requested target status payload.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload containing the transitioned task.
        
        Raises:
            HTTPException: If the task is missing, the transition is invalid, or blockers remain.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        target = body.status
        if task.status == "blocked" and target == "in_review":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Blocked tasks cannot transition to in_review directly. "
                    "Use /tasks/{task_id}/skip-to-precommit when eligible."
                ),
            )
        if task.status == "in_review" and target == "done":
            raise HTTPException(
                status_code=400,
                detail=(
                    "In-review tasks cannot transition to done directly. "
                    "Use /review/{task_id}/approve."
                ),
            )
        valid = VALID_TRANSITIONS.get(task.status, set())
        if target not in valid:
            raise HTTPException(status_code=400, detail=f"Invalid transition {task.status} -> {target}")
        if target == "cancelled":
            try:
                cancelled = orchestrator.cancel_task(task.id, source="api_transition")
            except ValueError as exc:
                if "Task not found" in str(exc):
                    raise HTTPException(status_code=404, detail=str(exc))
                raise HTTPException(status_code=400, detail=str(exc))
            bus.emit(
                channel="tasks",
                event_type="task.transitioned",
                entity_id=cancelled.id,
                payload={"status": cancelled.status},
            )
            return {"task": _task_payload(cancelled, container, orchestrator)}
        task.status = cast(TaskStatus, target)
        task.updated_at = now_iso()
        container.tasks.upsert(task)
        bus.emit(channel="tasks", event_type="task.transitioned", entity_id=task.id, payload={"status": task.status})
        return {"task": _task_payload(task, container, orchestrator)}

    @router.post("/tasks/{task_id}/run")
    async def run_task(task_id: str, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Trigger orchestrator execution for a queued task.
        
        Args:
            task_id: Identifier of the task to execute.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload containing the task after run dispatch.
        
        Raises:
            HTTPException: If the task cannot be run.
        """
        _, _, orchestrator = deps.ctx(project_dir)
        try:
            task = orchestrator.run_task(task_id)
        except ValueError as exc:
            if "Task not found" in str(exc):
                raise HTTPException(status_code=404, detail=str(exc))
            raise HTTPException(status_code=400, detail=str(exc))
        return {"task": _task_payload(task, orchestrator=orchestrator)}

    @router.post("/tasks/{task_id}/skip-to-precommit")
    async def skip_to_precommit(
        task_id: str,
        body: Optional[SkipToPrecommitRequest] = None,
        project_dir: Optional[str] = Query(None),
    ) -> dict[str, Any]:
        """Move an eligible blocked task directly to pre-commit review.

        Args:
            task_id: Identifier of the blocked task.
            body: Optional payload with reviewer guidance/audit note.
            project_dir: Optional project directory used to resolve runtime state.

        Returns:
            Updated task payload in pre-commit review state.

        Raises:
            HTTPException: If task lookup fails or task is not eligible.
        """
        container, _, orchestrator = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        allowed, reason_code = orchestrator.can_skip_to_precommit(task)
        if not allowed:
            raise HTTPException(
                status_code=409,
                detail=f"Task {task_id} is not eligible to skip to pre-commit ({reason_code or 'not_allowed'})",
            )
        guidance = str(body.guidance if body else "").strip()
        try:
            updated = orchestrator.skip_task_to_precommit(task_id, guidance=guidance)
        except ValueError as exc:
            message = str(exc)
            if "Task not found" in message:
                raise HTTPException(status_code=404, detail=message) from exc
            raise HTTPException(status_code=409, detail=message) from exc
        return {
            "task": _task_payload(updated, container, orchestrator),
            "message": "Task moved to pre-commit review. Approve to run commit.",
        }

    @router.post("/tasks/{task_id}/finalize-merge-conflict")
    async def finalize_merge_conflict(
        task_id: str,
        body: Optional[FinalizeMergeConflictRequest] = None,
        project_dir: Optional[str] = Query(None),
    ) -> dict[str, Any]:
        """Finalize a blocked merge-conflict task after manual Git resolution."""
        container, _, orchestrator = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        guidance = str(body.guidance if body else "").strip()
        try:
            updated = orchestrator.finalize_merge_conflict(task_id, guidance=guidance)
        except ValueError as exc:
            message = str(exc)
            if "Task not found" in message:
                raise HTTPException(status_code=404, detail=message) from exc
            raise HTTPException(status_code=409, detail=message) from exc
        return {
            "task": _task_payload(updated, container, orchestrator),
            "message": "Task finalized after manual merge verification.",
        }

    @router.post("/tasks/{task_id}/retry")
    async def retry_task(task_id: str, body: Optional[RetryTaskRequest] = None, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Retry a task after clearing error and gate state.
        
        Args:
            task_id: Identifier of the task to retry.
            body: Optional retry payload containing guidance and restart step.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload containing the retried task.
        
        Raises:
            HTTPException: If the task is missing or unresolved blockers remain.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        unresolved = _has_unresolved_blockers(container, task)
        if unresolved is not None:
            raise HTTPException(status_code=400, detail=f"Unresolved blocker: {unresolved}")
        # Capture previous error before clearing it.
        previous_error = task.error or ""
        task.retry_count += 1
        task.status = "queued"
        task.error = None
        task.pending_gate = None
        task.wait_state = None
        task.metadata = task.metadata if isinstance(task.metadata, dict) else {}
        task.metadata.pop("execution_checkpoint", None)
        task.metadata.pop("human_blocking_issues", None)
        task.metadata.pop("invalid_workdoc_path", None)
        task.metadata.pop("invalid_workdoc_error", None)
        task.metadata.pop("workdoc_sync_error_type", None)
        task.metadata.pop("workdoc_sync_mode", None)
        task.metadata.pop("workdoc_sync_step", None)
        task.metadata.pop("workdoc_sync_attempt", None)
        task.metadata.pop("recommended_action", None)
        if body and body.worker_provider:
            task.worker_provider = str(body.worker_provider).strip() or None
        guidance = (body.guidance if body else None) or ""
        ts = now_iso()
        if guidance.strip() or previous_error.strip():
            task.metadata["retry_guidance"] = {
                "ts": ts,
                "guidance": guidance.strip(),
                "previous_error": previous_error.strip(),
            }
        if guidance.strip():
            history_list: list[dict[str, Any]] = task.metadata.setdefault("human_review_actions", [])
            history_list.append({"action": "retry", "ts": ts, "guidance": guidance.strip(), "previous_error": previous_error.strip()})
        start_from = (body.start_from_step if body else None) or ""
        if start_from.strip():
            task.metadata["retry_from_step"] = start_from.strip()
        elif task.current_step:
            # Auto-default to the current step.  Pipeline template steps and
            # the special review/commit phases restart directly.  The synthetic
            # "implement_fix" step is also allowed — the executor will resume
            # at the parent verify step and re-enter the fix loop.
            valid_restart = set(task.pipeline_template or []) | {"review", "commit", "implement_fix"}
            if task.current_step in valid_restart:
                task.metadata["retry_from_step"] = task.current_step
            else:
                task.metadata.pop("retry_from_step", None)
        else:
            task.metadata.pop("retry_from_step", None)
        target_step = _effective_retry_target_step(task)
        if guidance.strip():
            set_active_human_guidance(
                task,
                source="retry",
                guidance=guidance.strip(),
                created_at=ts,
                target_step=target_step,
                fallback_step=_guidance_fallback_step(task, target_step=target_step),
            )
        else:
            clear_active_human_guidance(task, cleared_at=ts)
        task.updated_at = now_iso()
        container.tasks.upsert(task)
        bus.emit(
            channel="tasks",
            event_type="task.retry",
            entity_id=task.id,
            payload={
                "retry_count": task.retry_count,
                "start_from_step": task.metadata.get("retry_from_step"),
                "has_guidance": bool(guidance.strip()),
                "guidance": guidance.strip(),
                "previous_error_present": bool(previous_error.strip()),
            },
        )
        return {"task": _task_payload(task, container, orchestrator)}

    @router.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Cancel a task and emit a cancellation event.
        
        Args:
            task_id: Identifier of the task to cancel.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload containing the cancelled task.
        
        Raises:
            HTTPException: If the task does not exist.
        """
        container, _, orchestrator = deps.ctx(project_dir)
        try:
            task = orchestrator.cancel_task(task_id, source="api_cancel")
        except ValueError as exc:
            if "Task not found" in str(exc):
                raise HTTPException(status_code=404, detail=str(exc))
            raise HTTPException(status_code=400, detail=str(exc))
        return {"task": _task_payload(task, container, orchestrator)}

    @router.post("/tasks/{task_id}/approve-gate")
    async def approve_gate(task_id: str, body: ApproveGateRequest, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Clear a pending human gate on a task.
        
        Args:
            task_id: Identifier of the task awaiting gate approval.
            body: Gate approval payload, including optional gate name validation.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload with the updated task and cleared gate value.
        
        Raises:
            HTTPException: If the task is missing or no matching pending gate exists.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if not task.pending_gate:
            raise HTTPException(status_code=400, detail="No pending gate on this task")
        if body.gate and body.gate != task.pending_gate:
            raise HTTPException(status_code=400, detail=f"Gate mismatch: pending={task.pending_gate}, requested={body.gate}")
        action = str(getattr(body, "action", "approve") or "approve").strip().lower()
        if action not in {"approve", "request_changes"}:
            raise HTTPException(status_code=400, detail=f"Unsupported gate action: {action}")
        generation_policy = getattr(body, "generation_policy", None)
        requested_generation_policy = _generation_policy_overrides(
            generation_policy.model_dump(exclude_none=True) if generation_policy is not None else None
        )
        save_generation_defaults = bool(getattr(body, "save_generation_policy_as_default", False))

        cleared_gate = task.pending_gate
        acted_at = now_iso()
        should_resume = task.status == "in_progress"
        if not isinstance(task.metadata, dict):
            task.metadata = {}

        if action == "request_changes":
            if requested_generation_policy or save_generation_defaults:
                raise HTTPException(
                    status_code=400,
                    detail="Generation policy controls are only supported for gate approval.",
                )
            start_from_step = _request_changes_step_for_gate(task, cleared_gate)
            guidance = str(getattr(body, "guidance", "") or "").strip()
            task.retry_count += 1
            task.status = "queued"
            task.error = None
            task.pending_gate = None
            task.wait_state = None
            task.current_agent_id = None
            task.metadata.pop("execution_checkpoint", None)
            task.metadata.pop("recommended_action", None)
            if start_from_step:
                task.metadata["retry_from_step"] = start_from_step
            else:
                task.metadata.pop("retry_from_step", None)
            task.metadata["requested_changes"] = {
                "ts": acted_at,
                "guidance": guidance,
                "gate": cleared_gate,
            }
            if guidance:
                set_active_human_guidance(
                    task,
                    source="gate_request_changes",
                    guidance=guidance,
                    created_at=acted_at,
                    target_step=start_from_step,
                    fallback_step=_guidance_fallback_step(task, target_step=start_from_step),
                    gate=cleared_gate,
                )
            else:
                clear_active_human_guidance(task, cleared_at=acted_at)
            history: list[dict[str, Any]] = task.metadata.setdefault("human_review_actions", [])
            history.append(
                {
                    "action": "request_changes",
                    "ts": acted_at,
                    "guidance": guidance,
                    "gate": cleared_gate,
                }
            )
            _mark_latest_run_interrupted(
                container,
                task,
                summary=f"Changes requested at gate: {cleared_gate}",
                ts=acted_at,
            )
            task.updated_at = acted_at
            container.tasks.upsert(task)
            bus.emit(
                channel="tasks",
                event_type="task.changes_requested",
                entity_id=task.id,
                payload={"gate": cleared_gate, "guidance": guidance, "start_from_step": start_from_step},
            )
            orchestrator.ensure_worker()
            return {
                "task": _task_payload(task, container, orchestrator),
                "cleared_gate": cleared_gate,
                "message": _gate_changes_requested_message(cleared_gate, start_from_step=start_from_step),
                "approved_at": acted_at,
            }

        if (requested_generation_policy or save_generation_defaults) and cleared_gate != "before_generate_tasks":
            raise HTTPException(
                status_code=400,
                detail="Generation policy controls are only valid for the before_generate_tasks gate.",
            )

        effective_generation_policy: dict[str, Any] | None = None
        saved_generation_defaults: dict[str, Any] | None = None
        if cleared_gate == "before_generate_tasks":
            if not orchestrator.supports_task_generation(task):
                raise HTTPException(status_code=400, detail="Task pipeline does not include generate_tasks")
            effective_generation_policy = orchestrator.resolve_task_generation_policy(
                task,
                request_overrides=(requested_generation_policy or None),
            )
            if requested_generation_policy:
                orchestrator.set_pending_task_generation_override(
                    task,
                    effective_policy=effective_generation_policy,
                )
            else:
                orchestrator.set_pending_task_generation_override(task, effective_policy=None)
            if save_generation_defaults:
                saved_generation_defaults = orchestrator.persist_task_generation_defaults(
                    task,
                    effective_policy=effective_generation_policy,
                )

        # Carry review-specific choices from gate approval into task metadata
        # so that the post_comments / post_comment_responses steps pick them up.
        _VALID_REVIEW_DECISIONS = {"approve", "request_changes", "comment"}
        if cleared_gate == "before_post_review":
            review_decision = getattr(body, "review_decision", None)
            post_comments_flag = getattr(body, "post_comments", None)
            if review_decision and review_decision not in _VALID_REVIEW_DECISIONS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid review_decision '{review_decision}'. Must be one of: {', '.join(sorted(_VALID_REVIEW_DECISIONS))}",
                )
            # Fall back to LLM's proposal when user didn't choose.
            if not review_decision:
                review_decision = task.metadata.get("proposed_review_decision", "comment")
            task.metadata["review_decision"] = {
                "decision": review_decision,
                "body": task.metadata.get("review_summary", ""),
            }
            task.metadata["comment_dry_run"] = not post_comments_flag if post_comments_flag is not None else True

        if should_resume:
            checkpoint = task.metadata.get("execution_checkpoint")
            checkpoint_payload = dict(checkpoint) if isinstance(checkpoint, dict) else {}
            checkpoint_payload["gate"] = cleared_gate
            checkpoint_payload["resume_step"] = (
                str(checkpoint_payload.get("resume_step") or "").strip() or _resume_step_for_gate(cleared_gate)
            )
            checkpoint_payload["approved_gate"] = cleared_gate
            checkpoint_payload["resume_requested_at"] = acted_at
            task.metadata["execution_checkpoint"] = checkpoint_payload
        task.pending_gate = None
        task.wait_state = None
        task.updated_at = acted_at
        container.tasks.upsert(task)
        bus.emit(
            channel="tasks",
            event_type="task.gate_approved",
            entity_id=task.id,
            payload={"gate": cleared_gate, "effective_generation_policy": effective_generation_policy},
        )
        if should_resume:
            bus.emit(
                channel="tasks",
                event_type="task.resume_requested",
                entity_id=task.id,
                payload={"gate": cleared_gate, "effective_generation_policy": effective_generation_policy},
            )
            orchestrator.ensure_worker()
        return {
            "task": _task_payload(task, container, orchestrator),
            "cleared_gate": cleared_gate,
            "message": _gate_approved_message(cleared_gate, will_resume=should_resume),
            "approved_at": acted_at,
            "effective_generation_policy": effective_generation_policy,
            "saved_generation_defaults": saved_generation_defaults,
        }

    @router.post("/tasks/{task_id}/dependencies")
    async def add_dependency(task_id: str, body: AddDependencyRequest, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Link a task to another task as a blocker.
        
        Args:
            task_id: Identifier of the blocked task.
            body: Dependency payload identifying the blocker task.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload containing the updated blocked task.
        
        Raises:
            HTTPException: If either task cannot be found.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        blocker = container.tasks.get(body.depends_on)
        if not task or not blocker:
            raise HTTPException(status_code=404, detail="Task or dependency not found")
        if body.depends_on not in task.blocked_by:
            task.blocked_by.append(body.depends_on)
        if task.id not in blocker.blocks:
            blocker.blocks.append(task.id)
        container.tasks.upsert(task)
        container.tasks.upsert(blocker)
        bus.emit(channel="tasks", event_type="task.dependency_added", entity_id=task.id, payload={"depends_on": body.depends_on})
        return {"task": _task_payload(task, container, orchestrator)}

    @router.delete("/tasks/{task_id}/dependencies/{dep_id}")
    async def remove_dependency(task_id: str, dep_id: str, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Remove an existing dependency edge between two tasks.
        
        Args:
            task_id: Identifier of the task being unblocked.
            dep_id: Identifier of the blocker task to unlink.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload containing the updated task.
        
        Raises:
            HTTPException: If the task cannot be found.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        task.blocked_by = [item for item in task.blocked_by if item != dep_id]
        container.tasks.upsert(task)
        blocker = container.tasks.get(dep_id)
        if blocker:
            blocker.blocks = [item for item in blocker.blocks if item != task.id]
            container.tasks.upsert(blocker)
        bus.emit(channel="tasks", event_type="task.dependency_removed", entity_id=task.id, payload={"dep_id": dep_id})
        return {"task": _task_payload(task, container, orchestrator)}

    @router.post("/tasks/analyze-dependencies")
    async def analyze_dependencies(project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Run dependency inference and return inferred edges.
        
        Args:
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload containing inferred dependency edges across tasks.
        """
        container, _, orchestrator = deps.ctx(project_dir)
        orchestrator._maybe_analyze_dependencies()
        # Collect all inferred edges across tasks
        edges: list[dict[str, str]] = []
        for task in container.tasks.list():
            inferred = task.metadata.get("inferred_deps") if isinstance(task.metadata, dict) else None
            if isinstance(inferred, list):
                for dep in inferred:
                    if isinstance(dep, dict):
                        edges.append({"from": dep.get("from", ""), "to": task.id, "reason": dep.get("reason", "")})
        return {"edges": edges}

    @router.post("/tasks/{task_id}/reset-dep-analysis")
    async def reset_dep_analysis(task_id: str, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Remove dependency-analysis metadata from a task.
        
        Args:
            task_id: Identifier of the task whose analysis metadata is reset.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload containing the updated task.
        
        Raises:
            HTTPException: If the task cannot be found.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if isinstance(task.metadata, dict):
            # Remove inferred blocked_by entries and corresponding blocks on blockers
            inferred = task.metadata.get("inferred_deps")
            if isinstance(inferred, list):
                inferred_from_ids = {
                    dep_id
                    for dep in inferred
                    if isinstance(dep, dict)
                    for dep_id in [dep.get("from")]
                    if isinstance(dep_id, str) and dep_id
                }
                task.blocked_by = [bid for bid in task.blocked_by if bid not in inferred_from_ids]
                for blocker_id in inferred_from_ids:
                    blocker = container.tasks.get(blocker_id)
                    if blocker:
                        blocker.blocks = [bid for bid in blocker.blocks if bid != task.id]
                        container.tasks.upsert(blocker)
            task.metadata.pop("deps_analyzed", None)
            task.metadata.pop("inferred_deps", None)
        task.updated_at = now_iso()
        container.tasks.upsert(task)
        bus.emit(channel="tasks", event_type="task.dep_analysis_reset", entity_id=task.id, payload={})
        return {"task": _task_payload(task, container, orchestrator)}

    @router.get("/tasks/{task_id}/workdoc")
    async def get_task_workdoc(task_id: str, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Return the orchestrator work document for a task.
        
        Args:
            task_id: Identifier of the task whose work document is requested.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            The serialized work document payload.
        
        Raises:
            HTTPException: If the task cannot be found.
        """
        container, _, _ = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        orchestrator = deps.resolve_orchestrator(project_dir)
        try:
            return orchestrator.get_workdoc(task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/tasks/{task_id}/plan")
    async def get_task_plan(task_id: str, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Return the current plan document for a task.
        
        Args:
            task_id: Identifier of the task whose plan is requested.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            The serialized plan document payload.
        
        Raises:
            HTTPException: If the task is missing or plan retrieval fails validation.
        """
        container, _, _ = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        orchestrator = deps.resolve_orchestrator(project_dir)
        try:
            return orchestrator.get_plan_document(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/tasks/{task_id}/plan/refine")
    async def refine_task_plan(
        task_id: str,
        body: PlanRefineRequest,
        project_dir: Optional[str] = Query(None),
    ) -> dict[str, Any]:
        """Queue a plan-refinement job for a task.
        
        Args:
            task_id: Identifier of the task whose plan is being refined.
            body: Refinement request with feedback, optional instructions, and priority.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload containing the queued refinement job.
        
        Raises:
            HTTPException: If the task is missing, feedback is empty, or queueing fails.
        """
        container, _, orchestrator = deps.ctx(project_dir)
        if not container.tasks.get(task_id):
            raise HTTPException(status_code=404, detail="Task not found")
        if not str(body.feedback or "").strip():
            raise HTTPException(status_code=400, detail="feedback is required")
        try:
            job = orchestrator.queue_plan_refine_job(
                task_id=task_id,
                base_revision_id=body.base_revision_id,
                feedback=body.feedback,
                instructions=body.instructions,
                priority=body.priority,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"job": job.to_dict()}

    @router.get("/tasks/{task_id}/plan/jobs")
    async def list_plan_refine_jobs(task_id: str, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """List plan-refinement jobs associated with a task.
        
        Args:
            task_id: Identifier of the task whose refinement jobs are requested.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload containing serialized refinement jobs.
        
        Raises:
            HTTPException: If the task is missing or job lookup fails.
        """
        container, _, orchestrator = deps.ctx(project_dir)
        if not container.tasks.get(task_id):
            raise HTTPException(status_code=404, detail="Task not found")
        try:
            jobs = orchestrator.list_plan_refine_jobs(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"jobs": [job.to_dict() for job in jobs]}

    @router.get("/tasks/{task_id}/plan/jobs/{job_id}")
    async def get_plan_refine_job(task_id: str, job_id: str, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Fetch a specific plan-refinement job for a task.
        
        Args:
            task_id: Identifier of the owning task.
            job_id: Identifier of the refinement job.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload containing the requested refinement job.
        
        Raises:
            HTTPException: If the task or job cannot be found.
        """
        container, _, orchestrator = deps.ctx(project_dir)
        if not container.tasks.get(task_id):
            raise HTTPException(status_code=404, detail="Task not found")
        try:
            job = orchestrator.get_plan_refine_job(task_id, job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"job": job.to_dict()}

    @router.post("/tasks/{task_id}/plan/commit")
    async def commit_plan_revision(
        task_id: str,
        body: CommitPlanRequest,
        project_dir: Optional[str] = Query(None),
    ) -> dict[str, Any]:
        """Commit a plan revision as the task's accepted plan.
        
        Args:
            task_id: Identifier of the task whose plan is being committed.
            body: Commit payload containing the revision identifier.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload with committed and latest revision identifiers.
        
        Raises:
            HTTPException: If the task is missing or revision commit fails.
        """
        container, _, orchestrator = deps.ctx(project_dir)
        if not container.tasks.get(task_id):
            raise HTTPException(status_code=404, detail="Task not found")
        try:
            committed_revision_id = orchestrator.commit_plan_revision(task_id, body.revision_id)
            plan_doc = orchestrator.get_plan_document(task_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "committed_revision_id": committed_revision_id,
            "latest_revision_id": plan_doc.get("latest_revision_id"),
            "committed_plan_revision_id": plan_doc.get("committed_revision_id"),
        }

    @router.post("/tasks/{task_id}/plan/revisions")
    async def create_plan_revision(
        task_id: str,
        body: CreatePlanRevisionRequest,
        project_dir: Optional[str] = Query(None),
    ) -> dict[str, Any]:
        """Create a manual plan revision for a task.
        
        Args:
            task_id: Identifier of the task whose plan revision is created.
            body: Revision payload containing content and optional parent metadata.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload containing the created plan revision.
        
        Raises:
            HTTPException: If the task is missing or content is invalid.
        """
        container, _, orchestrator = deps.ctx(project_dir)
        if not container.tasks.get(task_id):
            raise HTTPException(status_code=404, detail="Task not found")
        if not str(body.content or "").strip():
            raise HTTPException(status_code=400, detail="content is required")
        try:
            revision = orchestrator.create_plan_revision(
                task_id=task_id,
                content=body.content,
                source="human_edit",
                parent_revision_id=body.parent_revision_id,
                feedback_note=body.feedback_note,
                step=None,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"revision": revision.to_dict()}

    @router.post("/tasks/{task_id}/generate-tasks")
    async def generate_tasks_from_plan(
        task_id: str, body: GenerateTasksRequest, project_dir: Optional[str] = Query(None)
    ) -> dict[str, Any]:
        """Generate child tasks from selected or override plan text.
        
        Args:
            task_id: Identifier of the parent task receiving generated children.
            body: Generation payload selecting plan source and dependency inference mode.
            project_dir: Optional project directory used to resolve runtime state.
        
        Returns:
            A payload with generated child identifiers and resolved source metadata.
        
        Raises:
            HTTPException: If the task is missing or generation inputs are inconsistent.
        """
        container, _, orchestrator = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if not orchestrator.supports_task_generation(task) and not orchestrator.supports_post_completion_generation(task):
            raise HTTPException(status_code=400, detail="Task does not support task generation")
        source = body.source
        if source is None:
            # Backward compatibility: previous API accepted only optional plan_override.
            source = "override" if str(body.plan_override or "").strip() else "latest"
        assert source is not None
        if source == "revision" and not body.revision_id:
            raise HTTPException(status_code=400, detail="revision_id is required when source=revision")
        if source == "override" and not str(body.plan_override or "").strip():
            raise HTTPException(status_code=400, detail="plan_override is required when source=override")
        if source != "revision" and body.revision_id:
            raise HTTPException(status_code=400, detail="revision_id is only valid when source=revision")
        if source != "override" and str(body.plan_override or "").strip():
            raise HTTPException(status_code=400, detail="plan_override is only valid when source=override")
        try:
            plan_text, resolved_revision_id = orchestrator.resolve_plan_text_for_generation(
                task_id=task_id,
                source=source,
                revision_id=body.revision_id,
                plan_override=body.plan_override,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        try:
            policy_overrides = _generation_policy_overrides(
                body.policy.model_dump(exclude_none=True) if body.policy is not None else None
            )
            if "infer_deps" in body.model_fields_set and "infer_deps" not in policy_overrides:
                policy_overrides["infer_deps"] = bool(body.infer_deps)
            created_ids, effective_policy = orchestrator.generate_tasks_from_plan(
                task_id,
                plan_text,
                infer_deps=(bool(body.infer_deps) if "infer_deps" in body.model_fields_set else None),
                generation_policy_overrides=(policy_overrides or None),
                save_as_default=bool(body.save_as_default),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # Re-fetch parent to get updated children_ids
        updated_task = container.tasks.get(task_id)
        children: list[dict[str, Any]] = []
        for child_id in created_ids:
            child_task = container.tasks.get(child_id)
            if child_task is not None:
                children.append(_task_payload(child_task, orchestrator=orchestrator))
        return {
            "task": _task_payload(updated_task, orchestrator=orchestrator) if updated_task else None,
            "created_task_ids": created_ids,
            "children": children,
            "source": source,
            "source_revision_id": resolved_revision_id,
            "effective_policy": effective_policy,
        }

    @router.post("/tasks/{task_id}/review-commit")
    async def review_commit(task_id: str, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Create a commit-review task from a completed task's commit.

        Extracts the commit SHA, diff, description, and plan from the source
        task and creates a new ``commit_review`` task pre-loaded with that
        context so the pipeline can analyze and fix any issues found.

        Args:
            task_id: Identifier of the completed source task to review.
            project_dir: Optional project directory for runtime state resolution.

        Returns:
            A payload containing the newly created review task.

        Raises:
            HTTPException: If the source task is missing (404), not done (400),
                or has no commit (400).
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        source_task = container.tasks.get(task_id)
        if not source_task:
            raise HTTPException(status_code=404, detail="Task not found")
        if source_task.status != "done":
            raise HTTPException(status_code=400, detail="Only completed tasks can be reviewed")

        # Idempotency: reject if a commit_review task already exists for this source.
        for existing in container.tasks.list():
            if (
                existing.task_type == "commit_review"
                and isinstance(existing.metadata, dict)
                and existing.metadata.get("source_task_id") == task_id
                and existing.status not in ("failed", "cancelled")
            ):
                raise HTTPException(status_code=409, detail=f"A commit review task already exists: {existing.id}")

        # Extract commit SHA from the latest run's steps.
        commit_sha: str | None = None
        for run_id in reversed(source_task.run_ids):
            run_record = container.runs.get(run_id)
            if not run_record:
                continue
            for step_entry in reversed(run_record.steps or []):
                if isinstance(step_entry, dict) and step_entry.get("step") == "commit" and step_entry.get("commit"):
                    commit_sha = str(step_entry["commit"]).strip()
                    break
            if commit_sha:
                break

        if not commit_sha:
            raise HTTPException(status_code=400, detail="No commit found on source task")

        # Fetch diff (truncate to ~100K characters to stay within prompt limits).
        git_dir = container.project_dir
        try:
            diff_result = subprocess.run(
                ["git", "diff", f"{commit_sha}~1..{commit_sha}"],
                cwd=git_dir, capture_output=True, text=True, check=True, timeout=15,
            )
            diff_text = _truncate_diff(diff_result.stdout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            diff_text = ""

        # Extract plan text from source task (committed revision → step outputs → empty).
        plan_text = ""
        source_meta = source_task.metadata if isinstance(source_task.metadata, dict) else {}
        for rev_key in ("committed_plan_revision_id", "latest_plan_revision_id"):
            rev_id = str(source_meta.get(rev_key) or "").strip()
            if not rev_id:
                continue
            revision = container.plan_revisions.get(rev_id)
            if revision and revision.task_id == task_id and str(revision.content or "").strip():
                plan_text = str(revision.content).strip()
                break
        if not plan_text:
            so = source_meta.get("step_outputs")
            if isinstance(so, dict):
                plan_text = str(so.get("plan") or "").strip()

        review_task = Task(
            title=f"Review: {source_task.title}",
            description=f"Review commit {commit_sha[:12]} from task {task_id}.",
            task_type="commit_review",
            status="queued",
            source="commit_review",
            metadata={
                "source_task_id": task_id,
                "source_commit_sha": commit_sha,
                "source_description": source_task.description or "",
                "source_plan": plan_text,
                "source_diff": diff_text,
            },
        )
        container.tasks.upsert(review_task)
        bus.emit(channel="tasks", event_type="task.created", entity_id=review_task.id, payload={"status": review_task.status})
        return {"task": _task_payload(review_task, orchestrator=orchestrator)}

    # ------------------------------------------------------------------
    # POST /tasks/{task_id}/review-pr  (legacy — uses pr_review pipeline)
    # ------------------------------------------------------------------

    @router.post("/tasks/{task_id}/review-pr")
    async def review_pr(task_id: str, pr_number: int = Query(...), project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Create a PR-review task from a GitHub pull request.

        Legacy endpoint that uses the original ``pr_review`` pipeline
        (review → implement → verify → review → commit), distinct from the
        mode-specific pipelines in ``POST /pull-requests/{number}/review``.

        Fetches the PR diff and metadata via the ``gh`` CLI and creates a
        ``pr_review`` task pre-loaded with that context.

        Args:
            task_id: Identifier of the source task associated with this PR.
            pr_number: GitHub pull request number.
            project_dir: Optional project directory for runtime state resolution.

        Returns:
            A payload containing the newly created review task.

        Raises:
            HTTPException: 404 if source task missing, 400 if ``gh`` CLI
                unavailable or PR fetch fails, 409 if duplicate review exists.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        source_task = container.tasks.get(task_id)
        if not source_task:
            raise HTTPException(status_code=404, detail="Task not found")

        if not shutil.which("gh"):
            raise HTTPException(status_code=400, detail="GitHub CLI (gh) is not installed. Install it from https://cli.github.com/")

        # Idempotency: reject if a pr_review task already exists for this source + PR number.
        for existing in container.tasks.list():
            if (
                existing.task_type == "pr_review"
                and isinstance(existing.metadata, dict)
                and existing.metadata.get("source_task_id") == task_id
                and existing.metadata.get("source_pr_number") == pr_number
                and existing.status not in ("failed", "cancelled")
            ):
                raise HTTPException(status_code=409, detail=f"A PR review task already exists: {existing.id}")

        git_dir = container.project_dir
        ctx = _fetch_github_pr_context(git_dir, pr_number)

        review_task = Task(
            title=f"PR Review: #{pr_number} — {ctx['title']}" if ctx["title"] else f"PR Review: #{pr_number}",
            description=f"Review pull request #{pr_number}.",
            task_type="pr_review",
            status="queued",
            source="pr_review",
            metadata={
                "source_task_id": task_id,
                "source_pr_number": pr_number,
                "source_description": ctx["body"],
                "source_diff": ctx["diff"],
                "source_stat": ctx["stat"],
                "source_url": ctx["url"],
                "source_ref": ctx["head_ref"],
                "source_base_ref": ctx["base_ref"],
            },
        )
        container.tasks.upsert(review_task)
        bus.emit(channel="tasks", event_type="task.created", entity_id=review_task.id, payload={"status": review_task.status})
        return {"task": _task_payload(review_task, orchestrator=orchestrator)}

    # ------------------------------------------------------------------
    # POST /tasks/{task_id}/review-mr  (legacy — uses mr_review pipeline)
    # ------------------------------------------------------------------

    @router.post("/tasks/{task_id}/review-mr")
    async def review_mr(task_id: str, mr_number: int = Query(...), project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Create an MR-review task from a GitLab merge request.

        Legacy endpoint that uses the original ``mr_review`` pipeline
        (review → implement → verify → review → commit), distinct from the
        mode-specific pipelines in ``POST /pull-requests/{number}/review``.

        Fetches the MR diff and metadata via the ``glab`` CLI and creates an
        ``mr_review`` task pre-loaded with that context.

        Args:
            task_id: Identifier of the source task associated with this MR.
            mr_number: GitLab merge request number.
            project_dir: Optional project directory for runtime state resolution.

        Returns:
            A payload containing the newly created review task.

        Raises:
            HTTPException: 404 if source task missing, 400 if ``glab`` CLI
                unavailable or MR fetch fails, 409 if duplicate review exists.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        source_task = container.tasks.get(task_id)
        if not source_task:
            raise HTTPException(status_code=404, detail="Task not found")

        if not shutil.which("glab"):
            raise HTTPException(status_code=400, detail="GitLab CLI (glab) is not installed. Install it from https://gitlab.com/gitlab-org/cli")

        # Idempotency: reject if an mr_review task already exists for this source + MR number.
        for existing in container.tasks.list():
            if (
                existing.task_type == "mr_review"
                and isinstance(existing.metadata, dict)
                and existing.metadata.get("source_task_id") == task_id
                and existing.metadata.get("source_mr_number") == mr_number
                and existing.status not in ("failed", "cancelled")
            ):
                raise HTTPException(status_code=409, detail=f"An MR review task already exists: {existing.id}")

        git_dir = container.project_dir
        ctx = _fetch_gitlab_mr_context(git_dir, mr_number)

        review_task = Task(
            title=f"MR Review: !{mr_number} — {ctx['title']}" if ctx["title"] else f"MR Review: !{mr_number}",
            description=f"Review merge request !{mr_number}.",
            task_type="mr_review",
            status="queued",
            source="mr_review",
            metadata={
                "source_task_id": task_id,
                "source_mr_number": mr_number,
                "source_description": ctx["body"],
                "source_diff": ctx["diff"],
                "source_stat": ctx["stat"],
                "source_url": ctx["url"],
                "source_ref": ctx["head_ref"],
                "source_base_ref": ctx["base_ref"],
                "source_project_id": ctx.get("project_id", ""),
            },
        )
        container.tasks.upsert(review_task)
        bus.emit(channel="tasks", event_type="task.created", entity_id=review_task.id, payload={"status": review_task.status})
        return {"task": _task_payload(review_task, orchestrator=orchestrator)}

    # ------------------------------------------------------------------
    # GET /pull-requests
    # ------------------------------------------------------------------

    @router.get("/pull-requests")
    async def list_pull_requests(project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """List open pull requests (GitHub) or merge requests (GitLab).

        Detects the git platform from the remote URL, fetches open PRs/MRs
        via the appropriate CLI, and annotates each with whether a review
        task already exists.

        Args:
            project_dir: Optional project directory for runtime state resolution.

        Returns:
            A dict with platform, error (if any), and items list.
        """
        container, _bus, _orchestrator = deps.ctx(project_dir)
        git_dir = container.project_dir
        platform = _detect_git_platform(git_dir)

        if platform is None:
            return {"platform": None, "error": "Could not detect GitHub or GitLab remote", "error_code": "no_remote", "items": []}

        # Check CLI availability.
        if platform == "github" and not shutil.which("gh"):
            return {"platform": platform, "error": "GitHub CLI (gh) is not installed. Install it from https://cli.github.com/", "error_code": "cli_not_installed", "items": []}
        if platform == "gitlab" and not shutil.which("glab"):
            return {"platform": platform, "error": "GitLab CLI (glab) is not installed. Install it from https://gitlab.com/gitlab-org/cli", "error_code": "cli_not_installed", "items": []}

        # Fetch open PRs/MRs.
        items: list[dict[str, Any]] = []
        try:
            if platform == "github":
                result = subprocess.run(
                    ["gh", "pr", "list", "--json", "number,title,author,headRefName,baseRefName,url", "--state", "open", "--limit", "50"],
                    cwd=git_dir, capture_output=True, text=True, check=True, timeout=30,
                )
                raw_items = json.loads(result.stdout)
                for raw in raw_items:
                    author_field = raw.get("author")
                    if isinstance(author_field, dict):
                        author = author_field.get("login", "")
                    else:
                        author = str(author_field or "")
                    items.append({
                        "number": raw.get("number"),
                        "title": raw.get("title", ""),
                        "author": author,
                        "head_ref": raw.get("headRefName", ""),
                        "base_ref": raw.get("baseRefName", ""),
                        "url": raw.get("url", ""),
                    })
            else:
                # Note: --state flag was removed in glab ~1.80; omit it since
                # `glab mr list` defaults to open MRs.
                result = subprocess.run(
                    ["glab", "mr", "list", "--output", "json"],
                    cwd=git_dir, capture_output=True, text=True, check=True, timeout=30,
                )
                raw_items = json.loads(result.stdout)
                for raw in raw_items:
                    author_field = raw.get("author")
                    if isinstance(author_field, dict):
                        author = author_field.get("username", "")
                    else:
                        author = str(author_field or "")
                    items.append({
                        "number": raw.get("iid"),
                        "title": raw.get("title", ""),
                        "author": author,
                        "head_ref": raw.get("source_branch", ""),
                        "base_ref": raw.get("target_branch", ""),
                        "url": raw.get("web_url", ""),
                    })
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
            stderr_text = ""
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
                stderr_text = exc.stderr.strip()
            auth_keywords = ("401", "403", "auth", "login", "token", "credential", "permission")
            if stderr_text and any(kw in stderr_text.lower() for kw in auth_keywords):
                error_code = "cli_not_authenticated"
                error_msg = f"CLI authentication issue: {stderr_text}"
            else:
                error_code = "cli_error"
                error_msg = f"Failed to list pull requests: {exc}"
                if stderr_text:
                    error_msg += f"\n{stderr_text}"
            return {"platform": platform, "error": error_msg, "error_code": error_code, "items": []}

        # Annotate with existing review tasks.
        task_type_key = "pr_review" if platform == "github" else "mr_review"
        meta_number_key = "source_pr_number" if platform == "github" else "source_mr_number"
        all_tasks = container.tasks.list()
        review_task_map: dict[int, str] = {}
        for t in all_tasks:
            if (
                t.task_type == task_type_key
                and isinstance(t.metadata, dict)
                and t.status not in ("failed", "cancelled")
            ):
                src_num = t.metadata.get(meta_number_key)
                if isinstance(src_num, int):
                    review_task_map[src_num] = t.id

        for item in items:
            num = item.get("number")
            existing_task_id = review_task_map.get(num) if isinstance(num, int) else None
            item["has_review_task"] = existing_task_id is not None
            item["review_task_id"] = existing_task_id

        return {"platform": platform, "error": None, "error_code": None, "items": items}

    # ------------------------------------------------------------------
    # GET /git-platform-status
    # ------------------------------------------------------------------

    @router.get("/git-platform-status")
    async def git_platform_status(project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Check GitHub/GitLab integration status: remote, CLI installed, CLI authenticated.

        Args:
            project_dir: Optional project directory for runtime state resolution.

        Returns:
            A dict with platform, remote_url, cli_installed, cli_authenticated,
            cli_name, and setup_steps.
        """
        container, _bus, _orchestrator = deps.ctx(project_dir)
        git_dir = container.project_dir

        # Step 1: Check remote.
        remote_url: str | None = None
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=git_dir, capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                remote_url = result.stdout.strip()
        except Exception:
            pass

        platform = _detect_git_platform(git_dir)
        cli_name: str | None = None
        cli_installed = False
        cli_authenticated = False

        if platform == "github":
            cli_name = "gh"
        elif platform == "gitlab":
            cli_name = "glab"

        # Step 2: Check CLI installed.
        if cli_name:
            cli_installed = shutil.which(cli_name) is not None

        # Step 3: Check CLI authenticated.
        if cli_installed and cli_name:
            try:
                auth_result = subprocess.run(
                    [cli_name, "auth", "status"],
                    cwd=git_dir, capture_output=True, text=True, timeout=10,
                )
                cli_authenticated = auth_result.returncode == 0
            except Exception:
                cli_authenticated = False

        setup_steps = [
            {"step": 1, "label": "Configure git remote", "done": remote_url is not None},
            {"step": 2, "label": f"Install {cli_name or 'CLI'}", "done": cli_installed},
            {"step": 3, "label": f"Authenticate {cli_name or 'CLI'}", "done": cli_authenticated},
        ]

        return {
            "platform": platform,
            "remote_url": remote_url,
            "cli_installed": cli_installed,
            "cli_authenticated": cli_authenticated,
            "cli_name": cli_name,
            "setup_steps": setup_steps,
        }

    # ------------------------------------------------------------------
    # POST /pull-requests/{number}/review
    # ------------------------------------------------------------------

    @router.post("/pull-requests/{number}/review")
    async def create_pull_request_review(number: int, body: CreatePullRequestReviewRequest, project_dir: Optional[str] = Query(None)) -> dict[str, Any]:
        """Create a review task for a pull request or merge request.

        Detects the git platform, fetches PR/MR context, maps the selected
        ``review_mode`` to the appropriate pipeline template, and creates a
        review task queued for execution.

        Args:
            number: Pull request or merge request number.
            body: Review mode, optional decision, and guidance.
            project_dir: Optional project directory for runtime state resolution.

        Returns:
            A payload containing the newly created review task.

        Raises:
            HTTPException: 400 if platform detection, CLI check, or unsupported
                mode for the detected platform; 409 if a review task already
                exists for this PR/MR number and mode.
        """
        container, bus, orchestrator = deps.ctx(project_dir)
        git_dir = container.project_dir
        platform = _detect_git_platform(git_dir)

        if platform is None:
            raise HTTPException(status_code=400, detail="Could not detect GitHub or GitLab remote")

        if platform == "github" and not shutil.which("gh"):
            raise HTTPException(status_code=400, detail="GitHub CLI (gh) is not installed. Install it from https://cli.github.com/")
        if platform == "gitlab" and not shutil.which("glab"):
            raise HTTPException(status_code=400, detail="GitLab CLI (glab) is not installed. Install it from https://gitlab.com/gitlab-org/cli")

        # Resolve task_type and pipeline_id from the selected review mode.
        if platform == "gitlab":
            task_type_key, pipeline_id = _REVIEW_MODE_TO_PIPELINE_GITLAB[body.review_mode]
        else:
            task_type_key, pipeline_id = _REVIEW_MODE_TO_PIPELINE_GITHUB[body.review_mode]

        meta_number_key = "source_pr_number" if platform == "github" else "source_mr_number"

        # Duplicate check using the mode-specific task_type.
        for existing in container.tasks.list():
            if (
                existing.task_type == task_type_key
                and isinstance(existing.metadata, dict)
                and existing.metadata.get(meta_number_key) == number
                and existing.status not in ("failed", "cancelled")
            ):
                raise HTTPException(status_code=409, detail=f"A review task already exists: {existing.id}")

        # Fetch context, optionally including comments.
        needs_comments = body.review_mode in _MODES_NEEDING_COMMENTS
        if platform == "github":
            ctx = _fetch_github_pr_context(git_dir, number, fetch_comments=needs_comments)
        else:
            ctx = _fetch_gitlab_mr_context(git_dir, number, fetch_comments=needs_comments)

        # Resolve pipeline template and set step names on the task.
        registry = PipelineRegistry()
        template = registry.get(pipeline_id)

        # Build metadata.
        metadata: dict[str, Any] = {
            meta_number_key: number,
            "source_description": ctx["body"],
            "source_diff": ctx["diff"],
            "source_stat": ctx["stat"],
            "source_url": ctx["url"],
            "source_ref": ctx["head_ref"],
            "source_base_ref": ctx["base_ref"],
            "review_mode": body.review_mode,
            "comment_dry_run": True,
            "final_pipeline_id": pipeline_id,
        }
        if platform == "gitlab" and ctx.get("project_id"):
            metadata["source_project_id"] = ctx["project_id"]
        if body.guidance:
            metadata["review_guidance"] = body.guidance
        if needs_comments and ctx.get("comments") is not None:
            metadata["source_comments"] = ctx["comments"]
            metadata["source_comments_formatted"] = str(ctx.get("comments_formatted") or "")

        if platform == "github":
            title = f"PR Review: #{number} — {ctx['title']}" if ctx["title"] else f"PR Review: #{number}"
            description = f"Review pull request #{number}."
        else:
            title = f"MR Review: !{number} — {ctx['title']}" if ctx["title"] else f"MR Review: !{number}"
            description = f"Review merge request !{number}."

        review_task = Task(
            title=title,
            description=description,
            task_type=task_type_key,
            status="queued",
            source=task_type_key,
            pipeline_template=template.step_names(),
            metadata=metadata,
        )
        container.tasks.upsert(review_task)
        bus.emit(channel="tasks", event_type="task.created", entity_id=review_task.id, payload={"status": review_task.status})
        return {"task": _task_payload(review_task, orchestrator=orchestrator)}

    @router.post("/tasks/{task_id}/post-review-comments", response_model=None)
    async def post_review_comments(task_id: str, project_dir: Optional[str] = Query(None)) -> dict[str, Any] | JSONResponse:
        """Publish review comments that were previously generated in dry-run mode.

        Args:
            task_id: ID of the task whose dry-run comments should be posted.
            project_dir: Optional project directory used to resolve runtime state.

        Returns:
            Summary of posted/failed counts and per-comment results.

        Raises:
            HTTPException: 404 if task not found, 409 if not a dry-run task or
                no generated comments exist.
        """
        from ...comments.models import CommentPostResult
        from ...comments.writer import (
            post_comments_batch,
            post_mr_review_decision,
            post_pr_review_decision,
        )

        container, bus, orchestrator = deps.ctx(project_dir)
        task = container.tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        meta = task.metadata if isinstance(task.metadata, dict) else {}
        if not meta.get("comment_dry_run"):
            raise HTTPException(status_code=409, detail="Task is not a dry-run review task")

        generated_comments = meta.get("generated_review_comments")
        if not isinstance(generated_comments, list) or len(generated_comments) == 0:
            raise HTTPException(status_code=409, detail="No generated review comments to post")

        platform_info = meta.get("comment_platform")
        if not isinstance(platform_info, dict) or not platform_info.get("platform"):
            raise HTTPException(status_code=409, detail="Missing comment platform info")

        git_dir = Path(orchestrator.step_project_dir(task))

        # Pre-check CLI availability so we can return a structured error_code.
        platform = str(platform_info.get("platform", ""))
        cli_name = "gh" if platform == "github" else "glab" if platform == "gitlab" else None
        if cli_name and not shutil.which(cli_name):
            return JSONResponse(
                status_code=409,
                content={
                    "detail": f"{cli_name} CLI is not installed. Install it to post review comments.",
                    "code": "cli_not_installed",
                },
            )
        if cli_name:
            try:
                auth_result = await asyncio.to_thread(
                    subprocess.run,
                    [cli_name, "auth", "status"],
                    cwd=str(git_dir),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if auth_result.returncode != 0:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "detail": f"{cli_name} CLI is not authenticated. Run '{cli_name} auth login' to authenticate.",
                            "code": "cli_not_authenticated",
                        },
                    )
            except Exception:
                pass  # If auth check itself fails, proceed and let post_comments_batch report errors.

        post_results = await asyncio.to_thread(
            post_comments_batch,
            platform_info,
            generated_comments,
            git_dir=git_dir,
        )

        results: list[dict[str, Any]] = []
        posted_count = 0
        failed_count = 0
        for r in post_results:
            results.append(r.to_dict())
            if r.success:
                posted_count += 1
            else:
                failed_count += 1

        task.metadata["posted_comments"] = results
        task.metadata["comment_dry_run"] = False

        # Post review decision if present and not yet posted.
        review_decision_raw = meta.get("review_decision")
        if isinstance(review_decision_raw, dict):
            decision_type = str(review_decision_raw.get("decision") or "comment")
            decision_body = str(review_decision_raw.get("body") or "")
            platform = str(platform_info.get("platform", ""))
            try:
                if platform == "github":
                    dr = await asyncio.to_thread(
                        post_pr_review_decision,
                        str(platform_info["owner"]),
                        str(platform_info["repo"]),
                        int(platform_info["number"]),
                        decision=decision_type,  # type: ignore[arg-type]
                        body=decision_body,
                        git_dir=git_dir,
                    )
                elif platform == "gitlab":
                    dr = await asyncio.to_thread(
                        post_mr_review_decision,
                        str(platform_info["project_id"]),
                        int(platform_info["number"]),
                        decision=decision_type,  # type: ignore[arg-type]
                        body=decision_body,
                        cwd=git_dir,
                    )
                else:
                    dr = CommentPostResult(success=False, error=f"Unsupported platform: {platform}")
                task.metadata["review_decision_result"] = dr.to_dict()
            except Exception as exc:
                task.metadata["review_decision_result"] = CommentPostResult(
                    success=False, error=str(exc),
                ).to_dict()

        container.tasks.upsert(task)
        bus.emit(
            channel="tasks",
            event_type="task.updated",
            entity_id=task.id,
            payload={"posted_count": posted_count, "failed_count": failed_count},
        )

        return {
            "posted_count": posted_count,
            "failed_count": failed_count,
            "results": results,
            "task": _task_payload(task, container, orchestrator),
        }
