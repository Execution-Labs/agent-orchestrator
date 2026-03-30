"""Tests for the workdoc (working document) feature."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from overdrive.runtime.domain.models import ReviewCycle, ReviewFinding, RunRecord, Task, now_iso
from overdrive.runtime.events.bus import EventBus
from overdrive.runtime.orchestrator.service import OrchestratorService
from overdrive.runtime.orchestrator.worker_adapter import DefaultWorkerAdapter, StepResult
from overdrive.runtime.storage.bootstrap import ensure_state_root
from overdrive.runtime.storage.container import Container
from overdrive.pipelines.registry import BUILTIN_TEMPLATES


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    ensure_state_root(tmp_path)
    return tmp_path


@pytest.fixture()
def service(project_dir: Path) -> OrchestratorService:
    container = Container(project_dir)
    bus = EventBus(container.events, container.project_id)
    return OrchestratorService(container, bus, worker_adapter=DefaultWorkerAdapter())


@pytest.fixture()
def task() -> Task:
    return Task(
        title="Add login page",
        description="Build a login form with validation",
        task_type="feature",
        priority="P1",
    )


# ------------------------------------------------------------------
# _init_workdoc
# ------------------------------------------------------------------


def test_init_creates_canonical_and_worktree_copy(service: OrchestratorService, task: Task, project_dir: Path) -> None:
    canonical = service._init_workdoc(task, project_dir)

    assert canonical.exists()
    assert canonical == service.container.state_root / "workdocs" / f"{task.id}.md"

    worktree = project_dir / ".workdoc.md"
    assert worktree.exists()

    # Contents should match
    assert canonical.read_text() == worktree.read_text()

    # Template should be rendered
    content = canonical.read_text()
    assert task.title in content
    assert task.id in content
    assert task.task_type in content
    assert "## Plan" in content
    assert "## Implementation Log" in content
    assert "## Verification Results" in content
    assert "## Review Findings" in content
    assert "## Fix Log" in content
    assert "## Analysis" not in content
    assert "## Profiling Baseline" not in content
    assert "## Final Report" not in content
    assert "<!-- WORKDOC:SCHEMA v1 -->" in content
    assert "<!-- WORKDOC:SECTION plan START -->" in content
    assert "<!-- WORKDOC:SECTION implementation_log START -->" in content
    assert "<!-- WORKDOC:SECTION verification_results START -->" in content
    assert "<!-- WORKDOC:SECTION review_findings START -->" in content
    assert "<!-- WORKDOC:SECTION fix_log START -->" in content

    # Metadata should be set
    assert task.metadata["workdoc_path"] == str(canonical)


def test_init_sentinelization_does_not_wrap_headings_inside_task_description(
    service: OrchestratorService, project_dir: Path
) -> None:
    task = Task(
        title="Description headings",
        description="Context line\n## Plan\nThis heading is part of description.",
        task_type="feature",
        priority="P2",
    )
    canonical = service._init_workdoc(task, project_dir)
    content = canonical.read_text(encoding="utf-8")

    # Exactly one managed plan sentinel pair (the real section), even though description contains "## Plan".
    assert content.count("<!-- WORKDOC:SECTION plan START -->") == 1
    assert content.count("<!-- WORKDOC:SECTION plan END -->") == 1

    # Description text remains intact.
    assert "This heading is part of description." in content


def test_init_verify_only_uses_minimal_template(service: OrchestratorService, project_dir: Path) -> None:
    task = Task(
        title="Verify only",
        description="Run checks only",
        task_type="verify_only",
        priority="P2",
    )
    canonical = service._init_workdoc(task, project_dir)
    content = canonical.read_text()
    assert "## Verification Results" in content
    assert "## Final Report" not in content
    assert "## Plan" not in content
    assert "## Analysis" not in content
    assert "## Implementation Log" not in content
    assert "## Review Findings" not in content
    assert "## Fix Log" not in content


def test_retry_does_not_reinitialize_existing_workdoc(service: OrchestratorService) -> None:
    """Retry should reuse existing canonical workdoc instead of re-rendering template."""
    init_calls = {"count": 0}
    original_init = service._workdoc_manager.init_workdoc

    def counting_init(task: Task, proj_dir: Path) -> Path:
        init_calls["count"] += 1
        return original_init(task, proj_dir)

    service._workdoc_manager.init_workdoc = counting_init  # type: ignore[assignment]
    try:
        retry_task = Task(
            title="Retry init behavior",
            description="Lock expected retry semantics",
            task_type="feature",
            status="queued",
            hitl_mode="autopilot",
            pipeline_template=["plan", "implement"],
            metadata={
                "scripted_steps": {
                    "plan": {"status": "ok", "summary": "Attempt 1 plan details"},
                    "implement": {"status": "error", "summary": "Fail to trigger retry"},
                }
            },
        )
        service.container.tasks.upsert(retry_task)

        first = service.run_task(retry_task.id)
        assert first.status == "blocked"

        refreshed = service.container.tasks.get(retry_task.id)
        assert refreshed is not None
        refreshed.status = "queued"
        refreshed.error = None
        service.container.tasks.upsert(refreshed)

        second = service.run_task(retry_task.id)
        assert second.status == "blocked"
    finally:
        service._workdoc_manager.init_workdoc = original_init  # type: ignore[assignment]

    assert init_calls["count"] == 1


def test_init_plan_only_uses_planning_template(service: OrchestratorService, project_dir: Path) -> None:
    task = Task(
        title="Initiative plan",
        description="Define initiative scope and split to tasks",
        task_type="plan_only",
        priority="P2",
    )
    canonical = service._init_workdoc(task, project_dir)
    content = canonical.read_text()
    assert "## Analysis" in content
    assert "## Plan" in content
    assert "## Generated Tasks" in content
    assert "## Final Report" not in content
    assert "## Implementation Log" not in content
    assert "## Review Findings" not in content
    assert "## Fix Log" not in content


def test_init_security_audit_uses_scan_template(service: OrchestratorService, project_dir: Path) -> None:
    task = Task(
        title="Security sweep",
        description="Run dependency and code scans",
        task_type="security",
        priority="P1",
    )
    canonical = service._init_workdoc(task, project_dir)
    content = canonical.read_text()
    assert "## Dependency Scan Findings" in content
    assert "## Code Scan Findings" in content
    assert "## Security Report" not in content
    assert "## Generated Remediation Tasks" in content
    assert "## Plan" not in content
    assert "## Implementation Log" not in content
    assert "## Review Findings" not in content
    assert "## Fix Log" not in content
    assert "<!-- WORKDOC:SCHEMA v1 -->" in content
    assert "<!-- WORKDOC:SECTION dependency_scan_findings START -->" in content
    assert "<!-- WORKDOC:SECTION code_scan_findings START -->" in content
    assert "<!-- WORKDOC:SECTION final_report START -->" not in content
    assert "<!-- WORKDOC:SECTION generated_tasks START -->" in content


def test_init_repo_review_uses_review_template(service: OrchestratorService, project_dir: Path) -> None:
    task = Task(
        title="Repository review",
        description="Assess repo health and plan improvements",
        task_type="repo_review",
        priority="P2",
    )
    canonical = service._init_workdoc(task, project_dir)
    content = canonical.read_text()
    assert "## Repository Analysis" in content
    assert "## Initiative Plan" in content
    assert "## Generated Tasks" in content
    assert "## Implementation Log" not in content
    assert "## Verification Results" not in content
    assert "## Review Findings" not in content
    assert "## Fix Log" not in content


def test_init_research_uses_minimal_research_template(service: OrchestratorService, project_dir: Path) -> None:
    task = Task(
        title="Research topic",
        description="Investigate architecture alternatives",
        task_type="research",
        priority="P2",
    )
    canonical = service._init_workdoc(task, project_dir)
    content = canonical.read_text()
    assert "## Research Analysis" in content
    assert "## Final Report" not in content
    assert "## Plan" not in content
    assert "## Implementation Log" not in content
    assert "## Verification Results" not in content
    assert "## Review Findings" not in content
    assert "## Fix Log" not in content


def test_init_review_uses_review_template(service: OrchestratorService, project_dir: Path) -> None:
    task = Task(
        title="Code review",
        description="Review changes and report findings",
        task_type="review",
        priority="P2",
    )
    canonical = service._init_workdoc(task, project_dir)
    content = canonical.read_text()
    assert "## Review Analysis" in content
    assert "## Review Findings" in content
    assert "## Final Report" not in content
    assert "## Implementation Log" not in content
    assert "## Verification Results" not in content
    assert "## Fix Log" not in content


def test_init_bug_fix_uses_bug_template(service: OrchestratorService, project_dir: Path) -> None:
    task = Task(
        title="Fix auth crash",
        description="Resolve null pointer in auth flow",
        task_type="bug",
        priority="P1",
    )
    canonical = service._init_workdoc(task, project_dir)
    content = canonical.read_text()
    assert "## Diagnosis" in content
    assert "## Fix Implementation" in content
    assert "## Verification Results" in content
    assert "## Review Findings" in content
    assert "## Fix Log" in content
    assert "## Plan" not in content
    assert "## Final Report" not in content


def test_init_refactor_uses_refactor_template(service: OrchestratorService, project_dir: Path) -> None:
    task = Task(
        title="Refactor auth flow",
        description="Restructure auth internals without behavior changes",
        task_type="refactor",
        priority="P2",
    )
    canonical = service._init_workdoc(task, project_dir)
    content = canonical.read_text()
    assert "## Refactor Analysis" in content
    assert "## Refactor Plan" in content
    assert "## Refactor Implementation" in content
    assert "## Verification Results" in content
    assert "## Review Findings" in content
    assert "## Fix Log" in content
    assert "## Final Report" not in content
    assert "## Profiling Baseline" not in content


def test_init_performance_uses_performance_template(service: OrchestratorService, project_dir: Path) -> None:
    task = Task(
        title="Optimize API latency",
        description="Improve p95 response time under load",
        task_type="performance",
        priority="P1",
    )
    canonical = service._init_workdoc(task, project_dir)
    content = canonical.read_text()
    assert "## Profiling Baseline" in content
    assert "## Optimization Plan" in content
    assert "## Optimization Implementation" in content
    assert "## Benchmark Results" in content
    assert "## Review Findings" in content
    assert "## Fix Log" in content
    assert "## Final Report" not in content
    assert "## Verification Results" not in content


def test_init_test_uses_test_template(service: OrchestratorService, project_dir: Path) -> None:
    task = Task(
        title="Improve auth tests",
        description="Add missing integration coverage",
        task_type="test",
        priority="P2",
    )
    canonical = service._init_workdoc(task, project_dir)
    content = canonical.read_text()
    assert "## Coverage Analysis" in content
    assert "## Test Implementation" in content
    assert "## Verification Results" in content
    assert "## Review Findings" in content
    assert "## Fix Log" in content
    assert "## Plan" not in content
    assert "## Final Report" not in content


def test_init_docs_uses_docs_template(service: OrchestratorService, project_dir: Path) -> None:
    task = Task(
        title="Docs refresh",
        description="Update onboarding and API usage docs",
        task_type="docs",
        priority="P2",
    )
    canonical = service._init_workdoc(task, project_dir)
    content = canonical.read_text()
    assert "## Documentation Analysis" in content
    assert "## Documentation Updates" in content
    assert "## Verification Results" in content
    assert "## Review Findings" in content
    assert "## Fix Log" in content
    assert "## Plan" not in content
    assert "## Final Report" not in content


def test_init_hotfix_uses_hotfix_template(service: OrchestratorService, project_dir: Path) -> None:
    task = Task(
        title="Urgent auth fix",
        description="Patch production login issue",
        task_type="hotfix",
        priority="P0",
    )
    canonical = service._init_workdoc(task, project_dir)
    content = canonical.read_text()
    assert "## Hotfix Implementation" in content
    assert "## Verification Results" in content
    assert "## Review Findings" in content
    assert "## Fix Log" in content
    assert "## Plan" not in content
    assert "## Analysis" not in content
    assert "## Final Report" not in content


def test_init_chore_uses_chore_template(service: OrchestratorService, project_dir: Path) -> None:
    task = Task(
        title="Formatting cleanup",
        description="Apply standard formatting rules",
        task_type="chore",
        priority="P3",
    )
    canonical = service._init_workdoc(task, project_dir)
    content = canonical.read_text()
    assert "## Chore Implementation" in content
    assert "## Verification Results" in content
    assert "## Plan" not in content
    assert "## Review Findings" not in content
    assert "## Fix Log" in content
    assert "## Final Report" not in content


def test_init_spike_uses_spike_template(service: OrchestratorService, project_dir: Path) -> None:
    task = Task(
        title="Evaluate caching strategy",
        description="Timebox an exploratory prototype",
        task_type="spike",
        priority="P2",
    )
    canonical = service._init_workdoc(task, project_dir)
    content = canonical.read_text()
    assert "## Spike Analysis" in content
    assert "## Prototype Notes" in content
    assert "## Final Report" not in content
    assert "## Review Findings" not in content
    assert "## Fix Log" not in content
    assert "## Verification Results" not in content


def test_builtin_pipelines_use_dedicated_templates(service: OrchestratorService) -> None:
    """All built-in pipelines should avoid falling back to generic workdoc template."""
    representative_task_types = {
        "feature": "feature",
        "bug_fix": "bug",
        "refactor": "refactor",
        "research": "research",
        "docs": "docs",
        "test": "test",
        "repo_review": "repo_review",
        "security_audit": "security",
        "review": "review",
        "commit_review": "commit_review",
        "pr_review": "pr_review",
        "mr_review": "mr_review",
        "pr_review_comment": "pr_review_comment",
        "mr_review_comment": "mr_review_comment",
        "pr_review_summarize": "pr_review_summarize",
        "mr_review_summarize": "mr_review_summarize",
        "pr_review_fix_only": "pr_review_fix_only",
        "pr_review_fix_respond": "pr_review_fix_respond",
        "mr_review_fix_respond": "mr_review_fix_respond",
        "performance": "performance",
        "hotfix": "hotfix",
        "spike": "spike",
        "chore": "chore",
        "plan_only": "initiative_plan",
        "verify_only": "verify_only",
    }
    assert set(representative_task_types.keys()) == set(BUILTIN_TEMPLATES.keys())

    for pipeline_id, task_type in representative_task_types.items():
        task = Task(title=f"Template check {pipeline_id}", task_type=task_type)
        template = service._workdoc_template_for_task(task)
        assert template != service._GENERIC_WORKDOC_TEMPLATE


def test_bootstrap_adds_workdoc_gitignore_entry(project_dir: Path) -> None:
    """Bootstrap's _ensure_gitignored adds .workdoc.md to .gitignore."""
    gitignore = project_dir / ".gitignore"
    assert gitignore.exists()
    lines = gitignore.read_text().splitlines()
    assert ".workdoc.md" in [line.strip() for line in lines]


