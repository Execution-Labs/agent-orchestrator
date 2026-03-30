"""Task execution loop helpers for orchestrator tasks."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ...collaboration.modes import normalize_hitl_mode
from ...comments.formatter import format_comments_for_prompt
from ...comments.models import CommentPostResult
from ...comments.reader import CommentFetchError, fetch_mr_comments, fetch_pr_comments
from ...comments.writer import (
    parse_source_url,
    post_comments_batch,
    post_mr_review_decision,
    post_pr_review_decision,
)
from ...pipelines.registry import PipelineRegistry
from ...worker import WorkerCancelledError
from ..domain.models import RunRecord, ReviewCycle, ReviewFinding, Task, now_iso
from .human_guidance import consume_human_guidance_for_step, promote_legacy_human_guidance
from .live_worker_adapter import _VERIFY_STEPS
from .worker_adapter import StepResult

if TYPE_CHECKING:
    from .service import OrchestratorService

logger = logging.getLogger(__name__)

# Step names handled directly by the executor (not dispatched to workers).
_ORCHESTRATOR_COMMENT_STEPS: set[str] = {"fetch_comments", "post_comments", "post_comment_responses"}


def _step_output_parse_error(step_name: str, exc: Exception, raw_output: str) -> str:
    """Format a parse error with a preview of the raw output that failed."""
    preview = repr(raw_output[:300]) if raw_output else "(empty)"
    return f"Cannot parse {step_name} output: {exc}\n\nReceived: {preview}"


def _prepare_post_review_gate_context(task: Task) -> None:
    """Extract LLM review output into metadata for the before_post_review gate UI."""
    if not isinstance(task.metadata, dict):
        return
    step_outputs: dict[str, str] = task.metadata.get("step_outputs", {})

    # Determine which review step produced the output we care about.
    review_output_raw: str | None = None
    for key in ("pr_review_comment", "pr_review_fix_respond"):
        if key in step_outputs:
            review_output_raw = step_outputs[key]
            break
    if not review_output_raw:
        return

    try:
        parsed = json.loads(review_output_raw)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(parsed, dict):
        return

    if "proposed_decision" in parsed:
        task.metadata["proposed_review_decision"] = parsed["proposed_decision"]
    if "summary" in parsed:
        task.metadata["review_summary"] = parsed["summary"]
    if "comments" in parsed and isinstance(parsed["comments"], list):
        task.metadata["review_comments_preview"] = parsed["comments"]
    elif "addressed_comments" in parsed:
        task.metadata["review_comments_preview"] = {
            "addressed": parsed.get("addressed_comments", []),
            "unaddressed": parsed.get("unaddressed_comments", []),
        }


def _get_current_head_sha(cwd: Path | str) -> str:
    """Return the current HEAD SHA for a directory, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


