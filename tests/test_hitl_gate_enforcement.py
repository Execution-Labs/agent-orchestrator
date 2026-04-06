"""Tests for HITL mode gate enforcement in the orchestrator."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from overdrive.runtime.domain.models import Task, now_iso
from overdrive.runtime.events import EventBus
from overdrive.runtime.orchestrator import OrchestratorService
from overdrive.runtime.storage.container import Container


def _service(tmp_path: Path) -> tuple[Container, OrchestratorService, EventBus]:
    container = Container(tmp_path)
    bus = EventBus(container.events, container.project_id)
    service = OrchestratorService(container, bus)
    return container, service, bus


def _git_init(path: Path) -> None:
    """Initialize a git repository with one commit for worktree-based tests."""
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True, text=True)
    (path / "README.md").write_text("# init\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Autopilot — no gates
# ---------------------------------------------------------------------------


def test_autopilot_no_gates(tmp_path: Path) -> None:
    """Task with hitl_mode='autopilot' runs straight to done, no gates."""
    container, service, _ = _service(tmp_path)
    task = Task(
        title="Auto task",
        status="queued",
        hitl_mode="autopilot",
    )
    container.tasks.upsert(task)

    result = service.run_task(task.id)

    assert result.status == "done"
    assert result.pending_gate is None


# ---------------------------------------------------------------------------
# Supervised — gates pause/resume flow
# ---------------------------------------------------------------------------


def test_supervised_blocks_at_gates(tmp_path: Path) -> None:
    """Supervised mode gates before implement, then pauses in pre-commit review."""
    container, service, _ = _service(tmp_path)
    task = Task(
        title="Supervised task",
        status="queued",
        hitl_mode="supervised",
    )
    container.tasks.upsert(task)

    gates_seen: list[str] = []

    def _mock_wait(t: Task, gate_name: str, timeout: int = 3600) -> bool:
        gates_seen.append(gate_name)
        # Simulate instant approval by clearing the gate
        t.pending_gate = None
        container.tasks.upsert(t)
        return True

    with patch.object(service, "_wait_for_gate", side_effect=_mock_wait):
        result = service.run_task(task.id)

    assert result.status == "in_review"
    assert "before_plan" not in gates_seen
    assert "before_implement" in gates_seen
    assert "before_commit" not in gates_seen


def test_supervised_keeps_previous_step_visible_until_gate_pause(tmp_path: Path) -> None:
    """Current step should not advance to the gated step before parking."""
    container, service, _ = _service(tmp_path)
    task = Task(
        title="Gate visibility task",
        status="queued",
        hitl_mode="supervised",
    )
    container.tasks.upsert(task)

    result = service.run_task(task.id)

    assert result.status == "in_progress"
    assert result.pending_gate == "before_implement"
    assert result.current_step == "plan"
    phase = result.metadata.get("pipeline_phase") if isinstance(result.metadata, dict) else None
    assert phase == "plan"


def test_supervised_consumes_targeted_human_guidance_once(tmp_path: Path) -> None:
    """Guidance targeted at plan is consumed after successful plan execution."""
    container, service, _ = _service(tmp_path)
    task = Task(
        title="Guided supervised task",
        status="queued",
        hitl_mode="supervised",
        metadata={
            "active_human_guidance": {
                "id": "hg-plan",
                "source": "gate_request_changes",
                "guidance": "Refine scope before implementation.",
                "created_at": now_iso(),
                "target_step": "plan",
                "fallback_step": "implement_fix",
                "consumed": False,
            }
        },
    )
    container.tasks.upsert(task)

    result = service.run_task(task.id)

    assert result.status == "in_progress"
    assert result.pending_gate == "before_implement"
    guidance = result.metadata.get("active_human_guidance") if isinstance(result.metadata, dict) else None
    assert isinstance(guidance, dict)
    assert guidance.get("consumed") is True
    assert guidance.get("consumed_step") == "plan"
    assert guidance.get("consumed_run_id")


def test_supervised_retry_from_implement_skips_plan_gate(tmp_path: Path) -> None:
    """Retrying from implement should not re-trigger before_implement approval."""
    container, service, _ = _service(tmp_path)
    task = Task(
        title="Retry from implement",
        status="queued",
        hitl_mode="supervised",
        retry_count=1,
        metadata={"retry_from_step": "implement"},
    )
    container.tasks.upsert(task)

    gates_seen: list[str] = []

    def _mock_wait(t: Task, gate_name: str, timeout: int = 3600) -> bool:
        gates_seen.append(gate_name)
        t.pending_gate = None
        container.tasks.upsert(t)
        return True

    with patch.object(service, "_wait_for_gate", side_effect=_mock_wait):
        result = service.run_task(task.id)

    assert result.status == "in_review"
    assert "before_implement" not in gates_seen
    assert "before_commit" not in gates_seen


# ---------------------------------------------------------------------------
# Review-only — no gates; pauses in pre-commit review
# ---------------------------------------------------------------------------


def test_review_only_pauses_in_precommit_review(tmp_path: Path) -> None:
    """review_only mode skips plan gate and pauses in pre-commit review."""
    container, service, _ = _service(tmp_path)
    task = Task(
        title="Review-only task",
        status="queued",
        hitl_mode="review_only",
    )
    container.tasks.upsert(task)

    gates_seen: list[str] = []

    def _mock_wait(t: Task, gate_name: str, timeout: int = 3600) -> bool:
        gates_seen.append(gate_name)
        t.pending_gate = None
        container.tasks.upsert(t)
        return True

    with patch.object(service, "_wait_for_gate", side_effect=_mock_wait):
        result = service.run_task(task.id)

    assert result.status == "in_review"
    assert "before_plan" not in gates_seen
    assert "before_implement" not in gates_seen
    assert "before_commit" not in gates_seen


def test_supervised_chore_skips_before_implement_gate(tmp_path: Path) -> None:
    """Chore pipeline starts at implement and should not gate before implement."""
    container, service, _ = _service(tmp_path)
    task = Task(
        title="Supervised chore",
        status="queued",
        hitl_mode="supervised",
        task_type="chore",
    )
    container.tasks.upsert(task)

    result = service.run_task(task.id)
    assert result.status == "in_review"
    assert result.pending_gate is None


def test_supervised_plan_only_pauses_before_generate_tasks(tmp_path: Path) -> None:
    """Plan-only pipeline should gate before generate_tasks in supervised mode."""
    container, service, _ = _service(tmp_path)
    task = Task(
        title="Supervised plan only",
        status="queued",
        hitl_mode="supervised",
        task_type="plan_only",
    )
    container.tasks.upsert(task)

    result = service.run_task(task.id)
    assert result.status == "in_progress"
    assert result.pending_gate == "before_generate_tasks"
    assert result.current_step == "initiative_plan"


def test_review_only_noncommit_pauses_before_done(tmp_path: Path) -> None:
    """Review-only non-commit pipeline should pause at before_done."""
    container, service, _ = _service(tmp_path)
    task = Task(
        title="Review-only research",
        status="queued",
        hitl_mode="review_only",
        task_type="research",
    )
    container.tasks.upsert(task)

    result = service.run_task(task.id)
    assert result.status == "in_progress"
    assert result.pending_gate == "before_done"


# ---------------------------------------------------------------------------
# Gate pause — parks the task
# ---------------------------------------------------------------------------


def test_gate_wait_parks_task(tmp_path: Path) -> None:
    """If nobody approves the gate, task remains paused at pending gate."""
    container, service, _ = _service(tmp_path)
    task = Task(
        title="Timeout task",
        status="queued",
        hitl_mode="supervised",
    )
    container.tasks.upsert(task)

    result = service.run_task(task.id)

    assert result.status == "in_progress"
    assert result.pending_gate == "before_implement"
    assert not result.error


def test_approved_gate_resumes_to_next_gate(tmp_path: Path) -> None:
    """Gate resume should continue the same run without retry semantics."""
    container, service, _ = _service(tmp_path)
    task = Task(
        title="Resume task",
        status="queued",
        hitl_mode="supervised",
    )
    container.tasks.upsert(task)

    parked = service.run_task(task.id)
    assert parked.pending_gate == "before_implement"
    assert parked.status == "in_progress"
    assert len(parked.run_ids) == 1
    first_run_id = parked.run_ids[-1]

    checkpoint = dict(parked.metadata.get("execution_checkpoint") or {})
    checkpoint["approved_gate"] = "before_implement"
    checkpoint["resume_requested_at"] = now_iso()
    parked.pending_gate = None
    parked.metadata["execution_checkpoint"] = checkpoint
    container.tasks.upsert(parked)

    result = service.run_task(task.id)

    assert result.status == "in_review"
    assert result.pending_gate is None
    assert result.run_ids == [first_run_id]

    runs = [run for run in container.runs.list() if run.task_id == task.id]
    assert len(runs) == 1
    assert runs[0].id == first_run_id

    started_events = [
        event
        for event in container.events.list_recent(limit=2000)
        if event.get("type") == "task.started" and event.get("entity_id") == task.id
    ]
    assert len(started_events) >= 2
    latest_payload = started_events[-1].get("payload") or {}
    assert latest_payload.get("is_retry") is False
    assert latest_payload.get("run_attempt") == 1
    workdoc = service.get_workdoc(task.id).get("content") or ""
    assert "## Retry Attempt 2" not in workdoc


# ---------------------------------------------------------------------------
# Gate approve API endpoint
# ---------------------------------------------------------------------------


def test_gate_approve_api(tmp_path: Path) -> None:
    """POST approve-gate clears pending_gate."""
    from fastapi.testclient import TestClient

    from overdrive.runtime.api.router import create_router

    container = Container(tmp_path)

    def resolve_container(_: Any = None) -> Container:
        return container

    bus = EventBus(container.events, container.project_id)
    service = OrchestratorService(container, bus)

    def resolve_orchestrator(_: Any = None) -> OrchestratorService:
        return service

    router = create_router(resolve_container, resolve_orchestrator, {})

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    task = Task(title="Gate test", status="in_progress", pending_gate="before_plan")
    container.tasks.upsert(task)

    resp = client.post(f"/api/tasks/{task.id}/approve-gate", json={"gate": "before_plan"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["cleared_gate"] == "before_plan"
    assert data["task"]["pending_gate"] is None
    checkpoint = data["task"]["metadata"].get("execution_checkpoint") or {}
    assert checkpoint.get("approved_gate") == "before_plan"
    assert checkpoint.get("resume_requested_at")
    assert checkpoint.get("resume_step") == "plan"
    assert "approved" in data["message"].lower()
    assert "task will resume shortly." in data["message"].lower()
    assert data["approved_at"]


def test_gate_approve_api_no_pending(tmp_path: Path) -> None:
    """400 when no pending gate on the task."""
    from fastapi.testclient import TestClient

    from overdrive.runtime.api.router import create_router

    container = Container(tmp_path)

    def resolve_container(_: Any = None) -> Container:
        return container

    bus = EventBus(container.events, container.project_id)
    service = OrchestratorService(container, bus)

    def resolve_orchestrator(_: Any = None) -> OrchestratorService:
        return service

    router = create_router(resolve_container, resolve_orchestrator, {})

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    task = Task(title="No gate task", status="in_progress", pending_gate=None)
    container.tasks.upsert(task)

    resp = client.post(f"/api/tasks/{task.id}/approve-gate", json={})
    assert resp.status_code == 400


def test_gate_approve_api_mismatch(tmp_path: Path) -> None:
    """400 when gate name doesn't match the pending gate."""
    from fastapi.testclient import TestClient

    from overdrive.runtime.api.router import create_router

    container = Container(tmp_path)

    def resolve_container(_: Any = None) -> Container:
        return container

    bus = EventBus(container.events, container.project_id)
    service = OrchestratorService(container, bus)

    def resolve_orchestrator(_: Any = None) -> OrchestratorService:
        return service

    router = create_router(resolve_container, resolve_orchestrator, {})

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    task = Task(title="Mismatch task", status="in_progress", pending_gate="before_plan")
    container.tasks.upsert(task)

    resp = client.post(f"/api/tasks/{task.id}/approve-gate", json={"gate": "before_commit"})
    assert resp.status_code == 400


