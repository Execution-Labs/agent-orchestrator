"""Tests for PR and MR review endpoints and diff truncation helper."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

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


def _create_task(container: Container, **kwargs: Any) -> Task:
    task = Task(**kwargs)
    container.tasks.upsert(task)
    return task


# ---------------------------------------------------------------------------
# Diff truncation
# ---------------------------------------------------------------------------


class TestDiffTruncation:
    """Verify the _truncate_diff helper."""

    def test_short_diff_unchanged(self):
        """Diffs under the limit are returned as-is."""
        from overdrive.runtime.api.routes_tasks import _truncate_diff
        diff = "a\nb\nc\n"
        assert _truncate_diff(diff) == diff

    def test_large_diff_truncated_at_line_boundary(self):
        """Diffs over 100K chars are truncated at the last newline before the limit."""
        from overdrive.runtime.api.routes_tasks import _truncate_diff
        line = "x" * 99 + "\n"  # 100 chars per line
        diff = line * 1200  # 120K chars total
        result = _truncate_diff(diff)
        assert len(result) <= 100_200  # some slack for the notice
        assert "[DIFF TRUNCATED" in result
        assert result.count("\n\n[DIFF TRUNCATED") == 1


# ---------------------------------------------------------------------------
# List pull requests (GitLab path)
# ---------------------------------------------------------------------------


class TestListPullRequestsGitlab:
    """Tests for GET /pull-requests with a GitLab remote."""

    def test_glab_mr_list_no_state_flag(self, tmp_path: Path):
        """glab mr list must be called without --state (removed in glab ~1.80)."""
        client, _container = _client_and_container(tmp_path)

        mr_json = json.dumps([
            {
                "iid": 7,
                "title": "Fix login",
                "author": {"username": "alice"},
                "source_branch": "fix-login",
                "target_branch": "main",
                "web_url": "https://gitlab.com/org/repo/-/merge_requests/7",
            },
        ])

        captured_cmds: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            captured_cmds.append(list(cmd))

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""
            r = Result()
            if cmd[0] == "glab":
                r.stdout = mr_json
            return r

        with (
            patch(
                "overdrive.runtime.api.routes_tasks._detect_git_platform",
                return_value="gitlab",
            ),
            patch(
                "overdrive.runtime.api.routes_tasks.shutil.which",
                return_value="/usr/bin/glab",
            ),
            patch(
                "overdrive.runtime.api.routes_tasks.subprocess.run",
                side_effect=mock_run,
            ),
        ):
            resp = client.get("/api/pull-requests")

        assert resp.status_code == 200
        data = resp.json()
        assert data["platform"] == "gitlab"
        assert data.get("error") is None
        assert len(data["items"]) == 1
        assert data["items"][0]["number"] == 7
        assert data["items"][0]["author"] == "alice"

        # Verify --state is NOT in the glab command.
        glab_cmds = [c for c in captured_cmds if c[0] == "glab"]
        assert len(glab_cmds) == 1
        assert "--state" not in glab_cmds[0]


# ---------------------------------------------------------------------------
# PR review endpoint
# ---------------------------------------------------------------------------


class TestReviewPR:
    """Tests for POST /tasks/{task_id}/review-pr."""

    def test_missing_task_returns_404(self, tmp_path: Path):
        client, _ = _client_and_container(tmp_path)
        resp = client.post("/api/tasks/nonexistent/review-pr?pr_number=1")
        assert resp.status_code == 404

    def test_missing_gh_cli_returns_400(self, tmp_path: Path):
        client, container = _client_and_container(tmp_path)
        task = _create_task(container, title="T", task_type="feature", status="queued")
        with patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value=None):
            resp = client.post(f"/api/tasks/{task.id}/review-pr?pr_number=1")
        assert resp.status_code == 400
        assert "gh" in resp.json()["detail"].lower()

    def test_successful_pr_review_creates_task(self, tmp_path: Path):
        client, container = _client_and_container(tmp_path)
        task = _create_task(container, title="T", task_type="feature", status="queued")

        pr_meta = json.dumps({
            "title": "Add feature X",
            "body": "Implements feature X",
            "headRefName": "feature-x",
            "baseRefName": "main",
            "url": "https://github.com/org/repo/pull/42",
        })

        def mock_run(cmd, **kwargs):
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""
            r = Result()
            if cmd[1:3] == ["pr", "view"]:
                r.stdout = pr_meta
            elif cmd[1:3] == ["pr", "diff"] and "--stat" not in cmd:
                r.stdout = "diff --git a/f.py b/f.py\n+hello\n"
            elif cmd[1:3] == ["pr", "diff"] and "--stat" in cmd:
                r.stdout = " f.py | 1 +\n 1 file changed\n"
            return r

        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/gh"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=mock_run),
        ):
            resp = client.post(f"/api/tasks/{task.id}/review-pr?pr_number=42")

        assert resp.status_code == 200
        review = resp.json()["task"]
        assert review["task_type"] == "pr_review"
        assert "#42" in review["title"]
        assert review["metadata"]["source_pr_number"] == 42
        assert review["metadata"]["source_task_id"] == task.id
        # source_diff and source_description are stripped from API payload
        # by _task_payload; verify they were stored by reading from storage.
        stored = container.tasks.get(review["id"])
        assert stored is not None
        assert isinstance(stored.metadata, dict)
        assert stored.metadata["source_diff"] != ""
        assert stored.metadata["source_stat"] != ""
        assert stored.metadata["source_url"] == "https://github.com/org/repo/pull/42"

    def test_duplicate_pr_review_returns_409(self, tmp_path: Path):
        client, container = _client_and_container(tmp_path)
        task = _create_task(container, title="T", task_type="feature", status="queued")
        # Pre-create an existing pr_review task for the same PR.
        _create_task(
            container,
            title="PR Review: #42",
            task_type="pr_review",
            status="queued",
            metadata={
                "source_task_id": task.id,
                "source_pr_number": 42,
            },
        )
        with patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/gh"):
            resp = client.post(f"/api/tasks/{task.id}/review-pr?pr_number=42")
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# MR review endpoint
# ---------------------------------------------------------------------------


class TestReviewMR:
    """Tests for POST /tasks/{task_id}/review-mr."""

    def test_missing_task_returns_404(self, tmp_path: Path):
        client, _ = _client_and_container(tmp_path)
        resp = client.post("/api/tasks/nonexistent/review-mr?mr_number=1")
        assert resp.status_code == 404

    def test_missing_glab_cli_returns_400(self, tmp_path: Path):
        client, container = _client_and_container(tmp_path)
        task = _create_task(container, title="T", task_type="feature", status="queued")
        with patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value=None):
            resp = client.post(f"/api/tasks/{task.id}/review-mr?mr_number=1")
        assert resp.status_code == 400
        assert "glab" in resp.json()["detail"].lower()

    def test_successful_mr_review_creates_task(self, tmp_path: Path):
        client, container = _client_and_container(tmp_path)
        task = _create_task(container, title="T", task_type="feature", status="queued")

        mr_meta = json.dumps({
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

        def mock_run(cmd, **kwargs):
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""
            r = Result()
            if cmd[0] == "glab" and cmd[1:3] == ["mr", "view"]:
                r.stdout = mr_meta
            elif cmd[0] == "glab" and cmd[1:3] == ["mr", "diff"]:
                r.stdout = "diff --git a/g.py b/g.py\n+world\n"
            elif cmd[0] == "git" and "diff" in cmd:
                r.stdout = " g.py | 1 +\n 1 file changed\n"
            return r

        with (
            patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/glab"),
            patch("overdrive.runtime.api.routes_tasks.subprocess.run", side_effect=mock_run),
        ):
            resp = client.post(f"/api/tasks/{task.id}/review-mr?mr_number=15")

        assert resp.status_code == 200
        review = resp.json()["task"]
        assert review["task_type"] == "mr_review"
        assert "!15" in review["title"]
        assert review["metadata"]["source_mr_number"] == 15
        assert review["metadata"]["source_task_id"] == task.id
        stored = container.tasks.get(review["id"])
        assert stored is not None
        assert isinstance(stored.metadata, dict)
        assert stored.metadata["source_diff"] != ""
        assert stored.metadata["source_url"] == "https://gitlab.com/org/repo/-/merge_requests/15"
        assert stored.metadata["source_diff_refs"] == {
            "base_sha": "base123",
            "start_sha": "start123",
            "head_sha": "head123",
        }

    def test_duplicate_mr_review_returns_409(self, tmp_path: Path):
        client, container = _client_and_container(tmp_path)
        task = _create_task(container, title="T", task_type="feature", status="queued")
        _create_task(
            container,
            title="MR Review: !15",
            task_type="mr_review",
            status="queued",
            metadata={
                "source_task_id": task.id,
                "source_mr_number": 15,
            },
        )
        with patch("overdrive.runtime.api.routes_tasks.shutil.which", return_value="/usr/bin/glab"):
            resp = client.post(f"/api/tasks/{task.id}/review-mr?mr_number=15")
        assert resp.status_code == 409
