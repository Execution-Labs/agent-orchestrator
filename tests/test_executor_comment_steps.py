"""Tests for orchestrator-side comment steps in TaskExecutor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from overdrive.comments.models import CommentPostResult, PRComment
from overdrive.comments.reader import CommentFetchError
from overdrive.runtime.domain.models import RunRecord, Task, now_iso
from overdrive.runtime.orchestrator.task_executor import (
    TaskExecutor,
    _extract_diff_file_paths,
    _should_preserve_step_outputs,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROJECT_DIR = Path("/tmp/fake-project")


def _make_service_mock() -> MagicMock:
    """Create a mock OrchestratorService with required attributes."""
    svc = MagicMock()
    svc.container.project_dir = _PROJECT_DIR
    svc.step_project_dir.return_value = _PROJECT_DIR
    svc._workdoc_canonical_path.return_value = _PROJECT_DIR / ".workdoc.md"
    svc._emit_task_blocked = MagicMock()
    svc.bus.emit = MagicMock()
    return svc


def _make_task(metadata: dict[str, Any] | None = None) -> Task:
    """Create a task with sensible defaults for comment step tests."""
    return Task(
        id="task-test-001",
        title="Test PR Review",
        task_type="pr_review_comment",
        status="in_progress",
        metadata=metadata or {},
    )


def _make_run(task_id: str = "task-test-001") -> RunRecord:
    return RunRecord(task_id=task_id, status="in_progress", started_at=now_iso(), steps=[])


def _sample_comments(count: int = 3) -> list[PRComment]:
    return [
        PRComment(
            id=f"comment-{i}",
            author=f"user-{i}",
            body=f"Comment body {i}",
            path="src/main.py" if i % 2 == 0 else None,
            line=(10 + i) if i % 2 == 0 else None,
            platform_id=str(100 + i),
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# fetch_comments
# ---------------------------------------------------------------------------


class TestExecuteFetchComments:
    def _make_executor(self) -> tuple[TaskExecutor, MagicMock]:
        svc = _make_service_mock()
        executor = TaskExecutor(svc)
        return executor, svc

    @patch("overdrive.runtime.orchestrator.task_executor.fetch_pr_comments")
    def test_success_github(self, mock_fetch: MagicMock) -> None:
        comments = _sample_comments(3)
        mock_fetch.return_value = comments
        executor, svc = self._make_executor()
        task = _make_task({"source_url": "https://github.com/owner/repo/pull/42"})
        run = _make_run()

        result = executor._execute_fetch_comments(task, run)

        assert result == "ok"
        assert len(task.metadata["fetched_comments"]) == 3
        assert task.metadata["formatted_comments"] != ""
        assert task.metadata["comment_platform"]["platform"] == "github"
        assert task.metadata["comment_platform"]["number"] == 42
        # Step recorded in run.
        assert len(run.steps) == 1
        assert run.steps[0]["step"] == "fetch_comments"
        assert run.steps[0]["status"] == "ok"
        assert run.steps[0]["comment_count"] == 3

    @patch("overdrive.runtime.orchestrator.task_executor.fetch_mr_comments")
    def test_success_gitlab(self, mock_fetch: MagicMock) -> None:
        comments = _sample_comments(2)
        mock_fetch.return_value = comments
        executor, svc = self._make_executor()
        task = _make_task({"source_url": "https://gitlab.com/group/project/-/merge_requests/10"})
        run = _make_run()

        result = executor._execute_fetch_comments(task, run)

        assert result == "ok"
        assert task.metadata["comment_platform"]["platform"] == "gitlab"
        assert len(task.metadata["fetched_comments"]) == 2

    @patch("overdrive.runtime.orchestrator.task_executor.fetch_pr_comments")
    def test_empty_comments(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = []
        executor, svc = self._make_executor()
        task = _make_task({"source_url": "https://github.com/o/r/pull/1"})
        run = _make_run()

        result = executor._execute_fetch_comments(task, run)

        assert result == "ok"
        assert task.metadata["fetched_comments"] == []
        assert task.metadata["formatted_comments"] == ""

    @patch("overdrive.runtime.orchestrator.task_executor.fetch_pr_comments")
    def test_fetch_error_blocks(self, mock_fetch: MagicMock) -> None:
        mock_fetch.side_effect = CommentFetchError("API error")
        executor, svc = self._make_executor()
        task = _make_task({"source_url": "https://github.com/o/r/pull/1"})
        run = _make_run()

        result = executor._execute_fetch_comments(task, run)

        assert result == "blocked"
        assert task.status == "blocked"
        assert "API error" in (task.error or "")
        assert run.steps[0]["status"] == "error"
        svc._emit_task_blocked.assert_called_once()

    def test_missing_source_url_blocks(self) -> None:
        executor, svc = self._make_executor()
        task = _make_task({})  # No source_url, no source_pr_number
        run = _make_run()

        result = executor._execute_fetch_comments(task, run)

        assert result == "blocked"
        assert task.status == "blocked"
        assert "source_url" in (task.error or "").lower() or "source" in (task.error or "").lower()

    @patch("overdrive.runtime.orchestrator.task_executor.fetch_pr_comments")
    def test_fallback_to_pr_number(self, mock_fetch: MagicMock) -> None:
        comments = _sample_comments(1)
        mock_fetch.return_value = comments
        executor, svc = self._make_executor()

        # Mock the git remote inference.
        with patch.object(
            executor, "_infer_github_owner_repo", return_value=("inferred-owner", "inferred-repo")
        ):
            task = _make_task({"source_pr_number": 99})
            run = _make_run()

            result = executor._execute_fetch_comments(task, run)

        assert result == "ok"
        mock_fetch.assert_called_once_with("inferred-owner", "inferred-repo", 99, _PROJECT_DIR)


# ---------------------------------------------------------------------------
# post_comments
# ---------------------------------------------------------------------------


class TestExecutePostComments:
    def _make_executor(self) -> tuple[TaskExecutor, MagicMock]:
        svc = _make_service_mock()
        # workdoc doesn't exist in test env — prevent file write errors.
        svc._workdoc_canonical_path.return_value = Path("/tmp/nonexistent-workdoc.md")
        executor = TaskExecutor(svc)
        return executor, svc

    @patch("overdrive.runtime.orchestrator.task_executor.post_comments_batch")
    def test_success(self, mock_batch: MagicMock) -> None:
        mock_batch.return_value = [
            CommentPostResult(success=True, platform_id="1001"),
            CommentPostResult(success=True, platform_id="1002"),
        ]
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "comments": [
                    {"path": "src/a.py", "line": 10, "body": "Fix this", "severity": "high"},
                    {"body": "General note", "severity": "low"},
                ],
                "summary": "Found 2 issues",
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_comment": worker_output},
                "comment_dry_run": False,
            }
        )
        run = _make_run()

        result = executor._execute_post_comments(task, run)

        assert result == "ok"
        assert len(task.metadata["posted_comments"]) == 2
        assert run.steps[0]["posted_count"] == 2
        assert run.steps[0]["failed_count"] == 0
        assert run.steps[0]["comment_dry_run"] is False
        # Verify post_status on results.
        for r in task.metadata["posted_comments"]:
            assert r["post_status"] == "posted"

    def test_dry_run_by_default(self) -> None:
        """No comment_dry_run in metadata → dry-run behavior (default True)."""
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "comments": [{"body": "Comment 1"}],
                "summary": "Summary",
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_comment": worker_output},
                # No comment_dry_run key — should default to True.
            }
        )
        run = _make_run()

        result = executor._execute_post_comments(task, run)

        assert result == "ok"
        assert run.steps[0]["comment_dry_run"] is True
        assert run.steps[0]["staged_count"] == 1
        assert run.steps[0]["posted_count"] == 0
        assert task.metadata["posted_comments"][0]["platform_id"] == "dry_run"
        assert task.metadata["posted_comments"][0]["post_status"] == "staged"

    def test_dry_run_explicit(self) -> None:
        """Explicit comment_dry_run=True produces staged results."""
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "comments": [{"body": "Comment 1", "path": "src/foo.py", "line": 5}],
                "summary": "Summary",
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_comment": worker_output},
                "comment_dry_run": True,
            }
        )
        run = _make_run()

        result = executor._execute_post_comments(task, run)

        assert result == "ok"
        assert run.steps[0]["comment_dry_run"] is True
        assert task.metadata["posted_comments"][0]["platform_id"] == "dry_run"
        assert task.metadata["posted_comments"][0]["post_status"] == "staged"
        # Comment data attached for workdoc.
        assert task.metadata["posted_comments"][0]["_comment_body"] == "Comment 1"
        assert task.metadata["posted_comments"][0]["_comment_path"] == "src/foo.py"
        assert task.metadata["posted_comments"][0]["_comment_line"] == 5

    @patch("overdrive.runtime.orchestrator.task_executor.post_comments_batch")
    def test_live_when_explicitly_false(self, mock_batch: MagicMock) -> None:
        """comment_dry_run=False → calls post_comments_batch, results have post_status='posted'."""
        mock_batch.return_value = [CommentPostResult(success=True, platform_id="1001")]
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "comments": [{"body": "Fix", "path": "a.py", "line": 1}],
                "summary": "S",
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_comment": worker_output},
                "comment_dry_run": False,
            }
        )
        run = _make_run()

        result = executor._execute_post_comments(task, run)

        assert result == "ok"
        mock_batch.assert_called_once()
        assert task.metadata["posted_comments"][0]["post_status"] == "posted"

    @patch("overdrive.runtime.orchestrator.task_executor.post_comments_batch")
    def test_gitlab_live_passes_diff_context(self, mock_batch: MagicMock) -> None:
        mock_batch.return_value = [CommentPostResult(success=True, platform_id="2001")]
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "comments": [{"body": "Fix", "path": "a.py", "line": 1}],
                "summary": "S",
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "gitlab", "project_id": "g%2Fr", "number": 5},
                "step_outputs": {"pr_review_comment": worker_output},
                "comment_dry_run": False,
                "source_diff": "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
                "source_diff_refs": {
                    "base_sha": "base123",
                    "start_sha": "start123",
                    "head_sha": "head123",
                },
            }
        )
        run = _make_run()

        result = executor._execute_post_comments(task, run)

        assert result == "ok"
        mock_batch.assert_called_once()
        call_args = mock_batch.call_args
        assert call_args.kwargs["source_diff"] == task.metadata["source_diff"]
        assert call_args.kwargs["gitlab_diff_refs"] == task.metadata["source_diff_refs"]

    @patch("overdrive.runtime.orchestrator.task_executor.post_comments_batch")
    def test_gitlab_unified_diff_keeps_matching_inline_comments(
        self, mock_batch: MagicMock
    ) -> None:
        """GitLab unified diffs should not cause valid inline comments to be dropped."""
        mock_batch.return_value = [CommentPostResult(success=True, platform_id="discussion-1")]
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "comments": [{"body": "Fix", "path": "src/example.py", "line": 10}],
                "summary": "S",
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "gitlab", "project_id": "g%2Fr", "number": 5},
                "step_outputs": {"pr_review_comment": worker_output},
                "comment_dry_run": False,
                "source_diff": "--- src/example.py\n+++ src/example.py\n@@ -9,1 +9,2 @@\n context\n+new\n",
                "source_diff_refs": {
                    "base_sha": "base123",
                    "start_sha": "start123",
                    "head_sha": "head123",
                },
            }
        )
        run = _make_run()

        result = executor._execute_post_comments(task, run)

        assert result == "ok"
        assert task.metadata["generated_review_comments"] == [
            {"path": "src/example.py", "line": 10, "body": "Fix", "severity": "medium", "post_status": "staged"},
        ]
        mock_batch.assert_called_once()

    @patch("overdrive.runtime.orchestrator.task_executor.post_comments_batch")
    def test_truncated_diff_does_not_drop_generated_comments(
        self, mock_batch: MagicMock
    ) -> None:
        mock_batch.return_value = [CommentPostResult(success=True, platform_id="discussion-1")]
        executor, _svc = self._make_executor()
        worker_output = json.dumps(
            {
                "comments": [{"body": "Fix", "path": "src/missing.py", "line": 10}],
                "summary": "S",
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "gitlab", "project_id": "g%2Fr", "number": 5},
                "step_outputs": {"pr_review_comment": worker_output},
                "comment_dry_run": False,
                "source_diff": "diff --git a/src/other.py b/src/other.py\n\n[DIFF TRUNCATED — exceeded 100K character limit.]",
                "source_diff_refs": {
                    "base_sha": "base123",
                    "start_sha": "start123",
                    "head_sha": "head123",
                },
            }
        )
        run = _make_run()

        result = executor._execute_post_comments(task, run)

        assert result == "ok"
        assert task.metadata["generated_review_comments"] == [
            {"path": "src/missing.py", "line": 10, "body": "Fix", "severity": "medium", "post_status": "staged"},
        ]
        mock_batch.assert_called_once()
        assert mock_batch.call_args.args[1] == [{"body": "Fix", "path": "src/missing.py", "line": 10}]

    @patch("overdrive.runtime.orchestrator.task_executor.post_comments_batch")
    def test_live_partial_failure_status(self, mock_batch: MagicMock) -> None:
        """Mixed success/failure → correct post_status values."""
        mock_batch.return_value = [
            CommentPostResult(success=True, platform_id="1001"),
            CommentPostResult(success=False, error="rate limited"),
        ]
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "comments": [{"body": "A"}, {"body": "B"}],
                "summary": "Summary",
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_comment": worker_output},
                "comment_dry_run": False,
            }
        )
        run = _make_run()

        result = executor._execute_post_comments(task, run)

        assert result == "ok"
        assert task.metadata["posted_comments"][0]["post_status"] == "posted"
        assert task.metadata["posted_comments"][1]["post_status"] == "failed"
        assert run.steps[0]["posted_count"] == 1
        assert run.steps[0]["failed_count"] == 1

    @patch("overdrive.runtime.orchestrator.task_executor.post_comments_batch")
    def test_total_failure_blocks(self, mock_batch: MagicMock) -> None:
        mock_batch.return_value = [
            CommentPostResult(success=False, error="401 Unauthorized"),
        ]
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "comments": [{"body": "Comment 1"}],
                "summary": "Summary",
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_comment": worker_output},
                "comment_dry_run": False,
            }
        )
        run = _make_run()

        result = executor._execute_post_comments(task, run)

        assert result == "blocked"
        assert task.status == "blocked"
        svc._emit_task_blocked.assert_called_once()

    @patch("overdrive.runtime.orchestrator.task_executor.post_comments_batch")
    def test_partial_failure_ok(self, mock_batch: MagicMock) -> None:
        mock_batch.return_value = [
            CommentPostResult(success=True, platform_id="1001"),
            CommentPostResult(success=False, error="rate limited"),
        ]
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "comments": [{"body": "A"}, {"body": "B"}],
                "summary": "Summary",
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_comment": worker_output},
                "comment_dry_run": False,
            }
        )
        run = _make_run()

        result = executor._execute_post_comments(task, run)

        assert result == "ok"
        assert run.steps[0]["posted_count"] == 1
        assert run.steps[0]["failed_count"] == 1

    def test_missing_platform_blocks(self) -> None:
        executor, svc = self._make_executor()
        task = _make_task({"step_outputs": {"pr_review_comment": "{}"}})
        run = _make_run()

        result = executor._execute_post_comments(task, run)

        assert result == "blocked"
        assert "comment_platform" in (task.error or "")

    def test_malformed_output_blocks(self) -> None:
        executor, svc = self._make_executor()
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_comment": "not valid json"},
            }
        )
        run = _make_run()

        result = executor._execute_post_comments(task, run)

        assert result == "blocked"
        assert "parse" in (task.error or "").lower()

    @patch("overdrive.runtime.orchestrator.task_executor.post_comments_batch")
    @patch("overdrive.runtime.orchestrator.task_executor.post_pr_review_decision")
    def test_review_decision_posted(self, mock_decision: MagicMock, mock_batch: MagicMock) -> None:
        mock_batch.return_value = [CommentPostResult(success=True, platform_id="1001")]
        mock_decision.return_value = CommentPostResult(success=True, platform_id="2001")
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "comments": [{"body": "Issue"}],
                "summary": "Needs changes",
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_comment": worker_output},
                "review_decision": {"decision": "request_changes", "body": "Please fix"},
                "comment_dry_run": False,
            }
        )
        run = _make_run()

        result = executor._execute_post_comments(task, run)

        assert result == "ok"
        mock_decision.assert_called_once()
        assert task.metadata["review_decision_result"]["success"] is True

    def test_review_decision_skipped_in_dry_run(self) -> None:
        """Review decision is not posted when comment_dry_run defaults to True."""
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "comments": [{"body": "Issue"}],
                "summary": "Needs changes",
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_comment": worker_output},
                "review_decision": {"decision": "request_changes", "body": "Please fix"},
                # No comment_dry_run — defaults to True.
            }
        )
        run = _make_run()

        with patch(
            "overdrive.runtime.orchestrator.task_executor.post_pr_review_decision"
        ) as mock_decision:
            result = executor._execute_post_comments(task, run)

        assert result == "ok"
        mock_decision.assert_not_called()
        assert "review_decision_result" not in task.metadata

    def test_post_status_field_in_metadata(self) -> None:
        """Verify task.metadata['posted_comments'] entries have post_status."""
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "comments": [{"body": "A"}, {"body": "B"}],
                "summary": "S",
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_comment": worker_output},
            }
        )
        run = _make_run()

        executor._execute_post_comments(task, run)

        for entry in task.metadata["posted_comments"]:
            assert "post_status" in entry
            assert entry["post_status"] == "staged"

    def test_workdoc_contains_comment_details(self, tmp_path: Path) -> None:
        """Verify workdoc file contains comment body previews and status badges."""
        svc = _make_service_mock()
        workdoc = tmp_path / ".workdoc.md"
        workdoc.write_text("# Task\n\n## Implementation Log\n\n_Pending_\n", encoding="utf-8")
        svc._workdoc_canonical_path.return_value = workdoc
        executor = TaskExecutor(svc)

        worker_output = json.dumps(
            {
                "comments": [
                    {"body": "Fix this issue", "path": "src/main.py", "line": 42},
                    {"body": "General note"},
                ],
                "summary": "Found 2 issues",
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_comment": worker_output},
            }
        )
        run = _make_run()

        executor._execute_post_comments(task, run)

        content = workdoc.read_text(encoding="utf-8")
        assert "## Posted Comments" in content
        assert "dry run" in content
        assert "`staged`" in content
        assert "`src/main.py` L42" in content
        assert "Fix this issue" in content
        assert "General note" in content
        assert "2 staged" in content

    def test_workdoc_replaced_on_rerun(self, tmp_path: Path) -> None:
        """Run twice → only one ## Posted Comments section exists."""
        svc = _make_service_mock()
        workdoc = tmp_path / ".workdoc.md"
        workdoc.write_text("# Task\n\n## Implementation Log\n\n_Pending_\n", encoding="utf-8")
        svc._workdoc_canonical_path.return_value = workdoc
        executor = TaskExecutor(svc)

        worker_output = json.dumps(
            {
                "comments": [{"body": "Comment"}],
                "summary": "S",
            }
        )
        meta = {
            "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
            "step_outputs": {"pr_review_comment": worker_output},
        }

        # First run.
        task = _make_task(dict(meta))
        run = _make_run()
        executor._execute_post_comments(task, run)

        # Second run.
        task2 = _make_task(dict(meta))
        run2 = _make_run()
        executor._execute_post_comments(task2, run2)

        content = workdoc.read_text(encoding="utf-8")
        assert content.count("## Posted Comments") == 1

    def test_empty_comments_workdoc(self, tmp_path: Path) -> None:
        """Empty generated_comments → workdoc section still written with '0 comments' summary."""
        svc = _make_service_mock()
        workdoc = tmp_path / ".workdoc.md"
        workdoc.write_text("# Task\n\n## Implementation Log\n\n_Pending_\n", encoding="utf-8")
        svc._workdoc_canonical_path.return_value = workdoc
        executor = TaskExecutor(svc)

        worker_output = json.dumps({"comments": [], "summary": ""})
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_comment": worker_output},
            }
        )
        run = _make_run()

        executor._execute_post_comments(task, run)

        content = workdoc.read_text(encoding="utf-8")
        assert "## Posted Comments" in content
        assert "0 comments" in content


