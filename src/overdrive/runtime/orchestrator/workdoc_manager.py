"""Workdoc templates and synchronization helpers for orchestrator tasks."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable

from ..domain.models import ReviewCycle, Task
from ..events.bus import EventBus
from ..storage.container import Container


class WorkdocManager:
    """Manage workdoc rendering and synchronization for task execution."""

    # Maps step names to (heading, placeholder_step) pairs.
    # placeholder_step is the step name used in the template placeholder text.
    _WORKDOC_SECTION_MAP: dict[str, tuple[str, str | None]] = {
        "plan": ("## Plan", "plan"),
        "initiative_plan": ("## Plan", "plan"),
        "commit_review": ("## Plan", "plan"),
        "pr_review": ("## Plan", "plan"),
        "mr_review": ("## Plan", "plan"),
        "analyze": ("## Analysis", "analyze"),
        "diagnose": ("## Analysis", "analyze"),
        "scan_deps": ("## Dependency Scan Findings", "scan_deps"),
        "scan_code": ("## Code Scan Findings", "scan_code"),
        "generate_tasks": ("## Generated Tasks", "generate_tasks"),
        "profile": ("## Profiling Baseline", "profile"),
        "implement": ("## Implementation Log", "implement"),
        "prototype": ("## Implementation Log", "implement"),
        "implement_fix": ("## Fix Log", None),  # placeholder uses different wording
        "verify": ("## Verification Results", "verify"),
        "benchmark": ("## Verification Results", "verify"),
        "report": ("## Final Report", "report"),
        # PR review pipelines
        "fetch_comments": ("## Fetched Comments", "fetch_comments"),
        "pr_review_comment": ("## Review Comments", "pr_review_comment"),
        "pr_review_summarize": ("## Review Summary", "pr_review_summarize"),
        "pr_review_fix_respond": ("## Review & Fix Plan", "pr_review_fix_respond"),
        "post_comments": ("## Posted Comments", "post_comments"),
        "post_comment_responses": ("## Comment Responses", "post_comment_responses"),
    }
    _WORKDOC_SENTINEL_ID_MAP: dict[str, str] = {
        "plan": "plan",
        "initiative_plan": "plan",
        "commit_review": "plan",
        "pr_review": "plan",
        "mr_review": "plan",
        "analyze": "analysis",
        "diagnose": "analysis",
        "scan_deps": "dependency_scan_findings",
        "scan_code": "code_scan_findings",
        "generate_tasks": "generated_tasks",
        "profile": "profiling_baseline",
        "implement": "implementation_log",
        "prototype": "implementation_log",
        "implement_fix": "fix_log",
        "verify": "verification_results",
        "benchmark": "verification_results",
        "report": "final_report",
        "review": "review_findings",
        "fetch_comments": "fetched_comments",
        "pr_review_comment": "review_comments",
        "pr_review_summarize": "review_summary",
        "pr_review_fix_respond": "review_fix_plan",
        "post_comments": "posted_comments",
        "post_comment_responses": "comment_responses",
    }
    _WORKDOC_HEADING_SENTINEL_MAP: dict[str, str] = {
        "## Plan": "plan",
        "## Initiative Plan": "plan",
        "## Refactor Plan": "plan",
        "## Optimization Plan": "plan",
        "## Analysis": "analysis",
        "## Diagnosis": "analysis",
        "## Repository Analysis": "analysis",
        "## Research Analysis": "analysis",
        "## Review Analysis": "analysis",
        "## Refactor Analysis": "analysis",
        "## Coverage Analysis": "analysis",
        "## Documentation Analysis": "analysis",
        "## Spike Analysis": "analysis",
        "## Dependency Scan Findings": "dependency_scan_findings",
        "## Code Scan Findings": "code_scan_findings",
        "## Generated Tasks": "generated_tasks",
        "## Generated Remediation Tasks": "generated_tasks",
        "## Profiling Baseline": "profiling_baseline",
        "## Implementation Log": "implementation_log",
        "## Fix Implementation": "implementation_log",
        "## Refactor Implementation": "implementation_log",
        "## Optimization Implementation": "implementation_log",
        "## Test Implementation": "implementation_log",
        "## Documentation Updates": "implementation_log",
        "## Hotfix Implementation": "implementation_log",
        "## Chore Implementation": "implementation_log",
        "## Prototype Notes": "implementation_log",
        "## Fix Log": "fix_log",
        "## Verification Results": "verification_results",
        "## Benchmark Results": "verification_results",
        "## Final Report": "final_report",
        "## Security Report": "final_report",
        "## Review Findings": "review_findings",
        "## Initiative Context": "initiative_context",
        "## Fetched Comments": "fetched_comments",
        "## Review Comments": "review_comments",
        "## Review Summary": "review_summary",
        "## Review & Fix Plan": "review_fix_plan",
        "## Posted Comments": "posted_comments",
        "## Comment Responses": "comment_responses",
    }
    _WORKDOC_SCHEMA_MARKER = "<!-- WORKDOC:SCHEMA v1 -->"
    _SYNC_DIAGNOSTIC_KEYS = (
        "workdoc_sync_error_type",
        "workdoc_sync_mode",
        "workdoc_sync_step",
        "workdoc_sync_attempt",
    )

    _FEATURE_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Plan

_Pending: will be populated by the plan step._

## Implementation Log

_Pending: will be populated by the implement step._

## Verification Results

_Pending: will be populated by the verify step._

## Review Findings

_Pending: will be populated by the review step._

## Fix Log

_Pending: will be populated as needed._
"""

    _GENERIC_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Plan

