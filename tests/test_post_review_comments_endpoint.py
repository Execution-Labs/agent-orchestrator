"""Tests for POST /api/tasks/{task_id}/post-review-comments endpoint."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from overdrive.comments.models import CommentPostResult
from overdrive.runtime.domain.models import Task
from overdrive.runtime.storage.container import Container
from overdrive.server.api import create_app


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True, text=True)
    (path / "README.md").write_text("# init\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True, text=True)


def _client_and_container(tmp_path: Path) -> tuple[TestClient, Container]:
    _git_init(tmp_path)
    app = create_app(project_dir=str(tmp_path))
    client = TestClient(app)
    # Trigger lazy container init by hitting any endpoint
    client.get("/api/settings")
    key = str(tmp_path.resolve())
    container = app.state.containers[key]
    return client, container


def _create_task(container: Container, **kwargs: Any) -> Task:
    task = Task(**kwargs)
    container.tasks.upsert(task)
    return task


class TestPostReviewComments:
    """Tests for POST /api/tasks/{task_id}/post-review-comments."""

    def test_missing_task_returns_404(self, tmp_path: Path) -> None:
        client, _ = _client_and_container(tmp_path)
        resp = client.post("/api/tasks/nonexistent/post-review-comments")
        assert resp.status_code == 404

    def test_non_dry_run_task_returns_409(self, tmp_path: Path) -> None:
        client, container = _client_and_container(tmp_path)
        task = _create_task(
            container,
            title="Review PR",
            task_type="pr_review",
            status="done",
            metadata={
                "comment_dry_run": False,
                "generated_review_comments": [{"path": "f.py", "line": 1, "body": "Fix", "severity": "medium"}],
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
            },
        )
        resp = client.post(f"/api/tasks/{task.id}/post-review-comments")
        assert resp.status_code == 409
        assert "not a dry-run" in resp.json()["detail"]

    def test_no_generated_comments_returns_409(self, tmp_path: Path) -> None:
        client, container = _client_and_container(tmp_path)
        task = _create_task(
            container,
            title="Review PR",
            task_type="pr_review",
            status="done",
            metadata={
                "comment_dry_run": True,
                "generated_review_comments": [],
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
            },
        )
        resp = client.post(f"/api/tasks/{task.id}/post-review-comments")
        assert resp.status_code == 409
        assert "No generated review comments" in resp.json()["detail"]

    def test_missing_platform_returns_409(self, tmp_path: Path) -> None:
        client, container = _client_and_container(tmp_path)
        task = _create_task(
            container,
            title="Review PR",
            task_type="pr_review",
            status="done",
            metadata={
                "comment_dry_run": True,
                "generated_review_comments": [{"path": "f.py", "line": 1, "body": "Fix", "severity": "medium"}],
            },
        )
        resp = client.post(f"/api/tasks/{task.id}/post-review-comments")
        assert resp.status_code == 409
        assert "Missing comment platform" in resp.json()["detail"]

    def test_successful_posting(self, tmp_path: Path) -> None:
        client, container = _client_and_container(tmp_path)
        comments = [
            {"path": "src/a.py", "line": 10, "body": "Use consistent naming", "severity": "low"},
            {"path": "src/b.py", "line": 20, "body": "Missing error handling", "severity": "high"},
        ]
        task = _create_task(
            container,
            title="Review PR",
            task_type="pr_review",
            status="done",
            metadata={
                "comment_dry_run": True,
                "generated_review_comments": comments,
                "comment_platform": {"platform": "github", "owner": "org", "repo": "repo", "number": 42},
            },
        )

        mock_results = [
            CommentPostResult(success=True, platform_id="c1"),
            CommentPostResult(success=False, error="rate limited"),
        ]

        mock_auth = subprocess.CompletedProcess(args=["gh", "auth", "status"], returncode=0)
        with patch(
            "overdrive.comments.writer.post_comments_batch",
            return_value=mock_results,
        ), patch("shutil.which", return_value="/usr/bin/gh"), patch(
            "overdrive.runtime.api.routes_tasks.subprocess.run",
            return_value=mock_auth,
        ):
            resp = client.post(f"/api/tasks/{task.id}/post-review-comments")

        assert resp.status_code == 200
        data = resp.json()
        assert data["posted_count"] == 1
        assert data["failed_count"] == 1
        assert len(data["results"]) == 2

        # Verify metadata was updated
        updated = container.tasks.get(task.id)
        assert updated is not None
        assert updated.metadata["comment_dry_run"] is False
        assert len(updated.metadata["posted_comments"]) == 2
        assert updated.metadata["posted_comments"][0]["success"] is True
        assert updated.metadata["posted_comments"][1]["success"] is False

    def test_gitlab_posting_passes_diff_context(self, tmp_path: Path) -> None:
        client, container = _client_and_container(tmp_path)
        task = _create_task(
            container,
            title="Review MR",
            task_type="mr_review_comment",
            status="done",
            metadata={
                "comment_dry_run": True,
                "generated_review_comments": [
                    {"path": "src/example.py", "line": 10, "body": "Inline issue", "severity": "medium"},
                ],
                "comment_platform": {"platform": "gitlab", "project_id": "org%2Frepo", "number": 15},
                "source_diff": "diff --git a/src/example.py b/src/example.py\n@@ -9,1 +9,2 @@\n context\n+new\n",
                "source_diff_refs": {
                    "base_sha": "base123",
                    "start_sha": "start123",
                    "head_sha": "head123",
                },
            },
        )

        mock_auth = subprocess.CompletedProcess(args=["glab", "auth", "status"], returncode=0)
        with patch(
            "overdrive.comments.writer.post_comments_batch",
            return_value=[CommentPostResult(success=True, platform_id="discussion-1")],
        ) as mock_batch, patch("shutil.which", return_value="/usr/bin/glab"), patch(
            "overdrive.runtime.api.routes_tasks.subprocess.run",
            return_value=mock_auth,
        ):
            resp = client.post(f"/api/tasks/{task.id}/post-review-comments")

        assert resp.status_code == 200
        call_args = mock_batch.call_args
        assert call_args.kwargs["source_diff"] == task.metadata["source_diff"]
        assert call_args.kwargs["gitlab_diff_refs"] == task.metadata["source_diff_refs"]

    def test_metadata_contains_generated_review_comments_after_executor(self, tmp_path: Path) -> None:
        """Verify generated_review_comments is not in internal metadata keys."""
        from overdrive.runtime.api.routes_tasks import _INTERNAL_TASK_METADATA_KEYS

        assert "generated_review_comments" not in _INTERNAL_TASK_METADATA_KEYS