def test_gate_request_changes_api_requeues_from_prior_step(tmp_path: Path) -> None:
    """Request-changes action should requeue and set a deterministic restart step."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from overdrive.runtime.api.router import create_router

    container = Container(tmp_path)

    def resolve_container(_: Any = None) -> Container:
        return container

    bus = EventBus(container.events, container.project_id)
    service = OrchestratorService(container, bus)

    def resolve_orchestrator(_: Any = None) -> OrchestratorService:
        return service

    router = create_router(resolve_container, resolve_orchestrator, {})
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    task = Task(
        title="Plan gate request changes",
        status="in_progress",
        task_type="feature",
        pending_gate="before_implement",
        pipeline_template=["plan", "implement", "verify", "review", "commit"],
        metadata={"execution_checkpoint": {"gate": "before_implement", "resume_step": "implement"}},
    )
    container.tasks.upsert(task)

    resp = client.post(
        f"/api/tasks/{task.id}/approve-gate",
        json={"gate": "before_implement", "action": "request_changes", "guidance": "Please tighten scope."},
    )
    assert resp.status_code == 200
    payload = resp.json()
    updated = payload["task"]
    assert updated["status"] == "queued"
    assert updated["pending_gate"] is None
    assert updated["metadata"].get("retry_from_step") == "plan"
    active_guidance = updated["metadata"].get("active_human_guidance") or {}
    assert active_guidance.get("source") == "gate_request_changes"
    assert active_guidance.get("target_step") == "plan"
    assert active_guidance.get("fallback_step") == "implement_fix"
    assert active_guidance.get("guidance") == "Please tighten scope."
    assert "changes requested" in str(payload.get("message") or "").lower()


def test_gate_request_changes_rerun_does_not_fail_missing_context(tmp_path: Path) -> None:
    """Request-changes at plan gate should re-run cleanly without retry-context failures."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from overdrive.runtime.api.router import create_router

    container = Container(tmp_path)

    def resolve_container(_: Any = None) -> Container:
        return container

    bus = EventBus(container.events, container.project_id)
    service = OrchestratorService(container, bus)

    def resolve_orchestrator(_: Any = None) -> OrchestratorService:
        return service

    router = create_router(resolve_container, resolve_orchestrator, {})
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    task = Task(
        title="Plan gate request changes rerun",
        status="queued",
        task_type="feature",
        hitl_mode="supervised",
        pipeline_template=["plan", "implement", "verify", "review", "commit"],
    )
    container.tasks.upsert(task)

    parked = service.run_task(task.id)
    assert parked.status == "in_progress"
    assert parked.pending_gate == "before_implement"

    # Pause the scheduler *before* approve-gate so ensure_worker() does not
    # immediately pick up the re-queued task in the background.
    service.control("pause")

    resp = client.post(
        f"/api/tasks/{task.id}/approve-gate",
        json={"gate": "before_implement", "action": "request_changes", "guidance": "Adjust plan scope."},
    )
    assert resp.status_code == 200
    queued = container.tasks.get(task.id)
    assert queued is not None
    assert queued.status == "queued"
    rerun = service.run_task(task.id)
    assert rerun.status == "in_progress"
    assert rerun.pending_gate == "before_implement"
    assert "Retained task context is missing" not in str(rerun.error or "")
    assert "Retry context missing" not in str(rerun.error or "")


