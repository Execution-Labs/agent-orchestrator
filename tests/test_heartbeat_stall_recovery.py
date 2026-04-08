"""Tests for heartbeat stall auto-recovery (Options A, B, C)."""

from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from overdrive.runtime.domain.models import RunRecord, Task
from overdrive.runtime.orchestrator.live_worker_adapter import (
    LiveWorkerAdapter,
    _DEFAULT_HEARTBEAT_GRACE_SECONDS,
    _HEARTBEAT_STALL_RETRY_GRACE_MULTIPLIER,
)
from overdrive.runtime.events.bus import EventBus
from overdrive.runtime.orchestrator.reconciler import OrchestratorReconciler
from overdrive.runtime.orchestrator.service import OrchestratorService
from overdrive.runtime.orchestrator.worker_adapter import DefaultWorkerAdapter
from overdrive.runtime.storage.container import Container
from overdrive.runtime.storage.task_helpers import is_retry_backoff_elapsed
from overdrive.worker import _has_live_children


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(
    *,
    task_id: str = "task-hb-test",
    status: str = "in_progress",
    metadata: dict | None = None,
) -> Task:
    return Task(
        id=task_id,
        title="heartbeat stall test",
        description="test",
        status=status,
        priority="P2",
        metadata=metadata if metadata is not None else {},
    )


def _make_run(task_id: str = "task-hb-test") -> RunRecord:
    return RunRecord(
        id="run-hb-1",
        task_id=task_id,
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
    )


# ===========================================================================
# Option C: _has_live_children
# ===========================================================================


class TestHasLiveChildren:
    def test_returns_true_when_children_found(self) -> None:
        mock_result = subprocess.CompletedProcess(
            args=["pgrep", "-P", "123"],
            returncode=0,
            stdout=b"456\n789\n",
            stderr=b"",
        )
        with patch("overdrive.worker.subprocess.run", return_value=mock_result):
            assert _has_live_children(123) is True

    def test_returns_false_when_no_children(self) -> None:
        mock_result = subprocess.CompletedProcess(
            args=["pgrep", "-P", "123"],
            returncode=1,
            stdout=b"",
            stderr=b"",
        )
        with patch("overdrive.worker.subprocess.run", return_value=mock_result):
            assert _has_live_children(123) is False

    def test_returns_false_when_pgrep_unavailable(self) -> None:
        with patch(
            "overdrive.worker.subprocess.run",
            side_effect=FileNotFoundError("pgrep not found"),
        ):
            assert _has_live_children(123) is False

    def test_returns_false_on_timeout(self) -> None:
        with patch(
            "overdrive.worker.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pgrep", timeout=2),
        ):
            assert _has_live_children(123) is False

    def test_returns_false_on_os_error(self) -> None:
        with patch(
            "overdrive.worker.subprocess.run",
            side_effect=OSError("something went wrong"),
        ):
            assert _has_live_children(123) is False

    def test_returns_false_when_stdout_empty(self) -> None:
        mock_result = subprocess.CompletedProcess(
            args=["pgrep", "-P", "123"],
            returncode=0,
            stdout=b"",
            stderr=b"",
        )
        with patch("overdrive.worker.subprocess.run", return_value=mock_result):
            assert _has_live_children(123) is False


# ===========================================================================
# Option C: Heartbeat loop integration
# ===========================================================================


