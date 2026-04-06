"""Post PR/MR comments and review decisions via ``gh api`` / ``glab api`` CLIs."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .models import CommentPostResult, ReviewDecisionType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

_GITHUB_PR_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)
_GITLAB_MR_RE = re.compile(
    r"https?://[^/]+/(?P<project>.+?)/-/merge_requests/(?P<number>\d+)"
)
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


def parse_source_url(url: str) -> dict[str, Any]:
    """Parse a GitHub PR or GitLab MR URL into platform-specific identifiers.

    Args:
        url: Full URL to a pull request or merge request.

    Returns:
        Dict with ``platform``, identifiers, and ``number``.
        For GitHub: ``{"platform": "github", "owner": ..., "repo": ..., "number": int}``.
        For GitLab: ``{"platform": "gitlab", "project_id": ..., "number": int}``.

    Raises:
        ValueError: If the URL does not match a known pattern.
    """
    m = _GITHUB_PR_RE.search(url)
    if m:
        return {
            "platform": "github",
            "owner": m.group("owner"),
            "repo": m.group("repo"),
            "number": int(m.group("number")),
        }
    m = _GITLAB_MR_RE.search(url)
    if m:
        # URL-encode the project path for the GitLab API.
        project_path = m.group("project")
        project_id = project_path.replace("/", "%2F")
        return {
            "platform": "gitlab",
            "project_id": project_id,
            "number": int(m.group("number")),
        }
    raise ValueError(f"Cannot parse PR/MR URL: {url}")


# ---------------------------------------------------------------------------
# Low-level CLI helpers
# ---------------------------------------------------------------------------


def _run_gh_api_post(
    endpoint: str, body_json: dict[str, Any], git_dir: Path
) -> tuple[bool, str]:
    """POST to GitHub API via ``gh api``.

    Args:
        endpoint: REST API path (e.g. ``repos/o/r/pulls/1/comments``).
        body_json: JSON body to send.
        git_dir: Working directory for ``gh`` CLI context.

    Returns:
        ``(success, response_or_error)`` tuple.
    """
    try:
        result = subprocess.run(
            [
                "gh", "api",
                "-X", "POST",
                "-H", "Accept: application/vnd.github+json",
                "--input", "-",
                endpoint,
            ],
            input=json.dumps(body_json),
            cwd=str(git_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip() or f"gh api POST failed (exit {result.returncode})"
    except subprocess.TimeoutExpired:
        return False, "gh api POST timed out"
    except OSError as exc:
        return False, f"gh api POST OS error: {exc}"


def _run_glab_api_post(
    endpoint: str, body_json: dict[str, Any], cwd: Path | None
) -> tuple[bool, str]:
    """POST to GitLab API via ``glab api``.

    Args:
        endpoint: REST API path (e.g. ``projects/.../merge_requests/.../notes``).
        body_json: JSON body to send.
        cwd: Working directory for ``glab`` CLI context.

    Returns:
        ``(success, response_or_error)`` tuple.
    """
    try:
        result = subprocess.run(
            [
                "glab", "api",
                "-X", "POST",
                "-H", "Content-Type: application/json",
                "--input", "-",
                endpoint,
            ],
            input=json.dumps(body_json),
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip() or f"glab api POST failed (exit {result.returncode})"
    except subprocess.TimeoutExpired:
        return False, "glab api POST timed out"
    except OSError as exc:
        return False, f"glab api POST OS error: {exc}"


def _get_gitlab_mr_head_sha(
    project_id: str,
    mr_number: int,
    *,
    cwd: Path | None = None,
) -> str | None:
    """Fetch the current HEAD SHA for a GitLab merge request."""
    endpoint = f"projects/{project_id}/merge_requests/{mr_number}"
    try:
        result = subprocess.run(
            ["glab", "api", endpoint, "-X", "GET"],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        data = json.loads(result.stdout or "{}")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None
    sha = str(data.get("sha") or "").strip()
    if sha:
        return sha
    diff_refs = data.get("diff_refs")
    if isinstance(diff_refs, dict):
        head_sha = str(diff_refs.get("head_sha") or "").strip()
        if head_sha:
            return head_sha
    return None


def _get_gitlab_mr_diff_refs(
    project_id: str,
    mr_number: int,
    *,
    cwd: Path | None = None,
) -> dict[str, str] | None:
    """Fetch the current diff refs for a GitLab merge request."""
    endpoint = f"projects/{project_id}/merge_requests/{mr_number}"
    try:
        result = subprocess.run(
            ["glab", "api", endpoint, "-X", "GET"],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        data = json.loads(result.stdout or "{}")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None
    diff_refs = data.get("diff_refs")
    if not isinstance(diff_refs, dict):
        return None
    base_sha = str(diff_refs.get("base_sha") or "").strip()
    start_sha = str(diff_refs.get("start_sha") or "").strip()
    head_sha = str(diff_refs.get("head_sha") or "").strip()
    if not base_sha or not start_sha or not head_sha:
        return None
    return {
        "base_sha": base_sha,
        "start_sha": start_sha,
        "head_sha": head_sha,
    }


def _normalize_gitlab_diff_refs(diff_refs: dict[str, Any] | None) -> dict[str, str] | None:
    """Validate and normalize GitLab diff refs required for inline comments."""
    if not isinstance(diff_refs, dict):
        return None
    base_sha = str(diff_refs.get("base_sha") or "").strip()
    start_sha = str(diff_refs.get("start_sha") or "").strip()
    head_sha = str(diff_refs.get("head_sha") or "").strip()
    if not base_sha or not start_sha or not head_sha:
        return None
    return {
        "base_sha": base_sha,
        "start_sha": start_sha,
        "head_sha": head_sha,
    }


def _resolve_gitlab_diff_position(
    *,
    path: str | None,
    line: int | None,
    source_diff: str | None,
    diff_refs: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Resolve a GitLab MR diff position payload from a unified diff anchor."""
    normalized_path = str(path or "").strip()
    if not normalized_path or line is None or line <= 0:
        return None
    normalized_refs = _normalize_gitlab_diff_refs(diff_refs)
    if normalized_refs is None:
        return None
    diff_text = str(source_diff or "")
    if not diff_text.strip():
        return None

    current_old_path: str | None = None
    current_new_path: str | None = None
    old_line_no: int | None = None
    new_line_no: int | None = None
    in_hunk = False
    candidates: list[tuple[int, dict[str, Any]]] = []

    for raw_line in diff_text.splitlines():
        match = _DIFF_GIT_RE.match(raw_line)
        if match:
            current_old_path = match.group(1)
            current_new_path = match.group(2)
            old_line_no = None
            new_line_no = None
            in_hunk = False
            continue

        if raw_line.startswith("--- "):
            in_hunk = False
            old_line_no = None
            new_line_no = None
            raw_old_path = raw_line[4:].strip()
            if raw_old_path == "/dev/null":
                current_old_path = None
            elif raw_old_path.startswith("a/"):
                current_old_path = raw_old_path[2:]
            else:
                current_old_path = raw_old_path
            continue

        if raw_line.startswith("+++ "):
            in_hunk = False
            old_line_no = None
            new_line_no = None
            raw_new_path = raw_line[4:].strip()
            if raw_new_path == "/dev/null":
                current_new_path = None
            elif raw_new_path.startswith("b/"):
                current_new_path = raw_new_path[2:]
            else:
                current_new_path = raw_new_path
            continue

        hunk_match = _HUNK_RE.match(raw_line)
        if hunk_match:
            old_line_no = int(hunk_match.group("old_start"))
            new_line_no = int(hunk_match.group("new_start"))
            in_hunk = True
            continue

        if not in_hunk:
            continue
        if raw_line.startswith("\\ No newline at end of file"):
            continue

        line_type = raw_line[:1]
        if line_type == "+" and not raw_line.startswith("+++"):
            entry_old_line = None
            entry_new_line = new_line_no
            if new_line_no is not None:
                new_line_no += 1
        elif line_type == "-" and not raw_line.startswith("---"):
            entry_old_line = old_line_no
            entry_new_line = None
            if old_line_no is not None:
                old_line_no += 1
        else:
            entry_old_line = old_line_no
            entry_new_line = new_line_no
            if old_line_no is not None:
                old_line_no += 1
            if new_line_no is not None:
                new_line_no += 1

        if current_new_path == normalized_path and entry_new_line == line:
            score = 3 if entry_old_line is None else 2
            candidates.append(
                (
                    score,
                    {
                        "old_path": current_old_path or normalized_path,
                        "new_path": current_new_path or normalized_path,
                        "old_line": entry_old_line,
                        "new_line": entry_new_line,
                    },
                )
            )
        if current_old_path == normalized_path and entry_old_line == line:
            score = 3 if entry_new_line is None else 2
            candidates.append(
                (
                    score,
                    {
                        "old_path": current_old_path or normalized_path,
                        "new_path": current_new_path or normalized_path,
                        "old_line": entry_old_line,
                        "new_line": entry_new_line,
                    },
                )
            )

    if not candidates:
        return None
    _, chosen = max(candidates, key=lambda item: item[0])
    position: dict[str, Any] = {
        "position_type": "text",
        "base_sha": normalized_refs["base_sha"],
        "start_sha": normalized_refs["start_sha"],
        "head_sha": normalized_refs["head_sha"],
        "old_path": chosen["old_path"],
        "new_path": chosen["new_path"],
    }
    if chosen["old_line"] is not None:
        position["old_line"] = chosen["old_line"]
    if chosen["new_line"] is not None:
        position["new_line"] = chosen["new_line"]
    return position


