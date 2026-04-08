"""Run Codex worker subprocesses and enforce heartbeat/timeout behavior."""

from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .io_utils import _heartbeat_from_progress
from .utils import _now_iso

logger = logging.getLogger(__name__)


def _stream_pipe(pipe: Any, file_path: Path, label: str, to_stderr: bool, quiet: bool = True) -> None:
    prefix = f"[codex {label}] "
    with open(file_path, "w") as handle:
        for line in iter(pipe.readline, ""):
            handle.write(line)
            handle.flush()
            if quiet:
                continue
            if to_stderr:
                sys.stderr.write(prefix + line)
                sys.stderr.flush()
            else:
                sys.stdout.write(prefix + line)
                sys.stdout.flush()
    try:
        pipe.close()
    except Exception:
        pass


def _latest_mtime(paths: list[Path]) -> Optional[datetime]:
    latest: Optional[datetime] = None
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        stamp = datetime.fromtimestamp(mtime, tz=timezone.utc)
        if latest is None or stamp > latest:
            latest = stamp
    return latest


def _has_live_children(pid: int) -> bool:
    """Check if the worker's process group has any members besides the leader.

    The worker is spawned with ``start_new_session=True`` so its PID becomes
    the process group ID.  We list all PIDs in that group via ``pgrep -g`` and
    check if any PID other than the leader exists — this catches grandchild
    processes even after intermediate shells have exited and the grandchild
    has been reparented to init.

    Falls back to ``pgrep -P`` (direct children only) on platforms where
    ``pgrep -g`` is unavailable.
    """
    try:
        # pgrep -g <pgid>: list all processes in the process group.
        result = subprocess.run(
            ["pgrep", "-g", str(pid)],
            capture_output=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            group_pids = {
                int(p) for p in result.stdout.decode().split() if p.strip().isdigit()
            }
            # Exclude the group leader (the worker itself).
            return bool(group_pids - {pid})
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # Fallback: direct-child check via pgrep -P.
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return False


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Terminate the worker and all processes in its group."""
    pgid = None
    if hasattr(os, "killpg"):
        try:
            pgid = os.getpgid(process.pid)
        except OSError:
            pgid = None

    if pgid is not None and pgid == process.pid:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
    else:
        process.terminate()

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if pgid is not None and pgid == process.pid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        process.kill()


class WorkerCancelledError(Exception):
    """Raised when a worker is cancelled mid-execution."""


def _run_codex_worker(
    command: str,
    prompt: str,
    project_dir: Path,
    run_dir: Path,
    timeout_seconds: int,
    heartbeat_seconds: int,
    heartbeat_grace_seconds: int,
    progress_path: Path,
    expected_run_id: Optional[str] = None,
    on_spawn: Optional[Callable[[int], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
    env: Optional[dict[str, str]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> dict[str, Any]:
    prompt_path = run_dir / "prompt.txt"
    prompt_path.write_text(prompt)

    start_wall = datetime.now(timezone.utc)
    try:
        formatted_command = command.format(
            prompt_file=str(prompt_path),
            project_dir=str(project_dir),
            run_dir=str(run_dir),
            prompt=prompt,
        )
    except KeyError as exc:
        raise ValueError(f"Unknown placeholder in codex command: {exc}") from exc

    command_parts = shlex.split(formatted_command)
    uses_prompt_placeholder = "{prompt_file}" in command or "{prompt}" in command
    expects_stdin = "-" in command_parts
    if not uses_prompt_placeholder and not expects_stdin:
        # Default to stdin for plain commands (e.g., "codex") so local-first
        # setups work without requiring placeholder syntax.
        expects_stdin = True

    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    start_time = time.monotonic()
    start_iso = _now_iso()
    timed_out = False
    no_heartbeat = False
    last_heartbeat = None

    process = subprocess.Popen(
        command_parts,
        cwd=project_dir,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )

    if on_spawn:
        try:
            on_spawn(process.pid)
        except Exception:
            pass

    stdout_thread = threading.Thread(
        target=_stream_pipe,
        args=(process.stdout, stdout_path, "stdout", False),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_stream_pipe,
        args=(process.stderr, stderr_path, "stderr", True),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    if not uses_prompt_placeholder and expects_stdin:
        if process.stdin:
            try:
                process.stdin.write(prompt)
                process.stdin.flush()
                process.stdin.close()
            except BrokenPipeError:
                pass

    poll_interval = max(5, min(heartbeat_seconds // 2, 30))
    heartbeat_tolerance = timedelta(seconds=poll_interval)

    while True:
        elapsed = time.monotonic() - start_time
        if timeout_seconds > 0 and elapsed > timeout_seconds:
            timed_out = True
            _terminate_process_group(process)
            break

        heartbeat = _heartbeat_from_progress(progress_path, expected_run_id)
        if heartbeat and heartbeat >= (start_wall - heartbeat_tolerance):
            last_heartbeat = heartbeat

        # Treat new stdout/stderr output as liveness even if progress heartbeats
        # are delayed or skipped by the worker.
        log_activity = _latest_mtime([stdout_path, stderr_path])

        now = datetime.now(timezone.utc)
        last_activity = start_wall
        if last_heartbeat and last_heartbeat > last_activity:
            last_activity = last_heartbeat
        if log_activity and log_activity >= (start_wall - heartbeat_tolerance) and log_activity > last_activity:
            last_activity = log_activity

        age = (now - last_activity).total_seconds()
        if age > heartbeat_grace_seconds:
            if _has_live_children(process.pid):
                logger.warning(
                    "Heartbeat grace exceeded (%.0fs) but worker PID %d has "
                    "active descendant processes — extending grace period.",
                    age,
                    process.pid,
                )
                last_activity = now
                continue
            no_heartbeat = True
            _terminate_process_group(process)
            break

        if is_cancelled and is_cancelled():
            _terminate_process_group(process)
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            raise WorkerCancelledError("Task cancelled by user")

        if cancel_event is not None:
            # Wait on the cancel event so that setting it from another thread
            # unblocks us immediately instead of sleeping for poll_interval.
            cancel_event.wait(timeout=poll_interval)
            if process.poll() is not None:
                break
        else:
            try:
                process.wait(timeout=poll_interval)
                break
            except subprocess.TimeoutExpired:
                pass

    exit_code = process.poll()
    if exit_code is None:
        exit_code = -1

    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)

    end_iso = _now_iso()
    runtime_seconds = int(time.monotonic() - start_time)

    return {
        "command": formatted_command,
        "prompt_path": str(prompt_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "start_time": start_iso,
        "end_time": end_iso,
        "runtime_seconds": runtime_seconds,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "no_heartbeat": no_heartbeat,
        "last_heartbeat": last_heartbeat.isoformat() if last_heartbeat else None,
    }
