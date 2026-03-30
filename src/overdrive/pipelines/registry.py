"""Pipeline template registry — defines step sequences for different task types.

A *PipelineTemplate* describes the ordered steps a task goes through, along with
optional conditions, parallel groups, and per-step configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step definition within a template
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StepDef:
    """One step in a pipeline template."""
    name: str                                      # references StepRegistry key
    display_name: str = ""                         # human-readable label
    required: bool = True                          # can be skipped?
    condition: Optional[str] = None                # skip-rule expression (evaluated at runtime)
    timeout_seconds: int = 0                        # max time for this step; 0 = no timeout
    retry_limit: int = 3                           # retries before escalating
    agent_role: Optional[str] = None               # preferred agent role (None = auto)
    config: dict[str, Any] = field(default_factory=dict)  # step-specific config


# ---------------------------------------------------------------------------
# Pipeline Template
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PipelineTemplate:
    """Immutable template defining how a type of task is executed."""
    id: str
    display_name: str
    description: str
    steps: tuple[StepDef, ...]
    task_types: tuple[str, ...] = ()     # which task types use this by default
    allow_skip: bool = True              # can user skip individual steps?
    allow_reorder: bool = False          # can steps be reordered at runtime?
    metadata: dict[str, Any] = field(default_factory=dict)

    def step_names(self) -> list[str]:
        """List step ids in execution order for this template.

        Returns:
            list[str]: Step names in the order the pipeline executes them.
        """
        return [s.name for s in self.steps]


# ---------------------------------------------------------------------------
# Built-in templates
# ---------------------------------------------------------------------------

FEATURE_PIPELINE = PipelineTemplate(
    id="feature",
    display_name="Feature Implementation",
    description="Full feature lifecycle: plan, implement, verify, review, commit.",
    task_types=("feature",),
    steps=(
        StepDef(name="plan", display_name="Plan"),
        StepDef(name="implement", display_name="Implement"),
        StepDef(name="verify", display_name="Verify"),
        StepDef(name="review", display_name="Review"),
        StepDef(name="commit", display_name="Commit"),
    ),
    metadata={"supports_skip_to_precommit": True},
)

BUG_FIX_PIPELINE = PipelineTemplate(
    id="bug_fix",
    display_name="Bug Fix",
    description="Diagnose, fix, verify, review, commit.",
    task_types=("bug",),
    steps=(
        StepDef(name="diagnose", display_name="Diagnose"),
        StepDef(name="implement", display_name="Fix"),
        StepDef(name="verify", display_name="Verify"),
        StepDef(name="review", display_name="Review"),
        StepDef(name="commit", display_name="Commit"),
    ),
    metadata={"supports_skip_to_precommit": True},
)

REFACTOR_PIPELINE = PipelineTemplate(
    id="refactor",
    display_name="Refactoring",
    description="Analyze current code, plan refactor, implement, verify, review.",
    task_types=("refactor",),
    steps=(
        StepDef(name="analyze", display_name="Analyze"),
        StepDef(name="plan", display_name="Plan"),
        StepDef(name="implement", display_name="Implement"),
        StepDef(name="verify", display_name="Verify"),
        StepDef(name="review", display_name="Review"),
        StepDef(name="commit", display_name="Commit"),
    ),
    metadata={"supports_skip_to_precommit": True},
)

RESEARCH_PIPELINE = PipelineTemplate(
    id="research",
    display_name="Research",
    description="Analyze findings and produce a research analysis.",
    task_types=("research",),
    steps=(
        StepDef(name="analyze", display_name="Analyze"),
    ),
)

DOCS_PIPELINE = PipelineTemplate(
    id="docs",
    display_name="Documentation",
    description="Analyze docs scope, write documentation, verify docs quality, review, commit.",
    task_types=("docs",),
    steps=(
        StepDef(name="analyze", display_name="Analyze Code"),
        StepDef(name="implement", display_name="Write Docs"),
        StepDef(name="verify", display_name="Verify Docs"),
        StepDef(name="review", display_name="Review"),
        StepDef(name="commit", display_name="Commit"),
    ),
    metadata={"supports_skip_to_precommit": True},
)

TEST_PIPELINE = PipelineTemplate(
    id="test",
    display_name="Testing",
    description="Analyze coverage, write tests, verify, commit.",
    task_types=("test",),
    steps=(
        StepDef(name="analyze", display_name="Analyze Coverage"),
        StepDef(name="implement", display_name="Write Tests"),
        StepDef(name="verify", display_name="Run Tests"),
        StepDef(name="review", display_name="Review"),
        StepDef(name="commit", display_name="Commit"),
    ),
    metadata={"supports_skip_to_precommit": True},
)

REPO_REVIEW_PIPELINE = PipelineTemplate(
    id="repo_review",
    display_name="Repository Review",
    description="Analyze repository state, produce an initiative plan, and generate improvement tasks.",
    task_types=("repo_review",),
    steps=(
        StepDef(name="analyze", display_name="Analyze"),
        StepDef(name="initiative_plan", display_name="Initiative Plan"),
        StepDef(name="generate_tasks", display_name="Generate Tasks"),
    ),
)

SECURITY_AUDIT_PIPELINE = PipelineTemplate(
    id="security_audit",
    display_name="Security Audit",
    description="Scan dependencies and code for security issues.",
    task_types=("security", "security_audit"),
    steps=(
        StepDef(name="scan_deps", display_name="Scan Dependencies"),
        StepDef(name="scan_code", display_name="Scan Code"),
        StepDef(name="generate_tasks", display_name="Generate Fix Tasks"),
    ),
)

REVIEW_PIPELINE = PipelineTemplate(
    id="review",
    display_name="Code Review",
    description="Analyze existing work and review changes.",
    task_types=("review",),
    steps=(
        StepDef(name="analyze", display_name="Analyze"),
        StepDef(name="review", display_name="Review"),
    ),
)

COMMIT_REVIEW_PIPELINE = PipelineTemplate(
    id="commit_review",
    display_name="Commit Review",
    description="Review a completed task's commit, fix issues found, and verify corrections.",
    task_types=("commit_review",),
    steps=(
        StepDef(name="commit_review", display_name="Review Commit"),
        StepDef(name="implement", display_name="Implement Fixes"),
        StepDef(name="verify", display_name="Verify"),
        StepDef(name="review", display_name="Review"),
        StepDef(name="commit", display_name="Commit"),
    ),
    metadata={"supports_skip_to_precommit": True},
)

PR_REVIEW_PIPELINE = PipelineTemplate(
    id="pr_review",
    display_name="PR Review",
    description="Review a GitHub pull request's changes, fix issues found, and verify corrections.",
    task_types=("pr_review",),
    steps=(
        StepDef(name="pr_review", display_name="Review PR"),
        StepDef(name="implement", display_name="Implement Fixes"),
        StepDef(name="verify", display_name="Verify"),
        StepDef(name="review", display_name="Review"),
        StepDef(name="commit", display_name="Commit"),
    ),
    metadata={"supports_skip_to_precommit": True},
)

MR_REVIEW_PIPELINE = PipelineTemplate(
    id="mr_review",
    display_name="MR Review",
    description="Review a GitLab merge request's changes, fix issues found, and verify corrections.",
    task_types=("mr_review",),
    steps=(
        StepDef(name="mr_review", display_name="Review MR"),
        StepDef(name="implement", display_name="Implement Fixes"),
        StepDef(name="verify", display_name="Verify"),
        StepDef(name="review", display_name="Review"),
        StepDef(name="commit", display_name="Commit"),
    ),
    metadata={"supports_skip_to_precommit": True},
)

PR_REVIEW_COMMENT_PIPELINE = PipelineTemplate(
    id="pr_review_comment",
    display_name="PR Review & Comment",
    description="Fetch existing comments, review PR changes, and post review comments.",
    task_types=("pr_review_comment",),
    steps=(
        StepDef(name="fetch_comments", display_name="Fetch Comments"),
        StepDef(name="pr_review_comment", display_name="Review & Comment"),
        StepDef(name="post_comments", display_name="Post Comments"),
    ),
)

MR_REVIEW_COMMENT_PIPELINE = PipelineTemplate(
    id="mr_review_comment",
    display_name="MR Review & Comment",
    description="Fetch existing comments, review MR changes, and post review comments.",
    task_types=("mr_review_comment",),
    steps=(
        StepDef(name="fetch_comments", display_name="Fetch Comments"),
        StepDef(name="pr_review_comment", display_name="Review & Comment"),
        StepDef(name="post_comments", display_name="Post Comments"),
    ),
)

PR_REVIEW_SUMMARIZE_PIPELINE = PipelineTemplate(
    id="pr_review_summarize",
    display_name="PR Review Summary",
    description="Fetch existing comments and produce a review summary.",
    task_types=("pr_review_summarize",),
    steps=(
        StepDef(name="fetch_comments", display_name="Fetch Comments"),
        StepDef(name="pr_review_summarize", display_name="Summarize Review"),
    ),
)

MR_REVIEW_SUMMARIZE_PIPELINE = PipelineTemplate(
    id="mr_review_summarize",
    display_name="MR Review Summary",
    description="Fetch existing comments and produce an MR review summary.",
    task_types=("mr_review_summarize",),
    steps=(
        StepDef(name="fetch_comments", display_name="Fetch Comments"),
        StepDef(name="pr_review_summarize", display_name="Summarize Review"),
    ),
)

PR_REVIEW_FIX_ONLY_PIPELINE = PipelineTemplate(
    id="pr_review_fix_only",
    display_name="PR Review & Fix",
    description="Review PR changes, implement fixes, verify, review, and commit.",
    task_types=("pr_review_fix_only",),
    steps=(
        StepDef(name="pr_review", display_name="Review PR"),
        StepDef(name="implement", display_name="Implement Fixes"),
        StepDef(name="verify", display_name="Verify"),
        StepDef(name="review", display_name="Review"),
        StepDef(name="commit", display_name="Commit"),
    ),
    metadata={"supports_skip_to_precommit": True},
)

PR_REVIEW_FIX_RESPOND_PIPELINE = PipelineTemplate(
    id="pr_review_fix_respond",
    display_name="PR Review, Fix & Respond",
    description="Fetch comments, review and fix PR changes, then respond to comments.",
    task_types=("pr_review_fix_respond",),
    steps=(
        StepDef(name="fetch_comments", display_name="Fetch Comments"),
        StepDef(name="pr_review_fix_respond", display_name="Review & Plan Fixes"),
        StepDef(name="implement", display_name="Implement Fixes"),
        StepDef(name="verify", display_name="Verify"),
        StepDef(name="review", display_name="Review"),
        StepDef(name="post_comment_responses", display_name="Post Comment Responses"),
        StepDef(name="commit", display_name="Commit"),
    ),
    metadata={"supports_skip_to_precommit": True},
)

MR_REVIEW_FIX_RESPOND_PIPELINE = PipelineTemplate(
    id="mr_review_fix_respond",
    display_name="MR Review, Fix & Respond",
    description="Fetch comments, review and fix MR changes, then respond to comments.",
    task_types=("mr_review_fix_respond",),
    steps=(
        StepDef(name="fetch_comments", display_name="Fetch Comments"),
        StepDef(name="pr_review_fix_respond", display_name="Review & Plan Fixes"),
        StepDef(name="implement", display_name="Implement Fixes"),
        StepDef(name="verify", display_name="Verify"),
        StepDef(name="review", display_name="Review"),
        StepDef(name="post_comment_responses", display_name="Post Comment Responses"),
        StepDef(name="commit", display_name="Commit"),
    ),
    metadata={"supports_skip_to_precommit": True},
)

PERFORMANCE_PIPELINE = PipelineTemplate(
    id="performance",
    display_name="Performance Optimization",
    description="Profile baseline, plan optimization, implement, benchmark to verify improvement.",
    task_types=("performance",),
    steps=(
        StepDef(name="profile", display_name="Profile Baseline"),
        StepDef(name="plan", display_name="Plan Optimization"),
        StepDef(name="implement", display_name="Implement"),
        StepDef(name="benchmark", display_name="Benchmark"),
        StepDef(name="review", display_name="Review"),
        StepDef(name="commit", display_name="Commit"),
    ),
    metadata={"supports_skip_to_precommit": True},
)


HOTFIX_PIPELINE = PipelineTemplate(
    id="hotfix",
    display_name="Hotfix",
    description="Abbreviated bug fix: skip diagnosis, go straight to fix, verify, review, commit.",
    task_types=("hotfix",),
    steps=(
        StepDef(name="implement", display_name="Fix"),
        StepDef(name="verify", display_name="Verify"),
        StepDef(name="review", display_name="Review"),
        StepDef(name="commit", display_name="Commit"),
    ),
    metadata={"supports_skip_to_precommit": True},
)

SPIKE_PIPELINE = PipelineTemplate(
    id="spike",
    display_name="Spike",
    description="Timeboxed exploration with throwaway prototyping. No commit.",
    task_types=("spike",),
    steps=(
        StepDef(name="analyze", display_name="Analyze Context"),
        StepDef(name="prototype", display_name="Prototype"),
    ),
)

CHORE_PIPELINE = PipelineTemplate(
    id="chore",
    display_name="Chore",
    description="Mechanical code change: implement, verify, commit. No plan or review.",
    task_types=("chore",),
    steps=(
        StepDef(name="implement", display_name="Implement"),
        StepDef(name="verify", display_name="Verify"),
        StepDef(name="commit", display_name="Commit"),
    ),
    metadata={"supports_skip_to_precommit": True},
)

PLAN_ONLY_PIPELINE = PipelineTemplate(
    id="plan_only",
    display_name="Initiative Plan",
    description="Initiative-level planning with decomposition into executable tasks.",
    task_types=("initiative_plan", "plan_only", "plan", "decompose"),
    steps=(
        StepDef(name="analyze", display_name="Analyze"),
        StepDef(name="initiative_plan", display_name="Initiative Plan"),
        StepDef(name="generate_tasks", display_name="Generate Tasks"),
    ),
)

VERIFY_ONLY_PIPELINE = PipelineTemplate(
    id="verify_only",
    display_name="Verify Only",
    description="Run tests and checks on current state without making changes.",
    task_types=("verify_only", "verify"),
    steps=(
        StepDef(name="verify", display_name="Run Checks"),
    ),
)


BUILTIN_TEMPLATES: dict[str, PipelineTemplate] = {
    t.id: t
    for t in [
        FEATURE_PIPELINE,
        BUG_FIX_PIPELINE,
        REFACTOR_PIPELINE,
        RESEARCH_PIPELINE,
        DOCS_PIPELINE,
        TEST_PIPELINE,
        REPO_REVIEW_PIPELINE,
        SECURITY_AUDIT_PIPELINE,
        REVIEW_PIPELINE,
        COMMIT_REVIEW_PIPELINE,
        PR_REVIEW_PIPELINE,
        MR_REVIEW_PIPELINE,
        PR_REVIEW_COMMENT_PIPELINE,
        MR_REVIEW_COMMENT_PIPELINE,
        PR_REVIEW_SUMMARIZE_PIPELINE,
        MR_REVIEW_SUMMARIZE_PIPELINE,
        PR_REVIEW_FIX_ONLY_PIPELINE,
        PR_REVIEW_FIX_RESPOND_PIPELINE,
        MR_REVIEW_FIX_RESPOND_PIPELINE,
        PERFORMANCE_PIPELINE,
        HOTFIX_PIPELINE,
        SPIKE_PIPELINE,
        CHORE_PIPELINE,
        PLAN_ONLY_PIPELINE,
        VERIFY_ONLY_PIPELINE,
    ]
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class PipelineRegistry:
    """Registry of pipeline templates.

    Starts with built-in templates and allows registration of custom templates
    (e.g. loaded from user .overdrive/pipelines/ YAML files).
    """

    def __init__(self) -> None:
        """Initialize the PipelineRegistry."""
        self._templates: dict[str, PipelineTemplate] = dict(BUILTIN_TEMPLATES)
        self._type_mapping: dict[str, str] = {}
        self._rebuild_type_mapping()

    def _rebuild_type_mapping(self) -> None:
        self._type_mapping = {}
        for tmpl in self._templates.values():
            for tt in tmpl.task_types:
                self._type_mapping[tt] = tmpl.id

    # -- query ---------------------------------------------------------------

    def get(self, template_id: str) -> PipelineTemplate:
        """Fetch a pipeline template by id or raise ``KeyError`` if missing.

        Args:
            template_id (str): Pipeline template identifier, such as ``feature``.

        Returns:
            PipelineTemplate: Registered pipeline template matching ``template_id``.

        Raises:
            KeyError: If no template is registered for ``template_id``.
        """
        if template_id not in self._templates:
            available = ", ".join(sorted(self._templates.keys()))
            raise KeyError(f"Unknown pipeline '{template_id}' (available: {available})")
        return self._templates[template_id]

    def list_templates(self) -> list[PipelineTemplate]:
        """List all registered pipeline templates.

        Returns:
            list[PipelineTemplate]: Registered templates including built-ins and overrides.
        """
        return list(self._templates.values())

    def resolve_for_task_type(self, task_type: str) -> PipelineTemplate:
        """Resolve the default pipeline template for a task type.

        Args:
            task_type (str): Task type label (for example ``feature`` or ``bug_fix``).

        Returns:
            PipelineTemplate: Best-match template for the task type, defaulting to ``feature``.
        """
        tmpl_id = self._type_mapping.get(task_type)
        if tmpl_id:
            return self._templates[tmpl_id]
        # Default to feature pipeline for unknown types
        return self._templates["feature"]

    # -- mutation ------------------------------------------------------------

    def register(self, template: PipelineTemplate) -> None:
        """Register or replace a pipeline template.

        Args:
            template (PipelineTemplate): Template definition to store by ``template.id``.
        """
        self._templates[template.id] = template
        self._rebuild_type_mapping()

    def unregister(self, template_id: str) -> None:
        """Remove a template by id if present.

        Args:
            template_id (str): Template identifier to remove from the registry.
        """
        self._templates.pop(template_id, None)
        self._rebuild_type_mapping()

    # -- YAML loading --------------------------------------------------------

    def load_from_yaml(self, path: Path) -> None:
        """Load pipeline templates from YAML files.

        *path* may be either:
        - A single ``.yaml`` / ``.yml`` file defining one template, or
        - A directory containing multiple YAML files (one template each).

        Each YAML file must be a mapping with at least ``id``, ``display_name``,
        ``description``, and ``steps`` (a list of step definitions).

        Args:
            path (Path): File or directory containing pipeline template YAML definitions.

        Raises:
            RuntimeError: If PyYAML is unavailable in the current environment.
        """
        if yaml is None:
            raise RuntimeError(
                "PyYAML is required to load pipeline YAML files. Install pyyaml."
            )

        if path.is_dir():
            for child in sorted(path.iterdir()):
                if child.suffix in (".yaml", ".yml") and child.is_file():
                    self._load_single_yaml(child)
        elif path.is_file():
            self._load_single_yaml(path)
        else:
            logger.debug("Pipeline YAML path does not exist: %s", path)

    def _load_single_yaml(self, path: Path) -> None:
        """Parse one YAML file into a ``PipelineTemplate`` and register it."""
        try:
            with open(path, "r") as fh:
                data = yaml.safe_load(fh)
        except Exception:
            logger.warning("Failed to parse pipeline YAML: %s", path, exc_info=True)
            return

        if not isinstance(data, dict):
            logger.warning("Pipeline YAML root is not a mapping: %s", path)
            return

        template_id = data.get("id")
        if not template_id:
            logger.warning("Pipeline YAML missing 'id': %s", path)
            return

        # Parse steps
        raw_steps = data.get("steps", [])
        if not isinstance(raw_steps, list):
            logger.warning("Pipeline YAML 'steps' is not a list: %s", path)
            return

        step_defs: list[StepDef] = []
        for raw_step in raw_steps:
            if not isinstance(raw_step, dict) or "name" not in raw_step:
                logger.warning("Skipping step entry without 'name' in %s", path)
                continue

            step_kwargs: dict[str, Any] = {"name": raw_step["name"]}
            # Copy recognized StepDef fields
            for field_name in (
                "display_name", "required", "condition", "timeout_seconds",
                "retry_limit", "agent_role",
            ):
                if field_name in raw_step:
                    step_kwargs[field_name] = raw_step[field_name]
            if "config" in raw_step and isinstance(raw_step["config"], dict):
                step_kwargs["config"] = raw_step["config"]

            step_defs.append(StepDef(**step_kwargs))

        # Parse task_types
        task_types = data.get("task_types", ())
        if isinstance(task_types, list):
            task_types = tuple(task_types)

        # Build metadata from unknown keys
        _KNOWN = {"id", "display_name", "description", "steps", "task_types",
                   "allow_skip", "allow_reorder"}
        metadata = {k: v for k, v in data.items() if k not in _KNOWN}

        template = PipelineTemplate(
            id=template_id,
            display_name=data.get("display_name", template_id.replace("_", " ").title()),
            description=data.get("description", ""),
            steps=tuple(step_defs),
            task_types=task_types,
            allow_skip=data.get("allow_skip", True),
            allow_reorder=data.get("allow_reorder", False),
            metadata=metadata,
        )

        self.register(template)
