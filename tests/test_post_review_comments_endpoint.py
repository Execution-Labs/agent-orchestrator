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
        # One comment failed, so dry_run stays True (not all posted).
        assert updated.metadata["comment_dry_run"] is True
        assert len(updated.metadata["posted_comments"]) == 2
        assert updated.metadata["posted_comments"][0]["success"] is True
        assert updated.metadata["posted_comments"][1]["success"] is False
        # Verify per-comment post_status tracking
        gen = updated.metadata["generated_review_comments"]
        assert gen[0]["post_status"] == "posted"
        assert gen[1]["post_status"] == "failed"

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

    def test_selective_posting_by_index(self, tmp_path: Path) -> None:
        """Post only index 1 of 3 comments; verify only that one is sent."""
        client, container = _client_and_container(tmp_path)
        comments = [
            {"path": "a.py", "line": 1, "body": "Comment 0", "severity": "low"},
            {"path": "b.py", "line": 2, "body": "Comment 1", "severity": "medium"},
            {"path": "c.py", "line": 3, "body": "Comment 2", "severity": "high"},
        ]
        task = _create_task(
            container,
            title="Review PR",
            task_type="pr_review",
            status="done",
            metadata={
                "comment_dry_run": True,
                "generated_review_comments": comments,
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
            },
        )

        mock_auth = subprocess.CompletedProcess(args=["gh", "auth", "status"], returncode=0)
        with patch(
            "overdrive.comments.writer.post_comments_batch",
            return_value=[CommentPostResult(success=True, platform_id="c1")],
        ) as mock_batch, patch("shutil.which", return_value="/usr/bin/gh"), patch(
            "overdrive.runtime.api.routes_tasks.subprocess.run",
            return_value=mock_auth,
        ):
            resp = client.post(
                f"/api/tasks/{task.id}/post-review-comments",
                json={"comments": [{"index": 1}]},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["posted_count"] == 1
        assert data["failed_count"] == 0
        assert data["skipped_count"] == 0

        # Only comment at index 1 was passed to post_comments_batch.
        batch_comments = mock_batch.call_args[0][1]
        assert len(batch_comments) == 1
        assert batch_comments[0]["body"] == "Comment 1"

        # Verify post_status tracking.
        updated = container.tasks.get(task.id)
        assert updated is not None
        gen = updated.metadata["generated_review_comments"]
        assert gen[0]["post_status"] == "staged"
        assert gen[1]["post_status"] == "posted"
        assert gen[2]["post_status"] == "staged"

    def test_body_override(self, tmp_path: Path) -> None:
        """Post index 0 with body override; verify the overridden body reaches post_comments_batch."""
        client, container = _client_and_container(tmp_path)
        comments = [
            {"path": "a.py", "line": 1, "body": "Original body", "severity": "low"},
        ]
        task = _create_task(
            container,
            title="Review PR",
            task_type="pr_review",
            status="done",
            metadata={
                "comment_dry_run": True,
                "generated_review_comments": comments,
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
            },
        )

        mock_auth = subprocess.CompletedProcess(args=["gh", "auth", "status"], returncode=0)
        with patch(
            "overdrive.comments.writer.post_comments_batch",
            return_value=[CommentPostResult(success=True, platform_id="c1")],
        ) as mock_batch, patch("shutil.which", return_value="/usr/bin/gh"), patch(
            "overdrive.runtime.api.routes_tasks.subprocess.run",
            return_value=mock_auth,
        ):
            resp = client.post(
                f"/api/tasks/{task.id}/post-review-comments",
                json={"comments": [{"index": 0, "body": "Edited body"}]},
            )

        assert resp.status_code == 200
        batch_comments = mock_batch.call_args[0][1]
        assert batch_comments[0]["body"] == "Edited body"

        # Original comment body in metadata should NOT be mutated.
        updated = container.tasks.get(task.id)
        assert updated is not None
        assert updated.metadata["generated_review_comments"][0]["body"] == "Original body"

    def test_multi_batch_posting(self, tmp_path: Path) -> None:
        """First batch posts index 0; second batch (no body) posts remaining staged."""
        client, container = _client_and_container(tmp_path)
        comments = [
            {"path": "a.py", "line": 1, "body": "C0", "severity": "low"},
            {"path": "b.py", "line": 2, "body": "C1", "severity": "medium"},
        ]
        task = _create_task(
            container,
            title="Review PR",
            task_type="pr_review",
            status="done",
            metadata={
                "comment_dry_run": True,
                "generated_review_comments": comments,
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
            },
        )

        mock_auth = subprocess.CompletedProcess(args=["gh", "auth", "status"], returncode=0)

        # Batch 1: post only index 0.
        with patch(
            "overdrive.comments.writer.post_comments_batch",
            return_value=[CommentPostResult(success=True, platform_id="c0")],
        ), patch("shutil.which", return_value="/usr/bin/gh"), patch(
            "overdrive.runtime.api.routes_tasks.subprocess.run",
            return_value=mock_auth,
        ):
            resp1 = client.post(
                f"/api/tasks/{task.id}/post-review-comments",
                json={"comments": [{"index": 0}]},
            )

        assert resp1.status_code == 200
        d1 = resp1.json()
        assert d1["posted_count"] == 1
        updated1 = container.tasks.get(task.id)
        assert updated1 is not None
        assert updated1.metadata["comment_dry_run"] is True  # Not all posted yet.
        assert len(updated1.metadata["posted_comments"]) == 1

        # Batch 2: no body — posts remaining staged (index 1).
        with patch(
            "overdrive.comments.writer.post_comments_batch",
            return_value=[CommentPostResult(success=True, platform_id="c1")],
        ), patch("shutil.which", return_value="/usr/bin/gh"), patch(
            "overdrive.runtime.api.routes_tasks.subprocess.run",
            return_value=mock_auth,
        ):
            resp2 = client.post(f"/api/tasks/{task.id}/post-review-comments")

        assert resp2.status_code == 200
        d2 = resp2.json()
        assert d2["posted_count"] == 1

        updated2 = container.tasks.get(task.id)
        assert updated2 is not None
        assert updated2.metadata["comment_dry_run"] is False  # All posted now.
        # posted_comments accumulates across batches.
        assert len(updated2.metadata["posted_comments"]) == 2

    def test_already_posted_skipped(self, tmp_path: Path) -> None:
        """Selecting a comment with post_status=posted returns skipped: true."""
        client, container = _client_and_container(tmp_path)
        comments = [
            {"path": "a.py", "line": 1, "body": "C0", "severity": "low", "post_status": "posted"},
            {"path": "b.py", "line": 2, "body": "C1", "severity": "medium"},
        ]
        task = _create_task(
            container,
            title="Review PR",
            task_type="pr_review",
            status="done",
            metadata={
                "comment_dry_run": True,
                "generated_review_comments": comments,
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
            },
        )

        mock_auth = subprocess.CompletedProcess(args=["gh", "auth", "status"], returncode=0)
        with patch(
            "overdrive.comments.writer.post_comments_batch",
            return_value=[CommentPostResult(success=True, platform_id="c1")],
        ), patch("shutil.which", return_value="/usr/bin/gh"), patch(
            "overdrive.runtime.api.routes_tasks.subprocess.run",
            return_value=mock_auth,
        ):
            resp = client.post(
                f"/api/tasks/{task.id}/post-review-comments",
                json={"comments": [{"index": 0}, {"index": 1}]},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["posted_count"] == 1
        assert data["skipped_count"] == 1
        # Find the skipped entry.
        skipped = [r for r in data["results"] if r.get("skipped")]
        assert len(skipped) == 1
        assert skipped[0]["index"] == 0
        assert skipped[0]["post_status"] == "posted"

    def test_out_of_range_index_returns_422(self, tmp_path: Path) -> None:
        """Selecting an out-of-range index returns 422."""
        client, container = _client_and_container(tmp_path)
        task = _create_task(
            container,
            title="Review PR",
            task_type="pr_review",
            status="done",
            metadata={
                "comment_dry_run": True,
                "generated_review_comments": [
                    {"path": "a.py", "line": 1, "body": "C0", "severity": "low"},
                    {"path": "b.py", "line": 2, "body": "C1", "severity": "medium"},
                ],
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
            },
        )

        resp = client.post(
            f"/api/tasks/{task.id}/post-review-comments",
            json={"comments": [{"index": 99}]},
        )
        assert resp.status_code == 422
        assert "out of range" in resp.json()["detail"]

    def test_negative_index_returns_422(self, tmp_path: Path) -> None:
        """Negative indices are rejected."""
        client, container = _client_and_container(tmp_path)
        task = _create_task(
            container,
            title="Review PR",
            task_type="pr_review",
            status="done",
            metadata={
                "comment_dry_run": True,
                "generated_review_comments": [
                    {"path": "a.py", "line": 1, "body": "C0", "severity": "low"},
                ],
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
            },
        )

        resp = client.post(
            f"/api/tasks/{task.id}/post-review-comments",
            json={"comments": [{"index": -1}]},
        )
        assert resp.status_code == 422

    def test_no_body_posts_all_staged(self, tmp_path: Path) -> None:
        """Backward-compat: no body posts all staged comments."""
        client, container = _client_and_container(tmp_path)
        comments = [
            {"path": "a.py", "line": 1, "body": "C0", "severity": "low"},
            {"path": "b.py", "line": 2, "body": "C1", "severity": "medium"},
        ]
        task = _create_task(
            container,
            title="Review PR",
            task_type="pr_review",
            status="done",
            metadata={
                "comment_dry_run": True,
                "generated_review_comments": comments,
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
            },
        )

        mock_auth = subprocess.CompletedProcess(args=["gh", "auth", "status"], returncode=0)
        with patch(
            "overdrive.comments.writer.post_comments_batch",
            return_value=[
                CommentPostResult(success=True, platform_id="c0"),
                CommentPostResult(success=True, platform_id="c1"),
            ],
        ) as mock_batch, patch("shutil.which", return_value="/usr/bin/gh"), patch(
            "overdrive.runtime.api.routes_tasks.subprocess.run",
            return_value=mock_auth,
        ):
            resp = client.post(f"/api/tasks/{task.id}/post-review-comments")

        assert resp.status_code == 200
        data = resp.json()
        assert data["posted_count"] == 2
        assert data["failed_count"] == 0
        # Both comments were passed to post_comments_batch.
        batch_comments = mock_batch.call_args[0][1]
        assert len(batch_comments) == 2

        updated = container.tasks.get(task.id)
        assert updated is not None
        assert updated.metadata["comment_dry_run"] is False

    def test_dry_run_stays_true_partial(self, tmp_path: Path) -> None:
        """After partial post, comment_dry_run remains True."""
        client, container = _client_and_container(tmp_path)
        comments = [
            {"path": "a.py", "line": 1, "body": "C0", "severity": "low"},
            {"path": "b.py", "line": 2, "body": "C1", "severity": "medium"},
        ]
        task = _create_task(
            container,
            title="Review PR",
            task_type="pr_review",
            status="done",
            metadata={
                "comment_dry_run": True,
                "generated_review_comments": comments,
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
            },
        )

        mock_auth = subprocess.CompletedProcess(args=["gh", "auth", "status"], returncode=0)
        with patch(
            "overdrive.comments.writer.post_comments_batch",
            return_value=[CommentPostResult(success=True, platform_id="c0")],
        ), patch("shutil.which", return_value="/usr/bin/gh"), patch(
            "overdrive.runtime.api.routes_tasks.subprocess.run",
            return_value=mock_auth,
        ):
            resp = client.post(
                f"/api/tasks/{task.id}/post-review-comments",
                json={"comments": [{"index": 0}]},
            )

        assert resp.status_code == 200
        updated = container.tasks.get(task.id)
        assert updated is not None
        assert updated.metadata["comment_dry_run"] is True

    def test_dry_run_flips_false_when_all_posted(self, tmp_path: Path) -> None:
        """After all comments posted, comment_dry_run becomes False."""
        client, container = _client_and_container(tmp_path)
        comments = [
            {"path": "a.py", "line": 1, "body": "C0", "severity": "low"},
        ]
        task = _create_task(
            container,
            title="Review PR",
            task_type="pr_review",
            status="done",
            metadata={
                "comment_dry_run": True,
                "generated_review_comments": comments,
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
            },
        )

        mock_auth = subprocess.CompletedProcess(args=["gh", "auth", "status"], returncode=0)
        with patch(
            "overdrive.comments.writer.post_comments_batch",
            return_value=[CommentPostResult(success=True, platform_id="c0")],
        ), patch("shutil.which", return_value="/usr/bin/gh"), patch(
            "overdrive.runtime.api.routes_tasks.subprocess.run",
            return_value=mock_auth,
        ):
            resp = client.post(f"/api/tasks/{task.id}/post-review-comments")

        assert resp.status_code == 200
        updated = container.tasks.get(task.id)
        assert updated is not None
        assert updated.metadata["comment_dry_run"] is False

    def test_no_staged_comments_returns_409(self, tmp_path: Path) -> None:
        """All comments already posted, no body → 409."""
        client, container = _client_and_container(tmp_path)
        comments = [
            {"path": "a.py", "line": 1, "body": "C0", "severity": "low", "post_status": "posted"},
            {"path": "b.py", "line": 2, "body": "C1", "severity": "medium", "post_status": "posted"},
        ]
        task = _create_task(
            container,
            title="Review PR",
            task_type="pr_review",
            status="done",
            metadata={
                "comment_dry_run": True,
                "generated_review_comments": comments,
                "comment_platform": {"platform": "github", "owner": "o", "repo": "r", "number": 1},
            },
        )

        resp = client.post(f"/api/tasks/{task.id}/post-review-comments")
        assert resp.status_code == 409
        assert "No staged comments" in resp.json()["detail"]

    def test_metadata_contains_generated_review_comments_after_executor(self, tmp_path: Path) -> None:
        """Verify generated_review_comments is not in internal metadata keys."""
        from overdrive.runtime.api.routes_tasks import _INTERNAL_TASK_METADATA_KEYS

        assert "generated_review_comments" not in _INTERNAL_TASK_METADATA_KEYS