def test_bootstrap_gitignore_idempotent(project_dir: Path) -> None:
    """Running ensure_state_root twice should not duplicate .gitignore entries."""
    from overdrive.runtime.storage.bootstrap import ensure_state_root
    ensure_state_root(project_dir)  # second call (first was in fixture)
    gitignore = project_dir / ".gitignore"
    content = gitignore.read_text()
    assert content.count(".workdoc.md") == 1


def test_bootstrap_gitignore_preserves_existing(tmp_path: Path) -> None:
    """Existing .gitignore content should be preserved."""
    from overdrive.runtime.storage.bootstrap import ensure_state_root
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("node_modules/\n.env\n", encoding="utf-8")
    ensure_state_root(tmp_path)
    content = gitignore.read_text()
    assert "node_modules/" in content
    assert ".env" in content
    assert ".workdoc.md" in content


# ------------------------------------------------------------------
# _cleanup_workdoc_for_commit
# ------------------------------------------------------------------


def test_cleanup_removes_worktree_copy(service: OrchestratorService, task: Task, project_dir: Path) -> None:
    service._init_workdoc(task, project_dir)
    worktree = project_dir / ".workdoc.md"
    assert worktree.exists()

    service._cleanup_workdoc_for_commit(project_dir)
    assert not worktree.exists()