_Pending: will be populated by the plan step._

## Analysis

_Pending: will be populated by the analyze step._

## Profiling Baseline

_Pending: will be populated by the profile step._

## Implementation Log

_Pending: will be populated by the implement step._

## Verification Results

_Pending: will be populated by the verify step._

## Review Findings

_Pending: will be populated by the review step._

## Fix Log

_Pending: will be populated as needed._
"""

    _VERIFY_ONLY_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Verification Results

_Pending: will be populated by the verify step._
"""

    _SECURITY_AUDIT_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Dependency Scan Findings

_Pending: will be populated by the scan_deps step._

## Code Scan Findings

_Pending: will be populated by the scan_code step._

## Generated Remediation Tasks

_Pending: will be populated by the generate_tasks step._
"""

    _REPO_REVIEW_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Repository Analysis

_Pending: will be populated by the analyze step._

## Initiative Plan

_Pending: will be populated by the plan step._

## Generated Tasks

_Pending: will be populated by the generate_tasks step._
"""

    _RESEARCH_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Research Analysis

_Pending: will be populated by the analyze step._
"""

    _REVIEW_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Review Analysis

_Pending: will be populated by the analyze step._

## Review Findings

_Pending: will be populated by the review step._
"""

    _COMMIT_REVIEW_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Plan

_Pending: will be populated by the commit review step with findings and fix tasks._

## Implementation Log

_Pending: will be populated by the implement step._

## Verification Results

_Pending: will be populated by the verify step._

## Review Findings

_Pending: will be populated by the review step._

## Fix Log

_Pending: will be populated as needed._
"""

    _PR_REVIEW_COMMENT_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Fetched Comments

_Pending: will be populated by the fetch comments step._

## Review Comments

_Pending: will be populated by the review & comment step._

## Posted Comments

_Pending: will be populated by the post comments step._
"""

    _PR_REVIEW_SUMMARIZE_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Fetched Comments

_Pending: will be populated by the fetch comments step._

## Review Summary

_Pending: will be populated by the summarize step._
"""

    _PR_REVIEW_FIX_RESPOND_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Fetched Comments

_Pending: will be populated by the fetch comments step._

## Review & Fix Plan

_Pending: will be populated by the review & plan fixes step._

## Implementation Log

_Pending: will be populated by the implement step._

## Verification Results

_Pending: will be populated by the verify step._

## Review Findings

_Pending: will be populated by the review step._

## Comment Responses

_Pending: will be populated by the post comment responses step._
"""

    _BUG_FIX_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Diagnosis

_Pending: will be populated by the diagnose step._

## Fix Implementation

_Pending: will be populated by the implement step._

## Verification Results

_Pending: will be populated by the verify step._

## Review Findings

_Pending: will be populated by the review step._

## Fix Log

_Pending: will be populated as needed._
"""

    _REFACTOR_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Refactor Analysis

_Pending: will be populated by the analyze step._

## Refactor Plan

_Pending: will be populated by the plan step._

## Refactor Implementation

_Pending: will be populated by the implement step._

## Verification Results

_Pending: will be populated by the verify step._

## Review Findings

_Pending: will be populated by the review step._

## Fix Log

_Pending: will be populated as needed._
"""

    _PERFORMANCE_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Profiling Baseline

_Pending: will be populated by the profile step._

## Optimization Plan

_Pending: will be populated by the plan step._

## Optimization Implementation

_Pending: will be populated by the implement step._

## Benchmark Results

_Pending: will be populated by the verify step._

## Review Findings

_Pending: will be populated by the review step._

## Fix Log

_Pending: will be populated as needed._
"""

    _TEST_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Coverage Analysis

_Pending: will be populated by the analyze step._

## Test Implementation

_Pending: will be populated by the implement step._

## Verification Results

_Pending: will be populated by the verify step._

## Review Findings

_Pending: will be populated by the review step._

## Fix Log

_Pending: will be populated as needed._
"""

    _DOCS_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Documentation Analysis

_Pending: will be populated by the analyze step._

## Documentation Updates

_Pending: will be populated by the implement step._

## Verification Results

_Pending: will be populated by the verify step._

## Review Findings

_Pending: will be populated by the review step._

## Fix Log

_Pending: will be populated as needed._
"""

    _HOTFIX_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Hotfix Implementation

_Pending: will be populated by the implement step._

## Verification Results

_Pending: will be populated by the verify step._

## Review Findings

_Pending: will be populated by the review step._

## Fix Log

_Pending: will be populated as needed._
"""

    _CHORE_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Chore Implementation

_Pending: will be populated by the implement step._