def test_plan_gate_request_changes_git_rerun_preserves_active_worktree_context(tmp_path: Path) -> None:
    """Plan-gate request changes should not fail on rerun in git-backed repos."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from overdrive.runtime.api.router import create_router

    _git_init(tmp_path)
    container = Container(tmp_path)

    def resolve_container(_: Any = None) -> Container:
        return container

    bus = EventBus(container.events, container.project_id)
    service = OrchestratorService(container, bus)

    def resolve_orchestrator(_: Any = None) -> OrchestratorService:
        return service

    router = create_router(resolve_container, resolve_orchestrator, {})
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    task = Task(
        title="Plan-only gate request changes",
        status="queued",
        task_type="initiative_plan",
        hitl_mode="supervised",
        pipeline_template=["analyze", "initiative_plan", "generate_tasks"],
    )
    container.tasks.upsert(task)

    parked = service.run_task(task.id)
    assert parked.status == "in_progress"
    assert parked.pending_gate == "before_generate_tasks"
    parked_task = container.tasks.get(task.id)
    assert parked_task is not None
    assert str(parked_task.metadata.get("worktree_dir") or "").strip()
    assert isinstance(parked_task.metadata.get("task_context"), dict)

    # Pause the scheduler *before* approve-gate so ensure_worker() does not
    # immediately pick up the re-queued task in the background.
    service.control("pause")

    resp = client.post(
        f"/api/tasks/{task.id}/approve-gate",
        json={
            "gate": "before_generate_tasks",
            "action": "request_changes",
            "guidance": "Refine plan",
        },
    )
    assert resp.status_code == 200
    queued = container.tasks.get(task.id)
    assert queued is not None
    assert queued.status == "queued"
    rerun = service.run_task(task.id)
    assert rerun.status == "in_progress"
    assert rerun.pending_gate == "before_generate_tasks"
    assert "Internal error during execution" not in str(rerun.error or "")
    assert "Retained task context is missing" not in str(rerun.error or "")
    assert "Retry context missing" not in str(rerun.error or "")


def test_retry_without_guidance_clears_active_human_guidance(tmp_path: Path) -> None:
    """Retry requests with no guidance should clear stale active guidance envelope."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from overdrive.runtime.api.router import create_router

    container = Container(tmp_path)

    def resolve_container(_: Any = None) -> Container:
        return container

    bus = EventBus(container.events, container.project_id)
    service = OrchestratorService(container, bus)

    def resolve_orchestrator(_: Any = None) -> OrchestratorService:
        return service

    router = create_router(resolve_container, resolve_orchestrator, {})
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    task = Task(
        title="Retry clears stale guidance",
        status="blocked",
        error="Needs retry",
        metadata={
            "active_human_guidance": {
                "id": "hg-stale",
                "source": "retry",
                "guidance": "Old retry guidance",
                "created_at": now_iso(),
                "target_step": "implement",
                "fallback_step": "implement_fix",
                "consumed": False,
            }
        },
    )
    container.tasks.upsert(task)

    resp = client.post(f"/api/tasks/{task.id}/retry", json={})
    assert resp.status_code == 200
    updated = resp.json()["task"]
    metadata = updated.get("metadata") or {}
    assert metadata.get("active_human_guidance") in (None, {})
    assert metadata.get("active_human_guidance_cleared_at")