def test_cleanup_noop_when_no_worktree_copy(project_dir: Path) -> None:
    """Should not raise when .workdoc.md doesn't exist."""
    OrchestratorService._cleanup_workdoc_for_commit(project_dir)
    assert not (project_dir / ".workdoc.md").exists()


# ------------------------------------------------------------------
# _refresh_workdoc
# ------------------------------------------------------------------


def test_refresh_copies_canonical_to_worktree(service: OrchestratorService, task: Task, project_dir: Path) -> None:
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    worktree = service._workdoc_worktree_path(project_dir)

    # Modify canonical to simulate orchestrator update
    updated = canonical.read_text().replace("_Pending: will be populated by the plan step._", "The actual plan.")
    canonical.write_text(updated, encoding="utf-8")

    service._refresh_workdoc(task, project_dir)

    assert worktree.read_text() == updated


def test_refresh_noop_when_no_canonical(service: OrchestratorService, task: Task, project_dir: Path) -> None:
    """If canonical doesn't exist, refresh should be a no-op."""
    service._refresh_workdoc(task, project_dir)
    # No exception raised, worktree not created
    assert not (project_dir / ".workdoc.md").exists()


# ------------------------------------------------------------------
# _sync_workdoc
# ------------------------------------------------------------------


def test_sync_accepts_worker_changes(service: OrchestratorService, task: Task, project_dir: Path) -> None:
    service._init_workdoc(task, project_dir)
    worktree = service._workdoc_worktree_path(project_dir)
    canonical = service._workdoc_canonical_path(task.id)

    # Worker modifies worktree
    modified = worktree.read_text().replace(
        "_Pending: will be populated by the plan step._",
        "# My detailed plan\n1. Do X\n2. Do Y",
    )
    worktree.write_text(modified, encoding="utf-8")

    service._sync_workdoc(task, "plan", project_dir, "some summary")

    # Canonical should now match the worker's version
    assert canonical.read_text() == modified


def test_sync_fallback_appends_summary(service: OrchestratorService, task: Task, project_dir: Path) -> None:
    """When worker didn't change the file, fallback should replace the placeholder."""
    service._init_workdoc(task, project_dir)

    service._sync_workdoc(task, "plan", project_dir, "Step 1: Do the thing\nStep 2: Verify")

    canonical = service._workdoc_canonical_path(task.id)
    content = canonical.read_text()
    assert "Step 1: Do the thing" in content
    assert "_Pending: will be populated by the plan step._" not in content