class TestHeartbeatLoopChildCheck:
    def test_grace_extended_when_children_alive(self, tmp_path: Path) -> None:
        """When grace is exceeded but children are alive, worker is NOT killed."""
        import json
        import sys

        from overdrive.utils import _now_iso
        from overdrive.worker import _run_codex_worker

        project_dir = tmp_path / "repo"
        project_dir.mkdir()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        progress_path = run_dir / "progress.json"
        progress_path.write_text(json.dumps({"run_id": "run-1", "heartbeat": _now_iso()}))

        # Script that sleeps silently for 4s then exits — normally this would
        # trigger a stall at heartbeat_grace_seconds=2, but _has_live_children
        # returning True should extend the grace.
        command = (
            f"{sys.executable} -c "
            "\"import sys,time,pathlib,subprocess; "
            "pathlib.Path(sys.argv[1]).read_text(); "
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(3)']); "
            "time.sleep(4); "
            "p.wait(); "
            "print('done',flush=True)\" "
            "{prompt_file}"
        )

        result = _run_codex_worker(
            command=command,
            prompt="hello",
            project_dir=project_dir,
            run_dir=run_dir,
            timeout_seconds=30,
            heartbeat_seconds=5,
            heartbeat_grace_seconds=2,
            progress_path=progress_path,
            expected_run_id="run-1",
        )

        assert result["exit_code"] == 0
        assert result["no_heartbeat"] is False

    def test_stall_detected_when_no_children(self, tmp_path: Path) -> None:
        """When grace is exceeded and no children, worker IS killed."""
        import json
        import sys

        from overdrive.utils import _now_iso
        from overdrive.worker import _run_codex_worker

        project_dir = tmp_path / "repo"
        project_dir.mkdir()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        progress_path = run_dir / "progress.json"
        progress_path.write_text(json.dumps({"run_id": "run-1", "heartbeat": _now_iso()}))

        # Script that sleeps silently — no child processes
        command = (
            f"{sys.executable} -c "
            "\"import sys,time,pathlib; "
            "pathlib.Path(sys.argv[1]).read_text(); "
            "time.sleep(30)\" "
            "{prompt_file}"
        )

        result = _run_codex_worker(
            command=command,
            prompt="hello",
            project_dir=project_dir,
            run_dir=run_dir,
            timeout_seconds=30,
            heartbeat_seconds=5,
            heartbeat_grace_seconds=2,
            progress_path=progress_path,
            expected_run_id="run-1",
        )

        assert result["no_heartbeat"] is True


# ===========================================================================
# Option B: Per-step heartbeat grace
# ===========================================================================


class TestPerStepHeartbeatGrace:
    def test_implement_gets_300s_default(self) -> None:
        _, grace = LiveWorkerAdapter._heartbeat_settings({}, step="implement")
        assert grace == 300

    def test_implement_fix_gets_300s_default(self) -> None:
        _, grace = LiveWorkerAdapter._heartbeat_settings({}, step="implement_fix")
        assert grace == 300

    def test_plan_gets_global_default(self) -> None:
        _, grace = LiveWorkerAdapter._heartbeat_settings({}, step="plan")
        assert grace == _DEFAULT_HEARTBEAT_GRACE_SECONDS

    def test_verify_gets_extended_grace(self) -> None:
        _, grace = LiveWorkerAdapter._heartbeat_settings({}, step="verify")
        assert grace == 900

    def test_no_step_gets_global_default(self) -> None:
        _, grace = LiveWorkerAdapter._heartbeat_settings({})
        assert grace == _DEFAULT_HEARTBEAT_GRACE_SECONDS

    def test_config_override_per_step(self) -> None:
        cfg = {"workers": {"heartbeat_grace_by_step": {"implement": 900}}}
        _, grace = LiveWorkerAdapter._heartbeat_settings(cfg, step="implement")
        assert grace == 900

    def test_global_config_applies_to_non_override_step(self) -> None:
        cfg = {"workers": {"heartbeat_grace_seconds": 360}}
        _, grace = LiveWorkerAdapter._heartbeat_settings(cfg, step="plan")
        assert grace == 360

    def test_global_config_overrides_builtin_per_step(self) -> None:
        cfg = {"workers": {"heartbeat_grace_seconds": 360}}
        _, grace = LiveWorkerAdapter._heartbeat_settings(cfg, step="implement")
        # User global config (360) takes precedence over built-in per-step default (300)
        assert grace == 360

    def test_stall_retry_multiplier(self) -> None:
        _, grace = LiveWorkerAdapter._heartbeat_settings(
            {}, step="implement", is_heartbeat_stall_retry=True,
        )
        assert grace == int(300 * _HEARTBEAT_STALL_RETRY_GRACE_MULTIPLIER)

    def test_stall_retry_multiplier_on_global_default(self) -> None:
        _, grace = LiveWorkerAdapter._heartbeat_settings(
            {}, step="plan", is_heartbeat_stall_retry=True,
        )
        assert grace == int(_DEFAULT_HEARTBEAT_GRACE_SECONDS * _HEARTBEAT_STALL_RETRY_GRACE_MULTIPLIER)

    def test_grace_at_least_heartbeat_seconds(self) -> None:
        cfg = {"workers": {"heartbeat_seconds": 1000, "heartbeat_grace_by_step": {"implement": 50}}}
        hb, grace = LiveWorkerAdapter._heartbeat_settings(cfg, step="implement")
        assert grace >= hb


