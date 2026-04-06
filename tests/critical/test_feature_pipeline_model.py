from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from overdrive.runtime.domain.models import Task
from overdrive.runtime.orchestrator.service import OrchestratorService
from overdrive.runtime.orchestrator.worker_adapter import DefaultWorkerAdapter
from overdrive.server.api import create_app
from tests.critical.assertions import assert_task_lifecycle_invariants


def _orchestrator_from_app(app: Any) -> OrchestratorService:
    orchestrators = list(app.state.orchestrators.values())
    assert orchestrators, "expected orchestrator to be initialized for app"
    return orchestrators[0]


def _latest_task(app: Any, task_id: str) -> Task:
    svc = _orchestrator_from_app(app)
    task = svc.container.tasks.get(task_id)
    assert task is not None
    assert_task_lifecycle_invariants(task, service=svc)
    return task


def _assert_no_missing_context_error(task_payload: dict[str, Any]) -> None:
    error_text = str(task_payload.get("error") or "")
    assert "Retained task context is missing" not in error_text
    assert "Retry context missing" not in error_text


def _wait_for_task_status(app: Any, task_id: str, *, expected: str, timeout: float = 5.0) -> Task:
    deadline = time.time() + timeout
    latest: Task | None = None
    while time.time() < deadline:
        latest = _latest_task(app, task_id)
        if latest.status == expected:
            return latest
        time.sleep(0.05)
    assert latest is not None
    assert latest.status == expected, f"task {task_id} status did not reach {expected!r}; got {latest.status!r}"
    return latest


def test_feature_autopilot_happy_path_reaches_done(tmp_path) -> None:
    app = create_app(project_dir=tmp_path, worker_adapter=DefaultWorkerAdapter())
    with TestClient(app) as client:
        created = client.post(
            "/api/tasks",
            json={
                "title": "Model autopilot happy path",
                "task_type": "feature",
                "hitl_mode": "autopilot",
                "status": "queued",
                "metadata": {"scripted_files": {"feature.txt": "ok\n"}},
            },
        )
        assert created.status_code == 200
        task_id = created.json()["task"]["id"]

        run = client.post(f"/api/tasks/{task_id}/run")
        assert run.status_code == 200
        payload = run.json()["task"]
        assert payload["status"] == "done"
        _assert_no_missing_context_error(payload)
        _latest_task(app, task_id)


@pytest.mark.parametrize("request_changes_cycles", [0, 1, 2])
def test_feature_supervised_plan_gate_request_changes_bounded(tmp_path, request_changes_cycles: int) -> None:
    app = create_app(project_dir=tmp_path, worker_adapter=DefaultWorkerAdapter())
    with TestClient(app) as client:
        created = client.post(
            "/api/tasks",
            json={
                "title": "Model supervised request-changes loop",
                "task_type": "feature",
                "hitl_mode": "supervised",
                "status": "queued",
                "pipeline_template": ["plan", "implement", "verify", "review", "commit"],
                "metadata": {"scripted_files": {"loop.txt": "loop\n"}},
            },
        )
        assert created.status_code == 200
        task_id = created.json()["task"]["id"]

        for _ in range(request_changes_cycles):
            run = client.post(f"/api/tasks/{task_id}/run")
            assert run.status_code == 200
            parked = run.json()["task"]
            assert parked["status"] == "in_progress"
            assert parked["pending_gate"] == "before_implement"
            _assert_no_missing_context_error(parked)

            # Pause the scheduler *before* approve-gate so ensure_worker() does
            # not immediately pick up the re-queued task in the background.
            _orchestrator_from_app(app).control("pause")

            request_changes = client.post(
                f"/api/tasks/{task_id}/approve-gate",
                json={
                    "gate": "before_implement",
                    "action": "request_changes",
                    "guidance": "refine plan",
                },
            )
            assert request_changes.status_code == 200
            queued = request_changes.json()["task"]
            assert queued["status"] == "queued"
            _assert_no_missing_context_error(queued)
            latest = _latest_task(app, task_id)
            assert latest.status == "queued"

        final_run = client.post(f"/api/tasks/{task_id}/run")
        assert final_run.status_code == 200
        payload = final_run.json()["task"]
        assert payload["status"] == "in_progress"
        assert payload["pending_gate"] == "before_implement"
        _assert_no_missing_context_error(payload)
        _latest_task(app, task_id)