def test_gate_approve_human_intervention_keeps_blocked_status(tmp_path: Path) -> None:
    """Approving intervention gate should clear gate without auto-resuming blocked task."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from overdrive.runtime.api.router import create_router

    container = Container(tmp_path)

    def resolve_container(_: Any = None) -> Container:
        return container

    bus = EventBus(container.events, container.project_id)
    service = OrchestratorService(container, bus)

    def resolve_orchestrator(_: Any = None) -> OrchestratorService:
        return service

    router = create_router(resolve_container, resolve_orchestrator, {})
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    task = Task(title="Needs intervention", status="blocked", pending_gate="human_intervention")
    container.tasks.upsert(task)

    resp = client.post(f"/api/tasks/{task.id}/approve-gate", json={"gate": "human_intervention"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["task"]["status"] == "blocked"
    assert payload["task"]["pending_gate"] is None
    assert "still blocked" in payload["message"].lower()
    checkpoint = payload["task"]["metadata"].get("execution_checkpoint") or {}
    assert checkpoint.get("resume_requested_at") in (None, "")


def test_explicit_hitl_mode_preserved() -> None:
    """When hitl_mode is explicitly set in data, it is preserved."""
    data = {"title": "Explicit", "hitl_mode": "supervised"}
    task = Task.from_dict(data)
    assert task.hitl_mode == "supervised"


def test_retry_count_from_dict_invalid_value_falls_back_to_zero() -> None:
    """Invalid persisted retry_count values are normalized to zero."""
    task = Task.from_dict({"title": "Retry parse", "retry_count": "not-a-number"})
    assert task.retry_count == 0


# ---------------------------------------------------------------------------
# Modes endpoint uses modes.py
# ---------------------------------------------------------------------------


def test_modes_endpoint_uses_modes_py(tmp_path: Path) -> None:
    """GET /collaboration/modes returns data from MODE_CONFIGS."""
    from fastapi.testclient import TestClient

    from overdrive.collaboration.modes import MODE_CONFIGS
    from overdrive.runtime.api.router import create_router

    container = Container(tmp_path)

    def resolve_container(_: Any = None) -> Container:
        return container

    bus = EventBus(container.events, container.project_id)
    service = OrchestratorService(container, bus)

    def resolve_orchestrator(_: Any = None) -> OrchestratorService:
        return service

    router = create_router(resolve_container, resolve_orchestrator, {})

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.get("/api/collaboration/modes")
    assert resp.status_code == 200
    data = resp.json()
    modes = data["modes"]
    assert len(modes) == len(MODE_CONFIGS)
    mode_names = [m["mode"] for m in modes]
    assert "autopilot" in mode_names
    assert "supervised" in mode_names
    assert "review_only" in mode_names

    # Verify descriptions come from modes.py, not hardcoded
    supervised = next(m for m in modes if m["mode"] == "supervised")
    assert supervised["description"] == MODE_CONFIGS["supervised"].description