def test_sync_fallback_writes_analyze_to_analysis_section(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Analyze summaries should be persisted into ## Analysis."""
    task = Task(
        title="Refactor auth module",
        description="Consolidate auth checks",
        task_type="refactor",
        priority="P2",
    )
    service._init_workdoc(task, project_dir)

    service._sync_workdoc(task, "analyze", project_dir, "Current state: duplicated auth checks in two modules.")

    canonical = service._workdoc_canonical_path(task.id)
    content = canonical.read_text()
    assert "Current state: duplicated auth checks in two modules." in content
    assert "_Pending: will be populated by the analyze step._" not in content


def test_sync_fallback_appends_under_existing_content(service: OrchestratorService, task: Task, project_dir: Path) -> None:
    """When placeholder is already replaced, summary appends under the heading."""
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    worktree = service._workdoc_worktree_path(project_dir)

    # Replace placeholder first
    text = canonical.read_text().replace(
        "_Pending: will be populated by the verify step._",
        "All tests passed.",
    )
    canonical.write_text(text, encoding="utf-8")
    worktree.write_text(text, encoding="utf-8")

    service._sync_workdoc(task, "verify", project_dir, "Lint clean.")

    content = canonical.read_text()
    assert "All tests passed." in content
    assert "Lint clean." in content


def test_sync_fallback_writes_attempt_scoped_entries(service: OrchestratorService, task: Task, project_dir: Path) -> None:
    """Repeated syncs should append attempt-scoped blocks without deleting prior attempts."""
    service._init_workdoc(task, project_dir)

    service._sync_workdoc(task, "verify", project_dir, "Attempt one verification", attempt=1)
    service._sync_workdoc(task, "verify", project_dir, "Attempt two verification", attempt=2)

    canonical = service._workdoc_canonical_path(task.id)
    content = canonical.read_text()
    assert "### Attempt 1" in content
    assert "Attempt one verification" in content
    assert "### Attempt 2" in content
    assert "Attempt two verification" in content


def test_sync_fallback_keeps_attempt_entries_inside_verification_sentinel(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Repeated verification summaries must stay inside the managed sentinel block."""
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)

    service._sync_workdoc(task, "verify", project_dir, "first verify summary", attempt=1)
    service._sync_workdoc(task, "verify", project_dir, "second verify summary", attempt=2)

    content = canonical.read_text(encoding="utf-8")
    start = content.index("<!-- WORKDOC:SECTION verification_results START -->")
    end = content.index("<!-- WORKDOC:SECTION verification_results END -->")
    body = content[start:end]
    outside = content[end:]

    assert "### Attempt 1" in body
    assert "first verify summary" in body
    assert "### Attempt 2" in body
    assert "second verify summary" in body
    assert "### Attempt 2" not in outside
    assert "second verify summary" not in outside


def test_sync_worker_changes_preserve_retry_history_sections(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Worker edits should update the active section without deleting retry history markers."""
    service._init_workdoc(task, project_dir)
    service._append_retry_attempt_marker(task, project_dir=project_dir, attempt=2, start_from_step="plan")
    canonical = service._workdoc_canonical_path(task.id)
    worktree = service._workdoc_worktree_path(project_dir)

    worker_text = worktree.read_text().replace(
        "_Pending: will be populated by the plan step._",
        "Worker-authored plan content",
    )
    worker_text = worker_text.replace("## Retry Attempt 2", "## REMOVED BY WORKER")
    worktree.write_text(worker_text, encoding="utf-8")

    service._sync_workdoc(task, "plan", project_dir, "fallback plan summary")

    content = canonical.read_text()
    assert "Worker-authored plan content" in content
    assert "## Retry Attempt 2" in content
    assert "## REMOVED BY WORKER" not in content


def test_retry_attempt_marker_sanitizes_previous_error_to_single_line(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Retry marker should avoid dumping multiline transcript fragments into workdoc."""
    service._init_workdoc(task, project_dir)
    task.metadata["retry_guidance"] = {
        "guidance": "",
        "previous_error": "Worker stalled (no heartbeat)\nfrom noisy.trace import lines\nthinking\n...",
    }

    service._append_retry_attempt_marker(task, project_dir=project_dir, attempt=2, start_from_step="plan")

    content = service._workdoc_canonical_path(task.id).read_text(encoding="utf-8")
    assert "- Previous error: Worker stalled (no heartbeat)" in content
    assert "from noisy.trace import lines" not in content
    assert "\nthinking\n" not in content


def test_sync_worker_changes_fallback_when_legacy_bounds_missing(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """When target sentinel markers are missing, sync must fallback-append instead of no-op."""
    service._init_workdoc(task, project_dir)
    worktree = service._workdoc_worktree_path(project_dir)
    canonical = service._workdoc_canonical_path(task.id)

    worker_text = (
        worktree.read_text(encoding="utf-8")
        .replace("<!-- WORKDOC:SECTION plan START -->", "")
        .replace("<!-- WORKDOC:SECTION plan END -->", "")
    )
    worktree.write_text(worker_text, encoding="utf-8")

    service._sync_workdoc(task, "plan", project_dir, "fallback from missing bounds", attempt=2)

    updated = canonical.read_text(encoding="utf-8")
    assert "### Attempt 2" in updated
    assert "fallback from missing bounds" in updated


def test_sync_worker_changes_without_summary_raises_for_missing_bounds(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Worker changes with no summary and missing target sentinels should raise diagnostics."""
    service._init_workdoc(task, project_dir)
    worktree = service._workdoc_worktree_path(project_dir)

    worker_text = (
        worktree.read_text(encoding="utf-8")
        .replace("<!-- WORKDOC:SECTION plan START -->", "")
        .replace("<!-- WORKDOC:SECTION plan END -->", "")
    )
    worktree.write_text(worker_text, encoding="utf-8")

    with pytest.raises(ValueError, match="section_id_mismatch"):
        service._sync_workdoc(task, "plan", project_dir, "", attempt=1)


def test_sync_sentinel_merge_preserves_internal_h2_content(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Sentinel-based merge must preserve worker content containing internal h2 headings."""
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    worktree = service._workdoc_worktree_path(project_dir)

    base = canonical.read_text(encoding="utf-8")
    base = base.replace(
        "## Plan\n\n_Pending: will be populated by the plan step._\n",
        "## Plan\n<!-- WORKDOC:SECTION plan START -->\n_Pending: will be populated by the plan step._\n<!-- WORKDOC:SECTION plan END -->\n",
    )
    canonical.write_text(base, encoding="utf-8")
    worktree.write_text(base, encoding="utf-8")

    worker_text = worktree.read_text(encoding="utf-8").replace(
        "_Pending: will be populated by the plan step._",
        "Primary plan line\n## Worker Internal Heading\nTrailing details after internal heading",
    )
    worktree.write_text(worker_text, encoding="utf-8")

    service._sync_workdoc(task, "plan", project_dir, "fallback not expected", attempt=1)

    updated = canonical.read_text(encoding="utf-8")
    assert "Primary plan line" in updated
    assert "## Worker Internal Heading" in updated
    assert "Trailing details after internal heading" in updated


def test_sync_sentinel_id_mismatch_fallbacks_with_summary(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """When sentinel markers exist but target section ID is missing, fallback summary should be used."""
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    worktree = service._workdoc_worktree_path(project_dir)

    canonical_text = canonical.read_text(encoding="utf-8").replace(
        "## Plan\n\n_Pending: will be populated by the plan step._\n",
        "## Plan\n<!-- WORKDOC:SECTION plan START -->\n_Pending: will be populated by the plan step._\n<!-- WORKDOC:SECTION plan END -->\n",
    )
    worker_text = canonical_text.replace(
        "WORKDOC:SECTION plan START",
        "WORKDOC:SECTION implementation_log START",
    ).replace(
        "WORKDOC:SECTION plan END",
        "WORKDOC:SECTION implementation_log END",
    )
    canonical.write_text(canonical_text, encoding="utf-8")
    worktree.write_text(worker_text, encoding="utf-8")

    service._sync_workdoc(task, "plan", project_dir, "fallback because section mismatch", attempt=3)

    updated = canonical.read_text(encoding="utf-8")
    assert "### Attempt 3" in updated
    assert "fallback because section mismatch" in updated
    # Diagnostics are cleared after successful fallback_append to avoid
    # leaving stale error-like metadata on the task.
    assert task.metadata.get("workdoc_sync_error_type") is None
    assert task.metadata.get("workdoc_sync_mode") is None

    events = service.container.events.list_recent(limit=50)
    matching = [e for e in events if e.get("type") == "workdoc.updated" and e.get("entity_id") == task.id]
    assert matching
    payload = matching[-1].get("payload") or {}
    assert payload.get("sync_mode") == "fallback_append"
    assert payload.get("reason") == "section_id_mismatch"


def test_sync_sentinel_id_mismatch_without_summary_raises(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Sentinel section mismatch with no summary should fail explicitly."""
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    worktree = service._workdoc_worktree_path(project_dir)

    canonical_text = canonical.read_text(encoding="utf-8").replace(
        "## Plan\n\n_Pending: will be populated by the plan step._\n",
        "## Plan\n<!-- WORKDOC:SECTION plan START -->\n_Pending: will be populated by the plan step._\n<!-- WORKDOC:SECTION plan END -->\n",
    )
    worker_text = canonical_text.replace(
        "WORKDOC:SECTION plan START",
        "WORKDOC:SECTION implementation_log START",
    ).replace(
        "WORKDOC:SECTION plan END",
        "WORKDOC:SECTION implementation_log END",
    )
    canonical.write_text(canonical_text, encoding="utf-8")
    worktree.write_text(worker_text, encoding="utf-8")

    with pytest.raises(ValueError, match="section_id_mismatch"):
        service._sync_workdoc(task, "plan", project_dir, "", attempt=1)
    assert task.metadata.get("workdoc_sync_error_type") == "section_id_mismatch"
    assert task.metadata.get("workdoc_sync_mode") == "blocked_invalid_structure"
    assert task.metadata.get("workdoc_sync_step") == "plan"
    assert task.metadata.get("workdoc_sync_attempt") == 1


def test_sync_legacy_heading_merge_without_sentinels(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Legacy workdocs without sentinels should still merge by heading."""
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    worktree = service._workdoc_worktree_path(project_dir)

    strip_markers = lambda s: re.sub(r"\n?<!--\s*WORKDOC:[^>]+-->\n?", "\n", s)
    canonical_text = strip_markers(canonical.read_text(encoding="utf-8"))
    worktree_text = canonical_text.replace(
        "_Pending: will be populated by the plan step._",
        "Legacy worker-authored plan update",
    )
    canonical.write_text(canonical_text, encoding="utf-8")
    worktree.write_text(worktree_text, encoding="utf-8")

    service._sync_workdoc(task, "plan", project_dir, "unused summary", attempt=1)

    updated = canonical.read_text(encoding="utf-8")
    assert "Legacy worker-authored plan update" in updated
    assert "<!-- WORKDOC:SECTION" not in updated


def test_sync_blocks_on_malformed_canonical_sentinel_structure(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Malformed canonical sentinel pairs must fail explicitly."""
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    worktree = service._workdoc_worktree_path(project_dir)

    malformed = canonical.read_text(encoding="utf-8").replace("<!-- WORKDOC:SECTION plan END -->", "")
    canonical.write_text(malformed, encoding="utf-8")
    worktree.write_text(
        malformed.replace("_Pending: will be populated by the plan step._", "worker changed plan text"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Malformed canonical workdoc section markers"):
        service._sync_workdoc(task, "plan", project_dir, "", attempt=1)
    assert task.metadata.get("workdoc_sync_error_type") == "canonical_malformed"
    assert task.metadata.get("workdoc_sync_mode") == "blocked_invalid_structure"


def test_sync_blocks_on_duplicate_canonical_sentinel_section_id(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Duplicate START/END blocks for same sentinel ID must be treated as malformed."""
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    worktree = service._workdoc_worktree_path(project_dir)

    text = canonical.read_text(encoding="utf-8")
    duplicate_block = (
        "<!-- WORKDOC:SECTION plan START -->\n"
        "Duplicate plan section\n"
        "<!-- WORKDOC:SECTION plan END -->\n"
    )
    text = text.rstrip() + "\n\n" + duplicate_block
    canonical.write_text(text, encoding="utf-8")
    worktree.write_text(
        text.replace("_Pending: will be populated by the plan step._", "worker changed plan text"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Malformed canonical workdoc section markers"):
        service._sync_workdoc(task, "plan", project_dir, "", attempt=1)
    assert task.metadata.get("workdoc_sync_error_type") == "canonical_malformed"
    assert task.metadata.get("workdoc_sync_mode") == "blocked_invalid_structure"


def test_sync_blocks_on_malformed_sentinel_comment_format(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Malformed WORKDOC:SECTION comments should be treated as malformed, not missing."""
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    worktree = service._workdoc_worktree_path(project_dir)

    malformed = canonical.read_text(encoding="utf-8").replace(
        "<!-- WORKDOC:SECTION plan START -->",
        "<!-- WORKDOC:SECTION plan-v2 START -->",
    )
    canonical.write_text(malformed, encoding="utf-8")
    worktree.write_text(
        malformed.replace("_Pending: will be populated by the plan step._", "worker changed plan"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Malformed canonical workdoc section markers"):
        service._sync_workdoc(task, "plan", project_dir, "", attempt=1)
    assert task.metadata.get("workdoc_sync_error_type") == "canonical_malformed"
    assert task.metadata.get("workdoc_sync_mode") == "blocked_invalid_structure"


def test_shared_verification_section_appends_attempt_numbering_across_steps(
    service: OrchestratorService, project_dir: Path
) -> None:
    """verify/benchmark should share one section with append-only attempt blocks."""
    task = Task(
        title="Shared verification",
        description="Validate shared verification section behavior",
        task_type="feature",
        priority="P1",
    )
    service._init_workdoc(task, project_dir)

    service._sync_workdoc(task, "verify", project_dir, "verify attempt one", attempt=1)
    service._sync_workdoc(task, "benchmark", project_dir, "benchmark attempt two", attempt=2)

    canonical = service._workdoc_canonical_path(task.id)
    content = canonical.read_text(encoding="utf-8")
    assert "### Attempt 1" in content
    assert "verify attempt one" in content
    assert "### Attempt 2" in content
    assert "benchmark attempt two" in content


def test_sync_verify_accepts_worker_changes(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Verify step should accept worker edits to workdoc (worker writes results directly)."""
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    worktree = service._workdoc_worktree_path(project_dir)

    # Worker writes verification results directly.
    modified = worktree.read_text().replace(
        "_Pending: will be populated by the verify step._",
        "### Attempt 1\nAll tests passed (exit code 0)",
    )
    worktree.write_text(modified, encoding="utf-8")

    service._sync_workdoc(task, "verify", project_dir, "orchestrator verify summary")

    content = canonical.read_text()
    assert "### Attempt 1" in content
    assert "All tests passed (exit code 0)" in content


def test_sync_orchestrator_summary_clears_stale_sync_diagnostics(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Expected orchestrator summary writes should not leave sync error diagnostics behind."""
    service._init_workdoc(task, project_dir)
    task.metadata["workdoc_sync_error_type"] = "section_id_mismatch"
    task.metadata["workdoc_sync_mode"] = "blocked_invalid_structure"
    task.metadata["workdoc_sync_step"] = "plan"
    task.metadata["workdoc_sync_attempt"] = 9

    # Use verify step — when worker doesn't modify the file, the orchestrator
    # fallback-appends the summary and should clear stale diagnostics.
    service._sync_workdoc(task, "verify", project_dir, "orchestrator verify summary", attempt=1)

    assert task.metadata.get("workdoc_sync_error_type") is None
    assert task.metadata.get("workdoc_sync_mode") is None
    assert task.metadata.get("workdoc_sync_step") is None
    assert task.metadata.get("workdoc_sync_attempt") is None


def test_sync_implement_fix_accepts_worker_changes(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """implement_fix should accept worker edits to workdoc (worker writes fix log directly)."""
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    worktree = service._workdoc_worktree_path(project_dir)

    # Worker writes fix log directly.
    modified = worktree.read_text().replace(
        "_Pending: will be populated as needed._",
        "### Fix cycle 2\nFixed null check in parser.",
    )
    worktree.write_text(modified, encoding="utf-8")

    service._sync_workdoc(task, "implement_fix", project_dir, "Fixed null check in parser.", attempt=2)

    content = canonical.read_text()
    assert "### Fix cycle 2" in content
    assert "Fixed null check in parser." in content


def test_sync_report_ignores_worker_changes_and_uses_summary(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Report step should be orchestrator-managed and append into Final Report.

    No built-in pipeline includes a report step anymore, but custom pipelines
    may still use one.  This test validates the orchestrator-managed sync
    behaviour by manually injecting a Final Report section into the workdoc.
    """
    task = Task(
        title="Custom report task",
        description="Task with custom pipeline that includes a report step",
        task_type="feature",
        priority="P2",
    )
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    worktree = service._workdoc_worktree_path(project_dir)

    # Inject a Final Report section into both canonical and worktree workdocs
    # so the report step has a heading to append under.
    report_section = (
        "\n## Final Report\n\n"
        "<!-- WORKDOC:SECTION final_report START -->\n"
        "_Pending: will be populated by the report step._\n"
        "<!-- WORKDOC:SECTION final_report END -->\n"
    )
    canonical.write_text(canonical.read_text() + report_section, encoding="utf-8")
    worktree.write_text(worktree.read_text() + report_section, encoding="utf-8")
    before = canonical.read_text()

    modified = worktree.read_text().replace(
        "_Pending: will be populated by the report step._",
        "worker-edited report text",
    )
    worktree.write_text(modified, encoding="utf-8")

    service._sync_workdoc(task, "report", project_dir, "Final status: done with known risk A.")

    content = canonical.read_text()
    assert content != modified
    assert "Final status: done with known risk A." in content
    assert "worker-edited report text" not in content
    assert content != before


def test_sync_profile_ignores_worker_changes_and_uses_summary(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Profile step should be orchestrator-managed and append into Profiling Baseline."""
    task = Task(
        title="Performance baseline",
        description="Profile request latency",
        task_type="performance",
        priority="P1",
    )
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    worktree = service._workdoc_worktree_path(project_dir)
    before = canonical.read_text()

    modified = worktree.read_text().replace(
        "_Pending: will be populated by the profile step._",
        "worker-edited profile text",
    )
    worktree.write_text(modified, encoding="utf-8")

    service._sync_workdoc(task, "profile", project_dir, "Baseline p95=220ms, cpu=78% under load test.")

    content = canonical.read_text()
    assert content != modified
    assert "Baseline p95=220ms, cpu=78% under load test." in content
    assert "worker-edited profile text" not in content
    assert content != before


def test_sync_noop_when_no_canonical(service: OrchestratorService, task: Task, project_dir: Path) -> None:
    """When canonical doesn't exist, sync should be a no-op."""
    service._sync_workdoc(task, "plan", project_dir, "some summary")
    # No exception raised


def test_sync_noop_when_no_summary_and_no_worker_change(service: OrchestratorService, task: Task, project_dir: Path) -> None:
    """When worker didn't change and summary is empty, nothing changes."""
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    before = canonical.read_text()

    service._sync_workdoc(task, "plan", project_dir, "")

    assert canonical.read_text() == before


def test_sync_no_heading_for_unknown_step(service: OrchestratorService, task: Task, project_dir: Path) -> None:
    """Steps not in the section map should not modify the workdoc."""
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    before = canonical.read_text()

    service._sync_workdoc(task, "commit", project_dir, "committed SHA abc123")

    assert canonical.read_text() == before


def test_commit_plan_revision_updates_workdoc_plan_section(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Committing a plan revision should refresh the workdoc ## Plan section."""
    service.container.tasks.upsert(task)
    service._init_workdoc(task, project_dir)

    base = service.create_plan_revision(
        task_id=task.id,
        content="Initial plan text",
        source="worker_plan",
        step="plan",
    )
    manual = service.create_plan_revision(
        task_id=task.id,
        content="Manually edited committed plan",
        source="human_edit",
        parent_revision_id=base.id,
    )

    service.commit_plan_revision(task.id, manual.id)

    content = service._workdoc_canonical_path(task.id).read_text()
    assert "Manually edited committed plan" in content
    assert "_Pending: will be populated by the plan step._" not in content


def test_commit_plan_revision_preserves_plan_sentinels_with_markdown_headings(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    """Committing a markdown-rich plan must not corrupt canonical section markers."""
    service.container.tasks.upsert(task)
    service._init_workdoc(task, project_dir)

    base = service.create_plan_revision(
        task_id=task.id,
        content="Initial plan text",
        source="worker_plan",
        step="plan",
    )
    manual = service.create_plan_revision(
        task_id=task.id,
        content=(
            "## Objective\n"
            "- Add dropdown inputs.\n\n"
            "## Verification Plan\n"
            "- Run focused tests.\n"
        ),
        source="human_edit",
        parent_revision_id=base.id,
    )

    service.commit_plan_revision(task.id, manual.id)

    content = service._workdoc_canonical_path(task.id).read_text(encoding="utf-8")
    assert content.count("<!-- WORKDOC:SECTION plan START -->") == 1
    assert content.count("<!-- WORKDOC:SECTION plan END -->") == 1
    assert "## Implementation Log" in content
    assert "## Objective" in content
    assert "## Verification Plan" in content
    state, _, _ = service._workdoc_manager._sentinel_section_bounds(content, "plan")
    assert state == "valid"


# ------------------------------------------------------------------
# _sync_workdoc_review
# ------------------------------------------------------------------


def test_review_sync_appends_findings(service: OrchestratorService, task: Task, project_dir: Path) -> None:
    service._init_workdoc(task, project_dir)

    findings = [
        ReviewFinding(
            id="f1", task_id=task.id, severity="high",
            category="security", summary="SQL injection risk",
            file="app/db.py", line=42,
        ),
        ReviewFinding(
            id="f2", task_id=task.id, severity="low",
            category="style", summary="Missing docstring",
        ),
    ]
    cycle = ReviewCycle(
        task_id=task.id,
        attempt=1,
        findings=findings,
        open_counts={"critical": 0, "high": 1, "medium": 0, "low": 1},
        decision="changes_requested",
    )

    service._sync_workdoc_review(task, cycle, project_dir)

    canonical = service._workdoc_canonical_path(task.id)
    content = canonical.read_text()
    assert "### Review Cycle 1" in content
    assert "changes_requested" in content
    assert "Open findings: critical=0, high=1, medium=0, low=1" in content
    assert "SQL injection risk" in content
    assert "[security]" in content
    assert "app/db.py:42" in content
    assert "Missing docstring" in content
    assert "_Pending: will be populated by the review step._" not in content


def test_review_sync_file_without_line(service: OrchestratorService, task: Task, project_dir: Path) -> None:
    """Finding with file but no line should render file only, not 'file:None'."""
    service._init_workdoc(task, project_dir)

    findings = [
        ReviewFinding(
            id="f1", task_id=task.id, severity="medium",
            category="quality", summary="Missing tests",
            file="app/views.py", line=None,
        ),
    ]
    cycle = ReviewCycle(
        task_id=task.id,
        attempt=1,
        findings=findings,
        open_counts={"critical": 0, "high": 0, "medium": 1, "low": 0},
        decision="changes_requested",
    )

    service._sync_workdoc_review(task, cycle, project_dir)

    content = service._workdoc_canonical_path(task.id).read_text()
    assert "(app/views.py)" in content
    assert "None" not in content


def test_review_sync_appends_multiple_cycles(service: OrchestratorService, task: Task, project_dir: Path) -> None:
    service._init_workdoc(task, project_dir)

    for attempt in (1, 2):
        findings = [
            ReviewFinding(
                id=f"f{attempt}", task_id=task.id, severity="medium",
                category="quality", summary=f"Issue from cycle {attempt}",
            ),
        ]
        cycle = ReviewCycle(
            task_id=task.id,
            attempt=attempt,
            findings=findings,
            open_counts={"critical": 0, "high": 0, "medium": 1, "low": 0},
            decision="changes_requested" if attempt == 1 else "approved",
        )
        service._sync_workdoc_review(task, cycle, project_dir)

    content = service._workdoc_canonical_path(task.id).read_text()
    assert "### Review Cycle 1" in content
    assert "### Review Cycle 2" in content
    assert "Issue from cycle 1" in content
    assert "Issue from cycle 2" in content


def test_review_sync_includes_suggested_fix_and_status(
    service: OrchestratorService, task: Task, project_dir: Path
) -> None:
    service._init_workdoc(task, project_dir)
    findings = [
        ReviewFinding(
            id="f1",
            task_id=task.id,
            severity="high",
            category="correctness",
            summary="Null access in edge path",
            file="app/core.py",
            line=7,
            suggested_fix="Guard for None before dereference.",
            status="resolved",
        ),
    ]
    cycle = ReviewCycle(
        task_id=task.id,
        attempt=1,
        findings=findings,
        open_counts={"critical": 0, "high": 0, "medium": 0, "low": 0},
        decision="approved",
    )
    service._sync_workdoc_review(task, cycle, project_dir)

    content = service._workdoc_canonical_path(task.id).read_text()
    assert "[correctness]" in content
    assert "Suggested fix: Guard for None before dereference." in content
    assert "Status: resolved" in content


# ------------------------------------------------------------------
# get_workdoc
# ------------------------------------------------------------------


def test_get_workdoc_returns_content(service: OrchestratorService, task: Task, project_dir: Path) -> None:
    service._init_workdoc(task, project_dir)

    result = service.get_workdoc(task.id)
    assert result["task_id"] == task.id
    assert result["exists"] is True
    assert task.title in result["content"]


def test_get_workdoc_raises_when_missing(service: OrchestratorService, task: Task) -> None:
    with pytest.raises(FileNotFoundError):
        service.get_workdoc(task.id)


def test_run_non_review_step_blocks_when_workdoc_missing(
    service: OrchestratorService,
    task: Task,
    project_dir: Path,
) -> None:
    service._init_workdoc(task, project_dir)
    service._workdoc_canonical_path(task.id).unlink()
    run = RunRecord(task_id=task.id, status="in_progress", started_at=now_iso(), branch=None)

    ok = service._run_non_review_step(task, run, "implement", attempt=1)

    assert ok == "blocked"
    updated_task = service.container.tasks.get(task.id)
    assert updated_task is not None
    assert updated_task.status == "blocked"
    assert updated_task.error and "Missing required workdoc" in updated_task.error
    updated_run = service.container.runs.get(run.id)
    assert updated_run is not None
    assert updated_run.status == "blocked"


def test_run_non_review_step_blocks_when_workdoc_invalid_encoding(
    service: OrchestratorService,
    task: Task,
    project_dir: Path,
) -> None:
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    canonical.write_bytes(b"\xff\xfe\xfa")
    run = RunRecord(task_id=task.id, status="in_progress", started_at=now_iso(), branch=None)

    ok = service._run_non_review_step(task, run, "implement", attempt=1)

    assert ok == "blocked"
    updated_task = service.container.tasks.get(task.id)
    assert updated_task is not None
    assert updated_task.status == "blocked"
    assert updated_task.error and "Invalid workdoc encoding" in updated_task.error
    assert updated_task.metadata.get("invalid_workdoc_path")
    updated_run = service.container.runs.get(run.id)
    assert updated_run is not None
    assert updated_run.status == "blocked"


def test_run_non_review_step_degrades_when_worktree_workdoc_missing_during_sync(
    service: OrchestratorService,
    task: Task,
    project_dir: Path,
) -> None:
    """Graceful degradation: missing worktree workdoc stores orphaned summary."""
    service._init_workdoc(task, project_dir)
    worktree = service._workdoc_worktree_path(project_dir)
    run = RunRecord(task_id=task.id, status="in_progress", started_at=now_iso(), branch=None)

    def run_step_and_delete(*, task: Task, step: str, attempt: int) -> StepResult:
        worktree.unlink(missing_ok=True)
        return StepResult(status="ok", summary="step summary")

    service.worker_adapter.run_step = run_step_and_delete  # type: ignore[method-assign]

    ok = service._run_non_review_step(task, run, "plan", attempt=1)

    assert ok == "ok"
    updated_task = service.container.tasks.get(task.id)
    assert updated_task is not None
    assert updated_task.status != "blocked"
    orphaned = updated_task.metadata.get("orphaned_step_summaries")
    assert isinstance(orphaned, dict)
    assert orphaned.get("plan") == "step summary"


def test_run_non_review_step_degrades_on_mismatched_sentinels(
    service: OrchestratorService,
    task: Task,
    project_dir: Path,
) -> None:
    """Graceful degradation: mismatched sentinels don't block the pipeline."""
    service._init_workdoc(task, project_dir)
    canonical = service._workdoc_canonical_path(task.id)
    worktree = service._workdoc_worktree_path(project_dir)
    run = RunRecord(task_id=task.id, status="in_progress", started_at=now_iso(), branch=None)

    canonical_text = canonical.read_text(encoding="utf-8")
    canonical.write_text(canonical_text, encoding="utf-8")

    def run_step_with_empty_summary(*, task: Task, step: str, attempt: int) -> StepResult:
        current = worktree.read_text(encoding="utf-8")
        mismatched = current.replace(
            "WORKDOC:SECTION plan START",
            "WORKDOC:SECTION implementation_log START",
        ).replace(
            "WORKDOC:SECTION plan END",
            "WORKDOC:SECTION implementation_log END",
        )
        worktree.write_text(mismatched, encoding="utf-8")
        return StepResult(status="ok", summary="")

    service.worker_adapter.run_step = run_step_with_empty_summary  # type: ignore[method-assign]

    ok = service._run_non_review_step(task, run, "plan", attempt=2)

    assert ok == "ok"
    updated_task = service.container.tasks.get(task.id)
    assert updated_task is not None
    assert updated_task.status != "blocked"
    # Sync diagnostics should be cleared after graceful degradation.
    assert updated_task.metadata.get("workdoc_sync_error_type") is None


def test_run_non_review_step_repairs_missing_section_then_syncs(
    service: OrchestratorService,
    project_dir: Path,
) -> None:
    """Layer 2 recovery: missing section is repaired from template, sync retried."""
    chore_task = Task(
        title="Cleanup configs",
        description="Remove stale config files",
        task_type="chore",
        priority="P3",
    )
    service._init_workdoc(chore_task, project_dir)
    canonical = service._workdoc_canonical_path(chore_task.id)

    # Simulate a template that's missing the Fix Log section by removing it.
    text = canonical.read_text(encoding="utf-8")
    # Remove the Fix Log section (heading + sentinels + content).
    text = re.sub(
        r"## Fix Log\n.*?<!-- WORKDOC:SECTION fix_log END -->\n",
        "",
        text,
        flags=re.DOTALL,
    )
    canonical.write_text(text, encoding="utf-8")
    worktree = service._workdoc_worktree_path(project_dir)
    worktree.write_text(text, encoding="utf-8")

    run = RunRecord(task_id=chore_task.id, status="in_progress", started_at=now_iso(), branch=None)

    def run_step_ok(*, task: Task, step: str, attempt: int) -> StepResult:
        return StepResult(status="ok", summary="Fix applied successfully")

    service.worker_adapter.run_step = run_step_ok  # type: ignore[method-assign]

    ok = service._run_non_review_step(chore_task, run, "implement_fix", attempt=1)

    assert ok == "ok"
    updated_task = service.container.tasks.get(chore_task.id)
    assert updated_task is not None
    assert updated_task.status != "blocked"
    # The canonical workdoc should now contain the repaired section.
    repaired_text = canonical.read_text(encoding="utf-8")
    assert "## Fix Log" in repaired_text