class TestCommentStepHelpers:
    def test_extract_diff_file_paths_supports_git_diff_headers(self) -> None:
        diff_text = "diff --git a/src/a.py b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n"

        assert _extract_diff_file_paths(diff_text) == {"src/a.py"}

    def test_extract_diff_file_paths_supports_unified_diff_headers(self) -> None:
        diff_text = "--- src/example.py\n+++ src/example.py\n@@ -9,1 +9,2 @@\n context\n+new\n"

        assert _extract_diff_file_paths(diff_text) == {"src/example.py"}

    def test_should_preserve_step_outputs_for_review_comment_tasks(self) -> None:
        task = _make_task(
            {
                "step_outputs": {"pr_review_comment": '{"comments":[{"body":"Fix"}]}'},
            }
        )

        assert _should_preserve_step_outputs(task) is True

    def test_should_preserve_step_outputs_for_fix_respond_tasks(self) -> None:
        task = Task(
            id="task-test-002",
            title="Test Fix Respond",
            task_type="pr_review_fix_respond",
            status="in_progress",
            metadata={"step_outputs": {"pr_review_fix_respond": '{"addressed_comments":[{"id":"c1"}]}'}},
        )

        assert _should_preserve_step_outputs(task) is True

    def test_should_not_preserve_step_outputs_without_review_output(self) -> None:
        task = _make_task(
            {
                "review_comments_preview": [{"path": "src/example.py", "line": 10, "body": "Fix"}],
            }
        )

        assert _should_preserve_step_outputs(task) is False