def _extract_id_from_response(response: str) -> str:
    """Extract the ``id`` field from a JSON API response string."""
    try:
        data = json.loads(response)
        if isinstance(data, dict) and "id" in data:
            return str(data["id"])
    except (json.JSONDecodeError, TypeError):
        pass
    return ""


# ---------------------------------------------------------------------------
# GitHub posting
# ---------------------------------------------------------------------------

_REVIEW_EVENT_MAP: dict[str, str] = {
    "approve": "APPROVE",
    "request_changes": "REQUEST_CHANGES",
    "comment": "COMMENT",
}

# Brief delay between consecutive API calls to avoid rate limiting.
_POST_DELAY_SECONDS = 0.5


def post_pr_comment(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    path: str | None = None,
    line: int | None = None,
    body: str,
    git_dir: Path,
    commit_id: str | None = None,
    in_reply_to: int | None = None,
) -> CommentPostResult:
    """Post a single comment to a GitHub pull request.

    For inline comments (with ``path`` and ``line``), uses the single-comment
    review endpoint. For replies to existing review comments, uses the reply
    endpoint. For general comments, uses the issue comment endpoint.

    Args:
        owner: Repository owner.
        repo: Repository name.
        pr_number: Pull request number.
        path: File path for inline comments.
        line: Line number for inline comments.
        body: Comment body text.
        git_dir: Local git directory for ``gh`` CLI context.
        commit_id: Commit SHA for inline comments (uses PR HEAD if omitted).
        in_reply_to: Platform ID of comment to reply to (for thread replies).

    Returns:
        :class:`CommentPostResult` indicating success or failure.
    """
    base = f"repos/{owner}/{repo}"

    if in_reply_to is not None:
        # Reply to an existing review comment thread.
        endpoint = f"{base}/pulls/{pr_number}/comments/{in_reply_to}/replies"
        payload: dict[str, Any] = {"body": body}
    elif path is not None and line is not None:
        # Inline comment via single-comment review.
        endpoint = f"{base}/pulls/{pr_number}/reviews"
        comment_obj: dict[str, Any] = {"path": path, "line": line, "body": body}
        payload = {
            "event": "COMMENT",
            "comments": [comment_obj],
        }
        if commit_id:
            payload["commit_id"] = commit_id
    else:
        # General PR comment (issue comment endpoint).
        endpoint = f"{base}/issues/{pr_number}/comments"
        payload = {"body": body}

    ok, response = _run_gh_api_post(endpoint, payload, git_dir)
    platform_id = _extract_id_from_response(response) if ok else ""
    return CommentPostResult(
        success=ok,
        platform_id=platform_id,
        error=response if not ok else None,
    )


