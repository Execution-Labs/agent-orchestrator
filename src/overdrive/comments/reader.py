"""Fetch PR/MR comments from GitHub (gh) and GitLab (glab) CLIs."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from loguru import logger as loguru_logger

from .models import PRComment, _comment_id

logger = logging.getLogger(__name__)


class CommentFetchError(Exception):
    """Raised when fetching comments from a platform fails."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------


def _run_gh_api(endpoint: str, git_dir: Path) -> str | None:
    """Run ``gh api --paginate`` for *endpoint* and return raw stdout.

    Args:
        endpoint: GitHub REST API path (e.g. ``repos/o/r/pulls/1/comments``).
        git_dir: Working directory so ``gh`` resolves the correct repo context.

    Returns:
        Raw stdout on success, ``None`` on any error.
    """
    try:
        result = subprocess.run(
            ["gh", "api", "--paginate", endpoint],
            cwd=git_dir,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return result.stdout
    except subprocess.CalledProcessError as exc:
        loguru_logger.warning("gh api {} failed (exit {}): {}", endpoint, exc.returncode, exc.stderr.strip())
    except subprocess.TimeoutExpired:
        loguru_logger.warning("gh api {} timed out", endpoint)
    except OSError as exc:
        loguru_logger.warning("gh api {} OS error: {}", endpoint, exc)
    return None


def _parse_paginated_json_gh(raw: str) -> list[dict[str, Any]]:
    """Parse ``gh api --paginate`` output into a merged list of objects.

    ``--paginate`` concatenates JSON arrays per page (e.g. ``[...][...]``).
    This function handles both single-page and multi-page output.

    Args:
        raw: Raw stdout from ``gh api --paginate``.

    Returns:
        Merged list of JSON objects, or ``[]`` on parse failure.
    """
    raw = raw.strip()
    if not raw:
        return []

    # Fast path: single page — standard JSON array.
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        return []
    except json.JSONDecodeError:
        pass

    # Slow path: concatenated arrays — use streaming decoder.
    decoder = json.JSONDecoder()
    items: list[dict[str, Any]] = []
    idx = 0
    while idx < len(raw):
        # Skip whitespace between arrays.
        while idx < len(raw) and raw[idx] in " \t\n\r":
            idx += 1
        if idx >= len(raw):
            break
        try:
            obj, end = decoder.raw_decode(raw, idx)
            if isinstance(obj, list):
                items.extend(obj)
            idx = end
        except json.JSONDecodeError:
            loguru_logger.warning("Failed to parse paginated JSON at offset {}", idx)
            return items if items else []
    return items


def _map_review_comment(raw: dict[str, Any]) -> PRComment:
    """Map a GitHub pull-request review comment to :class:`PRComment`."""
    user = raw.get("user") or {}
    line = raw.get("line") if raw.get("line") is not None else raw.get("original_line")
    in_reply_to = raw.get("in_reply_to_id")
    return PRComment(
        id=_comment_id(),
        author=str(user.get("login") or ""),
        body=str(raw.get("body") or ""),
        path=str(raw["path"]) if raw.get("path") is not None else None,
        line=int(line) if line is not None else None,
        created_at=str(raw.get("created_at") or ""),
        in_reply_to=str(in_reply_to) if in_reply_to is not None else None,
        platform_id=str(raw.get("id") or ""),
    )


def _map_issue_comment(raw: dict[str, Any]) -> PRComment:
    """Map a GitHub issue comment to :class:`PRComment`."""
    user = raw.get("user") or {}
    return PRComment(
        id=_comment_id(),
        author=str(user.get("login") or ""),
        body=str(raw.get("body") or ""),
        path=None,
        line=None,
        created_at=str(raw.get("created_at") or ""),
        platform_id=str(raw.get("id") or ""),
    )


def _map_review_decision(raw: dict[str, Any]) -> PRComment:
    """Map a GitHub review decision to :class:`PRComment`."""
    user = raw.get("user") or {}
    state = str(raw.get("state") or "").upper()
    body = str(raw.get("body") or "")
    combined_body = f"[{state}] {body}" if state else body
    return PRComment(
        id=_comment_id(),
        author=str(user.get("login") or ""),
        body=combined_body,
        path=None,
        line=None,
        created_at=str(raw.get("submitted_at") or ""),
        platform_id=str(raw.get("id") or ""),
    )


def fetch_pr_comments(
    owner: str,
    repo: str,
    pr_number: int,
    git_dir: Path,
) -> list[PRComment]:
    """Fetch all PR comments from GitHub via ``gh api``.

    Queries three endpoints (review comments, issue comments, review decisions),
    merges results into a single list sorted by ``created_at``.

    Args:
        owner: Repository owner (user or organization).
        repo: Repository name.
        pr_number: Pull request number.
        git_dir: Local git directory for ``gh`` CLI context.

    Returns:
        Unified list of :class:`PRComment` sorted chronologically.
        Returns ``[]`` if all endpoints fail.
    """
    base = f"repos/{owner}/{repo}"
    endpoints = [
        (f"{base}/pulls/{pr_number}/comments", _map_review_comment),
        (f"{base}/issues/{pr_number}/comments", _map_issue_comment),
        (f"{base}/pulls/{pr_number}/reviews", _map_review_decision),
    ]

    comments: list[PRComment] = []
    for endpoint, mapper in endpoints:
        raw = _run_gh_api(endpoint, git_dir)
        if raw is None:
            continue
        items = _parse_paginated_json_gh(raw)
        for item in items:
            comments.append(mapper(item))

    comments.sort(key=lambda c: c.created_at)
    return comments


# ---------------------------------------------------------------------------
# GitLab helpers
# ---------------------------------------------------------------------------


def _parse_paginated_json_gl(raw: str) -> list[dict]:  # type: ignore[type-arg]
    """Parse ``glab api --paginate`` output into a flat list.

    ``--paginate`` may return a single JSON array or multiple
    newline-separated JSON arrays.  This helper handles both forms.

    Args:
        raw: Raw stdout from ``glab api``.

    Returns:
        Flat list of note dicts.

    Raises:
        CommentFetchError: If the output is not valid JSON.
    """
    raw = raw.strip()
    if not raw:
        return []

    # Fast path: single valid JSON array.
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    # Slow path: newline-separated JSON arrays from --paginate.
    # glab concatenates pages as e.g. "[...]\n[...]", so split on the
    # boundary between a closing and opening bracket.
    fragments = re.split(r"\]\s*\n\s*\[", raw)
    merged: list[dict] = []  # type: ignore[type-arg]
    for i, fragment in enumerate(fragments):
        # Re-add brackets stripped by the split.
        if i == 0:
            fragment = fragment if fragment.rstrip().endswith("]") else fragment + "]"
        elif i == len(fragments) - 1:
            fragment = "[" + fragment if not fragment.lstrip().startswith("[") else fragment
        else:
            fragment = "[" + fragment + "]"
        try:
            part = json.loads(fragment)
            if isinstance(part, list):
                merged.extend(part)
            else:
                merged.append(part)
        except json.JSONDecodeError:
            raise CommentFetchError("Failed to parse glab api response")
    return merged


def _parse_note(note: dict, *, discussion_id: str | None = None) -> PRComment:  # type: ignore[type-arg]
    """Convert a single GitLab note dict into a :class:`PRComment`."""
    position = note.get("position")
    path: str | None = None
    line: int | None = None

    if isinstance(position, dict):
        path = position.get("new_path") or position.get("old_path")
        raw_line = position.get("new_line")
        if raw_line is None:
            raw_line = position.get("old_line")
        if raw_line is not None:
            line = int(raw_line)

    return PRComment(
        platform_id=str(note["id"]),
        author=note["author"]["username"],
        body=note["body"],
        created_at=note["created_at"],
        resolved=bool(note.get("resolved")),
        path=path,
        line=line,
        in_reply_to=None,
        discussion_id=discussion_id or (str(note.get("discussion_id")) if note.get("discussion_id") is not None else None),
    )


def _run_glab_api_paginated(endpoint: str, *, cwd: Path | None = None) -> list[dict[str, Any]]:
    """Run ``glab api --paginate`` and parse the JSON array response."""
    try:
        result = subprocess.run(
            ["glab", "api", "--paginate", endpoint, "-X", "GET"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        raise CommentFetchError(f"glab api failed: {exc.stderr}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommentFetchError("glab api timed out") from exc
    return _parse_paginated_json_gl(result.stdout)


def _discussion_note_map(discussions: list[dict[str, Any]]) -> dict[str, str]:
    """Build a note-id -> discussion-id lookup from GitLab discussions payload."""
    out: dict[str, str] = {}
    for discussion in discussions:
        if not isinstance(discussion, dict):
            continue
        raw_discussion_id = discussion.get("id")
        discussion_id = str(raw_discussion_id) if raw_discussion_id is not None else ""
        if not discussion_id:
            continue
        notes = discussion.get("notes")
        if not isinstance(notes, list):
            continue
        for note in notes:
            if not isinstance(note, dict):
                continue
            note_id = note.get("id")
            if note_id is None:
                continue
            out[str(note_id)] = discussion_id
    return out


def _parse_discussion_comments(discussions: list[dict[str, Any]]) -> list[PRComment]:
    """Convert GitLab discussions payload into unified comment objects."""
    comments: list[PRComment] = []
    for discussion in discussions:
        if not isinstance(discussion, dict):
            continue
        discussion_id = str(discussion.get("id") or "").strip() or None
        notes = discussion.get("notes")
        if not isinstance(notes, list):
            continue
        root_platform_id: str | None = None
        for note in notes:
            if not isinstance(note, dict):
                continue
            if note.get("system", False):
                continue
            try:
                comment = _parse_note(note, discussion_id=discussion_id)
            except (KeyError, TypeError) as exc:
                logger.warning("Skipping malformed discussion note %s: %s", note.get("id"), exc)
                continue
            if root_platform_id:
                comment.in_reply_to = root_platform_id
            else:
                root_platform_id = comment.platform_id
            comments.append(comment)
    return comments


def fetch_mr_comments(
    project_id: str | int,
    mr_number: int,
    *,
    cwd: Path | None = None,
) -> list[PRComment]:
    """Fetch all user comments from a GitLab merge request.

    Uses ``glab api --paginate`` to retrieve notes and parses them into
    the unified :class:`PRComment` format.  System-generated notes are
    filtered out.

    Args:
        project_id: GitLab project ID (numeric or URL-encoded path).
        mr_number: Merge request IID.
        cwd: Working directory for the ``glab`` subprocess.

    Returns:
        List of parsed comments sorted by ``created_at`` ascending.

    Raises:
        CommentFetchError: If ``glab`` is missing, the API call fails,
            times out, or returns unparseable output.
    """
    if shutil.which("glab") is None:
        raise CommentFetchError("glab CLI is not installed")

    discussions_endpoint = f"projects/{project_id}/merge_requests/{mr_number}/discussions"
    notes_endpoint = f"projects/{project_id}/merge_requests/{mr_number}/notes"

    discussions: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    discussions_error: CommentFetchError | None = None
    notes_error: CommentFetchError | None = None
    try:
        discussions = _run_glab_api_paginated(discussions_endpoint, cwd=cwd)
    except CommentFetchError as exc:
        discussions_error = exc
    try:
        notes = _run_glab_api_paginated(notes_endpoint, cwd=cwd)
    except CommentFetchError as exc:
        notes_error = exc

    if discussions_error is not None and notes_error is not None and not discussions and not notes:
        raise discussions_error or notes_error or CommentFetchError("glab api failed")

    comments_by_platform: dict[str, PRComment] = {}

    for comment in _parse_discussion_comments(discussions):
        comments_by_platform[comment.platform_id] = comment

    discussion_lookup = _discussion_note_map(discussions)
    for note in notes:
        if not isinstance(note, dict):
            continue
        if note.get("system", False):
            continue
        try:
            note_id = str(note.get("id")) if note.get("id") is not None else ""
            if note_id and note_id in comments_by_platform:
                continue
            comment = _parse_note(note, discussion_id=discussion_lookup.get(note_id))
            comments_by_platform[comment.platform_id] = comment
        except (KeyError, TypeError) as exc:
            logger.warning("Skipping malformed note %s: %s", note.get("id"), exc)

    comments = list(comments_by_platform.values())
    comments.sort(key=lambda c: c.created_at)
    return comments
