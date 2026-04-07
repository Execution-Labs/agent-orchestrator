"""Tests for review mode selection in the POST /pull-requests/{number}/review endpoint."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from overdrive.pipelines.registry import PipelineRegistry
from overdrive.runtime.api.routes_tasks import (
    _MODES_NEEDING_COMMENTS,
    _REVIEW_MODE_TO_PIPELINE,
)
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
    container = Container(tmp_path)
    return client, container


_PR_META = json.dumps({
    "title": "Add feature X",
    "body": "Implements feature X",
    "headRefName": "feature-x",
    "baseRefName": "main",
    "url": "https://github.com/org/repo/pull/42",
})

_PR_COMMENTS = json.dumps({
    "comments": [
        {"id": "C1", "author": {"login": "alice"}, "body": "Looks good", "createdAt": "2026-01-01T00:00:00Z"},
    ],
    "reviews": [
        {"id": "R1", "author": {"login": "bob"}, "body": "Needs work", "submittedAt": "2026-01-02T00:00:00Z"},
    ],
})


def _mock_subprocess_run_github(cmd: list[str], **kwargs: Any) -> Any:
    """Mock subprocess.run for GitHub CLI calls."""
    class Result:
        returncode = 0
        stdout = ""
        stderr = ""
    r = Result()
    if cmd[0] == "git" and "remote" in cmd:
        r.stdout = "https://github.com/org/repo.git"
    elif cmd[1:3] == ["pr", "view"] and "comments,reviews" in cmd:
        r.stdout = _PR_COMMENTS
    elif cmd[1:3] == ["pr", "view"]:
        r.stdout = _PR_META
    elif cmd[1:3] == ["pr", "diff"] and "--stat" not in cmd:
        r.stdout = "diff --git a/f.py b/f.py\n+hello\n"
    elif cmd[1:3] == ["pr", "diff"] and "--stat" in cmd:
        r.stdout = " f.py | 1 +\n 1 file changed\n"
    return r


def _mock_subprocess_run_gitlab(cmd: list[str], **kwargs: Any) -> Any:
    """Mock subprocess.run for GitLab CLI calls."""
    class Result:
        returncode = 0
        stdout = ""
        stderr = ""
    r = Result()
    if cmd[0] == "git" and "remote" in cmd:
        r.stdout = "https://gitlab.com/org/repo.git"
    elif cmd[0] == "glab" and cmd[1:3] == ["mr", "view"]:
        r.stdout = json.dumps({
            "title": "Add feature Y",
            "description": "Implements feature Y",
            "source_branch": "feature-y",
            "target_branch": "main",
            "diff_refs": {
                "base_sha": "base123",
                "start_sha": "start123",
                "head_sha": "head123",
            },
            "web_url": "https://gitlab.com/org/repo/-/merge_requests/15",
        })
    elif cmd[0] == "glab" and cmd[1:3] == ["mr", "diff"]:
        r.stdout = "diff --git a/g.py b/g.py\n+world\n"
    elif cmd[0] == "glab" and cmd[1] == "api":
        r.stdout = json.dumps([
            {
                "id": 101,
                "body": "Please add coverage",
                "author": {"username": "reviewer1"},
                "created_at": "2026-01-03T00:00:00Z",
                "system": False,
                "resolved": False,
                "position": None,
            },
        ])
    elif cmd[0] == "git" and "diff" in cmd:
        r.stdout = " g.py | 1 +\n 1 file changed\n"
    return r


# ---------------------------------------------------------------------------
# Mode-to-pipeline mapping
# ---------------------------------------------------------------------------


class TestModeToMapping:
    """Verify _REVIEW_MODE_TO_PIPELINE maps each mode to a valid pipeline."""

    @pytest.mark.parametrize("mode", ["fix_only", "review_comment", "summarize", "fix_respond"])
    def test_mode_maps_to_existing_pipeline(self, mode: str):
        task_type, pipeline_id = _REVIEW_MODE_TO_PIPELINE[mode]
        registry = PipelineRegistry()
        template = registry.get(pipeline_id)
        assert template.id == pipeline_id
        assert task_type in template.task_types


class TestModeMapping:
    """POST /pull-requests/{number}/review creates tasks with mode-specific task_type and pipeline."""

    @pytest.mark.parametrize("mode,expected_task_type,expected_pipeline_id", [
        ("fix_only", "pr_review_fix_only", "pr_review_fix_only"),
        ("review_comment", "pr_review_comment", "pr_review_comment"),
        ("summarize", "pr_review_summarize", "pr_review_summarize"),
        ("fix_respond", "pr_review_fix_respond", "pr_review_fix_respond"),
    ])
    def test_mode_creates_correct_task_type_and_pipeline(
        self, tmp_path: Path, mode: str, expected_task_type: str, expected_pipeline_id: str,
    ):
        client, container = _client_and_container(tmp_path)
        registry = PipelineRegistry()
        expected_steps = registry.get(expected_pipeline_id).step_names()

        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/gh"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=_mock_subprocess_run_github),
        ):
            resp = client.post("/api/pull-requests/42/review", json={"review_mode": mode})

        assert resp.status_code == 200
        task_data = resp.json()["task"]
        assert task_data["task_type"] == expected_task_type

        stored = container.tasks.get(task_data["id"])
        assert stored is not None
        assert stored.pipeline_template == expected_steps


# ---------------------------------------------------------------------------
# Metadata storage
# ---------------------------------------------------------------------------


class TestMetadataStorage:
    """Verify review_mode, comment_dry_run, and final_pipeline_id are stored.

    review_decision is no longer set at creation time — it's set at the
    before_post_review gate after the LLM completes its review.
    """

    def test_metadata_stored_for_fix_only(self, tmp_path: Path):
        client, container = _client_and_container(tmp_path)
        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/gh"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=_mock_subprocess_run_github),
        ):
            resp = client.post("/api/pull-requests/42/review", json={"review_mode": "fix_only"})

        assert resp.status_code == 200
        stored = container.tasks.get(resp.json()["task"]["id"])
        assert stored is not None
        assert isinstance(stored.metadata, dict)
        assert stored.metadata["review_mode"] == "fix_only"
        assert stored.metadata["comment_dry_run"] is True
        assert stored.metadata["final_pipeline_id"] == "pr_review_fix_only"
        assert "review_decision" not in stored.metadata

    def test_review_comment_no_decision_at_creation(self, tmp_path: Path):
        """review_comment mode no longer accepts review_decision at creation."""
        client, container = _client_and_container(tmp_path)
        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/gh"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=_mock_subprocess_run_github),
        ):
            resp = client.post("/api/pull-requests/42/review", json={
                "review_mode": "review_comment",
            })

        assert resp.status_code == 200
        stored = container.tasks.get(resp.json()["task"]["id"])
        assert stored is not None
        assert isinstance(stored.metadata, dict)
        assert stored.metadata["review_mode"] == "review_comment"
        assert stored.metadata["comment_dry_run"] is True
        assert "review_decision" not in stored.metadata

    def test_default_review_mode_is_fix_only(self, tmp_path: Path):
        """Calling without review_mode defaults to fix_only."""
        client, container = _client_and_container(tmp_path)
        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/gh"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=_mock_subprocess_run_github),
        ):
            resp = client.post("/api/pull-requests/42/review", json={})

        assert resp.status_code == 200
        stored = container.tasks.get(resp.json()["task"]["id"])
        assert stored is not None
        assert isinstance(stored.metadata, dict)
        assert stored.metadata["review_mode"] == "fix_only"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    """Pydantic validation for creation request."""

    def test_unknown_field_ignored(self, tmp_path: Path):
        """review_decision is silently ignored — Pydantic drops extra fields."""
        client, container = _client_and_container(tmp_path)
        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/gh"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=_mock_subprocess_run_github),
        ):
            resp = client.post("/api/pull-requests/42/review", json={
                "review_mode": "review_comment",
                "review_decision": "approve",
            })
        assert resp.status_code == 200
        stored = container.tasks.get(resp.json()["task"]["id"])
        assert stored is not None
        # review_decision should NOT be stored at creation time
        assert "review_decision" not in (stored.metadata or {})


# ---------------------------------------------------------------------------
# Comment fetching
# ---------------------------------------------------------------------------


class TestCommentFetching:
    """Verify comment fetching behavior based on review mode."""

    def test_review_comment_mode_fetches_and_stores_comments(self, tmp_path: Path):
        client, container = _client_and_container(tmp_path)
        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/gh"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=_mock_subprocess_run_github),
        ):
            resp = client.post("/api/pull-requests/42/review", json={
                "review_mode": "review_comment",
            })

        assert resp.status_code == 200
        stored = container.tasks.get(resp.json()["task"]["id"])
        assert stored is not None
        assert isinstance(stored.metadata, dict)
        comments = stored.metadata.get("source_comments")
        assert isinstance(comments, list)
        assert len(comments) == 2
        assert comments[0]["author"] == "alice"
        assert comments[0]["type"] == "comment"
        assert comments[1]["author"] == "bob"
        assert comments[1]["type"] == "review"

    def test_fix_only_mode_does_not_fetch_comments(self, tmp_path: Path):
        client, container = _client_and_container(tmp_path)
        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/gh"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=_mock_subprocess_run_github),
        ):
            resp = client.post("/api/pull-requests/42/review", json={"review_mode": "fix_only"})

        assert resp.status_code == 200
        stored = container.tasks.get(resp.json()["task"]["id"])
        assert stored is not None
        assert isinstance(stored.metadata, dict)
        assert "source_comments" not in stored.metadata

    def test_comment_fetch_failure_graceful(self, tmp_path: Path):
        """If comment fetching fails, source_comments should be an empty list."""
        client, container = _client_and_container(tmp_path)

        def mock_run(cmd: list[str], **kwargs: Any) -> Any:
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""
            r = Result()
            if cmd[0] == "git" and "remote" in cmd:
                r.stdout = "https://github.com/org/repo.git"
            elif cmd[1:3] == ["pr", "view"] and "comments,reviews" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            elif cmd[1:3] == ["pr", "view"]:
                r.stdout = _PR_META
            elif cmd[1:3] == ["pr", "diff"] and "--stat" not in cmd:
                r.stdout = "diff --git a/f.py b/f.py\n+hello\n"
            elif cmd[1:3] == ["pr", "diff"] and "--stat" in cmd:
                r.stdout = " f.py | 1 +\n 1 file changed\n"
            return r

        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/gh"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=mock_run),
        ):
            resp = client.post("/api/pull-requests/42/review", json={
                "review_mode": "summarize",
            })

        assert resp.status_code == 200
        stored = container.tasks.get(resp.json()["task"]["id"])
        assert stored is not None
        assert isinstance(stored.metadata, dict)
        assert stored.metadata["source_comments"] == []

    @pytest.mark.parametrize("mode", sorted(_MODES_NEEDING_COMMENTS))
    def test_all_comment_modes_store_source_comments(self, tmp_path: Path, mode: str):
        client, container = _client_and_container(tmp_path)
        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/gh"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=_mock_subprocess_run_github),
        ):
            resp = client.post("/api/pull-requests/42/review", json={"review_mode": mode})

        assert resp.status_code == 200
        stored = container.tasks.get(resp.json()["task"]["id"])
        assert stored is not None
        assert isinstance(stored.metadata, dict)
        assert "source_comments" in stored.metadata


# ---------------------------------------------------------------------------
# GitLab mode restriction
# ---------------------------------------------------------------------------


class TestGitLabModes:
    """GitLab supports the same review modes through MR-specific task types."""

    @pytest.mark.parametrize("mode,expected_task_type,expected_pipeline_id", [
        ("fix_only", "mr_review", "mr_review"),
        ("review_comment", "mr_review_comment", "mr_review_comment"),
        ("summarize", "mr_review_summarize", "mr_review_summarize"),
        ("fix_respond", "mr_review_fix_respond", "mr_review_fix_respond"),
    ])
    def test_gitlab_mode_creates_correct_task_type_and_pipeline(
        self, tmp_path: Path, mode: str, expected_task_type: str, expected_pipeline_id: str,
    ):
        client, container = _client_and_container(tmp_path)
        registry = PipelineRegistry()
        expected_steps = registry.get(expected_pipeline_id).step_names()
        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/glab"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=_mock_subprocess_run_gitlab),
            patch("overdrive.comments.reader.shutil.which", return_value="/usr/bin/glab"),
            patch("overdrive.comments.reader.subprocess.run", side_effect=_mock_subprocess_run_gitlab),
        ):
            resp = client.post("/api/pull-requests/15/review", json={"review_mode": mode})

        assert resp.status_code == 200
        task_data = resp.json()["task"]
        assert task_data["task_type"] == expected_task_type

        stored = container.tasks.get(task_data["id"])
        assert stored is not None
        assert stored.pipeline_template == expected_steps

    @pytest.mark.parametrize("mode", sorted(_MODES_NEEDING_COMMENTS))
    def test_gitlab_comment_modes_store_source_comments(self, tmp_path: Path, mode: str):
        client, container = _client_and_container(tmp_path)
        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/glab"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=_mock_subprocess_run_gitlab),
            patch("overdrive.comments.reader.shutil.which", return_value="/usr/bin/glab"),
            patch("overdrive.comments.reader.subprocess.run", side_effect=_mock_subprocess_run_gitlab),
        ):
            resp = client.post("/api/pull-requests/15/review", json={"review_mode": mode})

        assert resp.status_code == 200
        stored = container.tasks.get(resp.json()["task"]["id"])
        assert stored is not None
        assert isinstance(stored.metadata, dict)
        assert "source_comments" in stored.metadata
        assert "source_comments_formatted" in stored.metadata
        assert stored.metadata["source_diff_refs"] == {
            "base_sha": "base123",
            "start_sha": "start123",
            "head_sha": "head123",
        }


# ---------------------------------------------------------------------------
# Duplicate check with mode-specific task_type
# ---------------------------------------------------------------------------


class TestDuplicateCheckModeSpecific:
    """Multiple reviews for the same PR are allowed regardless of mode."""

    def test_different_modes_for_same_pr_allowed(self, tmp_path: Path):
        """Two different modes for the same PR number should not conflict."""
        client, container = _client_and_container(tmp_path)

        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/gh"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=_mock_subprocess_run_github),
        ):
            resp1 = client.post("/api/pull-requests/42/review", json={"review_mode": "fix_only"})
            assert resp1.status_code == 200

            resp2 = client.post("/api/pull-requests/42/review", json={
                "review_mode": "review_comment",
            })
            assert resp2.status_code == 200

        # Both tasks exist with different task_types.
        t1 = container.tasks.get(resp1.json()["task"]["id"])
        t2 = container.tasks.get(resp2.json()["task"]["id"])
        assert t1 is not None and t2 is not None
        assert t1.task_type != t2.task_type

    def test_same_mode_same_pr_allows_multiple(self, tmp_path: Path):
        """Multiple reviews with the same mode for the same PR are now allowed."""
        client, container = _client_and_container(tmp_path)

        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/gh"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=_mock_subprocess_run_github),
        ):
            resp1 = client.post("/api/pull-requests/42/review", json={"review_mode": "fix_only"})
            assert resp1.status_code == 200

            resp2 = client.post("/api/pull-requests/42/review", json={"review_mode": "fix_only"})
            assert resp2.status_code == 200

        t1 = container.tasks.get(resp1.json()["task"]["id"])
        t2 = container.tasks.get(resp2.json()["task"]["id"])
        assert t1 is not None and t2 is not None
        assert t1.id != t2.id


# ---------------------------------------------------------------------------
# Legacy endpoint backward compatibility
# ---------------------------------------------------------------------------


class TestLegacyEndpointCompat:
    """Legacy POST /tasks/{task_id}/review-pr and review-mr are unchanged."""

    def test_legacy_review_pr_uses_pr_review_type(self, tmp_path: Path):
        client, container = _client_and_container(tmp_path)
        task = Task(title="T", task_type="feature", status="queued")
        container.tasks.upsert(task)

        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/gh"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=_mock_subprocess_run_github),
        ):
            resp = client.post(f"/api/tasks/{task.id}/review-pr?pr_number=42")

        assert resp.status_code == 200
        review = resp.json()["task"]
        assert review["task_type"] == "pr_review"
        stored = container.tasks.get(review["id"])
        assert stored is not None
        assert isinstance(stored.metadata, dict)
        assert "review_mode" not in stored.metadata
        assert "review_decision" not in stored.metadata
        assert "comment_dry_run" not in stored.metadata

    def test_legacy_review_mr_uses_mr_review_type(self, tmp_path: Path):
        client, container = _client_and_container(tmp_path)
        task = Task(title="T", task_type="feature", status="queued")
        container.tasks.upsert(task)

        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/glab"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=_mock_subprocess_run_gitlab),
        ):
            resp = client.post(f"/api/tasks/{task.id}/review-mr?mr_number=15")

        assert resp.status_code == 200
        review = resp.json()["task"]
        assert review["task_type"] == "mr_review"
        stored = container.tasks.get(review["id"])
        assert stored is not None
        assert isinstance(stored.metadata, dict)
        assert "review_mode" not in stored.metadata
        assert "review_decision" not in stored.metadata
        assert "comment_dry_run" not in stored.metadata