# ---------------------------------------------------------------------------
# post_comment_responses
# ---------------------------------------------------------------------------


class TestExecutePostCommentResponses:
    def _make_executor(self) -> tuple[TaskExecutor, MagicMock]:
        svc = _make_service_mock()
        svc._workdoc_canonical_path.return_value = Path("/tmp/nonexistent-workdoc.md")
        executor = TaskExecutor(svc)
        return executor, svc

    @patch("overdrive.runtime.orchestrator.task_executor.post_comments_batch")
    def test_success(self, mock_batch: MagicMock) -> None:
        mock_batch.return_value = [CommentPostResult(success=True, platform_id="3001")]
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "addressed_comments": [
                    {"original_comment_id": "comment-0", "response_body": "Fixed in abc123"},
                ],
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_fix_respond": worker_output},
                "fetched_comments": [
                    {"id": "comment-0", "platform_id": "100", "author": "user", "body": "Fix this"},
                ],
                "comment_dry_run": False,
            }
        )
        run = _make_run()

        result = executor._execute_post_comment_responses(task, run)

        assert result == "ok"
        assert len(task.metadata["posted_responses"]) == 1
        assert run.steps[0]["posted_count"] == 1
        assert task.metadata["posted_responses"][0]["post_status"] == "posted"
        # Verify in_reply_to was resolved to platform_id 100.
        call_args = mock_batch.call_args
        posted_comments = call_args[0][1]
        assert posted_comments[0]["in_reply_to"] == 100

    def test_dry_run_by_default(self) -> None:
        """No comment_dry_run in metadata → dry-run behavior, staged status."""
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "addressed_comments": [
                    {"original_comment_id": "comment-0", "response_body": "Done"},
                ],
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_fix_respond": worker_output},
                "fetched_comments": [{"id": "comment-0", "platform_id": "100"}],
                # No comment_dry_run — defaults to True.
            }
        )
        run = _make_run()

        result = executor._execute_post_comment_responses(task, run)

        assert result == "ok"
        assert run.steps[0]["comment_dry_run"] is True
        assert task.metadata["posted_responses"][0]["platform_id"] == "dry_run"
        assert task.metadata["posted_responses"][0]["post_status"] == "staged"

    def test_dry_run_explicit(self) -> None:
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "addressed_comments": [
                    {"original_comment_id": "comment-0", "response_body": "Done"},
                ],
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_fix_respond": worker_output},
                "fetched_comments": [{"id": "comment-0", "platform_id": "100"}],
                "comment_dry_run": True,
            }
        )
        run = _make_run()

        result = executor._execute_post_comment_responses(task, run)

        assert result == "ok"
        assert run.steps[0]["comment_dry_run"] is True
        assert task.metadata["posted_responses"][0]["platform_id"] == "dry_run"
        assert task.metadata["posted_responses"][0]["post_status"] == "staged"
        assert task.metadata["posted_responses"][0]["_comment_body"] == "Done"
        assert task.metadata["posted_responses"][0]["_original_comment_id"] == "comment-0"

    @patch("overdrive.runtime.orchestrator.task_executor.post_comments_batch")
    def test_live_status_tracking(self, mock_batch: MagicMock) -> None:
        """comment_dry_run=False → posts replies, results have correct post_status."""
        mock_batch.return_value = [CommentPostResult(success=True, platform_id="3001")]
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "addressed_comments": [
                    {"original_comment_id": "comment-0", "response_body": "Fixed"},
                ],
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_fix_respond": worker_output},
                "fetched_comments": [{"id": "comment-0", "platform_id": "100"}],
                "comment_dry_run": False,
            }
        )
        run = _make_run()

        result = executor._execute_post_comment_responses(task, run)

        assert result == "ok"
        assert task.metadata["posted_responses"][0]["post_status"] == "posted"

    @patch("overdrive.runtime.orchestrator.task_executor.post_comments_batch")
    def test_gitlab_reply_uses_discussion_id_when_available(self, mock_batch: MagicMock) -> None:
        mock_batch.return_value = [CommentPostResult(success=True, platform_id="3001")]
        executor, _svc = self._make_executor()
        worker_output = json.dumps(
            {
                "addressed_comments": [
                    {"original_comment_id": "comment-0", "response_body": "Fixed"},
                ],
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "gitlab", "project_id": "g%2Fr", "number": 5},
                "step_outputs": {"pr_review_fix_respond": worker_output},
                "fetched_comments": [
                    {"id": "comment-0", "platform_id": "100", "discussion_id": "discussion-abc"}
                ],
                "comment_dry_run": False,
            }
        )
        run = _make_run()

        result = executor._execute_post_comment_responses(task, run)

        assert result == "ok"
        call_args = mock_batch.call_args
        posted_comments = call_args[0][1]
        assert posted_comments[0]["discussion_id"] == "discussion-abc"
        assert posted_comments[0]["in_reply_to"] == 100

    def test_missing_original_comment_posts_as_top_level(self) -> None:
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "addressed_comments": [
                    {"original_comment_id": "nonexistent-id", "response_body": "Reply"},
                ],
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_fix_respond": worker_output},
                "fetched_comments": [],  # No matching comment.
                "comment_dry_run": True,
            }
        )
        run = _make_run()

        result = executor._execute_post_comment_responses(task, run)

        # Should still succeed (staged in dry_run mode).
        assert result == "ok"
        assert run.steps[0]["staged_count"] == 1
        assert run.steps[0]["posted_count"] == 0

    def test_skips_empty_response_body(self) -> None:
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "addressed_comments": [
                    {"original_comment_id": "comment-0", "response_body": ""},
                    {"original_comment_id": "comment-1", "response_body": "Fixed"},
                ],
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_fix_respond": worker_output},
                "fetched_comments": [
                    {"id": "comment-0", "platform_id": "100"},
                    {"id": "comment-1", "platform_id": "101"},
                ],
                "comment_dry_run": True,
            }
        )
        run = _make_run()

        result = executor._execute_post_comment_responses(task, run)

        assert result == "ok"
        assert run.steps[0]["skipped_count"] == 1
        assert run.steps[0]["staged_count"] == 1
        assert run.steps[0]["posted_count"] == 0

    @patch("overdrive.runtime.orchestrator.task_executor.post_comments_batch")
    def test_total_failure_blocks(self, mock_batch: MagicMock) -> None:
        mock_batch.return_value = [CommentPostResult(success=False, error="403")]
        executor, svc = self._make_executor()
        worker_output = json.dumps(
            {
                "addressed_comments": [
                    {"original_comment_id": "comment-0", "response_body": "Done"},
                ],
            }
        )
        task = _make_task(
            {
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
                "step_outputs": {"pr_review_fix_respond": worker_output},
                "fetched_comments": [{"id": "comment-0", "platform_id": "100"}],
                "comment_dry_run": False,
            }
        )
        run = _make_run()

        result = executor._execute_post_comment_responses(task, run)

        assert result == "blocked"
        assert task.status == "blocked"
        svc._emit_task_blocked.assert_called_once()

    def test_missing_platform_blocks(self) -> None:
        executor, svc = self._make_executor()
        task = _make_task(
            {
                "step_outputs": {"pr_review_fix_respond": "{}"},
            }
        )
        run = _make_run()

        result = executor._execute_post_comment_responses(task, run)

        assert result == "blocked"
        assert "comment_platform" in (task.error or "")


# ---------------------------------------------------------------------------
# CommentPostResult.post_status
# ---------------------------------------------------------------------------


class TestCommentPostResultPostStatus:
    def test_default_is_staged(self) -> None:
        r = CommentPostResult()
        assert r.post_status == "staged"

    def test_to_dict_includes_post_status(self) -> None:
        r = CommentPostResult(success=True, post_status="posted")
        assert r.to_dict()["post_status"] == "posted"

    def test_from_dict_preserves_post_status(self) -> None:
        d = {"success": True, "platform_id": "123", "post_status": "failed"}
        r = CommentPostResult.from_dict(d)
        assert r.post_status == "failed"

    def test_from_dict_defaults_to_staged(self) -> None:
        d = {"success": True, "platform_id": "123"}
        r = CommentPostResult.from_dict(d)
        assert r.post_status == "staged"

    def test_round_trip(self) -> None:
        original = CommentPostResult(success=True, platform_id="abc", post_status="posted")
        restored = CommentPostResult.from_dict(original.to_dict())
        assert restored.post_status == original.post_status
        assert restored.success == original.success
        assert restored.platform_id == original.platform_id