## Verification Results

_Pending: will be populated by the verify step._

## Fix Log

_Pending: will be populated as needed._
"""

    _SPIKE_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Spike Analysis

_Pending: will be populated by the analyze step._

## Prototype Notes

_Pending: will be populated by the implement step._
"""

    _PLAN_ONLY_WORKDOC_TEMPLATE = """\
# Working Document: {title}

**Task ID:** {task_id}
**Type:** {task_type} | **Priority:** {priority}
**Created:** {created_at}

---

## Task Description

{description}

---

## Analysis

_Pending: will be populated by the analyze step._

## Plan

_Pending: will be populated by the plan step._

## Generated Tasks

_Pending: will be populated by the generate_tasks step._
"""

    def __init__(
        self,
        container: Container,
        bus: EventBus,
        *,
        pipeline_id_resolver: Callable[[Task], str],
    ) -> None:
        """Initialize the manager with orchestrator dependencies."""
        self.container = container
        self.bus = bus
        self._pipeline_id_resolver = pipeline_id_resolver

    def workdoc_template_for_task(self, task: Task) -> str:
        """Select the default workdoc template for the task's pipeline."""
        pipeline_id = self._pipeline_id_resolver(task)
        template_by_pipeline: dict[str, str] = {
            "feature": self._FEATURE_WORKDOC_TEMPLATE,
            "bug_fix": self._BUG_FIX_WORKDOC_TEMPLATE,
            "refactor": self._REFACTOR_WORKDOC_TEMPLATE,
            "research": self._RESEARCH_WORKDOC_TEMPLATE,
            "docs": self._DOCS_WORKDOC_TEMPLATE,
            "test": self._TEST_WORKDOC_TEMPLATE,
            "repo_review": self._REPO_REVIEW_WORKDOC_TEMPLATE,
            "security_audit": self._SECURITY_AUDIT_WORKDOC_TEMPLATE,
            "review": self._REVIEW_WORKDOC_TEMPLATE,
            "commit_review": self._COMMIT_REVIEW_WORKDOC_TEMPLATE,
            "pr_review": self._COMMIT_REVIEW_WORKDOC_TEMPLATE,
            "mr_review": self._COMMIT_REVIEW_WORKDOC_TEMPLATE,
            "pr_review_comment": self._PR_REVIEW_COMMENT_WORKDOC_TEMPLATE,
            "mr_review_comment": self._PR_REVIEW_COMMENT_WORKDOC_TEMPLATE,
            "pr_review_summarize": self._PR_REVIEW_SUMMARIZE_WORKDOC_TEMPLATE,
            "mr_review_summarize": self._PR_REVIEW_SUMMARIZE_WORKDOC_TEMPLATE,
            "pr_review_fix_only": self._COMMIT_REVIEW_WORKDOC_TEMPLATE,
            "pr_review_fix_respond": self._PR_REVIEW_FIX_RESPOND_WORKDOC_TEMPLATE,
            "mr_review_fix_respond": self._PR_REVIEW_FIX_RESPOND_WORKDOC_TEMPLATE,
            "performance": self._PERFORMANCE_WORKDOC_TEMPLATE,
            "hotfix": self._HOTFIX_WORKDOC_TEMPLATE,
            "spike": self._SPIKE_WORKDOC_TEMPLATE,
            "chore": self._CHORE_WORKDOC_TEMPLATE,
            "plan_only": self._PLAN_ONLY_WORKDOC_TEMPLATE,
            "verify_only": self._VERIFY_ONLY_WORKDOC_TEMPLATE,
        }
        return template_by_pipeline.get(pipeline_id, self._GENERIC_WORKDOC_TEMPLATE)

    def workdoc_section_for_step(self, task: Task, step: str) -> tuple[str, str | None] | None:
        """Resolve section heading and placeholder mapping for a step."""
        section = self._WORKDOC_SECTION_MAP.get(step)
        if not section:
            return None
        heading, placeholder_step = section
        pipeline_id = self._pipeline_id_resolver(task)
        section_overrides: dict[str, dict[str, tuple[str, str | None]]] = {
            "security_audit": {
                "report": ("## Security Report", "report"),
                "generate_tasks": ("## Generated Remediation Tasks", "generate_tasks"),
            },
            "repo_review": {
                "analyze": ("## Repository Analysis", "analyze"),
                "initiative_plan": ("## Initiative Plan", "plan"),
            },
            "research": {
                "analyze": ("## Research Analysis", "analyze"),
            },
            "review": {
                "analyze": ("## Review Analysis", "analyze"),
                "implement_fix": ("## Review Findings", "review"),
            },
            "bug_fix": {
                "diagnose": ("## Diagnosis", "diagnose"),
                "implement": ("## Fix Implementation", "implement"),
            },
            "refactor": {
                "analyze": ("## Refactor Analysis", "analyze"),
                "plan": ("## Refactor Plan", "plan"),
                "implement": ("## Refactor Implementation", "implement"),
            },
            "performance": {
                "plan": ("## Optimization Plan", "plan"),
                "implement": ("## Optimization Implementation", "implement"),
                "benchmark": ("## Benchmark Results", "verify"),
            },
            "test": {
                "analyze": ("## Coverage Analysis", "analyze"),
                "implement": ("## Test Implementation", "implement"),
            },
            "docs": {
                "analyze": ("## Documentation Analysis", "analyze"),
                "implement": ("## Documentation Updates", "implement"),
            },
            "hotfix": {
                "implement": ("## Hotfix Implementation", "implement"),
            },
            "chore": {
                "implement": ("## Chore Implementation", "implement"),
            },
            "spike": {
                "analyze": ("## Spike Analysis", "analyze"),
                "prototype": ("## Prototype Notes", "implement"),
            },
        }
        override = section_overrides.get(pipeline_id, {}).get(step)
        if override:
            heading, placeholder_step = override
        return heading, placeholder_step

    def workdoc_canonical_path(self, task_id: str) -> Path:
        """Return canonical workdoc path for task state."""
        return self.container.state_root / "workdocs" / f"{task_id}.md"

    @staticmethod
    def workdoc_worktree_path(project_dir: Path) -> Path:
        """Return worktree-local workdoc path."""
        return project_dir / ".workdoc.md"

    def init_workdoc(self, task: Task, project_dir: Path) -> Path:
        """Render the workdoc template and write canonical + worktree copies."""
        workdocs_dir = self.container.state_root / "workdocs"
        workdocs_dir.mkdir(parents=True, exist_ok=True)

        canonical = self.workdoc_canonical_path(task.id)
        template = self.workdoc_template_for_task(task)
        content = (
            template
            .replace("{title}", task.title)
            .replace("{task_id}", task.id)
            .replace("{task_type}", task.task_type)
            .replace("{priority}", task.priority)
            .replace("{created_at}", task.created_at)
            .replace("{description}", task.description or "(no description)")
        )
        # Inject initiative context for child tasks spawned from initiative plans.
        if isinstance(task.metadata, dict):
            init_ctx = task.metadata.get("initiative_context")
            if isinstance(init_ctx, dict) and init_ctx:
                ctx_section = self._render_initiative_context_section(init_ctx)
                # Insert after the --- separator following Task Description,
                # before the first pipeline section heading.
                desc_sep = content.find("\n---\n", content.find("## Task Description"))
                if desc_sep != -1:
                    insert_pos = desc_sep + len("\n---\n")
                    content = content[:insert_pos] + "\n" + ctx_section + "\n" + content[insert_pos:]
        content = self._apply_schema_and_section_sentinels(content)
        canonical.write_text(content, encoding="utf-8")

        worktree_copy = self.workdoc_worktree_path(project_dir)
        shutil.copy2(str(canonical), str(worktree_copy))

        if not isinstance(task.metadata, dict):
            task.metadata = {}
        task.metadata["workdoc_path"] = str(canonical)
        return canonical

    @staticmethod
    def _render_initiative_context_section(ctx: dict[str, str]) -> str:
        """Render the Initiative Context workdoc section from metadata."""
        parent_title = ctx.get("parent_title", "")
        parent_id = ctx.get("parent_id", "")
        objective = ctx.get("objective", "")
        plan_excerpt = ctx.get("plan_excerpt", "")
        lines = ["## Initiative Context", ""]
        lines.append(f"**Parent Task:** {parent_title} (`{parent_id}`)")
        lines.append("")
        lines.append("**Objective:**")
        lines.append(objective or "(none)")
        lines.append("")
        lines.append("**Relevant Plan:**")
        lines.append(plan_excerpt or "(none)")
        lines.append("")
        return "\n".join(lines)

    @classmethod
    def _apply_schema_and_section_sentinels(cls, text: str) -> str:
        """Inject schema marker and section sentinels into initialized workdocs."""
        updated = cls._ensure_schema_marker(text)
        managed_start = cls._managed_sections_start_offset(updated)
        for heading, section_id in cls._WORKDOC_HEADING_SENTINEL_MAP.items():
            updated = cls._wrap_heading_with_sentinel(updated, heading, section_id, start_offset=managed_start)
        return updated

    @classmethod
    def _ensure_schema_marker(cls, text: str) -> str:
        marker = cls._WORKDOC_SCHEMA_MARKER
        if marker in text:
            return text
        first_newline = text.find("\n")
        if first_newline == -1:
            return marker + "\n" + text
        return text[: first_newline + 1] + marker + "\n" + text[first_newline + 1 :]

    @staticmethod
    def _wrap_heading_with_sentinel(text: str, heading: str, section_id: str, *, start_offset: int = 0) -> str:
        """Wrap heading body with sentinel markers when not already present."""
        heading_re = re.compile(rf"(?m)^{re.escape(heading)}\s*$")
        searchable = text[start_offset:] if start_offset > 0 else text
        matches = list(heading_re.finditer(searchable))
        if not matches:
            return text
        start_marker = f"<!-- WORKDOC:SECTION {section_id} START -->"
        end_marker = f"<!-- WORKDOC:SECTION {section_id} END -->"
        for match in reversed(matches):
            absolute_start = (start_offset + match.start()) if start_offset > 0 else match.start()
            absolute_end = (start_offset + match.end()) if start_offset > 0 else match.end()
            heading_line_end = text.find("\n", absolute_end)
            if heading_line_end == -1:
                continue
            section_start = heading_line_end + 1
            rest = text[section_start:]
            next_heading = re.search(r"^## ", rest, re.MULTILINE)
            section_end = section_start + next_heading.start() if next_heading else len(text)
            body = text[section_start:section_end]
            if start_marker in body and end_marker in body:
                continue
            if "<!-- WORKDOC:SECTION " in body:
                # Avoid nesting or rewriting already-structured blocks.
                continue
            if body and not body.endswith("\n"):
                body += "\n"
            wrapped = f"{start_marker}\n{body}{end_marker}\n"
            text = text[:section_start] + wrapped + text[section_end:]
        return text

    @staticmethod
    def _managed_sections_start_offset(text: str) -> int:
        """Return offset where managed workdoc sections begin (after metadata/description prelude)."""
        first = text.find("\n---\n")
        if first == -1:
            return 0
        second = text.find("\n---\n", first + 1)
        if second == -1:
            return first + len("\n---\n")
        return second + len("\n---\n")

    @staticmethod
    def cleanup_workdoc_for_commit(project_dir: Path) -> None:
        """Remove the worktree .workdoc.md before commit."""
        workdoc = project_dir / ".workdoc.md"
        if workdoc.exists():
            workdoc.unlink()

    def refresh_workdoc(
        self,
        task: Task,
        project_dir: Path,
        *,
        read_triplet: Callable[[], tuple[Path, Path, str, str] | None] | None = None,
    ) -> None:
        """Copy canonical workdoc to worktree so worker sees the latest version."""
        canonical = self.workdoc_canonical_path(task.id)
        if not canonical.exists():
            return
        worktree_copy = self.workdoc_worktree_path(project_dir)
        if read_triplet is not None:
            triplet = read_triplet()
            if triplet is None:
                return
            _, _, canonical_text, _ = triplet
            worktree_copy.write_text(canonical_text, encoding="utf-8")
            return
        shutil.copy2(str(canonical), str(worktree_copy))

    def sync_workdoc(
        self,
        task: Task,
        step: str,
        project_dir: Path,
        summary: str | None,
        attempt: int | None = None,
        *,
        read_workdoc_pair: Callable[[], tuple[str, str] | None] | None = None,
    ) -> None:
        """Post-step sync: accept worker changes or fallback-append summary."""
        canonical = self.workdoc_canonical_path(task.id)
        if not canonical.exists():
            return
        worktree_copy = self.workdoc_worktree_path(project_dir)
        if read_workdoc_pair is None and not worktree_copy.exists():
            return

        if read_workdoc_pair is not None:
            pair = read_workdoc_pair()
            if pair is None:
                return
            canonical_text, worktree_text = pair
        else:
            try:
                canonical_text = canonical.read_text(encoding="utf-8")
                worktree_text = worktree_copy.read_text(encoding="utf-8")
            except FileNotFoundError:
                # Parallel task cleanup can remove the worktree-local workdoc between
                # existence checks and read operations. Treat that as a benign no-op.
                return

        changed = False
        sync_mode: str | None = None
        sync_reason: str | None = None
        orchestrator_managed_steps = {"report", "profile"}
        allow_worker_workdoc_write = step not in orchestrator_managed_steps
        if worktree_text != canonical_text and allow_worker_workdoc_write:
            section = self.workdoc_section_for_step(task, step)
            if not section:
                return
            heading, _ = section
            section_id = self._WORKDOC_SENTINEL_ID_MAP.get(step)
            fallback_reason: str | None = None
            allow_legacy_merge = True

            if section_id is not None:
                canonical_state, canonical_bounds, canonical_has_markers = self._sentinel_section_bounds(canonical_text, section_id)
                worker_state, worker_bounds, worker_has_markers = self._sentinel_section_bounds(worktree_text, section_id)

                if canonical_state == "malformed":
                    self._set_sync_diagnostics(
                        task,
                        error_type="canonical_malformed",
                        mode="blocked_invalid_structure",
                        step=step,
                        attempt=attempt,
                    )
                    raise ValueError(f"Malformed canonical workdoc section markers for step '{step}'")
                if canonical_state == "valid":
                    if worker_state == "valid" and worker_bounds is not None and canonical_bounds is not None:
                        c_start, c_end = canonical_bounds
                        w_start, w_end = worker_bounds
                        updated = canonical_text[:c_start] + worktree_text[w_start:w_end] + canonical_text[c_end:]
                        if updated != canonical_text:
                            canonical.write_text(updated, encoding="utf-8")
                            worktree_copy.write_text(updated, encoding="utf-8")
                            changed = True
                            sync_mode = "sentinel_merge"
                        else:
                            # Worker edits outside managed section are intentionally ignored.
                            sync_mode = "sentinel_merge"
                    elif worker_state == "missing" and worker_has_markers:
                        fallback_reason = "section_id_mismatch"
                        allow_legacy_merge = False
                    elif worker_state == "missing":
                        fallback_reason = "worktree_missing_section"
                        allow_legacy_merge = False
                    else:
                        fallback_reason = "worktree_malformed"
                        allow_legacy_merge = False
                elif canonical_state == "missing":
                    if worker_state == "valid":
                        fallback_reason = "canonical_missing_section"
                    elif worker_state == "malformed":
                        fallback_reason = "worktree_malformed"
                else:
                    self._set_sync_diagnostics(
                        task,
                        error_type="canonical_malformed",
                        mode="blocked_invalid_structure",
                        step=step,
                        attempt=attempt,
                    )
                    raise ValueError(f"Malformed canonical workdoc section markers for step '{step}'")

            if not changed and sync_mode is None and allow_legacy_merge:
                canonical_bounds = self._section_bounds(canonical_text, heading)
                worker_bounds = self._section_bounds(worktree_text, heading)
                if canonical_bounds and worker_bounds:
                    c_start, c_end = canonical_bounds
                    w_start, w_end = worker_bounds
                    updated = canonical_text[:c_start] + worktree_text[w_start:w_end] + canonical_text[c_end:]
                    if updated != canonical_text:
                        canonical.write_text(updated, encoding="utf-8")
                        worktree_copy.write_text(updated, encoding="utf-8")
                        changed = True
                        sync_mode = "legacy_heading_merge"
                    else:
                        sync_mode = "legacy_heading_merge"
                else:
                    fallback_reason = fallback_reason or "legacy_bounds_missing"

            if not changed and sync_mode not in {"sentinel_merge", "legacy_heading_merge"}:
                if summary and summary.strip():
                    summary_updated = self._append_summary_under_heading(
                        canonical_text,
                        heading=heading,
                        placeholder_step=section[1],
                        step=step,
                        summary=summary,
                        attempt=attempt,
                    )
                    if summary_updated is not None and summary_updated != canonical_text:
                        canonical.write_text(summary_updated, encoding="utf-8")
                        worktree_copy.write_text(summary_updated, encoding="utf-8")
                        changed = True
                        sync_mode = "fallback_append"
                        sync_reason = fallback_reason or "worker_unstructured_change"
                        self._set_sync_diagnostics(
                            task,
                            error_type=sync_reason,
                            mode="fallback_append",
                            step=step,
                            attempt=attempt,
                        )
                else:
                    reason = fallback_reason or "worker_unstructured_change"
                    self._set_sync_diagnostics(
                        task,
                        error_type=reason,
                        mode="blocked_invalid_structure",
                        step=step,
                        attempt=attempt,
                    )
                    raise ValueError(f"Unable to sync workdoc for step '{step}': {reason}")
        elif summary and summary.strip():
            section = self.workdoc_section_for_step(task, step)
            if not section:
                return
            heading, placeholder_step = section
            summary_updated = self._append_summary_under_heading(
                canonical_text,
                heading=heading,
                placeholder_step=placeholder_step,
                step=step,
                summary=summary,
                attempt=attempt,
            )
            if summary_updated is None:
                self._set_sync_diagnostics(
                    task,
                    error_type="orchestrator_append_failed",
                    mode="blocked_invalid_structure",
                    step=step,
                    attempt=attempt,
                )
                raise ValueError(f"Unable to sync workdoc for step '{step}': orchestrator_append_failed")
            canonical.write_text(summary_updated, encoding="utf-8")
            worktree_copy.write_text(summary_updated, encoding="utf-8")
            changed = True
            sync_mode = "fallback_append"
            sync_reason = "orchestrator_summary"

        if changed:
            self.clear_sync_diagnostics(task)

        if changed:
            payload: dict[str, object] = {"step": step}
            if sync_mode:
                payload["sync_mode"] = sync_mode
            if sync_reason:
                payload["reason"] = sync_reason
            self.bus.emit(
                channel="tasks",
                event_type="workdoc.updated",
                entity_id=task.id,
                payload=payload,
            )

    def sync_workdoc_review(self, task: Task, cycle: ReviewCycle, project_dir: Path) -> None:
        """Append review cycle findings to canonical/worktree workdoc."""
        canonical = self.workdoc_canonical_path(task.id)
        if not canonical.exists():
            return

        text = canonical.read_text(encoding="utf-8")
        lines: list[str] = [
            f"### Review Cycle {cycle.attempt} — {cycle.decision}",
        ]
        counts = cycle.open_counts or {}
        lines.append(
            "Open findings: "
            f"critical={int(counts.get('critical', 0))}, "
            f"high={int(counts.get('high', 0))}, "
            f"medium={int(counts.get('medium', 0))}, "
            f"low={int(counts.get('low', 0))}"
        )
        for finding in cycle.findings:
            if finding.file and finding.line:
                loc = f" ({finding.file}:{finding.line})"
            elif finding.file:
                loc = f" ({finding.file})"
            else:
                loc = ""
            category = f"[{finding.category}] " if finding.category else ""
            lines.append(f"- **[{finding.severity}]** {category}{finding.summary}{loc}")
            if finding.suggested_fix:
                lines.append(f"  - Suggested fix: {finding.suggested_fix}")
            if finding.status and finding.status != "open":
                lines.append(f"  - Status: {finding.status}")
        block = "\n".join(lines)

        placeholder = "_Pending: will be populated by the review step._"
        if placeholder in text:
            updated = text.replace(placeholder, block, 1)
        else:
            heading = "## Review Findings"
            idx = text.find(heading)
            if idx == -1:
                updated = text.rstrip() + "\n\n" + heading + "\n\n" + block + "\n"
            else:
                next_heading = re.search(r"^## ", text[idx + len(heading) :], re.MULTILINE)
                if next_heading:
                    insert_pos = idx + len(heading) + next_heading.start()
                    updated = text[:insert_pos] + block + "\n\n" + text[insert_pos:]
                else:
                    updated = text.rstrip() + "\n\n" + block + "\n"

        canonical.write_text(updated, encoding="utf-8")
        worktree_copy = self.workdoc_worktree_path(project_dir)
        if worktree_copy.exists():
            worktree_copy.write_text(updated, encoding="utf-8")

        self.bus.emit(
            channel="tasks",
            event_type="workdoc.updated",
            entity_id=task.id,
            payload={"step": "review", "cycle": cycle.attempt},
        )

    def append_retry_attempt_marker(
        self,
        task: Task,
        *,
        project_dir: Path,
        attempt: int,
        start_from_step: str | None = None,
    ) -> None:
        """Append a retry-attempt marker block to canonical/worktree workdoc."""
        canonical = self.workdoc_canonical_path(task.id)
        if not canonical.exists():
            return

        text = canonical.read_text(encoding="utf-8")
        heading = f"## Retry Attempt {int(attempt)}"
        if heading in text:
            return

        lines = [heading]
        guidance = ""
        previous_error = ""
        if isinstance(task.metadata, dict):
            retry_guidance = task.metadata.get("retry_guidance")
            if isinstance(retry_guidance, dict):
                guidance = str(retry_guidance.get("guidance") or "").strip()
                previous_error = str(retry_guidance.get("previous_error") or "").strip()

        if start_from_step:
            lines.append(f"- Start from step: {start_from_step}")
        if guidance:
            lines.append(f"- Guidance: {guidance}")
        if previous_error:
            first_line = next((line.strip() for line in previous_error.splitlines() if line.strip()), "")
            compact = first_line[:240]
            if len(first_line) > 240:
                compact += "..."
            if compact:
                lines.append(f"- Previous error: {compact}")

        block = "\n".join(lines)
        updated = text.rstrip() + "\n\n" + block + "\n"
        canonical.write_text(updated, encoding="utf-8")
        worktree_copy = self.workdoc_worktree_path(project_dir)
        if worktree_copy.exists():
            worktree_copy.write_text(updated, encoding="utf-8")

        self.bus.emit(
            channel="tasks",
            event_type="workdoc.updated",
            entity_id=task.id,
            payload={
                "step": "retry",
                "attempt": int(attempt),
                "start_from_step": start_from_step,
                "has_guidance": bool(guidance),
            },
        )
    @staticmethod
    def _section_bounds(text: str, heading: str) -> tuple[int, int] | None:
        """Return [start, end) bounds for the content under ``heading``."""
        idx = text.find(heading)
        if idx == -1:
            return None
        newline_after = text.find("\n", idx)
        if newline_after == -1:
            return None
        start = newline_after + 1
        rest = text[start:]
        next_heading = re.search(r"^## ", rest, re.MULTILINE)
        end = start + next_heading.start() if next_heading else len(text)
        return start, end

    @staticmethod
    def _sentinel_section_bounds(text: str, section_id: str) -> tuple[str, tuple[int, int] | None, bool]:
        """Return (state, bounds, has_markers) for sentinel section parsing.

        States: ``valid``, ``missing``, ``malformed``.
        """
        raw_marker_re = re.compile(r"<!--\s*WORKDOC:SECTION\b[^>]*-->")
        strict_marker_re = re.compile(r"<!--\s*WORKDOC:SECTION\s+([a-z0-9_]+)\s+(START|END)\s*-->")
        raw_markers = list(raw_marker_re.finditer(text))
        if not raw_markers:
            return "missing", None, False
        markers: list[tuple[re.Match[str], str, str]] = []
        for raw in raw_markers:
            strict = strict_marker_re.fullmatch(raw.group(0))
            if strict is None:
                return "malformed", None, True
            markers.append((raw, strict.group(1), strict.group(2)))

        open_sections: dict[str, int] = {}
        completed_sections: set[str] = set()
        start_content: int | None = None
        end_content: int | None = None
        for raw, sid, kind in markers:
            if kind == "START":
                if sid in open_sections:
                    return "malformed", None, True
                if sid == section_id and sid in completed_sections:
                    # Duplicate section blocks for the same sentinel ID are malformed.
                    return "malformed", None, True
                open_sections[sid] = raw.end()
                if sid == section_id:
                    start_content = raw.end()
            else:
                if sid not in open_sections:
                    return "malformed", None, True
                start_pos = open_sections.pop(sid)
                completed_sections.add(sid)
                if sid == section_id:
                    start_content = start_pos
                    end_content = raw.start()
        if open_sections:
            return "malformed", None, True
        if start_content is not None and end_content is not None and start_content <= end_content:
            return "valid", (start_content, end_content), True
        return "missing", None, True

    @classmethod
    def _append_summary_under_heading(
        cls,
        canonical_text: str,
        *,
        heading: str,
        placeholder_step: str | None,
        step: str,
        summary: str,
        attempt: int | None,
    ) -> str | None:
        """Append or replace summary content inside the target heading block."""
        if placeholder_step:
            placeholder = f"_Pending: will be populated by the {placeholder_step} step._"
        else:
            placeholder = "_Pending: will be populated as needed._"
        trimmed = summary.strip()
        attempt_num = int(attempt or 1)
        if step == "implement_fix":
            trimmed = f"### Fix Cycle {attempt_num}\n{trimmed}"
        else:
            trimmed = f"### Attempt {attempt_num}\n{trimmed}"
        section_id = cls._WORKDOC_SENTINEL_ID_MAP.get(step)
        if section_id is not None:
            state, bounds, _ = cls._sentinel_section_bounds(canonical_text, section_id)
            if state == "valid" and bounds is not None:
                start, end = bounds
                body = canonical_text[start:end]
                if placeholder in body:
                    updated_body = body.replace(placeholder, trimmed, 1)
                else:
                    body_stripped = body.rstrip()
                    if body_stripped:
                        updated_body = f"{body_stripped}\n\n{trimmed}\n"
                    else:
                        updated_body = f"{trimmed}\n"
                return canonical_text[:start] + updated_body + canonical_text[end:]
            if state == "malformed":
                return None
        if placeholder in canonical_text:
            return canonical_text.replace(placeholder, trimmed, 1)
        idx = canonical_text.find(heading)
        if idx == -1:
            return None
        newline_after = canonical_text.find("\n", idx)
        if newline_after == -1:
            return canonical_text + "\n\n" + trimmed
        rest = canonical_text[newline_after + 1 :]
        next_heading = re.search(r"^## ", rest, re.MULTILINE)
        if next_heading:
            insert_pos = newline_after + 1 + next_heading.start()
            return canonical_text[:insert_pos] + trimmed + "\n\n" + canonical_text[insert_pos:]
        return canonical_text.rstrip() + "\n\n" + trimmed + "\n"

    def repair_missing_section(self, task: Task, step: str) -> bool:
        """Re-inject a missing workdoc section from the pipeline template.

        Returns True if the canonical workdoc was repaired, False otherwise.
        """
        canonical = self.workdoc_canonical_path(task.id)
        if not canonical.exists():
            return False
        section = self.workdoc_section_for_step(task, step)
        if not section:
            return False
        heading, placeholder_step = section
        try:
            canonical_text = canonical.read_text(encoding="utf-8")
        except OSError:
            return False
        # Already present — nothing to repair.
        if heading in canonical_text:
            return False
        # Build the section block with sentinels.
        section_id = self._WORKDOC_SENTINEL_ID_MAP.get(step)
        if placeholder_step:
            placeholder = f"_Pending: will be populated by the {placeholder_step} step._"
        else:
            placeholder = "_Pending: will be populated as needed._"
        if section_id:
            start_marker = f"<!-- WORKDOC:SECTION {section_id} START -->"
            end_marker = f"<!-- WORKDOC:SECTION {section_id} END -->"
            block = f"{heading}\n\n{start_marker}\n{placeholder}\n{end_marker}\n"
        else:
            block = f"{heading}\n\n{placeholder}\n"
        repaired = canonical_text.rstrip() + "\n\n" + block
        canonical.write_text(repaired, encoding="utf-8")
        # Update worktree copy if the task has a worktree dir.
        worktree_dir = (task.metadata or {}).get("worktree_dir")
        if worktree_dir:
            worktree_copy = self.workdoc_worktree_path(Path(worktree_dir))
            if worktree_copy.parent.exists():
                worktree_copy.write_text(repaired, encoding="utf-8")
        return True

    @classmethod
    def clear_sync_diagnostics(cls, task: Task) -> None:
        """Remove workdoc sync diagnostic metadata from a task."""
        if not isinstance(task.metadata, dict):
            task.metadata = {}
        for key in cls._SYNC_DIAGNOSTIC_KEYS:
            task.metadata.pop(key, None)

    @classmethod
    def _set_sync_diagnostics(
        cls,
        task: Task,
        *,
        error_type: str,
        mode: str,
        step: str,
        attempt: int | None,
    ) -> None:
        """Persist normalized sync diagnostic metadata on a task."""
        if not isinstance(task.metadata, dict):
            task.metadata = {}
        task.metadata["workdoc_sync_error_type"] = error_type
        task.metadata["workdoc_sync_mode"] = mode
        task.metadata["workdoc_sync_step"] = step
        task.metadata["workdoc_sync_attempt"] = int(attempt or 1)