@pytest.mark.parametrize("plan_failures", [1, 2])
def test_feature_plan_block_then_retry_bounded(tmp_path, plan_failures: int) -> None:
    app = create_app(project_dir=tmp_path, worker_adapter=DefaultWorkerAdapter())
    with TestClient(app) as client:
        created = client.post(
            "/api/tasks",
            json={
                "title": "Model plan retry bounded",
                "task_type": "feature",
                "hitl_mode": "autopilot",
                "status": "queued",
                "metadata": {
                    "scripted_steps": {"plan": {"status": "error", "summary": "plan failed"}},
                    "scripted_files": {"retry.txt": "retry\n"},
                },
            },
        )
        assert created.status_code == 200
        task_id = created.json()["task"]["id"]

        for _ in range(plan_failures):
            run = client.post(f"/api/tasks/{task_id}/run")
            assert run.status_code == 200
            blocked = run.json()["task"]
            assert blocked["status"] == "blocked"
            _assert_no_missing_context_error(blocked)
            _latest_task(app, task_id)

            _orchestrator_from_app(app).control("pause")
            retry = client.post(f"/api/tasks/{task_id}/retry")
            assert retry.status_code == 200
            queued = retry.json()["task"]
            assert queued["status"] == "queued"
            _assert_no_missing_context_error(queued)
            _latest_task(app, task_id)

        svc = _orchestrator_from_app(app)
        latest = svc.container.tasks.get(task_id)
        assert latest is not None
        metadata = latest.metadata if isinstance(latest.metadata, dict) else {}
        scripted = metadata.get("scripted_steps")
        scripted_map = scripted if isinstance(scripted, dict) else {}
        scripted_map["plan"] = {"status": "ok", "summary": "plan recovered"}
        metadata["scripted_steps"] = scripted_map
        latest.metadata = metadata
        svc.container.tasks.upsert(latest)

        final_run = client.post(f"/api/tasks/{task_id}/run")
        assert final_run.status_code == 200
        done = final_run.json()["task"]
        assert done["status"] == "done"
        _assert_no_missing_context_error(done)
        _latest_task(app, task_id)


def test_feature_implement_block_then_retry_resumes(tmp_path) -> None:
    app = create_app(project_dir=tmp_path, worker_adapter=DefaultWorkerAdapter())
    with TestClient(app) as client:
        created = client.post(
            "/api/tasks",
            json={
                "title": "Model implement retry",
                "task_type": "feature",
                "hitl_mode": "autopilot",
                "status": "queued",
                "metadata": {
                    "scripted_steps": {
                        "implement": {"status": "error", "summary": "implement failed once"},
                    },
                    "scripted_files": {"impl.txt": "impl\n"},
                },
            },
        )
        assert created.status_code == 200
        task_id = created.json()["task"]["id"]

        first = client.post(f"/api/tasks/{task_id}/run")
        assert first.status_code == 200
        _assert_no_missing_context_error(first.json()["task"])
        blocked_task = _wait_for_task_status(app, task_id, expected="blocked")
        assert blocked_task.status == "blocked"

        _orchestrator_from_app(app).control("pause")
        retry = client.post(f"/api/tasks/{task_id}/retry")
        assert retry.status_code == 200
        queued = retry.json()["task"]
        assert queued["status"] == "queued"
        _assert_no_missing_context_error(queued)
        _latest_task(app, task_id)

        svc = _orchestrator_from_app(app)
        latest = svc.container.tasks.get(task_id)
        assert latest is not None
        metadata = latest.metadata if isinstance(latest.metadata, dict) else {}
        scripted = metadata.get("scripted_steps")
        scripted_map = scripted if isinstance(scripted, dict) else {}
        scripted_map["implement"] = {"status": "ok", "summary": "implement recovered"}
        metadata["scripted_steps"] = scripted_map
        latest.metadata = metadata
        svc.container.tasks.upsert(latest)

        second = client.post(f"/api/tasks/{task_id}/run")
        assert second.status_code == 200
        _assert_no_missing_context_error(second.json()["task"])
        done_task = _wait_for_task_status(app, task_id, expected="done")
        assert done_task.status == "done"