# ===========================================================================
# Option A: Heartbeat stall recovery in OrchestratorService
# ===========================================================================


class TestHeartbeatStallRecovery:
    @pytest.fixture()
    def service(self, tmp_path: Path) -> MagicMock:
        """Create a minimal mock OrchestratorService with the real methods patched in."""
        svc = MagicMock(spec=OrchestratorService)
        # Bind the real methods so we can test them
        svc._is_heartbeat_stall = OrchestratorService._is_heartbeat_stall.__get__(svc)
        svc._handle_recoverable_heartbeat_stall = OrchestratorService._handle_recoverable_heartbeat_stall.__get__(svc)
        svc._heartbeat_stall_max_retries = OrchestratorService._heartbeat_stall_max_retries.__get__(svc)
        svc._clear_heartbeat_stall_recovery_tracking = OrchestratorService._clear_heartbeat_stall_recovery_tracking.__get__(svc)
        svc._WAIT_KIND_AUTO_RECOVERY = OrchestratorService._WAIT_KIND_AUTO_RECOVERY
        svc._HEARTBEAT_STALL_SUMMARY_PATTERN = OrchestratorService._HEARTBEAT_STALL_SUMMARY_PATTERN
        svc._HEARTBEAT_STALL_MAX_RETRIES = OrchestratorService._HEARTBEAT_STALL_MAX_RETRIES
        svc._HEARTBEAT_STALL_RETRY_BASE_SECONDS = OrchestratorService._HEARTBEAT_STALL_RETRY_BASE_SECONDS
        svc._HEARTBEAT_STALL_RETRY_MAX_SECONDS = OrchestratorService._HEARTBEAT_STALL_RETRY_MAX_SECONDS

        # Mock container and bus
        mock_config = MagicMock()
        mock_config.load.return_value = {}
        svc.container = MagicMock()
        svc.container.config = mock_config
        svc.bus = MagicMock()

        return svc

    def test_is_heartbeat_stall_matches(self, service: MagicMock) -> None:
        assert service._is_heartbeat_stall("Worker stalled (no heartbeat or output activity).") is True

    def test_is_heartbeat_stall_case_insensitive(self, service: MagicMock) -> None:
        assert service._is_heartbeat_stall("WORKER STALLED something") is True

    def test_is_heartbeat_stall_no_match(self, service: MagicMock) -> None:
        assert service._is_heartbeat_stall("Worker timed out") is False

    def test_is_heartbeat_stall_none(self, service: MagicMock) -> None:
        assert service._is_heartbeat_stall(None) is False

    def test_first_stall_triggers_retry(self, service: MagicMock) -> None:
        task = _make_task()
        run = _make_run()

        result = service._handle_recoverable_heartbeat_stall(
            task, run, step="implement", summary="Worker stalled (no heartbeat or output activity).",
        )

        assert result is True
        assert task.status == "queued"
        assert task.metadata["heartbeat_stall_recovery_attempts_by_step"]["implement"] == 1
        assert task.metadata["heartbeat_stall_auto_requeue_pending"] is True
        assert "heartbeat_stall_next_retry_at" in task.metadata
        service.bus.emit.assert_any_call(
            channel="tasks",
            event_type="task.auto_recovering",
            entity_id=task.id,
            payload=pytest.approx(
                {
                    "step": "implement",
                    "recovery_type": "heartbeat_stall",
                    "attempt": 1,
                    "max_retries": 2,
                    "backoff_seconds": 30,
                    "next_retry_at": task.metadata["heartbeat_stall_next_retry_at"],
                    "error": task.error,
                },
                abs=1,
            ),
        )

    def test_retry_limit_exhaustion(self, service: MagicMock) -> None:
        task = _make_task(metadata={
            "heartbeat_stall_recovery_attempts_by_step": {"implement": 2},
        })
        run = _make_run()

        result = service._handle_recoverable_heartbeat_stall(
            task, run, step="implement", summary="Worker stalled (no heartbeat or output activity).",
        )

        assert result is True
        # Tracking should be cleaned up
        assert "heartbeat_stall_recovery_attempts_by_step" not in task.metadata
        # Should escalate via _block_for_human_issues
        service._block_for_human_issues.assert_called_once()
        call_args = service._block_for_human_issues.call_args
        assert call_args[0][0] is task
        assert call_args[0][1] is run
        assert call_args[0][2] == "implement"

    def test_second_attempt_backoff(self, service: MagicMock) -> None:
        task = _make_task(metadata={
            "heartbeat_stall_recovery_attempts_by_step": {"implement": 1},
        })
        run = _make_run()

        result = service._handle_recoverable_heartbeat_stall(
            task, run, step="implement", summary="Worker stalled (no heartbeat or output activity).",
        )

        assert result is True
        assert task.metadata["heartbeat_stall_recovery_attempts_by_step"]["implement"] == 2
        assert task.metadata["heartbeat_stall_recovery_backoff_seconds"] == 60  # 30 * 2^1

    def test_non_stall_error_not_handled(self, service: MagicMock) -> None:
        task = _make_task()
        run = _make_run()

        result = service._handle_recoverable_heartbeat_stall(
            task, run, step="implement", summary="Worker exited with code 1",
        )

        assert result is False

    def test_disabled_via_config(self, service: MagicMock) -> None:
        service.container.config.load.return_value = {
            "workers": {"environment": {"max_heartbeat_stall_retries": 0}},
        }
        task = _make_task()
        run = _make_run()

        result = service._handle_recoverable_heartbeat_stall(
            task, run, step="implement", summary="Worker stalled (no heartbeat or output activity).",
        )

        assert result is False

    def test_cleanup_clears_metadata(self, service: MagicMock) -> None:
        task = _make_task(metadata={
            "heartbeat_stall_auto_requeue_pending": True,
            "heartbeat_stall_next_retry_at": "2026-03-14T00:00:00+00:00",
            "heartbeat_stall_recovery_backoff_seconds": 30,
            "heartbeat_stall_recovery_attempts_by_step": {"implement": 1},
        })
        task.wait_state = {
            "kind": "auto_recovery_wait",
            "step": "implement",
            "reason_code": "heartbeat_stall",
        }

        service._clear_heartbeat_stall_recovery_tracking(task, step="implement")

        assert "heartbeat_stall_auto_requeue_pending" not in task.metadata
        assert "heartbeat_stall_next_retry_at" not in task.metadata
        assert "heartbeat_stall_recovery_backoff_seconds" not in task.metadata
        assert "heartbeat_stall_recovery_attempts_by_step" not in task.metadata
        service._clear_wait_state.assert_called_once_with(task)

    def test_cleanup_preserves_other_step_attempts(self, service: MagicMock) -> None:
        task = _make_task(metadata={
            "heartbeat_stall_recovery_attempts_by_step": {"implement": 1, "verify": 1},
        })

        service._clear_heartbeat_stall_recovery_tracking(task, step="implement")

        assert task.metadata["heartbeat_stall_recovery_attempts_by_step"] == {"verify": 1}

    def test_worktree_preserved_after_requeue(self, service: MagicMock) -> None:
        task = _make_task(metadata={
            "worktree_dir": "/tmp/worktree-123",
            "preserved_branch": "task-branch-123",
        })
        run = _make_run()

        service._handle_recoverable_heartbeat_stall(
            task, run, step="implement", summary="Worker stalled (no heartbeat or output activity).",
        )

        assert task.metadata["worktree_dir"] == "/tmp/worktree-123"
        assert task.metadata["preserved_branch"] == "task-branch-123"