def post_pr_review_decision(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    decision: ReviewDecisionType,
    body: str,
    git_dir: Path,
    commit_id: str | None = None,
) -> CommentPostResult:
    """Submit a review decision on a GitHub pull request.

    Args:
        owner: Repository owner.
        repo: Repository name.
        pr_number: Pull request number.
        decision: One of ``approve``, ``request_changes``, or ``comment``.
        body: Review body text.
        git_dir: Local git directory for ``gh`` CLI context.
        commit_id: Optional commit SHA to pin the review to.

    Returns:
        :class:`CommentPostResult` indicating success or failure.
    """
    event = _REVIEW_EVENT_MAP.get(decision, "COMMENT")
    endpoint = f"repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    payload: dict[str, Any] = {"event": event, "body": body}
    if commit_id:
        payload["commit_id"] = commit_id

    ok, response = _run_gh_api_post(endpoint, payload, git_dir)
    platform_id = _extract_id_from_response(response) if ok else ""
    return CommentPostResult(
        success=ok,
        platform_id=platform_id,
        error=response if not ok else None,
    )


# ---------------------------------------------------------------------------
# GitLab posting
# ---------------------------------------------------------------------------


def post_mr_comment(
    project_id: str,
    mr_number: int,
    *,
    path: str | None = None,
    line: int | None = None,
    position: dict[str, Any] | None = None,
    body: str,
    cwd: Path | None = None,
    in_reply_to: int | None = None,
    discussion_id: str | None = None,
) -> CommentPostResult:
    """Post a comment to a GitLab merge request.

    For inline comments, creates a new discussion with position info.
    For replies, posts to the discussion's notes endpoint.
    For general comments, posts a top-level note.

    Args:
        project_id: URL-encoded GitLab project path or numeric ID.
        mr_number: Merge request IID.
        path: File path for inline comments.
        line: Line number for inline comments.
        position: Fully resolved GitLab diff position payload for inline comments.
        body: Comment body text.
        cwd: Working directory for ``glab`` CLI context.
        in_reply_to: Platform ID of note to reply to.
        discussion_id: GitLab discussion ID for threaded replies.

    Returns:
        :class:`CommentPostResult` indicating success or failure.
    """
    base = f"projects/{project_id}/merge_requests/{mr_number}"

    if discussion_id:
        endpoint = f"{base}/discussions/{discussion_id}/notes"
        payload: dict[str, Any] = {"body": body}
    elif in_reply_to is not None:
        # Fallback when only the original note ID is available.
        endpoint = f"{base}/notes"
        payload = {"body": body}
    elif position is not None:
        endpoint = f"{base}/discussions"
        payload = {
            "body": body,
            "position": position,
        }
    elif path is not None and line is not None:
        # Inline comment via discussions endpoint.
        endpoint = f"{base}/discussions"
        payload = {
            "body": body,
            "position": {
                "position_type": "text",
                "new_path": path,
                "new_line": line,
            },
        }
    else:
        # General MR note.
        endpoint = f"{base}/notes"
        payload = {"body": body}

    ok, response = _run_glab_api_post(endpoint, payload, cwd)
    platform_id = _extract_id_from_response(response) if ok else ""
    return CommentPostResult(
        success=ok,
        platform_id=platform_id,
        error=response if not ok else None,
    )


