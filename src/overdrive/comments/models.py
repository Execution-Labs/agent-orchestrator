"""PR/MR comment data models for platform-agnostic comment integration."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

ReviewDecisionType = Literal["approve", "request_changes", "comment"]

_VALID_DECISIONS: set[str] = {"approve", "request_changes", "comment"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _comment_id() -> str:
    return f"comment-{uuid.uuid4().hex[:10]}"


@dataclass
class PRComment:
    """A single PR/MR comment, platform-agnostic."""

    id: str = field(default_factory=_comment_id)
    author: str = ""
    body: str = ""
    path: Optional[str] = None
    line: Optional[int] = None
    created_at: str = field(default_factory=_now_iso)
    resolved: bool = False
    in_reply_to: Optional[str] = None
    platform_id: str = ""
    discussion_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the comment to a plain dictionary.

        Returns:
            dict[str, Any]: Result produced by this call.
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PRComment:
        """Deserialize a comment, normalizing optional fields.

        Args:
            data (dict[str, Any]): Serialized payload consumed by this operation.

        Returns:
            PRComment: Result produced by this call.
        """
        raw_line = data.get("line")
        line: int | None
        try:
            line = int(raw_line) if raw_line is not None else None
        except (TypeError, ValueError):
            line = None
        return cls(
            id=str(data.get("id") or _comment_id()),
            author=str(data.get("author") or ""),
            body=str(data.get("body") or ""),
            path=(str(data.get("path")) if data.get("path") is not None else None),
            line=line,
            created_at=str(data.get("created_at") or _now_iso()),
            resolved=bool(data.get("resolved", False)),
            in_reply_to=(str(data.get("in_reply_to")) if data.get("in_reply_to") is not None else None),
            platform_id=str(data.get("platform_id") or ""),
            discussion_id=(str(data.get("discussion_id")) if data.get("discussion_id") is not None else None),
        )


@dataclass
class ReviewDecision:
    """A review decision (approve, request changes, or comment)."""

    decision: ReviewDecisionType = "comment"
    body: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize the decision to a plain dictionary.

        Returns:
            dict[str, Any]: Result produced by this call.
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewDecision:
        """Deserialize a review decision, validating the decision value.

        Args:
            data (dict[str, Any]): Serialized payload consumed by this operation.

        Returns:
            ReviewDecision: Result produced by this call.
        """
        raw_decision = str(data.get("decision") or "comment")
        decision = raw_decision if raw_decision in _VALID_DECISIONS else "comment"
        return cls(
            decision=decision,  # type: ignore[arg-type]
            body=str(data.get("body") or ""),
        )


@dataclass
class CommentPostResult:
    """Result of posting a comment to a platform."""

    success: bool = False
    platform_id: str = ""
    error: Optional[str] = None
    post_status: str = "staged"  # "staged" | "posted" | "failed"

    def to_dict(self) -> dict[str, Any]:
        """Serialize the result to a plain dictionary.

        Returns:
            dict[str, Any]: Result produced by this call.
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommentPostResult:
        """Deserialize a post result, coercing types.

        Args:
            data (dict[str, Any]): Serialized payload consumed by this operation.

        Returns:
            CommentPostResult: Result produced by this call.
        """
        return cls(
            success=bool(data.get("success", False)),
            platform_id=str(data.get("platform_id") or ""),
            error=(str(data.get("error")) if data.get("error") is not None else None),
            post_status=str(data.get("post_status") or "staged"),
        )