# ===========================================================================
# Backoff enforcement in task_helpers
# ===========================================================================


class TestBackoffElapsed:
    def test_environment_backoff_respected(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        task = _make_task(status="queued", metadata={"environment_next_retry_at": future})
        assert is_retry_backoff_elapsed(task) is False

    def test_heartbeat_stall_backoff_respected(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        task = _make_task(status="queued", metadata={"heartbeat_stall_next_retry_at": future})
        assert is_retry_backoff_elapsed(task) is False

    def test_heartbeat_stall_backoff_elapsed(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        task = _make_task(status="queued", metadata={"heartbeat_stall_next_retry_at": past})
        assert is_retry_backoff_elapsed(task) is True

    def test_no_backoff_keys(self) -> None:
        task = _make_task(status="queued", metadata={})
        assert is_retry_backoff_elapsed(task) is True

    def test_coexistence_uses_latest_timestamp(self) -> None:
        """When both backoff keys are set, the latest (most restrictive) wins."""
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        task = _make_task(status="queued", metadata={
            "environment_next_retry_at": future,
            "heartbeat_stall_next_retry_at": past,
        })
        # Environment backoff is in the future → not elapsed
        assert is_retry_backoff_elapsed(task) is False

    def test_coexistence_heartbeat_future_blocks(self) -> None:
        """When heartbeat stall backoff is later than environment, it blocks."""
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        task = _make_task(status="queued", metadata={
            "environment_next_retry_at": past,
            "heartbeat_stall_next_retry_at": future,
        })
        # Heartbeat stall backoff is in the future → not elapsed
        assert is_retry_backoff_elapsed(task) is False


# ===========================================================================
# Reconciler stale-future detection
# ===========================================================================


class TestReconcilerStaleFuture:
    """Tests for OrchestratorReconciler stale-future safety net."""

    def _make_service(self, tmp_path: Path) -> OrchestratorService:
        container = Container(tmp_path)
        bus = EventBus(container.events, container.project_id)
        return OrchestratorService(container, bus, worker_adapter=DefaultWorkerAdapter())

    def test_stale_future_removed_from_active_ids(self, tmp_path: Path) -> None:
        """Reconciler detects stale log output and requeues the task."""
        svc = self._make_service(tmp_path)

        # Create stale log files (modified long ago)
        log_dir = tmp_path / "stale_run"
        log_dir.mkdir()
        stdout_log = log_dir / "stdout.log"
        stderr_log = log_dir / "stderr.log"
        stdout_log.write_text("some output")
        stderr_log.write_text("")
        # Set mtime to 20 minutes ago
        old_time = time.time() - 1200
        os.utime(stdout_log, (old_time, old_time))
        os.utime(stderr_log, (old_time, old_time))

        task = Task(
            title="stale future test",
            status="in_progress",
            metadata={
                "active_logs": {
                    "stdout_path": str(stdout_log),
                    "stderr_path": str(stderr_log),
                    "started_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
                },
            },
        )
        svc.container.tasks.upsert(task)
        # No execution lease → lease is expired

        reconciler = OrchestratorReconciler(svc)
        stale = reconciler._detect_stale_futures({task.id})
        assert task.id in stale

    def test_active_lease_prevents_stale_detection(self, tmp_path: Path) -> None:
        """Task with active lease is not considered stale even if logs are old."""
        svc = self._make_service(tmp_path)

        log_dir = tmp_path / "leased_run"
        log_dir.mkdir()
        stdout_log = log_dir / "stdout.log"
        stderr_log = log_dir / "stderr.log"
        stdout_log.write_text("output")
        stderr_log.write_text("")
        old_time = time.time() - 1200
        os.utime(stdout_log, (old_time, old_time))
        os.utime(stderr_log, (old_time, old_time))

        task = Task(
            title="leased task test",
            status="in_progress",
            metadata={
                "active_logs": {
                    "stdout_path": str(stdout_log),
                    "stderr_path": str(stderr_log),
                    "started_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
                },
            },
        )
        svc.container.tasks.upsert(task)
        # Acquire a live lease
        svc._acquire_execution_lease(task)

        reconciler = OrchestratorReconciler(svc)
        stale = reconciler._detect_stale_futures({task.id})
        assert task.id not in stale

    def test_no_active_logs_skipped(self, tmp_path: Path) -> None:
        """Task without active_logs metadata is not checked for staleness."""
        svc = self._make_service(tmp_path)

        task = Task(
            title="no logs test",
            status="in_progress",
            metadata={},
        )
        svc.container.tasks.upsert(task)

        reconciler = OrchestratorReconciler(svc)
        stale = reconciler._detect_stale_futures({task.id})
        assert task.id not in stale

    def test_recent_output_not_stale(self, tmp_path: Path) -> None:
        """Task with recent log output is not considered stale."""
        svc = self._make_service(tmp_path)

        log_dir = tmp_path / "fresh_run"
        log_dir.mkdir()
        stdout_log = log_dir / "stdout.log"
        stderr_log = log_dir / "stderr.log"
        stdout_log.write_text("fresh output")
        stderr_log.write_text("")
        # mtime is now (just created) → not stale

        task = Task(
            title="fresh output test",
            status="in_progress",
            metadata={
                "active_logs": {
                    "stdout_path": str(stdout_log),
                    "stderr_path": str(stderr_log),
                    "started_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                },
            },
        )
        svc.container.tasks.upsert(task)

        reconciler = OrchestratorReconciler(svc)
        stale = reconciler._detect_stale_futures({task.id})
        assert task.id not in stale

    def test_signal_cancel_called_on_stale(self, tmp_path: Path) -> None:
        """Worker adapter signal_cancel is called for stale futures."""
        svc = self._make_service(tmp_path)

        reconciler = OrchestratorReconciler(svc)

        mock_adapter = MagicMock()
        svc.worker_adapter = mock_adapter
        reconciler._cancel_stale_future("task-123")
        mock_adapter.signal_cancel.assert_called_once_with("task-123")

    def test_stale_future_causes_requeue_via_invariants(self, tmp_path: Path) -> None:
        """End-to-end: stale future → invariants requeue task to queued."""
        svc = self._make_service(tmp_path)

        log_dir = tmp_path / "e2e_run"
        log_dir.mkdir()
        stdout_log = log_dir / "stdout.log"
        stderr_log = log_dir / "stderr.log"
        stdout_log.write_text("old output")
        stderr_log.write_text("")
        old_time = time.time() - 1200
        os.utime(stdout_log, (old_time, old_time))
        os.utime(stderr_log, (old_time, old_time))

        task = Task(
            title="e2e stale requeue",
            status="in_progress",
            metadata={
                "active_logs": {
                    "stdout_path": str(stdout_log),
                    "stderr_path": str(stderr_log),
                    "started_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
                },
            },
        )
        svc.container.tasks.upsert(task)

        fake_future: Future[None] = Future()
        with svc._futures_lock:
            svc._futures[task.id] = fake_future

        reconciler = OrchestratorReconciler(svc)
        result = reconciler.run_once(source="manual")

        refreshed = svc.container.tasks.get(task.id)
        assert refreshed is not None
        assert refreshed.status == "queued"
        assert "stale" in (refreshed.error or "").lower() or "recovered" in (refreshed.error or "").lower()
        repairs = [r for r in result["items"] if r.get("code") == "stale_in_progress"]
        assert len(repairs) == 1

    def test_config_override_threshold(self, tmp_path: Path) -> None:
        """Custom stale_future_threshold_seconds is respected."""
        svc = self._make_service(tmp_path)
        cfg = svc.container.config.load()
        cfg["orchestrator"] = {"stale_future_threshold_seconds": 120}
        svc.container.config.save(cfg)

        reconciler = OrchestratorReconciler(svc)
        assert reconciler._stale_future_threshold_seconds() == 120