class TaskExecutor:
    """Drive end-to-end task execution while service remains the public facade."""

    def __init__(self, service: OrchestratorService) -> None:
        """Store orchestrator service dependencies used across execution phases."""
        self._service = service

    def _ensure_workdoc_or_block(self, task: Task, run: RunRecord, *, step: str) -> bool:
        """Verify canonical workdoc exists; otherwise block task/run for this step."""
        svc = self._service
        try:
            if svc._validate_task_workdoc(task) is not None:
                return True
            svc._block_for_missing_workdoc(task, run, step=step)
            return False
        except ValueError as exc:
            svc._block_for_invalid_workdoc(task, run, step=step, detail=str(exc))
            return False

    def _prepare_workdoc_for_run(
        self,
        task: Task,
        run: RunRecord,
        *,
        project_dir: Path,
        first_step: str,
        had_prior_runs: bool,
        append_retry_marker: bool,
        retry_from_step: str | None = None,
    ) -> bool:
        """Initialize on first run, refresh on retry, and block if retry lost canonical workdoc."""
        svc = self._service
        try:
            canonical = svc._validate_task_workdoc(task)
        except ValueError as exc:
            svc._block_for_invalid_workdoc(task, run, step=first_step, detail=str(exc))
            return False
        svc._clear_invalid_workdoc_markers(task)
        if canonical is not None:
            try:
                svc._refresh_workdoc_with_diagnostics(task, project_dir)
            except ValueError as exc:
                svc._block_for_invalid_workdoc(task, run, step=first_step, detail=str(exc))
                return False
            if append_retry_marker:
                attempt = max(1, len(task.run_ids))
                svc._append_retry_attempt_marker(
                    task,
                    project_dir=project_dir,
                    attempt=attempt,
                    start_from_step=retry_from_step or None,
                )
            return True
        if had_prior_runs:
            svc._block_for_missing_workdoc(task, run, step=first_step)
            return False
        svc._init_workdoc(task, project_dir)
        return True

    def _branch_exists(self, branch_name: str) -> bool:
        """Return whether a local branch exists in the project repository."""
        svc = self._service
        normalized = str(branch_name or "").strip()
        if not normalized:
            return False
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/heads/{normalized}"],
                cwd=svc.container.project_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except Exception:
            return False
        return result.returncode == 0

    def _prepare_precommit_review_context(self, task: Task, worktree_dir: Path | None) -> tuple[bool, str]:
        """Persist task-scoped changes for pre-commit human review.

        Returns:
            tuple[bool, str]: ``(ok, reason)`` where ``reason`` is non-empty when
            ``ok`` is ``False``.
        """
        svc = self._service
        # Non-git flows can legitimately run without task worktrees.
        if worktree_dir is None:
            return True, ""
        if not worktree_dir.exists() or not worktree_dir.is_dir():
            return False, "missing task worktree context"

        has_task_changes = svc._has_uncommitted_changes(worktree_dir) or svc._has_commits_ahead(worktree_dir)
        if not has_task_changes:
            return False, "no task-scoped changes available"

        preserve_outcome = svc._preserve_worktree_work(task, worktree_dir)
        if isinstance(preserve_outcome, bool):
            preserve_status = "preserved" if preserve_outcome else "failed"
            preserve_reason = "legacy_bool_result"
        else:
            preserve_status = str(getattr(preserve_outcome, "get", lambda _k, _d=None: _d)("status") or "").strip()
            preserve_reason = str(getattr(preserve_outcome, "get", lambda _k, _d=None: _d)("reason_code") or "failed_to_preserve").strip()
        if preserve_status != "preserved":
            reason = preserve_reason
            return False, f"failed to preserve task-scoped changes ({reason})"

        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        preserved_branch = str(metadata.get("preserved_branch") or "").strip()
        if not preserved_branch:
            return False, "missing preserved branch metadata"
        if not self._branch_exists(preserved_branch):
            return False, "preserved branch is not available"
        base_branch = str(metadata.get("preserved_base_branch") or svc._run_branch or "HEAD").strip() or "HEAD"
        base_sha = str(metadata.get("preserved_base_sha") or "").strip()
        head_sha = str(metadata.get("preserved_head_sha") or "").strip()
        if not base_sha:
            try:
                base_result = subprocess.run(
                    ["git", "rev-parse", "--verify", f"{base_branch}^{{commit}}"],
                    cwd=svc.container.project_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                if base_result.returncode == 0:
                    base_sha = base_result.stdout.strip()
            except Exception:
                base_sha = ""
        if not head_sha:
            try:
                head_result = subprocess.run(
                    ["git", "rev-parse", "--verify", f"refs/heads/{preserved_branch}^{{commit}}"],
                    cwd=svc.container.project_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                if head_result.returncode == 0:
                    head_sha = head_result.stdout.strip()
            except Exception:
                head_sha = ""
        diff_range = f"{base_sha}..{head_sha}" if base_sha and head_sha else f"{base_branch}..{preserved_branch}"
        try:
            diff_result = subprocess.run(
                ["git", "diff", "--binary", "--no-color", diff_range],
                cwd=svc.container.project_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except Exception:
            return False, "failed to prepare review context fingerprint"
        if diff_result.returncode != 0:
            return False, "failed to prepare review context fingerprint"
        fingerprint = hashlib.sha256((diff_result.stdout or "").encode("utf-8", errors="replace")).hexdigest()
        if not isinstance(task.metadata, dict):
            task.metadata = {}
        task.metadata["review_context"] = {
            "run_id": task.run_ids[-1] if task.run_ids else None,
            "preserved_branch": preserved_branch,
            "base_branch": base_branch,
            "base_sha": base_sha or None,
            "head_sha": head_sha or None,
            "prepared_at": now_iso(),
            "diff_fingerprint": fingerprint,
        }
        return True, ""

    # ------------------------------------------------------------------
    # Orchestrator-side comment steps
    # ------------------------------------------------------------------

    def _resolve_comment_platform(self, task: Task) -> dict[str, Any]:
        """Resolve platform identifiers from task metadata.

        Checks ``source_url`` first, then falls back to explicit
        ``source_pr_number`` / ``source_mr_number`` metadata fields.

        Returns:
            Platform info dict suitable for the comment reader/writer.

        Raises:
            ValueError: If no platform identifiers can be resolved.
        """
        meta = task.metadata if isinstance(task.metadata, dict) else {}
        source_url = str(meta.get("source_url") or "").strip()
        if source_url:
            return parse_source_url(source_url)

        pr_number = meta.get("source_pr_number")
        mr_number = meta.get("source_mr_number")
        if pr_number is not None:
            # Infer owner/repo from git remote.
            owner, repo = self._infer_github_owner_repo(task)
            return {
                "platform": "github",
                "owner": owner,
                "repo": repo,
                "number": int(pr_number),
            }
        if mr_number is not None:
            project_id = str(meta.get("source_project_id") or "").strip()
            if not project_id:
                raise ValueError("source_mr_number set but source_project_id is missing")
            return {
                "platform": "gitlab",
                "project_id": project_id,
                "number": int(mr_number),
            }
        raise ValueError("No source_url, source_pr_number, or source_mr_number in task metadata")

    def _infer_github_owner_repo(self, task: Task) -> tuple[str, str]:
        """Infer GitHub owner/repo from the git remote origin URL."""
        svc = self._service
        project_dir = svc.step_project_dir(task)
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            url = result.stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            raise ValueError(f"Cannot infer GitHub owner/repo from git remote: {exc}") from exc

        # Handle HTTPS: https://github.com/owner/repo.git
        m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
        if m:
            return m.group(1), m.group(2)
        raise ValueError(f"Cannot parse GitHub owner/repo from remote URL: {url}")

    def _execute_fetch_comments(self, task: Task, run: RunRecord) -> str:
        """Fetch PR/MR comments and store formatted output in task metadata.

        Returns:
            ``"ok"`` on success, ``"blocked"`` on failure.
        """
        svc = self._service
        step_started = now_iso()
        meta = task.metadata if isinstance(task.metadata, dict) else {}
        if not isinstance(task.metadata, dict):
            task.metadata = {}

        try:
            platform_info = self._resolve_comment_platform(task)
        except ValueError as exc:
            logger.warning("fetch_comments: cannot resolve platform for task %s: %s", task.id, exc)
            task.status = "blocked"
            task.error = f"Cannot resolve PR/MR source: {exc}"
            svc.container.tasks.upsert(task)
            run.steps.append({"step": "fetch_comments", "status": "error", "ts": now_iso(), "started_at": step_started, "error": str(exc)})
            svc.container.runs.upsert(run)
            svc._emit_task_blocked(task)
            return "blocked"

        platform = str(platform_info.get("platform", ""))
        git_dir = svc.step_project_dir(task)

        try:
            if platform == "github":
                comments = fetch_pr_comments(
                    str(platform_info["owner"]),
                    str(platform_info["repo"]),
                    int(platform_info["number"]),
                    git_dir,
                )
            elif platform == "gitlab":
                comments = fetch_mr_comments(
                    str(platform_info["project_id"]),
                    int(platform_info["number"]),
                    cwd=git_dir,
                )
            else:
                raise CommentFetchError(f"Unsupported platform: {platform}")
        except (CommentFetchError, Exception) as exc:
            logger.warning("fetch_comments: failed for task %s: %s", task.id, exc)
            task.status = "blocked"
            task.error = f"Failed to fetch comments: {exc}"
            svc.container.tasks.upsert(task)
            run.steps.append({"step": "fetch_comments", "status": "error", "ts": now_iso(), "started_at": step_started, "error": str(exc)})
            svc.container.runs.upsert(run)
            svc._emit_task_blocked(task)
            return "blocked"

        formatted_text = format_comments_for_prompt(comments)
        task.metadata["fetched_comments"] = [c.to_dict() for c in comments]
        task.metadata["formatted_comments"] = formatted_text
        task.metadata["source_comments_formatted"] = formatted_text
        task.metadata["comment_platform"] = platform_info
        svc.container.tasks.upsert(task)

        run.steps.append({
            "step": "fetch_comments",
            "status": "ok",
            "ts": now_iso(),
            "started_at": step_started,
            "comment_count": len(comments),
        })
        svc.container.runs.upsert(run)

        logger.info("fetch_comments: fetched %d comments for task %s", len(comments), task.id)

        # Write fetched comments summary to workdoc.
        fetch_summary = f"Fetched **{len(comments)}** comment(s) from {platform_info.get('platform', 'unknown')}."
        if comments:
            fetch_summary += "\n\n" + formatted_text[:2000]
        svc._sync_workdoc(task, "fetch_comments", svc.step_project_dir(task), fetch_summary)

        svc.bus.emit(
            channel="tasks",
            event_type="task.step_completed",
            entity_id=task.id,
            payload={"step": "fetch_comments", "comment_count": len(comments)},
        )
        return "ok"

    def _execute_post_comments(self, task: Task, run: RunRecord) -> str:
        """Post generated review comments and optional review decision.

        Reads generated comments from the ``pr_review_comment`` worker step
        output in ``task.metadata["step_outputs"]``. Respects the
        ``comment_dry_run`` flag (defaults to ``True`` for safe-by-default
        behavior).

        Returns:
            ``"ok"`` on success or dry_run, ``"blocked"`` on total failure.
        """
        svc = self._service
        step_started = now_iso()
        meta = task.metadata if isinstance(task.metadata, dict) else {}
        if not isinstance(task.metadata, dict):
            task.metadata = {}

        platform_info = meta.get("comment_platform")
        if not isinstance(platform_info, dict) or not platform_info.get("platform"):
            task.status = "blocked"
            task.error = "Missing comment_platform metadata; fetch_comments may not have run"
            svc.container.tasks.upsert(task)
            run.steps.append({"step": "post_comments", "status": "error", "ts": now_iso(), "started_at": step_started, "error": task.error})
            svc.container.runs.upsert(run)
            svc._emit_task_blocked(task)
            return "blocked"

        # Parse generated comments from prior worker step output.
        step_outputs = meta.get("step_outputs") or {}
        raw_output = step_outputs.get("pr_review_comment", "")
        try:
            parsed = json.loads(raw_output) if isinstance(raw_output, str) else raw_output
            if not isinstance(parsed, dict):
                raise ValueError("Expected JSON object from pr_review_comment output")
            generated_comments = parsed.get("comments") or []
            summary_text = str(parsed.get("summary") or "")

            # Filter out comments targeting files not in the diff.
            diff_text = str(meta.get("source_diff") or "")
            if diff_text and generated_comments:
                import re as _re
                diff_files = set(_re.findall(r"^diff --git a/(.+?) b/", diff_text, _re.MULTILINE))
                before = len(generated_comments)
                generated_comments = [c for c in generated_comments if c.get("path") in diff_files]
                dropped = before - len(generated_comments)
                if dropped:
                    logger.warning(
                        "post_comments: dropped %d comment(s) targeting files outside the diff for task %s",
                        dropped, task.id,
                    )

            # Persist generated comment details in a client-visible metadata key.
            task.metadata["generated_review_comments"] = [
                {"path": c.get("path"), "line": c.get("line"), "body": c.get("body"), "severity": c.get("severity", "medium")}
                for c in generated_comments
            ]
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("post_comments: failed to parse worker output for task %s: %s", task.id, exc)
            task.status = "blocked"
            task.error = _step_output_parse_error("pr_review_comment", exc, raw_output)
            svc.container.tasks.upsert(task)
            run.steps.append({"step": "post_comments", "status": "error", "ts": now_iso(), "started_at": step_started, "error": task.error})
            svc.container.runs.upsert(run)
            svc._emit_task_blocked(task)
            return "blocked"

        dry_run = bool(meta.get("comment_dry_run", True))
        git_dir = svc.step_project_dir(task)
        posted_count = 0
        staged_count = 0
        failed_count = 0
        results: list[dict[str, Any]] = []

        if generated_comments and not dry_run:
            post_results = post_comments_batch(
                platform_info,
                generated_comments,
                git_dir=git_dir,
            )
            for idx, r in enumerate(post_results):
                status = "posted" if r.success else "failed"
                result_dict = r.to_dict()
                result_dict["post_status"] = status
                # Attach comment data for workdoc rendering.
                if idx < len(generated_comments):
                    comment = generated_comments[idx]
                    result_dict["_comment_body"] = str(comment.get("body", ""))[:200]
                    result_dict["_comment_path"] = comment.get("path", "")
                    result_dict["_comment_line"] = comment.get("line")
                results.append(result_dict)
                if r.success:
                    posted_count += 1
                else:
                    failed_count += 1
        else:
            # Dry run (default) or no comments: record without posting.
            for comment in generated_comments:
                result_dict = CommentPostResult(success=True, platform_id="dry_run", post_status="staged").to_dict()
                result_dict["_comment_body"] = str(comment.get("body", ""))[:200]
                result_dict["_comment_path"] = comment.get("path", "")
                result_dict["_comment_line"] = comment.get("line")
                results.append(result_dict)
                staged_count += 1

        task.metadata["posted_comments"] = results

        # Post review decision if requested.
        review_decision_raw = meta.get("review_decision")
        decision_result: dict[str, Any] | None = None
        if isinstance(review_decision_raw, dict) and not dry_run:
            decision_type = str(review_decision_raw.get("decision") or "comment")
            decision_body = str(review_decision_raw.get("body") or summary_text)
            platform = str(platform_info.get("platform", ""))
            try:
                if platform == "github":
                    dr = post_pr_review_decision(
                        str(platform_info["owner"]),
                        str(platform_info["repo"]),
                        int(platform_info["number"]),
                        decision=decision_type,  # type: ignore[arg-type]
                        body=decision_body,
                        git_dir=git_dir,
                    )
                elif platform == "gitlab":
                    dr = post_mr_review_decision(
                        str(platform_info["project_id"]),
                        int(platform_info["number"]),
                        decision=decision_type,  # type: ignore[arg-type]
                        body=decision_body,
                        cwd=git_dir,
                    )
                else:
                    dr = CommentPostResult(success=False, error=f"Unsupported platform: {platform}")
                decision_result = dr.to_dict()
                task.metadata["review_decision_result"] = decision_result
            except Exception as exc:
                logger.warning("post_comments: review decision failed for task %s: %s", task.id, exc)
                decision_result = CommentPostResult(success=False, error=str(exc)).to_dict()
                task.metadata["review_decision_result"] = decision_result

        # Write structured comments to workdoc.
        self._write_generated_comments_to_workdoc(
            task,
            heading="## Posted Comments",
            results=results,
            dry_run=dry_run,
            decision_result=decision_result,
        )

        svc.container.tasks.upsert(task)

        step_log: dict[str, Any] = {
            "step": "post_comments",
            "status": "ok",
            "ts": now_iso(),
            "started_at": step_started,
            "posted_count": posted_count,
            "staged_count": staged_count,
            "failed_count": failed_count,
            "comment_dry_run": dry_run,
        }

        # Total failure: no comments posted and we had comments to post.
        if not dry_run and generated_comments and posted_count == 0:
            step_log["status"] = "error"
            run.steps.append(step_log)
            svc.container.runs.upsert(run)
            task.status = "blocked"
            task.error = f"Failed to post all {failed_count} comments"
            svc.container.tasks.upsert(task)
            svc._emit_task_blocked(task)
            return "blocked"

        run.steps.append(step_log)
        svc.container.runs.upsert(run)

        logger.info(
            "post_comments: task %s posted=%d staged=%d failed=%d dry_run=%s",
            task.id, posted_count, staged_count, failed_count, dry_run,
        )
        svc.bus.emit(
            channel="tasks",
            event_type="task.step_completed",
            entity_id=task.id,
            payload={"step": "post_comments", "posted_count": posted_count, "staged_count": staged_count, "failed_count": failed_count, "comment_dry_run": dry_run},
        )
        return "ok"

    def _execute_post_comment_responses(self, task: Task, run: RunRecord) -> str:
        """Post reply comments to existing threads for fix_respond pipeline.

        Reads addressed comments from the ``pr_review_fix_respond`` worker step
        output and posts replies using the ``in_reply_to`` platform IDs from
        the fetched comments.

        Returns:
            ``"ok"`` on success or dry_run, ``"blocked"`` on total failure.
        """
        svc = self._service
        step_started = now_iso()
        meta = task.metadata if isinstance(task.metadata, dict) else {}
        if not isinstance(task.metadata, dict):
            task.metadata = {}

        platform_info = meta.get("comment_platform")
        if not isinstance(platform_info, dict) or not platform_info.get("platform"):
            task.status = "blocked"
            task.error = "Missing comment_platform metadata; fetch_comments may not have run"
            svc.container.tasks.upsert(task)
            run.steps.append({"step": "post_comment_responses", "status": "error", "ts": now_iso(), "started_at": step_started, "error": task.error})
            svc.container.runs.upsert(run)
            svc._emit_task_blocked(task)
            return "blocked"

        # Parse addressed comments from prior worker step output.
        step_outputs = meta.get("step_outputs") or {}
        raw_output = step_outputs.get("pr_review_fix_respond", "")
        try:
            parsed = json.loads(raw_output) if isinstance(raw_output, str) else raw_output
            if not isinstance(parsed, dict):
                raise ValueError("Expected JSON object from pr_review_fix_respond output")
            addressed_comments = parsed.get("addressed_comments") or []
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("post_comment_responses: failed to parse worker output for task %s: %s", task.id, exc)
            task.status = "blocked"
            task.error = _step_output_parse_error("pr_review_fix_respond", exc, raw_output)
            svc.container.tasks.upsert(task)
            run.steps.append({"step": "post_comment_responses", "status": "error", "ts": now_iso(), "started_at": step_started, "error": task.error})
            svc.container.runs.upsert(run)
            svc._emit_task_blocked(task)
            return "blocked"

        # Build lookup from internal comment ID to platform/discussion IDs.
        fetched_comments = meta.get("fetched_comments") or []
        id_to_reply_target: dict[str, dict[str, str]] = {}
        for fc in fetched_comments:
            if isinstance(fc, dict):
                cid = str(fc.get("id") or "")
                pid = str(fc.get("platform_id") or "")
                discussion_id = str(fc.get("discussion_id") or "")
                if cid and (pid or discussion_id):
                    id_to_reply_target[cid] = {
                        "platform_id": pid,
                        "discussion_id": discussion_id,
                    }

        dry_run = bool(meta.get("comment_dry_run", True))
        git_dir = svc.step_project_dir(task)
        posted_count = 0
        staged_count = 0
        failed_count = 0
        skipped_count = 0
        results: list[dict[str, Any]] = []

        for addressed in addressed_comments:
            if not isinstance(addressed, dict):
                continue
            original_id = str(addressed.get("original_comment_id") or "")
            response_body = str(addressed.get("response_body") or "")
            if not response_body:
                skipped_count += 1
                continue

            reply_target = id_to_reply_target.get(original_id, {})
            platform_id = str(reply_target.get("platform_id") or "")
            discussion_id = str(reply_target.get("discussion_id") or "")
            if not platform_id and not discussion_id:
                logger.warning(
                    "post_comment_responses: no reply target for comment %s in task %s, posting as top-level",
                    original_id, task.id,
                )

            if dry_run:
                result_dict = CommentPostResult(success=True, platform_id="dry_run", post_status="staged").to_dict()
                result_dict["_comment_body"] = response_body[:200]
                result_dict["_original_comment_id"] = original_id
                results.append(result_dict)
                staged_count += 1
                continue

            try:
                reply_to = int(platform_id) if platform_id else None
            except (ValueError, TypeError):
                logger.warning("post_comment_responses: non-numeric platform_id %r for comment %s", platform_id, original_id)
                reply_to = None
            comment_data: dict[str, Any] = {
                "body": response_body,
                "in_reply_to": reply_to,
            }
            if discussion_id:
                comment_data["discussion_id"] = discussion_id
            batch_results = post_comments_batch(
                platform_info,
                [comment_data],
                git_dir=git_dir,
            )
            r = batch_results[0] if batch_results else CommentPostResult(success=False, error="No result")
            status = "posted" if r.success else "failed"
            result_dict = r.to_dict()
            result_dict["post_status"] = status
            result_dict["_comment_body"] = response_body[:200]
            result_dict["_original_comment_id"] = original_id
            results.append(result_dict)
            if r.success:
                posted_count += 1
            else:
                failed_count += 1

        task.metadata["posted_responses"] = results

        # Write structured comments to workdoc.
        self._write_generated_comments_to_workdoc(
            task,
            heading="## Comment Responses",
            results=results,
            dry_run=dry_run,
        )

        svc.container.tasks.upsert(task)

        step_log: dict[str, Any] = {
            "step": "post_comment_responses",
            "status": "ok",
            "ts": now_iso(),
            "started_at": step_started,
            "posted_count": posted_count,
            "staged_count": staged_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "comment_dry_run": dry_run,
        }

        # Total failure: no responses posted and we had responses to post.
        total_to_post = len(addressed_comments) - skipped_count
        if not dry_run and total_to_post > 0 and posted_count == 0:
            step_log["status"] = "error"
            run.steps.append(step_log)
            svc.container.runs.upsert(run)
            task.status = "blocked"
            task.error = f"Failed to post all {failed_count} comment responses"
            svc.container.tasks.upsert(task)
            svc._emit_task_blocked(task)
            return "blocked"

        run.steps.append(step_log)
        svc.container.runs.upsert(run)

        logger.info(
            "post_comment_responses: task %s posted=%d staged=%d failed=%d skipped=%d dry_run=%s",
            task.id, posted_count, staged_count, failed_count, skipped_count, dry_run,
        )
        svc.bus.emit(
            channel="tasks",
            event_type="task.step_completed",
            entity_id=task.id,
            payload={"step": "post_comment_responses", "posted_count": posted_count, "staged_count": staged_count, "failed_count": failed_count, "comment_dry_run": dry_run},
        )
        return "ok"

    def _write_generated_comments_to_workdoc(
        self,
        task: Task,
        *,
        heading: str,
        results: list[dict[str, Any]],
        dry_run: bool,
        decision_result: dict[str, Any] | None = None,
    ) -> None:
        """Write a structured generated-comments section to the task workdoc.

        Each comment entry shows its body preview, file/line info, and post
        status badge (``staged``, ``posted``, or ``failed``).  On re-run the
        existing section with the same *heading* is replaced rather than
        duplicated.

        Args:
            task: The task owning the workdoc.
            heading: Markdown heading for the section (e.g. ``## Generated Comments``).
            results: List of result dicts (from ``CommentPostResult.to_dict()``
                with extra ``_comment_body``, ``_comment_path``, ``_comment_line``,
                and optionally ``_original_comment_id`` keys).
            dry_run: Whether this was a dry-run execution.
            decision_result: Optional review-decision result dict.
        """
        svc = self._service
        canonical = svc._workdoc_canonical_path(task.id)
        if not canonical.exists():
            return

        lines: list[str] = [f"{heading}\n"]
        if dry_run:
            lines.append("**Mode:** dry run \u2014 comments staged for review\n\n")
        else:
            lines.append("**Mode:** live \u2014 comments posted\n\n")

        staged_count = 0
        posted_count = 0
        failed_count = 0

        for idx, result in enumerate(results, start=1):
            status = str(result.get("post_status", "staged"))
            if status == "staged":
                staged_count += 1
            elif status == "posted":
                posted_count += 1
            else:
                failed_count += 1

            lines.append(f"### Comment {idx} \u00b7 `{status}`\n")

            # File/line info.
            path = result.get("_comment_path", "")
            line_num = result.get("_comment_line")
            if path:
                loc = f"`{path}`"
                if line_num is not None:
                    loc += f" L{line_num}"
                lines.append(f"**File:** {loc}\n")

            # Original comment reference (for responses).
            orig_id = result.get("_original_comment_id")
            if orig_id:
                lines.append(f"**In reply to:** `{orig_id}`\n")

            # Body preview.
            body = str(result.get("_comment_body", ""))
            if body:
                preview = body[:200]
                if len(body) > 200:
                    preview += "\u2026"
                lines.append(f"> {preview}\n")

            # Error info for failed comments.
            error = result.get("error")
            if status == "failed" and error:
                lines.append(f"**Error:** {error}\n")

            lines.append("\n")

        # Decision result.
        if decision_result:
            success = "succeeded" if decision_result.get("success") else "failed"
            lines.append(f"**Review decision:** {success}\n\n")

        # Summary line.
        parts: list[str] = []
        if staged_count:
            parts.append(f"{staged_count} staged")
        if posted_count:
            parts.append(f"{posted_count} posted")
        if failed_count:
            parts.append(f"{failed_count} failed")
        lines.append(f"---\n**Summary:** {', '.join(parts) if parts else '0 comments'}\n")

        summary_block = "".join(lines)

        try:
            content = canonical.read_text(encoding="utf-8")

            # Replace existing section with same heading on re-run.
            section_start = content.find(heading)
            if section_start != -1:
                # Find the end of this section: next H2 heading or end of file.
                rest = content[section_start + len(heading):]
                next_h2 = re.search(r"^## ", rest, re.MULTILINE)
                if next_h2:
                    section_end = section_start + len(heading) + next_h2.start()
                else:
                    section_end = len(content)
                updated = content[:section_start] + summary_block + "\n" + content[section_end:]
            else:
                # Insert before Implementation Log if it exists.
                log_marker = "## Implementation Log"
                idx = content.find(log_marker)
                if idx != -1:
                    updated = content[:idx] + summary_block + "\n" + content[idx:]
                else:
                    updated = content.rstrip() + "\n\n" + summary_block

            canonical.write_text(updated, encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to update workdoc for task %s: %s", task.id, exc)

    def _run_orchestrator_comment_step(self, task: Task, run: RunRecord, step: str) -> str:
        """Dispatch an orchestrator-side comment step to the appropriate handler.

        Returns:
            Step outcome string (``"ok"`` or ``"blocked"``).
        """
        if step == "fetch_comments":
            return self._execute_fetch_comments(task, run)
        elif step == "post_comments":
            return self._execute_post_comments(task, run)
        elif step == "post_comment_responses":
            return self._execute_post_comment_responses(task, run)
        return "ok"

    def execute_task(self, task: Task) -> None:
        """Execute one task and normalize top-level cancellation/error outcomes.

        This wrapper ensures task/run status persistence and event emission are
        consistent even when the inner execution loop raises.
        """
        svc = self._service
        try:
            self.execute_task_inner(task)
        except svc._Cancelled:
            logger.info("Task %s was cancelled by user", task.id)
            fresh = svc.container.tasks.get(task.id)
            if fresh:
                if fresh.status != "cancelled":
                    fresh.status = "cancelled"
                    svc.container.tasks.upsert(fresh)
                for run_id in reversed(fresh.run_ids):
                    run = svc.container.runs.get(run_id)
                    if run and run.status == "in_progress":
                        run.accumulate_worker_seconds()
                        run.status = "cancelled"
                        run.finished_at = now_iso()
                        run.summary = "Cancelled by user"
                        svc.container.runs.upsert(run)
                        break
                svc.bus.emit(
                    channel="tasks",
                    event_type="task.cancelled",
                    entity_id=fresh.id,
                    payload={"status": "cancelled"},
                )
        except Exception as exc:
            logger.exception("Unexpected error executing task %s", task.id)
            fresh = svc.container.tasks.get(task.id) or task
            fresh.status = "blocked"
            fresh.current_agent_id = None
            exc_type = type(exc).__name__
            exc_msg = str(exc).strip()
            detail = f"{exc_type}: {exc_msg}" if exc_msg else exc_type
            fresh.error = f"Internal error during execution: {detail}"
            if isinstance(fresh.metadata, dict):
                raw_worktree_dir = str(fresh.metadata.get("worktree_dir") or "").strip()
                if raw_worktree_dir:
                    svc._mark_task_context_retained(
                        fresh,
                        reason=fresh.error,
                        expected_on_retry=True,
                    )
            svc.container.tasks.upsert(fresh)

    def execute_task_inner(self, task: Task) -> None:
        """Run the full pipeline, including retries, gates, review, and merge.

        Coordinates worktree lifecycle, run-record updates, verify-fix loops,
        human-gate checks, review cycles, and final commit/merge behavior.
        """
        svc = self._service
        fresh_task = svc.container.tasks.get(task.id)
        if fresh_task is not None:
            task = fresh_task
        worktree_dir: Optional[Path] = None
        try:
            if not isinstance(task.metadata, dict):
                task.metadata = {}
            task.metadata.pop("environment_auto_requeue_pending", None)
            task_context_raw = task.metadata.get("task_context")
            task_context = task_context_raw if isinstance(task_context_raw, dict) else {}
            expected_on_retry = bool(task_context.get("expected_on_retry"))
            old_preserved = str(task.metadata.get("preserved_branch") or "").strip()

            retained_path_raw = str(task_context.get("worktree_dir") or task.metadata.get("worktree_dir") or "").strip()
            retained_path = svc._resolve_retained_task_worktree(task, retained_path_raw)
            # Only fail closed when retry context was explicitly retained or preserved.
            # Stale worktree metadata from prior gate cleanup should not block re-runs.
            context_expected = bool(expected_on_retry or old_preserved)

            if retained_path is not None:
                worktree_dir = retained_path
            else:
                if old_preserved:
                    try:
                        worktree_dir = svc._create_worktree_from_branch(task, old_preserved)
                    except subprocess.CalledProcessError:
                        stale_dir = svc.container.state_root / "worktrees" / str(task.id)
                        if stale_dir.exists():
                            subprocess.run(
                                ["git", "worktree", "remove", str(stale_dir), "--force"],
                                cwd=svc.container.project_dir,
                                capture_output=True,
                                text=True,
                                timeout=60,
                            )
                        try:
                            worktree_dir = svc._create_worktree_from_branch(task, old_preserved)
                        except subprocess.CalledProcessError:
                            worktree_dir = None
                    if worktree_dir:
                        for key in (
                            "preserved_branch",
                            "preserved_base_branch",
                            "preserved_base_sha",
                            "preserved_head_sha",
                            "preserved_merge_base_sha",
                            "preserved_at",
                        ):
                            task.metadata.pop(key, None)
                        task.metadata.pop("merge_conflict", None)
                elif not context_expected:
                    worktree_dir = svc._create_worktree(task)

            if worktree_dir is None and context_expected:
                task.status = "blocked"
                task.current_agent_id = None
                task.pending_gate = None
                task.wait_state = None
                task.error = "Retained task context is missing; request changes to regenerate implementation context."
                task.current_step = task.current_step or None
                svc._mark_task_context_retained(task, reason="context_attach_failed", expected_on_retry=True)
                svc.container.tasks.upsert(task)
                svc._emit_task_blocked(task)
                return

            if worktree_dir:
                task.metadata["worktree_dir"] = str(worktree_dir)
                context_branch = str(task_context.get("task_branch") or f"task-{task.id}").strip() or f"task-{task.id}"
                svc._record_task_context(task, worktree_dir=worktree_dir, task_branch=context_branch)
                svc._clear_task_context_retained(task)
                svc.container.tasks.upsert(task)

            registry = PipelineRegistry()
            template = registry.resolve_for_task_type(task.task_type)
            steps = task.pipeline_template if task.pipeline_template else template.step_names()
            task.pipeline_template = steps
            has_review = "review" in steps
            has_commit = "commit" in steps
            first_step = steps[0] if steps else "plan"

            workdoc_dir = worktree_dir if worktree_dir else svc.container.project_dir
            svc._ensure_scope_contract_baseline_ref(task, workdoc_dir)
            svc.container.tasks.upsert(task)
            had_prior_runs = bool(task.run_ids)
            checkpoint = svc._execution_checkpoint(task)
            checkpoint_run_id = str(checkpoint.get("run_id") or "").strip()
            run: RunRecord | None = None
            created_new_run = False
            if checkpoint_run_id:
                existing_run = svc.container.runs.get(checkpoint_run_id)
                if existing_run and existing_run.task_id == task.id and existing_run.status in {"waiting_gate", "in_progress"}:
                    run = existing_run
                    run.status = "in_progress"
                    run.finished_at = None
                    run.started_at = now_iso()
                    if run.summary and str(run.summary).startswith("Paused at gate:"):
                        run.summary = None
            if run is None:
                created_new_run = True
                task_branch = f"task-{task.id}" if worktree_dir else svc._ensure_branch()
                run = RunRecord(task_id=task.id, status="in_progress", started_at=now_iso(), branch=task_branch)
                run.steps = []
                if run.id not in task.run_ids:
                    task.run_ids.append(run.id)
            elif run.id not in task.run_ids:
                task.run_ids.append(run.id)
            svc.container.runs.upsert(run)

            # Gate resumes can restart execution on the same run; treat retry metadata
            # as "new run created due to retry" rather than "task has any retry_count".
            is_retry_run = created_new_run and int(task.retry_count or 0) > 0
            retry_from = ""
            has_retry_guidance = False
            checkpoint_resume_step = str(checkpoint.get("resume_step") or "").strip()
            if isinstance(task.metadata, dict):
                retry_from = str(task.metadata.get("retry_from_step", "") or "").strip()
                retry_guidance = task.metadata.get("retry_guidance")
                if isinstance(retry_guidance, dict):
                    has_retry_guidance = bool(str(retry_guidance.get("guidance") or "").strip())
            if promote_legacy_human_guidance(task):
                svc.container.tasks.upsert(task)
            if not retry_from and checkpoint_resume_step:
                retry_from = checkpoint_resume_step
            run_attempt = max(1, len(task.run_ids))
            append_retry_marker = created_new_run and is_retry_run
            if not self._prepare_workdoc_for_run(
                task,
                run,
                project_dir=workdoc_dir,
                first_step=first_step,
                had_prior_runs=had_prior_runs,
                append_retry_marker=append_retry_marker,
                retry_from_step=retry_from,
            ):
                return

            cfg = svc.container.config.load()
            orch_cfg = dict(cfg.get("orchestrator") or {})
            max_review_attempts = int(orch_cfg.get("max_review_attempts", 10) or 10)
            max_verify_fix_attempts = int(orch_cfg.get("max_verify_fix_attempts", 3) or 3)

            if isinstance(task.metadata, dict):
                retry_from = str(task.metadata.pop("retry_from_step", "") or "").strip()

            # Resolve retry_from="implement_fix" to the parent verify step
            # that originally triggered the fix loop.  The loop below will
            # re-enter the verify-fix cycle and dispatch implement_fix.
            resume_implement_fix = False
            if retry_from == "implement_fix":
                parent_step = str((task.metadata or {}).get("pipeline_phase") or "").strip()
                if parent_step in steps:
                    retry_from = parent_step
                    resume_implement_fix = True
                else:
                    # Fallback: find the first verify-like step in the pipeline.
                    # Without this, retry_from becomes "" and all phase-1 steps
                    # may be skipped, allowing review+commit without verification.
                    fallback = next((s for s in steps if s in _VERIFY_STEPS), None)
                    if fallback:
                        retry_from = fallback
                        resume_implement_fix = True
                    else:
                        retry_from = ""

            start_step: str | None
            if retry_from in steps:
                start_step = retry_from
            elif retry_from in {"review", "commit"}:
                start_step = retry_from
            elif retry_from == svc._BEFORE_DONE_RESUME_STEP:
                start_step = None
            else:
                start_step = task.current_step or (steps[0] if steps else None)
            task.current_step = start_step
            task.metadata["pipeline_phase"] = start_step
            task.status = "in_progress"
            task.wait_state = None
            task.current_agent_id = svc._choose_agent_for_task(task)
            svc.container.tasks.upsert(task)
            svc.bus.emit(
                channel="tasks",
                event_type="task.started",
                entity_id=task.id,
                payload={
                    "run_id": run.id,
                    "agent_id": task.current_agent_id,
                    "run_attempt": run_attempt,
                    "is_retry": is_retry_run,
                    "start_from_step": retry_from or None,
                    "has_retry_guidance": has_retry_guidance,
                    "retry_count": int(task.retry_count),
                },
            )

            def _consume_human_guidance(step_name: str) -> None:
                if consume_human_guidance_for_step(task, step=step_name, run_id=run.id):
                    svc.container.tasks.upsert(task)

            mode = normalize_hitl_mode(getattr(task, "hitl_mode", "autopilot"))
            # Retry flows resumed from implement/review/commit should not require
            # re-approval of the original plan gate.  However, retries from
            # pre-implement planning steps (plan, initiative_plan, commit_review)
            # must still pause so the user can review the refreshed output.
            skip_before_implement_gate = bool(
                is_retry_run and retry_from and retry_from not in {"plan", "initiative_plan", "commit_review", "pr_review", "mr_review"}
            )

            skip_phase1 = retry_from in ("review", "commit")
            resume_from_done_gate = retry_from == svc._BEFORE_DONE_RESUME_STEP
            reached_retry_step = not retry_from
            early_complete = False
            last_phase1_step: str | None = None
            # Steps declared *after* "review" in the pipeline must wait until
            # the review cycle completes (e.g. "report" needs review findings).
            _post_review_step_set: set[str] = set()
            if has_review:
                _saw_review = False
                for _s in steps:
                    if _s == "review":
                        _saw_review = True
                    elif _s != "commit" and _saw_review:
                        _post_review_step_set.add(_s)
            # When resuming from the before_done gate after early completion,
            # skip the phase-1 loop entirely and go straight to finalization.
            if resume_from_done_gate and task.metadata.get("early_complete"):
                early_complete = True
            for step in steps:
                if step in ("review", "commit") or step in _post_review_step_set:
                    continue
                if resume_from_done_gate:
                    continue
                if skip_phase1:
                    last_phase1_step = step
                    run.steps.append({"step": step, "status": "skipped", "ts": now_iso()})
                    svc.container.runs.upsert(run)
                    continue
                if not reached_retry_step:
                    if step == retry_from:
                        reached_retry_step = True
                    else:
                        last_phase1_step = step
                        run.steps.append({"step": step, "status": "skipped", "ts": now_iso()})
                        svc.container.runs.upsert(run)
                        continue
                svc._check_cancelled(task)
                gate_name = svc._gate_for_step(
                    task=task,
                    mode=mode,
                    steps=steps,
                    step=step,
                    skip_before_implement_gate=skip_before_implement_gate,
                )
                if gate_name and svc._should_gate(mode, gate_name):
                    if gate_name == "before_post_review":
                        _prepare_post_review_gate_context(task)
                        svc.container.tasks.upsert(task)
                        if not svc._wait_for_gate(task, gate_name, resume_step=step):
                            return
                    elif not svc._wait_for_gate(task, gate_name):
                        return
                task.current_step = step
                task.metadata["pipeline_phase"] = step
                svc.container.tasks.upsert(task)
                # When resuming from implement_fix, skip the initial verify
                # run and enter the fix loop directly — the failure context
                # is already in task metadata from the previous run.
                verify_failed = False
                if resume_implement_fix and step in _VERIFY_STEPS:
                    verify_failed = True
                    resume_implement_fix = False  # consume the flag
                else:
                    if step in _ORCHESTRATOR_COMMENT_STEPS:
                        step_outcome = self._run_orchestrator_comment_step(task, run, step)
                    else:
                        step_outcome = svc._run_non_review_step(task, run, step, attempt=1)
                    if step_outcome == "ok":
                        verify_failed = False
                    elif step_outcome == "no_action_needed":
                        early_complete = True
                        last_phase1_step = step
                        break
                    elif step_outcome == "verify_failed":
                        verify_failed = True
                    elif step_outcome == "verify_degraded":
                        last_phase1_step = step
                        continue
                    else:
                        return
                if verify_failed:
                    fixed = False
                    for fix_attempt in range(1, max_verify_fix_attempts + 1):
                        task.status = "in_progress"
                        task.metadata["verify_failure"] = task.error
                        svc._capture_verify_output(task)
                        task.error = None
                        task.retry_count += 1
                        svc.container.tasks.upsert(task)
                        run.status = "in_progress"
                        run.finished_at = None
                        run.summary = None
                        svc.container.runs.upsert(run)

                        task.current_step = "implement_fix"
                        task.metadata["pipeline_phase"] = step
                        svc.container.tasks.upsert(task)
                        fix_outcome = svc._run_non_review_step(task, run, "implement_fix", attempt=fix_attempt + 1)
                        if fix_outcome != "ok":
                            return
                        _consume_human_guidance("implement_fix")
                        task.metadata.pop("verify_failure", None)
                        task.metadata.pop("verify_output", None)
                        task.current_step = step
                        task.metadata["pipeline_phase"] = step
                        svc.container.tasks.upsert(task)
                        verify_outcome = svc._run_non_review_step(task, run, step, attempt=fix_attempt + 1)
                        if verify_outcome == "ok":
                            _consume_human_guidance(step)
                            fixed = True
                            break
                        if verify_outcome == "verify_degraded":
                            fixed = True
                            break
                        if verify_outcome == "auto_requeued":
                            return
                        if verify_outcome != "verify_failed":
                            return
                    if fixed:
                        last_phase1_step = step
                        continue
                    # All verify-fix attempts exhausted — ensure task is blocked.
                    task.status = "blocked"
                    task.wait_state = None
                    task.error = task.error or f"Could not fix {step} after {max_verify_fix_attempts} attempts"
                    task.current_step = step
                    svc.container.tasks.upsert(task)
                    svc._finalize_run(task, run, status="blocked", summary=f"Blocked: {step} failed after {max_verify_fix_attempts} fix attempts")
                    svc._emit_task_blocked(task)
                    return
                _consume_human_guidance(step)
                last_phase1_step = step

            # -- Early completion: step signalled no further action needed ----
            if early_complete:
                # Record remaining phase-1 steps as skipped in the run log.
                if last_phase1_step:
                    found = False
                    for remaining_step in steps:
                        if remaining_step in ("review", "commit"):
                            run.steps.append({"step": remaining_step, "status": "skipped", "ts": now_iso()})
                            continue
                        if not found:
                            if remaining_step == last_phase1_step:
                                found = True
                            continue
                        run.steps.append({"step": remaining_step, "status": "skipped", "ts": now_iso()})
                    svc.container.runs.upsert(run)

                # HITL gate: supervised/review_only need approval before done.
                requires_done_gate = svc._should_gate(mode, "before_done")
                pipeline_id = svc._pipeline_id_for_task(task)
                if requires_done_gate and pipeline_id not in svc._DECOMPOSITION_PIPELINES and not resume_from_done_gate:
                    gate_resume_step = svc._BEFORE_DONE_RESUME_STEP
                    task.current_step = last_phase1_step
                    task.metadata["pipeline_phase"] = last_phase1_step
                    task.metadata["early_complete"] = True
                    svc.container.tasks.upsert(task)
                    if not svc._wait_for_gate(task, "before_done", resume_step=gate_resume_step):
                        return

                # Clean up worktree if present (no changes to commit).
                if worktree_dir:
                    subprocess.run(
                        ["git", "worktree", "remove", str(worktree_dir), "--force"],
                        cwd=svc.container.project_dir,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    subprocess.run(
                        ["git", "branch", "-D", f"task-{task.id}"],
                        cwd=svc.container.project_dir,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    worktree_dir = None
                    task.metadata.pop("worktree_dir", None)
                    task.metadata.pop("task_context", None)

                # Mark done.
                svc._run_summarize_step(task, run)
                task.status = "done"
                task.wait_state = None
                task.current_step = None
                task.metadata.pop("pipeline_phase", None)
                task.metadata.pop("early_complete", None)
                run.status = "done"
                run.summary = "Pipeline completed — no action needed"
                svc.bus.emit(
                    channel="tasks",
                    event_type="task.done",
                    entity_id=task.id,
                    payload={"early_complete": True},
                )

            if not early_complete:
                next_phase = "review" if has_review and retry_from != "commit" else "commit"
                if (has_review and retry_from != "commit") or has_commit:
                    if not self._ensure_workdoc_or_block(task, run, step=next_phase):
                        return

            if not early_complete and has_commit:
                impl_dir = worktree_dir or svc.container.project_dir
                svc._cleanup_workdoc_for_commit(impl_dir)
                # Skip the "no changes" guard when retrying from review/commit.
                # The task already produced changes in a prior run — the guard
                # only protects against empty implementation output on fresh runs.
                # When the task branch is already merged into the base branch,
                # _has_commits_ahead returns False (commits are reachable from
                # base), causing a false "no changes" block on retry.
                if not skip_phase1 and not svc._has_uncommitted_changes(impl_dir) and not svc._has_commits_ahead(impl_dir):
                    task.status = "blocked"
                    task.wait_state = None
                    task.error = "No file changes detected after implementation"
                    task.current_step = last_phase1_step or "implement"
                    task.metadata["pipeline_phase"] = last_phase1_step or "implement"
                    svc.container.tasks.upsert(task)
                    svc._finalize_run(task, run, status="blocked", summary="Blocked: no changes produced by implementation steps")
                    svc._emit_task_blocked(task)
                    return

            if not early_complete:
                svc._check_cancelled(task)
            review_passed = False
            if not early_complete and has_review and retry_from not in {"commit", svc._BEFORE_DONE_RESUME_STEP}:
                post_fix_validation_step = svc._select_post_fix_validation_step(steps)

                review_attempt = 0
                review_passed = False

                while review_attempt < max_review_attempts:
                    svc._check_cancelled(task)
                    review_attempt += 1
                    task.current_step = "review"
                    task.metadata["pipeline_phase"] = "review"
                    if review_attempt > 1:
                        task.metadata["review_history"] = svc._build_review_history_summary(task.id)
                    else:
                        task.metadata.pop("review_history", None)
                    svc.container.tasks.upsert(task)
                    if not self._ensure_workdoc_or_block(task, run, step="review"):
                        return
                    review_project_dir = worktree_dir or svc.container.project_dir
                    try:
                        svc._refresh_workdoc_with_diagnostics(task, review_project_dir)
                    except ValueError as exc:
                        svc._block_for_invalid_workdoc(task, run, step="review", detail=str(exc))
                        return
                    review_started = now_iso()
                    svc._heartbeat_execution_lease(task)
                    svc.container.tasks.upsert(task)
                    findings, review_result = svc._findings_from_result(task, review_attempt)
                    svc._heartbeat_execution_lease(task)
                    svc.container.tasks.upsert(task)
                    svc._defer_out_of_scope_review_findings(task, findings)
                    svc.container.tasks.upsert(task)
                    review_step_log: dict[str, object] = {
                        "step": "review",
                        "status": "ok",
                        "ts": now_iso(),
                        "started_at": review_started,
                    }
                    review_last_logs = task.metadata.get("last_logs") if isinstance(task.metadata, dict) else None
                    if isinstance(review_last_logs, dict):
                        for key in ("stdout_path", "stderr_path", "progress_path"):
                            if review_last_logs.get(key):
                                review_step_log[key] = review_last_logs[key]
                    if review_result.human_blocking_issues:
                        review_step_log["status"] = "blocked"
                        review_step_log["human_blocking_issues"] = review_result.human_blocking_issues
                        run.steps.append(review_step_log)
                        svc.container.runs.upsert(run)
                        svc._block_for_human_issues(
                            task,
                            run,
                            "review",
                            review_result.summary,
                            review_result.human_blocking_issues,
                        )
                        return
                    if review_result.status != "ok":
                        review_step_log["status"] = review_result.status or "error"
                        run.steps.append(review_step_log)
                        svc.container.runs.upsert(run)
                        if svc._handle_recoverable_environment_failure(
                            task,
                            run,
                            step="review",
                            summary=review_result.summary,
                        ):
                            return
                        task.status = "blocked"
                        task.error = review_result.summary or "Review step failed"
                        task.pending_gate = None
                        task.wait_state = None
                        task.current_step = "review"
                        task.metadata["pipeline_phase"] = "review"
                        svc.container.tasks.upsert(task)
                        svc._finalize_run(task, run, status="blocked", summary="Blocked during review")
                        svc._emit_task_blocked(task)
                        return
                    open_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
                    for finding in findings:
                        if finding.status == "open" and finding.severity in open_counts:
                            open_counts[finding.severity] += 1
                    cycle = ReviewCycle(
                        task_id=task.id,
                        attempt=review_attempt,
                        findings=findings,
                        open_counts=open_counts,
                        decision="changes_requested" if svc._exceeds_quality_gate(task, findings) else "approved",
                    )
                    if not self._ensure_workdoc_or_block(task, run, step="review"):
                        return
                    svc.container.reviews.append(cycle)
                    svc._sync_workdoc_review(task, cycle, worktree_dir or svc.container.project_dir)
                    review_step_log["status"] = cycle.decision
                    review_step_log["open_counts"] = open_counts
                    run.steps.append(review_step_log)
                    svc.container.runs.upsert(run)
                    svc.bus.emit(
                        channel="review",
                        event_type="task.reviewed",
                        entity_id=task.id,
                        payload={"attempt": review_attempt, "decision": cycle.decision, "open_counts": open_counts},
                    )
                    svc._clear_environment_recovery_tracking(task, step="review")
                    _consume_human_guidance("review")

                    if cycle.decision == "approved":
                        review_passed = True
                        break

                    if review_attempt >= max_review_attempts:
                        break

                    open_findings = [f.to_dict() for f in findings if f.status == "open"]
                    task.metadata["review_findings"] = open_findings
                    task.metadata["review_history"] = svc._build_review_history_summary(task.id)

                    task.retry_count += 1
                    task.current_step = "implement_fix"
                    task.metadata["pipeline_phase"] = "review"
                    svc.container.tasks.upsert(task)
                    review_fix_outcome = svc._run_non_review_step(task, run, "implement_fix", attempt=review_attempt)
                    if review_fix_outcome != "ok":
                        return
                    _consume_human_guidance("implement_fix")

                    if post_fix_validation_step:
                        task.current_step = post_fix_validation_step
                        task.metadata["pipeline_phase"] = "review"
                        svc.container.tasks.upsert(task)
                        validation_outcome = svc._run_non_review_step(task, run, post_fix_validation_step, attempt=review_attempt)
                        if validation_outcome != "ok":
                            if validation_outcome == "auto_requeued":
                                return
                            if validation_outcome == "verify_degraded":
                                task.current_step = "review"
                                task.metadata["pipeline_phase"] = "review"
                                svc.container.tasks.upsert(task)
                                continue
                            if validation_outcome != "verify_failed":
                                return
                            validation_fixed = False
                            for vfix in range(1, max_verify_fix_attempts + 1):
                                task.status = "in_progress"
                                task.metadata["verify_failure"] = task.error
                                svc._capture_verify_output(task)
                                task.error = None
                                task.retry_count += 1
                                svc.container.tasks.upsert(task)
                                run.status = "in_progress"
                                run.finished_at = None
                                run.summary = None
                                svc.container.runs.upsert(run)

                                task.current_step = "implement_fix"
                                task.metadata["pipeline_phase"] = "review"
                                svc.container.tasks.upsert(task)
                                validation_fix_outcome = svc._run_non_review_step(task, run, "implement_fix", attempt=vfix + 1)
                                if validation_fix_outcome != "ok":
                                    return
                                _consume_human_guidance("implement_fix")
                                task.metadata.pop("verify_failure", None)
                                task.metadata.pop("verify_output", None)
                                task.current_step = post_fix_validation_step
                                task.metadata["pipeline_phase"] = "review"
                                svc.container.tasks.upsert(task)
                                retry_validation_outcome = svc._run_non_review_step(task, run, post_fix_validation_step, attempt=vfix + 1)
                                if retry_validation_outcome == "ok":
                                    _consume_human_guidance(post_fix_validation_step)
                                    validation_fixed = True
                                    break
                                if retry_validation_outcome == "verify_degraded":
                                    validation_fixed = True
                                    break
                                if retry_validation_outcome == "auto_requeued":
                                    return
                                if retry_validation_outcome != "verify_failed":
                                    return
                            if not validation_fixed:
                                task.status = "blocked"
                                task.wait_state = None
                                task.error = task.error or f"Could not fix {post_fix_validation_step} after {max_verify_fix_attempts} attempts"
                                task.current_step = post_fix_validation_step
                                svc.container.tasks.upsert(task)
                                svc._finalize_run(task, run, status="blocked", summary=f"Blocked: {post_fix_validation_step} failed after {max_verify_fix_attempts} fix attempts")
                                svc._emit_task_blocked(task)
                                return
                        else:
                            _consume_human_guidance(post_fix_validation_step)

                    task.metadata.pop("review_findings", None)
                    task.metadata.pop("review_history", None)
                    task.metadata.pop("verify_environment_note", None)
                    task.metadata.pop("verify_environment_kind", None)

                if not review_passed:
                    task.metadata.pop("review_history", None)
                    task.metadata.pop("verify_environment_note", None)
                    task.metadata.pop("verify_environment_kind", None)
                    task.status = "blocked"
                    task.wait_state = None
                    task.error = "Review attempt cap exceeded"
                    task.current_step = "review"
                    task.metadata["pipeline_phase"] = "review"
                    svc.container.tasks.upsert(task)
                    svc._finalize_run(task, run, status="blocked", summary="Blocked due to unresolved review findings")
                    svc._emit_task_blocked(task)
                    return

            # -- Post-review steps (e.g. report after review findings exist) --
            if not early_complete and _post_review_step_set and review_passed:
                for step in steps:
                    if step not in _post_review_step_set:
                        continue
                    svc._check_cancelled(task)
                    task.current_step = step
                    task.metadata["pipeline_phase"] = step
                    svc.container.tasks.upsert(task)
                    if step in _ORCHESTRATOR_COMMENT_STEPS:
                        step_outcome = self._run_orchestrator_comment_step(task, run, step)
                    else:
                        step_outcome = svc._run_non_review_step(task, run, step, attempt=1)
                    if step_outcome not in ("ok", "no_action_needed"):
                        return
                    _consume_human_guidance(step)

            if not early_complete:
                svc._check_cancelled(task)
            if not early_complete and has_commit:
                # Supervised/review-only flows pause in in_review before commit so
                # footer review actions drive final approve/request-changes.
                precommit_review_modes = {"supervised", "review_only"}
                requires_precommit_review = mode in precommit_review_modes
                if requires_precommit_review and retry_from != "commit":
                    context_ok, context_reason = self._prepare_precommit_review_context(task, worktree_dir)
                    if not context_ok:
                        task.status = "blocked"
                        task.pending_gate = None
                        task.wait_state = None
                        task.current_step = "review"
                        task.current_agent_id = None
                        task.metadata["pipeline_phase"] = "review"
                        task.metadata.pop("pending_precommit_approval", None)
                        task.metadata.pop("review_stage", None)
                        detail = context_reason.strip()
                        task.error = "Failed to preserve task-scoped changes for pre-commit review"
                        if detail:
                            task.error = f"{task.error}: {detail}"
                        svc.container.tasks.upsert(task)
                        svc._finalize_run(task, run, status="blocked", summary="Blocked: pre-commit review context unavailable")
                        svc._emit_task_blocked(task)
                        return

                    # Context is now preserved on task branch; current worktree has
                    # already been removed by preserve_worktree_work.
                    worktree_dir = None
                    task.metadata.pop("worktree_dir", None)
                    task.metadata.pop("task_context", None)
                    svc._run_summarize_step(task, run, gate_context="pre_commit")
                    # Ensure a summary step exists so the frontend doesn't
                    # fall back to the stale plan-gate summary.
                    if not (run.steps and isinstance(run.steps[-1], dict) and run.steps[-1].get("step") == "summary"):
                        run.steps.append({
                            "step": "summary",
                            "status": "ok",
                            "ts": now_iso(),
                            "summary": "Implementation completed. Awaiting pre-commit approval.",
                        })
                    task.status = "in_review"
                    task.current_step = "review"
                    task.metadata["pipeline_phase"] = "review"
                    task.metadata["pending_precommit_approval"] = True
                    task.metadata["review_stage"] = "pre_commit"
                    svc.container.tasks.upsert(task)
                    run.accumulate_worker_seconds()
                    run.status = "in_review"
                    run.finished_at = now_iso()
                    # Use LLM-generated summary if available, fall back to static string
                    precommit_summary = None
                    if run.steps:
                        last = run.steps[-1]
                        if isinstance(last, dict) and last.get("step") == "summary":
                            precommit_summary = last.get("summary")
                    run.summary = precommit_summary or "Awaiting pre-commit approval"
                    svc.container.runs.upsert(run)
                    svc.bus.emit(
                        channel="review",
                        event_type="task.awaiting_human",
                        entity_id=task.id,
                        payload={"stage": "pre_commit"},
                    )
                    return

                task.metadata.pop("pending_precommit_approval", None)
                task.metadata.pop("review_stage", None)
                task.current_step = "commit"
                task.metadata["pipeline_phase"] = "commit"
                svc.container.tasks.upsert(task)
                if not self._ensure_workdoc_or_block(task, run, step="commit"):
                    return

                commit_started = now_iso()
                svc._cleanup_workdoc_for_commit(worktree_dir or svc.container.project_dir)
                commit_sha = svc._commit_for_task(task, worktree_dir)
                if not commit_sha:
                    # No new commit was created — check if the branch already
                    # carries prior committed work (e.g. from a preserved branch
                    # retry).  If so, use the branch HEAD as the commit ref so
                    # merge_and_cleanup can still merge it into the base branch.
                    commit_cwd = worktree_dir or svc.container.project_dir
                    if svc._has_commits_ahead(commit_cwd):
                        head_result = subprocess.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=commit_cwd,
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        commit_sha = head_result.stdout.strip() if head_result.returncode == 0 else None
                    if not commit_sha:
                        git_present = (commit_cwd / ".git").exists() or (svc.container.project_dir / ".git").exists()
                        if git_present:
                            task.status = "blocked"
                            task.wait_state = None
                            task.error = "Commit failed (no changes to commit)"
                            svc.container.tasks.upsert(task)
                            svc._finalize_run(task, run, status="blocked", summary="Blocked: commit produced no changes")
                            svc._emit_task_blocked(task)
                            return
                run.steps.append(
                    {
                        "step": "commit",
                        "status": "ok",
                        "ts": now_iso(),
                        "started_at": commit_started,
                        "commit": commit_sha,
                    }
                )
                svc.container.runs.upsert(run)
                _consume_human_guidance("commit")

                if worktree_dir:
                    # Capture base branch HEAD before merge so we can detect
                    # whether other tasks merged since our worktree was created.
                    pre_merge_base_sha = _get_current_head_sha(svc.container.project_dir)

                    merge_result = svc._merge_and_cleanup(task, worktree_dir)
                    merge_status = str((merge_result or {}).get("status") or "ok")
                    if merge_status == "ok":
                        worktree_dir = None

                        # Post-merge integration health check.
                        # Skip when the base branch hasn't moved since the
                        # worktree was created — full verify already covered
                        # that state.
                        base_branch_sha = (
                            (task.metadata.get("task_context") or {}).get("base_branch_sha") or ""
                        )
                        run_post_merge_check = not (
                            base_branch_sha and pre_merge_base_sha == base_branch_sha
                        )

                        if run_post_merge_check:
                            health_result = svc._integration_health.run_check(
                                trigger_task_id=task.id, force=True,
                            )
                            if health_result and not health_result.passed:
                                task.metadata["integration_health_degraded"] = True
                                task.metadata["integration_health_check"] = {
                                    "passed": False,
                                    "exit_code": health_result.exit_code,
                                    "ts": now_iso(),
                                    "trigger": "post_merge_divergence",
                                }

                merge_failure_reason = str(task.metadata.get("merge_failure_reason_code") or "").strip()
                if task.metadata.get("merge_conflict"):
                    task.status = "blocked"
                    task.wait_state = None
                    task.error = "Merge conflict could not be resolved automatically"
                    svc.container.tasks.upsert(task)
                    svc._finalize_run(task, run, status="blocked", summary="Blocked due to unresolved merge conflict")
                    svc._emit_task_blocked(task)
                    return
                if merge_failure_reason in {"dirty_overlapping", "git_error"}:
                    task.status = "blocked"
                    task.wait_state = None
                    if not str(task.error or "").strip():
                        if merge_failure_reason == "dirty_overlapping":
                            task.error = "Integration branch has local changes that overlap this merge"
                        else:
                            task.error = "Git merge failed before conflict resolution"
                    svc.container.tasks.upsert(task)
                    svc._finalize_run(task, run, status="blocked", summary=f"Blocked due to merge failure ({merge_failure_reason})")
                    svc._emit_task_blocked(task, payload={"error": task.error, "reason_code": merge_failure_reason})
                    return

                svc._run_summarize_step(task, run)
                task.status = "done"
                task.wait_state = None
                task.current_step = None
                task.metadata.pop("pipeline_phase", None)
                task.metadata.pop("pending_precommit_approval", None)
                task.metadata.pop("review_stage", None)
                run.status = "done"
                run.summary = "Pipeline completed"
                svc.bus.emit(
                    channel="tasks",
                    event_type="task.done",
                    entity_id=task.id,
                    payload={"commit": commit_sha},
                )
            elif not early_complete:
                requires_done_gate = svc._should_before_done_gate(task=task, mode=mode, has_commit=has_commit)
                if requires_done_gate and retry_from != svc._BEFORE_DONE_RESUME_STEP:
                    gate_resume_step = svc._BEFORE_DONE_RESUME_STEP
                    task.current_step = last_phase1_step
                    task.metadata["pipeline_phase"] = last_phase1_step
                    svc.container.tasks.upsert(task)
                    if not svc._wait_for_gate(task, "before_done", resume_step=gate_resume_step):
                        return

                if worktree_dir:
                    subprocess.run(
                        ["git", "worktree", "remove", str(worktree_dir), "--force"],
                        cwd=svc.container.project_dir,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    subprocess.run(
                        ["git", "branch", "-D", f"task-{task.id}"],
                        cwd=svc.container.project_dir,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    worktree_dir = None
                    task.metadata.pop("worktree_dir", None)
                    task.metadata.pop("task_context", None)

                svc._run_summarize_step(task, run)
                task.status = "done"
                task.wait_state = None
                task.current_step = None
                task.metadata.pop("pipeline_phase", None)
                run.status = "done"
                run.summary = "Pipeline completed"
                svc.bus.emit(channel="tasks", event_type="task.done", entity_id=task.id, payload={})

            task.error = None
            task.metadata.pop("execution_checkpoint", None)
            task.metadata.pop("step_outputs", None)
            task.metadata.pop("worktree_dir", None)
            task.metadata.pop("task_context", None)
            task.metadata.pop("recommended_action", None)
            task.metadata.pop("early_complete", None)
            run.accumulate_worker_seconds()
            run.finished_at = now_iso()
            with svc.container.transaction():
                svc.container.runs.upsert(run)
                svc.container.tasks.upsert(task)
        finally:
            latest = svc.container.tasks.get(task.id)
            if latest is not None:
                task = latest

            worktree_removed = False
            metadata_changed = False
            exception_in_flight = sys.exc_info()[1] is not None
            if worktree_dir and worktree_dir.exists():
                keep_active_context = task.status in {"in_progress", "in_review"} or (
                    task.status == "queued" and bool(task.metadata.get("environment_auto_requeue_pending"))
                )
                if task.status == "blocked" or exception_in_flight:
                    task.metadata["worktree_dir"] = str(worktree_dir)
                    svc._record_task_context(task, worktree_dir=worktree_dir, task_branch=f"task-{task.id}")
                    svc._mark_task_context_retained(
                        task,
                        reason=str(task.error or ("unexpected_exception" if exception_in_flight else "blocked")),
                        expected_on_retry=True,
                    )
                    metadata_changed = True
                elif keep_active_context:
                    # Keep task context for active non-terminal states (for example
                    # gate waits). This avoids deleting context/branch state while
                    # the task is still expected to continue.
                    task.metadata["worktree_dir"] = str(worktree_dir)
                    svc._record_task_context(task, worktree_dir=worktree_dir, task_branch=f"task-{task.id}")
                    svc._clear_task_context_retained(task)
                    metadata_changed = True
                else:
                    subprocess.run(
                        ["git", "worktree", "remove", str(worktree_dir), "--force"],
                        cwd=svc.container.project_dir,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    worktree_removed = True
                    if task.metadata.pop("worktree_dir", None):
                        metadata_changed = True
                    if isinstance(task.metadata.get("task_context"), dict):
                        task.metadata.pop("task_context", None)
                        metadata_changed = True
                    if task.status in {"done", "cancelled"}:
                        subprocess.run(
                            ["git", "branch", "-D", f"task-{task.id}"],
                            cwd=svc.container.project_dir,
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )

            if task.status == "cancelled":
                cancel_cleanup = svc._cleanup_cancelled_task_context(task, force=True)
                if any(
                    bool(cancel_cleanup.get(key))
                    for key in ("metadata_changed", "worktree_removed", "branch_deleted", "lease_released")
                ):
                    metadata_changed = True
                worktree_removed = worktree_removed or bool(cancel_cleanup.get("worktree_removed"))
            elif task.status == "done" and task.metadata.get("worktree_dir"):
                task.metadata.pop("worktree_dir", None)
                metadata_changed = True

            lease_removed = svc._release_execution_lease(task)
            if worktree_removed or lease_removed or metadata_changed:
                svc.container.tasks.upsert(task)