def post_mr_review_decision(
    project_id: str,
    mr_number: int,
    *,
    decision: ReviewDecisionType,
    body: str,
    cwd: Path | None = None,
) -> CommentPostResult:
    """Post a native review decision on a GitLab merge request.

    GitLab exposes merge request review state through quick actions in notes.
    We use that path instead of synthetic ``[APPROVED]``-style notes so the
    merge request reflects real approval / requested-changes / reviewed state
    while still preserving the generated summary text in a single post.

    Args:
        project_id: URL-encoded GitLab project path or numeric ID.
        mr_number: Merge request IID.
        decision: One of ``approve``, ``request_changes``, or ``comment``.
        body: Review body text.
        cwd: Working directory for ``glab`` CLI context.

    Returns:
        :class:`CommentPostResult` indicating success or failure.
    """
    command_map: dict[ReviewDecisionType, str] = {
        "request_changes": "/submit_review requested_changes",
        "comment": "/submit_review reviewed",
    }
    if decision == "approve":
        payload: dict[str, Any] = {}
        sha = _get_gitlab_mr_head_sha(project_id, mr_number, cwd=cwd)
        if sha:
            payload["sha"] = sha
        approve_endpoint = f"projects/{project_id}/merge_requests/{mr_number}/approve"
        ok, response = _run_glab_api_post(approve_endpoint, payload, cwd)
        if not ok:
            return CommentPostResult(success=False, error=response)
        if not body.strip():
            return CommentPostResult(success=True)

        note_ok, note_response = _run_glab_api_post(
            f"projects/{project_id}/merge_requests/{mr_number}/notes",
            {"body": body},
            cwd,
        )
        return CommentPostResult(
            success=note_ok,
            platform_id=_extract_id_from_response(note_response) if note_ok else "",
            error=note_response if not note_ok else None,
        )

    command = command_map.get(decision, "/submit_review reviewed")
    formatted_body = f"{body}\n\n{command}" if body else command
    endpoint = f"projects/{project_id}/merge_requests/{mr_number}/notes"
    payload = {"body": formatted_body}

    ok, response = _run_glab_api_post(endpoint, payload, cwd)
    platform_id = _extract_id_from_response(response) if ok else ""
    return CommentPostResult(success=ok, platform_id=platform_id, error=response if not ok else None)


# ---------------------------------------------------------------------------
# Batch posting helper
# ---------------------------------------------------------------------------


def post_comments_batch(
    platform_info: dict[str, Any],
    comments: list[dict[str, Any]],
    *,
    git_dir: Path,
    commit_id: str | None = None,
    source_diff: str | None = None,
    gitlab_diff_refs: dict[str, Any] | None = None,
) -> list[CommentPostResult]:
    """Post multiple comments, inserting a brief delay between calls.

    Args:
        platform_info: Parsed platform dict from :func:`parse_source_url`.
        comments: List of comment dicts with ``path``, ``line``, ``body``, and
            optionally ``in_reply_to``.
        git_dir: Local git directory for CLI context.
        commit_id: Optional commit SHA for inline comments.
        source_diff: Unified diff text used to resolve GitLab inline positions.
        gitlab_diff_refs: GitLab MR diff refs containing base/start/head SHAs.

    Returns:
        List of :class:`CommentPostResult` in the same order as *comments*.
    """
    results: list[CommentPostResult] = []
    platform = str(platform_info.get("platform", ""))
    resolved_gitlab_diff_refs = _normalize_gitlab_diff_refs(gitlab_diff_refs)
    attempted_gitlab_diff_ref_lookup = False

    for i, comment in enumerate(comments):
        if i > 0:
            time.sleep(_POST_DELAY_SECONDS)

        body = str(comment.get("body") or "")
        path = comment.get("path")
        raw_line = comment.get("line")
        line = int(raw_line) if raw_line is not None and int(raw_line) > 0 else None
        raw_reply = comment.get("in_reply_to")
        in_reply_to = int(raw_reply) if raw_reply is not None else None
        discussion_id = str(comment.get("discussion_id") or "").strip() or None

        # Inline comment requires a valid line; skip if path is set but line
        # is missing/zero (the LLM failed to resolve a diff line number).
        if path is not None and line is None and in_reply_to is None:
            results.append(CommentPostResult(
                success=False,
                error=f"Skipped: no valid diff line number for {path}",
            ))
            continue

        if platform == "github":
            result = post_pr_comment(
                str(platform_info["owner"]),
                str(platform_info["repo"]),
                int(platform_info["number"]),
                path=str(path) if path is not None else None,
                line=line,
                body=body,
                git_dir=git_dir,
                commit_id=commit_id,
                in_reply_to=in_reply_to,
            )
        elif platform == "gitlab":
            position: dict[str, Any] | None = None
            if path is not None and line is not None and in_reply_to is None:
                if resolved_gitlab_diff_refs is None and not attempted_gitlab_diff_ref_lookup:
                    attempted_gitlab_diff_ref_lookup = True
                    resolved_gitlab_diff_refs = _get_gitlab_mr_diff_refs(
                        str(platform_info["project_id"]),
                        int(platform_info["number"]),
                        cwd=git_dir,
                    )
                position = _resolve_gitlab_diff_position(
                    path=str(path),
                    line=line,
                    source_diff=source_diff,
                    diff_refs=resolved_gitlab_diff_refs,
                )
                if position is None:
                    results.append(
                        CommentPostResult(
                            success=False,
                            error=f"Skipped: could not resolve GitLab diff position for {path}:{line}",
                        )
                    )
                    continue
            result = post_mr_comment(
                str(platform_info["project_id"]),
                int(platform_info["number"]),
                path=str(path) if path is not None else None,
                line=line,
                position=position,
                body=body,
                cwd=git_dir,
                in_reply_to=in_reply_to,
                discussion_id=discussion_id,
            )
        else:
            result = CommentPostResult(success=False, error=f"Unsupported platform: {platform}")

        results.append(result)
    return results
