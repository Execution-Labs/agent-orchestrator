"""Worker adapter that dispatches pipeline steps to real Codex/Ollama providers."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from ...pipelines.registry import PipelineRegistry
from ...prompts import load as load_prompt
from ...workers.config import WorkerProviderSpec, get_workers_runtime_config, resolve_worker_for_step
from ...workers.diagnostics import test_worker
from ...worker import WorkerCancelledError
from ...workers.run import WorkerRunResult, run_worker
from ..domain.models import RunRecord, Task, now_iso
from ..domain.scope_contract import normalize_scope_contract
from ..storage.container import Container
from .env_resolver import resolve_env_vars
from .venv_detector import detect_python_venv
from .environment_preflight import (
    provider_has_capabilities,
    required_capabilities_for_step,
    run_environment_preflight,
    workers_environment_config,
)
from .human_guidance import render_human_guidance_prompt
from .worker_adapter import StepResult

logger = logging.getLogger(__name__)

# Step category mapping
_PLANNING_STEPS = {"plan", "analyze", "plan_refine", "initiative_plan", "initiative_plan_refine", "commit_review", "pr_review", "mr_review", "pr_review_comment", "pr_review_fix_respond"}
_IMPL_STEPS = {"implement", "prototype"}
_FIX_STEPS = {"implement_fix"}
_VERIFY_STEPS = {"verify", "benchmark"}
_REVIEW_STEPS = {"review"}
_REPORT_STEPS = {"report", "summarize", "pr_review_summarize"}
_SCAN_STEPS = {"scan_deps", "scan_code"}
_TASK_GEN_STEPS = {"generate_tasks"}
_DIAGNOSIS_STEPS = {"diagnose"}
_MERGE_RESOLVE_STEPS = {"resolve_merge"}
_DEP_ANALYSIS_STEPS = {"analyze_deps"}
_PIPELINE_CLASSIFICATION_STEPS = {"pipeline_classify"}

# Which prior step outputs each category should receive in its prompt.
# None = inject all available outputs (for reporting/summarize steps).
_STEP_OUTPUT_INJECTION: dict[str, tuple[str, ...] | None] = {
    "implementation": ("plan", "analyze", "diagnose", "profile"),
    "diagnosis": (),
    "planning": (),
    "review": ("verify", "benchmark"),
    "reporting": None,
    "task_generation": ("initiative_plan", "plan", "analyze", "scan_deps", "scan_code"),
    "fix": ("plan",),
    "scanning": ("scan_deps",),
}

_STEP_TIMEOUT_ALIASES = {"implement_fix": "implement"}
_DEFAULT_STEP_TIMEOUT_SECONDS = 0  # 0 = no timeout
_DEFAULT_HEARTBEAT_SECONDS = 60
_DEFAULT_HEARTBEAT_GRACE_SECONDS = 240
_DEFAULT_HEARTBEAT_GRACE_BY_STEP: dict[str, int] = {
    "implement": 300,
    "implement_fix": 300,
    "verify": 900,
    "benchmark": 900,
}
_HEARTBEAT_STALL_RETRY_GRACE_MULTIPLIER = 1.5

# ---------------------------------------------------------------------------
# Prompt layers
# ---------------------------------------------------------------------------

_STEP_PROMPT_FILES: dict[str, str] = {
    "planning": "steps/plan.md",
    "implementation": "steps/implement.md",
    "fix": "steps/fix.md",
    "verification": "steps/verify.md",
    "review": "steps/review.md",
    "reporting": "steps/report.md",
    "scanning": "steps/scan_code.md",
    "task_generation": "steps/task_generation.md",
    "diagnosis": "steps/diagnose.md",
    "merge_resolution": "steps/merge_resolution.md",
    "dependency_analysis": "steps/dependency_analysis.md",
    "pipeline_classification": "steps/pipeline_classify.md",
    "general": "steps/general.md",
}


_SETTINGS_PROMPT_STEPS: tuple[str, ...] = (
    "analyze",
    "analyze_deps",
    "benchmark",
    "commit_review",
    "diagnose",
    "mr_review",
    "generate_tasks",
    "implement",
    "implement_fix",
    "initiative_plan",
    "initiative_plan_refine",
    "pipeline_classify",
    "plan",
    "plan_refine",
    "pr_review",
    "pr_review_comment",
    "pr_review_fix_respond",
    "pr_review_summarize",
    "profile",
    "prototype",
    "report",
    "resolve_merge",
    "review",
    "scan_code",
    "scan_deps",
    "summarize",
    "verify",
)

_LANGUAGE_DEFAULTS: dict[str, str] = {
    "python": (
        "### Python defaults\n"
        "- Google-style docstrings.\n"
        "- Type hints (Python 3.10+ syntax) on public APIs.\n"
        "- Prefer precise typing; avoid untyped public interfaces."
    ),
    "typescript": (
        "### TypeScript defaults\n"
        "- JSDoc on exported symbols where helpful.\n"
        "- Strict TypeScript; avoid `any` (prefer `unknown` + narrowing).\n"
        "- Preserve null/undefined safety."
    ),
    "javascript": (
        "### JavaScript defaults\n"
        "- JSDoc on exported symbols and non-trivial params/returns.\n"
        "- Keep runtime validation clear at module boundaries."
    ),
    "go": (
        "### Go defaults\n"
        "- Godoc conventions on exported symbols.\n"
        "- Return errors with context; avoid panics in library paths."
    ),
    "rust": (
        "### Rust defaults\n"
        "- `///` doc comments on public items.\n"
        "- Prefer `Result` propagation over `unwrap`/`expect` outside tests/tools."
    ),
}

_LANGUAGE_MARKERS: list[tuple[str, str]] = [
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("tsconfig.json", "typescript"),
    ("package.json", "javascript"),
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
]


_DEFAULT_PROJECT_COMMANDS: dict[str, dict[str, str]] = {
    "python": {
        "test": "pytest",
        "lint": "ruff check .",
        "typecheck": "mypy .",
    },
    "typescript": {
        "test": "npx vitest run 2>/dev/null || npm test",
        "lint": "npx eslint . 2>/dev/null || true",
        "typecheck": "npx tsc --noEmit",
    },
    "javascript": {
        "test": "npm test",
        "lint": "npx eslint . 2>/dev/null || true",
    },
    "go": {
        "test": "go test ./...",
        "lint": "golangci-lint run 2>/dev/null || true",
    },
    "rust": {
        "test": "cargo test",
        "lint": "cargo clippy -- -D warnings 2>/dev/null || true",
    },
}

_JS_TS_SOURCE_EXTENSIONS = {
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".vue",
    ".svelte",
}
_FRONTEND_HINT_PREFIXES = (
    "frontend/",
    "web/",
    "ui/",
)


def _instruction_prompt_name(step: str, task_type: str) -> str:
    """Resolve the markdown prompt template path for a pipeline step."""
    category = _step_category(step)
    pipeline_id = PipelineRegistry().resolve_for_task_type(task_type).id
    if step == "commit_review":
        return "steps/commit_review.md"
    if step == "pr_review":
        return "steps/pr_review.md"
    if step == "mr_review":
        return "steps/mr_review.md"
    if step == "pr_review_comment":
        return "steps/pr_review_comment.md"
    if step == "pr_review_summarize":
        return "steps/pr_review_summarize.md"
    if step == "pr_review_fix_respond":
        return "steps/pr_review_fix_respond.md"
    if step == "plan_refine":
        return "steps/plan_refine.md"
    if step == "initiative_plan_refine":
        return "steps/initiative_plan_refine.md"
    if step == "implement" and pipeline_id == "docs":
        return "steps/implement_docs.md"
    if step == "analyze":
        return "steps/analyze.md"
    if step == "initiative_plan":
        return "steps/initiative_plan.md"
    if step == "diagnose":
        return "steps/diagnose.md"
    if step == "prototype":
        return "steps/prototype.md"
    if step == "profile":
        return "steps/profile.md"
    if step == "scan_deps":
        return "steps/scan_deps.md"
    if step == "scan_code":
        return "steps/scan_code.md"
    if step == "summarize":
        return "steps/summarize.md"
    return _STEP_PROMPT_FILES[category]


def _normalize_prompt_overrides(value: Any) -> dict[str, str]:
    """Normalize per-step prompt override map from runtime config."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for raw_step, raw_prompt in value.items():
        step = str(raw_step or "").strip().lower()
        if not step:
            continue
        prompt = raw_prompt if isinstance(raw_prompt, str) else str(raw_prompt)
        if not prompt.strip():
            continue
        out[step] = prompt
    return out


def _normalize_prompt_injections(value: Any) -> dict[str, str]:
    """Normalize per-step additive prompt injections from runtime config."""
    return _normalize_prompt_overrides(value)




def get_configurable_step_prompt_defaults() -> dict[str, str]:
    """Return default prompt text for settings-managed pipeline steps."""
    return {step: load_prompt(_instruction_prompt_name(step, "feature")) for step in _SETTINGS_PROMPT_STEPS}


def _is_subpath(child: Path, parent: Path) -> bool:
    """Return True if *child* is a descendant of *parent*."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _apply_venv_to_defaults(
    defaults: dict[str, str],
    venv_bin_dir: Path,
    venv_rel_prefix: str,
) -> dict[str, str]:
    """Prefix default command executables with the venv bin path when available.

    For each command, checks whether the executable (first token) exists in
    *venv_bin_dir*.  If so, replaces it with a relative path like
    ``.venv/bin/pytest`` so ``_resolve_command_paths`` can later resolve it
    to an absolute path for worktree support.

    Args:
        defaults: Default commands dict (e.g. ``{"test": "pytest", ...}``).
        venv_bin_dir: Absolute path to the venv's ``bin/`` directory.
        venv_rel_prefix: Relative prefix from project_dir to venv bin dir
            (e.g. ``".venv/bin"``).

    Returns:
        New dict with executables prefixed where the binary exists in the venv.
    """
    result: dict[str, str] = {}
    for key, cmd in defaults.items():
        parts = cmd.split(None, 1)
        if not parts:
            result[key] = cmd
            continue
        exe_name = parts[0]
        # Only prefix bare command names (no path separators)
        if "/" not in exe_name and (venv_bin_dir / exe_name).is_file():
            prefixed = f"{venv_rel_prefix}/{exe_name}"
            result[key] = prefixed if len(parts) == 1 else f"{prefixed} {parts[1]}"
        else:
            result[key] = cmd
    return result


def _resolve_command_paths(
    commands: dict[str, dict[str, str]],
    project_dir: Path,
) -> dict[str, dict[str, str]]:
    """Resolve relative executable paths in project commands to absolute paths.

    If the first token of a command starts with '.' and contains '/'
    (e.g. ``.venv/bin/ruff``), resolve it against *project_dir* so that
    workers running in git worktrees (which lack gitignored dirs like
    ``.venv``) can still find the binary.
    """
    resolved: dict[str, dict[str, str]] = {}
    for lang, cmds in commands.items():
        resolved_cmds: dict[str, str] = {}
        for key, cmd in cmds.items():
            parts = cmd.split(None, 1)  # split on first whitespace
            if parts and parts[0].startswith(".") and "/" in parts[0]:
                abs_exe = str((project_dir / parts[0]).resolve())
                resolved_cmds[key] = abs_exe if len(parts) == 1 else f"{abs_exe} {parts[1]}"
            else:
                resolved_cmds[key] = cmd
        resolved[lang] = resolved_cmds
    return resolved


def detect_project_languages(project_dir: Path) -> list[str]:
    """Detect project languages from repository marker files.

    Args:
        project_dir (Path): Repository root to scan for known language marker
            files such as ``pyproject.toml`` and ``Cargo.toml``.

    Returns:
        list[str]: Ordered language identifiers inferred from marker presence.
            Duplicates are removed when multiple markers map to the same
            language. When both TypeScript and JavaScript markers are
            present, ``javascript`` is omitted because ``typescript`` is
            the stronger signal.
    """
    seen: dict[str, None] = {}  # ordered set
    for marker, lang in _LANGUAGE_MARKERS:
        if (project_dir / marker).exists() and lang not in seen:
            seen[lang] = None
    langs = list(seen)
    # TypeScript subsumes JavaScript — drop the weaker signal
    if "typescript" in seen and "javascript" in seen:
        langs.remove("javascript")
    return langs


def _check_binary_available(cmd: str, project_dir: Path) -> bool:
    """Check if the first token of a command is available on PATH or in project venv."""
    first_token = cmd.split()[0] if cmd.strip() else ""
    if not first_token:
        return False
    # Resolve relative paths against project dir
    candidate = project_dir / first_token
    if candidate.is_file():
        return True
    return shutil.which(first_token) is not None


def get_auto_detected_defaults(
    project_dir: Path,
) -> dict[str, Any]:
    """Return auto-detected project commands and environment info for display.

    This mirrors the detection logic used at execution time so the UI can
    show users what the worker will use when no overrides are configured.

    Returns:
        Dict with ``languages``, ``commands``, ``venv``,
        ``binary_warnings`` keys.
    """
    langs = detect_project_languages(project_dir)
    venv_info = detect_python_venv(project_dir)

    commands: dict[str, dict[str, str]] = {}
    for lang in langs:
        defaults = _DEFAULT_PROJECT_COMMANDS.get(lang)
        if not defaults:
            continue
        cmds = dict(defaults)
        if lang == "python" and venv_info is not None:
            rel_prefix = (
                str(venv_info.path.relative_to(project_dir) / "bin")
                if _is_subpath(venv_info.path, project_dir)
                else str(venv_info.bin_dir)
            )
            cmds = _apply_venv_to_defaults(cmds, venv_info.bin_dir, rel_prefix)
        if lang in ("javascript", "typescript"):
            cmds = _filter_npm_commands_by_scripts(cmds, project_dir)
        if cmds:
            commands[lang] = cmds

    # Check binary availability for each detected command
    binary_warnings: dict[str, list[str]] = {}
    for lang, cmds in commands.items():
        missing: list[str] = []
        for step, cmd in cmds.items():
            if not _check_binary_available(cmd, project_dir):
                missing.append(step)
        if missing:
            binary_warnings[lang] = missing

    venv_payload: dict[str, str] | None = None
    if venv_info is not None:
        venv_payload = {
            "path": str(venv_info.path),
            "source": venv_info.source,
        }

    return {
        "languages": langs,
        "commands": commands,
        "venv": venv_payload,
        "binary_warnings": binary_warnings,
    }


_LANGUAGE_DISPLAY_NAMES: dict[str, str] = {
    "python": "Python",
    "typescript": "TypeScript",
    "javascript": "JavaScript",
    "go": "Go",
    "rust": "Rust",
}


def _format_project_commands(
    project_commands: dict[str, dict[str, str]],
    project_languages: list[str],
) -> str:
    """Format project commands for detected languages into a prompt section."""
    _COMMAND_LABELS = {"test": "Test", "lint": "Lint", "typecheck": "Typecheck", "format": "Format"}
    blocks: list[tuple[str, list[str]]] = []
    for lang in project_languages:
        cmds = project_commands.get(lang)
        if not isinstance(cmds, dict):
            continue
        lines = []
        for key in ("test", "lint", "typecheck", "format"):
            val = cmds.get(key)
            if isinstance(val, str) and val.strip():
                lines.append(f"- {_COMMAND_LABELS[key]}: `{val.strip()}`")
        if lines:
            blocks.append((lang, lines))
    if not blocks:
        return ""
    parts = ["## Project commands"]
    if len(blocks) == 1:
        parts.extend(blocks[0][1])
    else:
        for lang, lines in blocks:
            display = _LANGUAGE_DISPLAY_NAMES.get(lang, lang.title())
            parts.append(f"### {display}")
            parts.extend(lines)
    return "\n".join(parts)


def _merge_token_usage(base: dict[str, Any], additional: dict[str, Any]) -> dict[str, Any]:
    """Sum token counts and cost from *additional* into *base*, returning a new dict."""
    merged = dict(base)
    for key in ("input_tokens", "output_tokens"):
        base_val = base.get(key)
        add_val = additional.get(key)
        if base_val is not None or add_val is not None:
            merged[key] = (base_val or 0) + (add_val or 0)
    base_cost = base.get("cost_usd")
    add_cost = additional.get("cost_usd")
    if base_cost is not None or add_cost is not None:
        merged["cost_usd"] = (base_cost or 0.0) + (add_cost or 0.0)
    return merged


def _step_category(step: str) -> str:
    if step in _PLANNING_STEPS:
        return "planning"
    if step in _IMPL_STEPS:
        return "implementation"
    if step in _FIX_STEPS:
        return "fix"
    if step in _VERIFY_STEPS:
        return "verification"
    if step in _REVIEW_STEPS:
        return "review"
    if step in _REPORT_STEPS:
        return "reporting"
    if step in _SCAN_STEPS:
        return "scanning"
    if step in _TASK_GEN_STEPS:
        return "task_generation"
    if step in _DIAGNOSIS_STEPS:
        return "diagnosis"
    if step in _MERGE_RESOLVE_STEPS:
        return "merge_resolution"
    if step in _DEP_ANALYSIS_STEPS:
        return "dependency_analysis"
    if step in _PIPELINE_CLASSIFICATION_STEPS:
        return "pipeline_classification"
    return "general"


# Steps that support early pipeline completion when no action is needed.
_EARLY_COMPLETE_STEPS = {
    "commit_review", "pr_review", "mr_review",    # review pipelines
    "pr_review_comment", "pr_review_summarize",   # comment-aware review pipelines
    "diagnose",                                    # bug_fix: no bug found
    "scan_code",                                   # security_audit: both scans clean
    "profile",                                     # performance: no bottlenecks
}


def _is_no_action_needed(step: str, summary: str | None) -> bool:
    """Detect whether a step's output indicates no further action is required.

    Applies to steps listed in ``_EARLY_COMPLETE_STEPS``.  The step prompts
    instruct the worker to output "No issues found" when the analysis
    determines no downstream work is needed.

    Args:
        step: Pipeline step name that produced the output.
        summary: Worker summary text to inspect.

    Returns:
        True if the step signals that no downstream work is needed.
    """
    if step not in _EARLY_COMPLETE_STEPS:
        return False
    if not summary:
        return False
    lower = summary.lower()
    if "no issues found" in lower:
        return True
    if step == "diagnose" and ("no bug found" in lower or "no bug identified" in lower):
        return True
    if step == "profile" and "no performance issues" in lower:
        return True
    return False


_WORKDOC_STEP_INSTRUCTIONS: dict[str, str] = {
    "planning": (
        "## Working Document\n"
        "A working document is available at `.workdoc.md` in the project root.\n"
        "Read it to understand the task context, then **replace** the `## Plan` section's\n"
        "placeholder with your full plan. Write the file back when done."
    ),
    "implementation": (
        "## Working Document\n"
        "A working document is available at `.workdoc.md` in the project root.\n"
        "Read the `## Plan` section for the implementation guide. As you work,\n"
        "update the `## Implementation Log` section with completed items,\n"
        "key decisions, and any deviations from the plan. Write the file back when done."
    ),
    "fix": (
        "## Working Document\n"
        "A working document is available at `.workdoc.md` in the project root.\n"
        "Read the full document for context on prior work. As you work,\n"
        "update the `## Fix Log` section with which issues were fixed,\n"
        "what changed, and key decisions. Write the file back when done."
    ),
    "verification": (
        "## Working Document\n"
        "A working document is available at `.workdoc.md` in the project root.\n"
        "Read the implementation context for what was changed. As you work,\n"
        "update the `## Verification Results` section with commands run,\n"
        "outcomes, and any fixes applied. Write the file back when done."
    ),
    "review": (
        "## Working Document\n"
        "A working document is available at `.workdoc.md` in the project root.\n"
        "Read it for full context on the plan, implementation, and verification.\n"
        "Do NOT modify the file — the orchestrator will append review findings."
    ),
}

_WORKDOC_STEP_INSTRUCTIONS_BY_STEP: dict[str, str] = {
    "analyze": (
        "## Working Document\n"
        "A working document is available at `.workdoc.md` in the project root.\n"
        "Read it to understand the task context, then **replace** the `## Analysis` section's\n"
        "placeholder with your analysis. Write the file back when done."
    ),
    "prototype": (
        "## Working Document\n"
        "A working document is available at `.workdoc.md` in the project root.\n"
        "Read the `## Plan` section for context when present. As you work,\n"
        "update the `## Implementation Log` section with prototype notes:\n"
        "- hypotheses tested,\n"
        "- experiments run,\n"
        "- observed results,\n"
        "- decision recommendation.\n"
        "Write the file back when done."
    ),
    "diagnose": (
        "## Working Document\n"
        "A working document is available at `.workdoc.md` in the project root.\n"
        "Read it to understand the task context, then **replace** the `## Analysis` section's\n"
        "placeholder with your diagnosis. Write the file back when done."
    ),
    "initiative_plan": (
        "## Working Document\n"
        "A working document is available at `.workdoc.md` in the project root.\n"
        "Read it to understand the task context, then **replace** the `## Plan` section's\n"
        "placeholder with your initiative plan. Write the file back when done."
    ),
    "profile": (
        "## Working Document\n"
        "A working document is available at `.workdoc.md` in the project root.\n"
        "Read it for context before profiling.\n"
        "Do NOT modify `.workdoc.md` in this step — the orchestrator writes\n"
        "profiling output into `## Profiling Baseline`."
    ),
    "report": (
        "## Working Document\n"
        "A working document is available at `.workdoc.md` in the project root.\n"
        "Read it for final context (plan, implementation, verification, review).\n"
        "Do NOT modify `.workdoc.md` in this step — the orchestrator writes\n"
        "the report output into `## Final Report`."
    ),
    "pr_review_comment": (
        "## Working Document\n"
        "A working document is available at `.workdoc.md` in the project root.\n"
        "Read it for task context and any fetched comments. When done,\n"
        "**replace** the `## Review Comments` section with your review output.\n"
        "Write the file back when done."
    ),
    "pr_review_summarize": (
        "## Working Document\n"
        "A working document is available at `.workdoc.md` in the project root.\n"
        "Read it for task context and any fetched comments. When done,\n"
        "**replace** the `## Review Summary` section with your summary.\n"
        "Write the file back when done."
    ),
    "pr_review_fix_respond": (
        "## Working Document\n"
        "A working document is available at `.workdoc.md` in the project root.\n"
        "Read it for task context and fetched comments. When done,\n"
        "**replace** the `## Review & Fix Plan` section with your review\n"
        "and fix plan. Write the file back when done."
    ),
}


_WORKDOC_SKIP_STEPS = {"plan_refine", "initiative_plan_refine"}  # These steps don't go through the sync path.


def _workdoc_prompt_section(step: str) -> str:
    """Build workdoc instructions for a step, or return an empty string."""
    if step in _WORKDOC_SKIP_STEPS:
        return ""
    step_specific = _WORKDOC_STEP_INSTRUCTIONS_BY_STEP.get(step)
    if step_specific:
        return step_specific
    category = _step_category(step)
    return _WORKDOC_STEP_INSTRUCTIONS.get(category, "")


_DEPENDENCY_POLICY_INSTRUCTIONS: dict[str, dict[str, str]] = {
    "implementation": {
        "permissive": (
            "## Dependency policy — permissive\n"
            "Prefer using well-maintained libraries over manual implementation.\n"
            "Install what you need — favor proven packages for non-trivial functionality."
        ),
        "prudent": (
            "## Dependency policy — prudent\n"
            "Prefer using what is already available in the project.\n"
            "Only install a new dependency if implementing it manually would be\n"
            "unreliable or disproportionately complex. Justify any new addition."
        ),
        "strict": (
            "## Dependency policy — strict\n"
            "Do NOT install new dependencies or add entries to package.json,\n"
            "requirements.txt, pyproject.toml, or any other manifest.\n"
            "Work only with what is already installed in the project."
        ),
    },
    "planning": {
        "permissive": (
            "## Dependency policy — permissive\n"
            "The plan should recommend libraries where appropriate.\n"
            "Prefer well-maintained packages for non-trivial functionality."
        ),
        "prudent": (
            "## Dependency policy — prudent\n"
            "The plan should prefer existing dependencies but may suggest new\n"
            "ones with clear justification for why manual implementation is\n"
            "unreliable or disproportionately complex."
        ),
        "strict": (
            "## Dependency policy — strict\n"
            "The plan must NOT include any new dependencies.\n"
            "All solutions must use only what is already installed in the project."
        ),
    },
    "review": {
        "permissive": (
            "## Dependency policy — permissive\n"
            "Do not raise findings for adding new dependencies.\n"
            "New well-maintained libraries are acceptable."
        ),
        "prudent": (
            "## Dependency policy — prudent\n"
            "Flag any newly added dependency as a low-severity finding for awareness.\n"
            "Verify the addition is justified and not easily replaceable with existing code."
        ),
        "strict": (
            "## Dependency policy — strict\n"
            "Flag any change to dependency manifests (package.json, requirements.txt,\n"
            "pyproject.toml, etc.) as a high-severity finding.\n"
            "No new dependencies are allowed under this policy."
        ),
    },
}

_REVIEW_ALLOWED_SEVERITIES = {"critical", "high", "medium", "low"}
_REVIEW_ALLOWED_CATEGORIES = {
    "correctness",
    "reliability",
    "security",
    "performance",
    "api_contract",
    "documentation",
    "maintainability",
    "test_coverage",
    "other",
}
_VERIFY_ALLOWED_STATUSES = {"pass", "fail", "skip", "environment"}
_VERIFY_ALLOWED_REASON_CODES = {
    "assertion_failure",
    "type_error",
    "lint_violation",
    "baseline_failure",
    "scope_violation",
    "tool_missing",
    "config_missing",
    "no_tests",
    "permission_denied",
    "resource_limit",
    "os_incompatibility",
    "infrastructure",
    "unknown",
}

_PLAN_PREAMBLE_PREFIXES: tuple[str, ...] = (
    "the plan has been prepared",
    "here's a summary",
    "here is a summary",
    "summary of the key",
)
_PLAN_TRAILING_CHAT_PREFIXES: tuple[str, ...] = (
    "should i ",
    "would you like",
    "want me to ",
    "do you want me to ",
)


def _normalize_planning_text(text: str) -> str:
    """Normalize planning/refine outputs by stripping chatty wrappers.

    Planning revisions should contain only the plan body. Some worker replies
    add conversational prefaces or trailing confirmation questions; remove those
    when they can be identified deterministically.
    """
    raw = (text or "").strip()
    if not raw:
        return ""

    # Remove markdown fences when the whole payload is fenced text.
    if raw.startswith("```") and raw.endswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 2:
            raw = "\n".join(lines[1:-1]).strip()

    lines = raw.splitlines()
    heading_idx = next((idx for idx, line in enumerate(lines) if re.match(r"^\s*#{1,6}\s+\S", line)), None)

    # Drop meta-preface if the message starts with a known wrapper phrase.
    if lines:
        first = lines[0].strip().lower()
        if any(first.startswith(prefix) for prefix in _PLAN_PREAMBLE_PREFIXES) and heading_idx is not None:
            lines = lines[heading_idx:]

    # Drop trailing "should I..." style follow-up questions and any divider right above.
    cut_idx: int | None = None
    for idx, line in enumerate(lines):
        lowered = line.strip().lower()
        if any(lowered.startswith(prefix) for prefix in _PLAN_TRAILING_CHAT_PREFIXES):
            cut_idx = idx
            break
    if cut_idx is not None:
        lines = lines[:cut_idx]
        while lines and lines[-1].strip() in {"", "---"}:
            lines.pop()

    return "\n".join(lines).strip()


def _frontend_gate_root(project_dir: Path) -> Path | None:
    """Best-effort locate frontend workspace root for tsc/vite gate checks."""
    direct_pkg = project_dir / "package.json"
    if direct_pkg.exists():
        try:
            if '"vite"' in direct_pkg.read_text(encoding="utf-8", errors="replace"):
                return project_dir
        except Exception:
            pass
    nested = project_dir / "frontend"
    if (nested / "package.json").exists():
        return nested
    return None


def _collect_changed_paths(project_dir: Path) -> list[str]:
    """Return relative changed paths from git status (tracked + untracked)."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    changed: list[str] = []
    for raw in (result.stdout or "").splitlines():
        line = str(raw).rstrip()
        if not line:
            continue
        payload = line[3:] if len(line) >= 4 else line
        payload = payload.strip()
        if not payload:
            continue
        if " -> " in payload:
            payload = payload.split(" -> ", 1)[1].strip()
        changed.append(payload.strip('"'))
    return changed


def _load_package_scripts(package_json_path: Path) -> dict[str, str]:
    """Load npm scripts from a package.json file."""
    try:
        payload = json.loads(package_json_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in scripts.items():
        if isinstance(key, str) and isinstance(value, str) and value.strip():
            out[key.strip()] = value.strip()
    return out


def _path_suggests_js_ts_workspace(relative_path: str) -> bool:
    """Return True when a changed path suggests JS/TS workspace verification."""
    normalized = str(relative_path or "").strip().replace("\\", "/")
    if not normalized:
        return False
    if normalized.startswith(_FRONTEND_HINT_PREFIXES):
        return True
    return Path(normalized).suffix.lower() in _JS_TS_SOURCE_EXTENSIONS


def _nearest_workspace_with_package(project_dir: Path, relative_path: str) -> Path | None:
    """Return closest ancestor workspace containing package.json for a changed path."""
    rel = Path(str(relative_path).replace("\\", "/"))
    search = rel if rel.suffix == "" else rel.parent
    while True:
        candidate = (project_dir / search / "package.json").resolve()
        if candidate.exists():
            return candidate.parent
        if search == Path("."):
            break
        search = search.parent
    return None


def _npm_run_script_command(*, workspace: Path, project_dir: Path, script: str) -> str:
    """Render a stable npm run command for a workspace script."""
    if workspace.resolve() == project_dir.resolve():
        return f"npm run {script}"
    rel = workspace.resolve().relative_to(project_dir.resolve())
    return f"npm --prefix {str(rel)} run {script}"


def _detect_required_verify_commands(
    project_dir: Path,
    *,
    changed_paths: Sequence[str] | None = None,
) -> list[str]:
    """Detect required JS/TS workspace verification commands for changed files."""
    paths = list(changed_paths or _collect_changed_paths(project_dir))
    if not paths:
        return []
    workspaces: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        normalized = str(path or "").strip()
        if not _path_suggests_js_ts_workspace(normalized):
            continue
        workspace = _nearest_workspace_with_package(project_dir, normalized)
        if workspace is None:
            continue
        resolved = workspace.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        workspaces.append(resolved)
    required: list[str] = []
    for workspace in workspaces:
        scripts = _load_package_scripts(workspace / "package.json")
        for script in ("build", "typecheck"):
            if script in scripts:
                required.append(_npm_run_script_command(workspace=workspace, project_dir=project_dir, script=script))
    deduped: list[str] = []
    seen_cmds: set[str] = set()
    for cmd in required:
        if cmd in seen_cmds:
            continue
        seen_cmds.add(cmd)
        deduped.append(cmd)
    return deduped


def _inject_required_verify_commands(
    project_commands: dict[str, dict[str, str]] | None,
    project_languages: list[str] | None,
    required_commands: Sequence[str],
) -> dict[str, dict[str, str]]:
    """Merge required verify commands into the command hint map."""
    merged: dict[str, dict[str, str]] = {
        lang: dict(cmds)
        for lang, cmds in (project_commands or {}).items()
        if isinstance(cmds, dict)
    }
    normalized_required = [str(cmd).strip() for cmd in required_commands if str(cmd).strip()]
    if not normalized_required:
        return merged
    preferred_lang = (
        "typescript"
        if (project_languages and "typescript" in project_languages)
        else (
            "javascript"
            if (project_languages and "javascript" in project_languages)
            else ("typescript" if "typescript" in merged else ("javascript" if "javascript" in merged else "typescript"))
        )
    )
    lang_cmds = merged.get(preferred_lang, {})
    if not isinstance(lang_cmds, dict):
        lang_cmds = {}
    existing_typecheck = str(lang_cmds.get("typecheck") or "").strip()
    chain: list[str] = []
    if existing_typecheck:
        chain.append(existing_typecheck)
    for cmd in normalized_required:
        if existing_typecheck and cmd in existing_typecheck:
            continue
        chain.append(cmd)
    lang_cmds["typecheck"] = " && ".join(chain) if chain else existing_typecheck
    merged[preferred_lang] = lang_cmds
    return merged


_NPM_PRIMARY_COMMAND_RE = re.compile(r"^\s*npm\s+(?:run\s+)?(\w[\w-]*)")


def _filter_npm_commands_by_scripts(
    commands: dict[str, str],
    project_dir: Path,
) -> dict[str, str]:
    """Remove commands whose primary npm script is absent from package.json.

    Only filters when ``npm`` is the first command in the string.  Compound
    commands where ``npm`` appears as a fallback (e.g. after ``||``) are kept
    because the primary command works independently.
    """
    pkg_path = project_dir / "package.json"
    scripts = _load_package_scripts(pkg_path)
    filtered: dict[str, str] = {}
    for key, cmd in commands.items():
        match = _NPM_PRIMARY_COMMAND_RE.match(cmd)
        if match:
            script_name = match.group(1)
            if script_name not in scripts:
                continue
        filtered[key] = cmd
    return filtered


def _has_prisma_schema(project_dir: Path) -> bool:
    """Return whether this task worktree contains a Prisma schema."""
    return (project_dir / "prisma" / "schema.prisma").exists()


def _inject_prisma_verify_command_hints(
    project_commands: dict[str, dict[str, str]] | None,
    project_languages: list[str] | None,
) -> dict[str, dict[str, str]]:
    """Inject deterministic Prisma verification commands into command hints."""
    merged: dict[str, dict[str, str]] = {
        lang: dict(cmds)
        for lang, cmds in (project_commands or {}).items()
        if isinstance(cmds, dict)
    }
    preferred_lang = (
        "typescript"
        if (project_languages and "typescript" in project_languages)
        else ("javascript" if (project_languages and "javascript" in project_languages) else "typescript")
    )
    lang_cmds = merged.get(preferred_lang, {})
    if not isinstance(lang_cmds, dict):
        lang_cmds = {}
    prisma_verify = (
        "docker compose up -d postgres && "
        "sh -lc 'for i in $(seq 1 30); do "
        "docker compose ps --format json 2>/dev/null | rg -q \"healthy\" && exit 0; "
        "sleep 1; done; exit 1' && "
        "./node_modules/.bin/prisma format && "
        "./node_modules/.bin/prisma validate && "
        "sh -lc 'for i in $(seq 1 5); do "
        "./node_modules/.bin/prisma migrate deploy && exit 0; "
        "sleep 2; done; exit 1'"
    )
    existing_format = str(lang_cmds.get("format") or "").strip()
    if existing_format:
        if prisma_verify not in existing_format:
            lang_cmds["format"] = f"{existing_format} && {prisma_verify}"
    else:
        lang_cmds["format"] = prisma_verify
    merged[preferred_lang] = lang_cmds
    return merged


def _normalize_verify_environment_note(
    *,
    note: str,
    reason_code: str,
    project_dir: Path,
    task: Task,
) -> tuple[str, str | None, str | None]:
    """Rewrite common frontend gate infra failures into deterministic diagnostics."""
    text = str(note or "").strip()
    if not text:
        return text, None, None
    lower = text.lower()
    task_context = f"{task.title}\n{task.description}".lower()
    verify_reason_override: str | None = None

    # Prefer deterministic Prisma blockers by priority:
    # missing env var > docker unavailable > local toolchain missing > network.
    mentions_prisma = any(
        token in lower or token in task_context
        for token in ("prisma", "migrate dev", "schema.prisma", "database_url")
    )
    if mentions_prisma and reason_code in {"tool_missing", "config_missing", "infrastructure", "unknown", "os_incompatibility"}:
        has_prisma_schema = (project_dir / "prisma" / "schema.prisma").exists()
        has_package_json = (project_dir / "package.json").exists()
        prisma_bin = project_dir / "node_modules" / ".bin" / "prisma"
        node_modules_dir = project_dir / "node_modules"
        missing_database_url = (
            "environment variable not found: database_url" in lower
            or ("database_url" in lower and "not found" in lower)
        )
        docker_unavailable = any(
            token in lower
            for token in (
                "cannot connect to the docker daemon",
                "is the docker daemon running",
                "/var/run/docker.sock",
                "docker.sock",
            )
        )
        registry_unavailable = any(
            token in lower
            for token in (
                "enotfound registry.npmjs.org",
                "getaddrinfo enotfound registry.npmjs.org",
            )
        )
        prisma_toolchain_missing = has_prisma_schema and has_package_json and (
            not node_modules_dir.is_dir() or not prisma_bin.exists()
        )

        if missing_database_url:
            normalized = (
                "Prisma verification is blocked in this task worktree: required environment variable "
                "`DATABASE_URL` is missing. Set `DATABASE_URL` for the task environment and re-run verify. "
                "[reason_code=config_missing; env_kind=prisma_env_missing]"
            )
            return normalized, "prisma_env_missing", "config_missing"

        if docker_unavailable:
            normalized = (
                "Prisma verification is blocked in this environment: Docker daemon is unavailable for the "
                "required Postgres migration flow (for example `prisma migrate dev`). Start Docker/Compose "
                "and re-run verify. [reason_code=infrastructure; env_kind=docker_unavailable]"
            )
            return normalized, "docker_unavailable", "infrastructure"

        if prisma_toolchain_missing:
            normalized = (
                "Prisma verification is blocked in this task worktree: local Prisma CLI is missing "
                f"(expected `{prisma_bin}`). Install dependencies in the same environment (for example: "
                "`npm install`) and re-run verify. "
                "[reason_code=tool_missing; env_kind=prisma_cli_missing]"
            )
            return normalized, "prisma_cli_missing", "tool_missing"

        if registry_unavailable:
            normalized = (
                "Prisma verification is blocked by network constraints in this environment: npm registry "
                "is unreachable (`registry.npmjs.org`). Restore network access and re-run verify. "
                "[reason_code=infrastructure; env_kind=network_unavailable]"
            )
            return normalized, "network_unavailable", "infrastructure"

    mentions_frontend_gate = any(
        token in lower or token in task_context
        for token in ("tsc --noemit", "vite build", "build gate")
    )
    if not mentions_frontend_gate:
        return text, None, None
    if reason_code not in {"tool_missing", "config_missing", "infrastructure", "unknown", "os_incompatibility"}:
        return text, None, None

    frontend_root = _frontend_gate_root(project_dir)
    if frontend_root is None:
        return text, None, None
    missing: list[str] = []
    if not (frontend_root / "node_modules").is_dir():
        missing.append("frontend/node_modules")
    if not (frontend_root / "node_modules" / ".bin" / "tsc").exists():
        missing.append("tsc binary")
    if not (frontend_root / "node_modules" / ".bin" / "vite").exists():
        missing.append("vite binary")
    if not missing:
        return text, None, None

    rel = frontend_root.relative_to(project_dir) if frontend_root != project_dir else Path(".")
    rel_display = str(rel).rstrip("/") or "."
    normalized = (
        "Frontend build gate is blocked in this task worktree: missing local frontend toolchain "
        f"({', '.join(missing)}) under `{rel_display}`. Install dependencies in the same environment "
        f"(for example: `cd {rel_display} && npm ci`) and re-run verify. "
        "[reason_code=tool_missing; env_kind=frontend_toolchain_missing]"
    )
    verify_reason_override = "tool_missing"
    return normalized, "frontend_toolchain_missing", verify_reason_override


def _load_workdoc_snapshot(task: Task, project_dir: Path) -> str:
    """Load canonical/worktree workdoc text for run-end summary context."""
    candidates: list[Path] = []
    if isinstance(task.metadata, dict):
        workdoc_path = task.metadata.get("workdoc_path")
        if isinstance(workdoc_path, str) and workdoc_path.strip():
            candidates.append(Path(workdoc_path.strip()))
    candidates.append(project_dir / ".workdoc.md")

    for path in candidates:
        try:
            if path.exists() and path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text[:30000]
        except Exception:
            continue
    return "(workdoc unavailable)"


def build_step_prompt(
    *,
    task: Task,
    step: str,
    attempt: int,
    project_languages: list[str] | None = None,
    project_commands: dict[str, dict[str, str]] | None = None,
    require_prisma_verify: bool = False,
    required_verify_commands: list[str] | None = None,
    prompt_overrides: dict[str, str] | None = None,
    prompt_injections: dict[str, str] | None = None,
) -> str:
    """Build a prompt from Task fields with step-specific instructions.

    Args:
        task (Task): Task whose title, description, and metadata are used to
            build the worker instruction payload.
        step (str): Pipeline step name that selects the step-specific
            instruction template.
        attempt (int): One-based attempt number for the current step run.
        project_languages (list[str] | None): Detected project languages used
            to populate language-specific guidance in the prompt.
        project_commands (dict[str, dict[str, str]] | None): Optional
            language-to-command mapping surfaced to workers as execution hints.
        require_prisma_verify (bool): Whether to include mandatory Prisma
            verification gates in the rendered prompt.
        required_verify_commands (list[str] | None): Deterministic extra verify
            gates inferred from changed JS/TS workspace files.
        prompt_overrides (dict[str, str] | None): Optional per-step prompt text
            overrides loaded from project settings.
        prompt_injections (dict[str, str] | None): Optional per-step prompt text
            snippets appended after the base task metadata block.

    Returns:
        str: Fully rendered prompt text sent to the worker process.
    """
    category = _step_category(step)
    normalized_overrides = _normalize_prompt_overrides(prompt_overrides)
    normalized_injections = _normalize_prompt_injections(prompt_injections)
    instruction = normalized_overrides.get(step.strip().lower()) or load_prompt(
        _instruction_prompt_name(step, task.task_type)
    )
    preamble = load_prompt("preamble.md")
    guardrails = load_prompt("guardrails.md")

    # Special prompt for dependency analysis
    if category == "dependency_analysis" and isinstance(task.metadata, dict):
        parts = [preamble, "", instruction, ""]

        candidate_tasks = task.metadata.get("candidate_tasks")
        if isinstance(candidate_tasks, list) and candidate_tasks:
            parts.append("## Tasks to analyze")
            parts.append("")
            for ct in candidate_tasks:
                if not isinstance(ct, dict):
                    continue
                parts.append(f"- ID: {ct.get('id', '?')}")
                parts.append(f"  Title: {ct.get('title', '?')}")
                desc = str(ct.get("description") or "")[:200]
                if desc:
                    parts.append(f"  Description: {desc}")
                parts.append(f"  Type: {ct.get('task_type', 'feature')}")
                labels = ct.get("labels")
                if isinstance(labels, list) and labels:
                    parts.append(f"  Labels: {', '.join(str(label) for label in labels)}")
                parts.append("")

        existing_tasks = task.metadata.get("existing_tasks")
        if isinstance(existing_tasks, list) and existing_tasks:
            parts.append("## Already-scheduled tasks (may be blockers)")
            parts.append("")
            for et in existing_tasks:
                if not isinstance(et, dict):
                    continue
                parts.append(f"- ID: {et.get('id', '?')}")
                parts.append(f"  Title: {et.get('title', '?')}")
                parts.append(f"  Status: {et.get('status', '?')}")
                parts.append("")

        parts.append("## Rules")
        parts.append("- Only output edges where one task MUST complete before another can start.")
        parts.append("- If uncertain, omit the edge (prefer false negatives over false positives).")
        parts.append("- Use the exact task IDs from above.")
        parts.append("- Add concise, concrete reason per edge.")
        parts.append("- Include confidence (`high` or `medium`) and short evidence reference per edge.")
        parts.append("- If all tasks are independent, return an empty edges array.")
        parts.append("- Do not create circular dependencies.")

        parts.append("")
        parts.append(guardrails)

        return "\n".join(parts)

    if category == "pipeline_classification":
        parts = [preamble, "", instruction, ""]
        parts.append(f"Task: {task.title}")
        if task.description:
            parts.append(f"Description: {task.description}")
        allowed = task.metadata.get("classification_allowed_pipelines") if isinstance(task.metadata, dict) else None
        if isinstance(allowed, list) and allowed:
            cleaned = [str(item).strip() for item in allowed if str(item).strip()]
            if cleaned:
                registry = PipelineRegistry()
                templates_by_id = {t.id: t for t in registry.list_templates()}
                parts.append("")
                parts.append("## Allowed pipelines")
                for pid in cleaned:
                    tmpl = templates_by_id.get(pid)
                    if tmpl:
                        parts.append(f"- `{pid}` — {tmpl.description}")
                    else:
                        parts.append(f"- `{pid}`")
        parts.append("")
        parts.append(guardrails)
        return "\n".join(parts)

    parts = [preamble, "", instruction, ""]

    parts.append(f"Task: {task.title}")
    if task.description:
        parts.append(f"Description: {task.description}")
    parts.append(f"Type: {task.task_type}")
    if attempt > 1 and step != "implement":
        parts.append(f"Attempt: {attempt}")
    step_injection = normalized_injections.get(step.strip().lower())
    if step_injection:
        parts.append("")
        parts.append("## Project-configured step injection")
        parts.append(step_injection)

    # Inject workdoc instructions for steps that use the working document.
    workdoc_block = _workdoc_prompt_section(step)
    if workdoc_block:
        parts.append("")
        parts.append(workdoc_block)

    # Inject source context for commit_review steps.
    if step == "commit_review" and isinstance(task.metadata, dict):
        _cr_desc = str(task.metadata.get("source_description") or "").strip()
        _cr_plan = str(task.metadata.get("source_plan") or "").strip()
        _cr_diff = str(task.metadata.get("source_diff") or "").strip()
        _cr_sha = str(task.metadata.get("source_commit_sha") or "").strip()
        if _cr_desc:
            parts.append("")
            parts.append("## Original task description")
            parts.append(_cr_desc)
        if _cr_plan:
            parts.append("")
            parts.append("## Original task plan")
            parts.append(_cr_plan)
        if _cr_diff:
            parts.append("")
            parts.append(f"## Commit diff to review ({_cr_sha[:12]})" if _cr_sha else "## Commit diff to review")
            parts.append(f"```diff\n{_cr_diff}\n```")

    # Inject source context for pr_review / mr_review and comment-aware review steps.
    _PR_CONTEXT_STEPS = {"pr_review", "mr_review", "pr_review_comment", "pr_review_summarize", "pr_review_fix_respond"}
    if step in _PR_CONTEXT_STEPS and isinstance(task.metadata, dict):
        _pr_desc = str(task.metadata.get("source_description") or "").strip()
        _pr_url = str(task.metadata.get("source_url") or "").strip()
        _pr_diff = str(task.metadata.get("source_diff") or "").strip()
        _pr_ref = str(task.metadata.get("source_ref") or "").strip()
        _pr_base = str(task.metadata.get("source_base_ref") or "").strip()
        _pr_stat = str(task.metadata.get("source_stat") or "").strip()
        if _pr_desc:
            parts.append("")
            parts.append("## PR/MR description")
            parts.append(_pr_desc)
        if _pr_url:
            parts.append("")
            parts.append(f"## Source URL\n{_pr_url}")
        if _pr_stat:
            parts.append("")
            parts.append("## Diff stat (all changed files)")
            parts.append(f"```\n{_pr_stat}\n```")
        if _pr_diff:
            parts.append("")
            ref_label = _pr_ref or "head"
            base_label = _pr_base or "base"
            label = f"PR/MR diff ({base_label}...{ref_label})"
            parts.append(f"## {label}")
            # Surface the list of changed files so comment paths can be validated.
            _diff_files = re.findall(r"^diff --git a/(.+?) b/", _pr_diff, re.MULTILINE)
            if _diff_files:
                parts.append("**Files changed in this diff** (only these paths are valid for comments):")
                for _df in _diff_files:
                    parts.append(f"- `{_df}`")
                parts.append("")
            parts.append(f"```diff\n{_pr_diff}\n```")
        _pr_guidance = str(task.metadata.get("review_guidance") or "").strip()
        if _pr_guidance:
            parts.append("")
            parts.append("## Review guidance from the user")
            parts.append(_pr_guidance)

    # Inject formatted comment context for comment-aware review steps.
    _COMMENT_AWARE_STEPS = {"pr_review_comment", "pr_review_summarize", "pr_review_fix_respond"}
    if step in _COMMENT_AWARE_STEPS and isinstance(task.metadata, dict):
        _comments_formatted = str(task.metadata.get("source_comments_formatted") or "").strip()
        if _comments_formatted:
            parts.append("")
            parts.append("## Existing review comments")
            parts.append(_comments_formatted)

    # Inject outputs from prior pipeline steps.
    # For implement/implement_fix, rely on the workdoc as the single source of truth.
    step_outputs = task.metadata.get("step_outputs") if isinstance(task.metadata, dict) else None
    has_plan_override = (
        isinstance(task.metadata, dict)
        and task.metadata.get("plan_for_generation")
        and step == "generate_tasks"
    )
    if (
        isinstance(step_outputs, dict)
        and step_outputs
        and category in _STEP_OUTPUT_INJECTION
        and step not in {"implement", "implement_fix", "initiative_plan"}
        and not has_plan_override
    ):
        inject_keys = _STEP_OUTPUT_INJECTION[category]
        if step == "report" and task.task_type in {"security", "security_audit"}:
            # Security audit report should synthesize only scan evidence.
            inject_keys = ("scan_deps", "scan_code")
        if step == "generate_tasks" and task.task_type == "initiative_plan":
            # Initiative planning flow uses initiative_plan output as the only
            # decomposition source to avoid legacy fallback drift.
            inject_keys = ("initiative_plan",)
        elif step == "generate_tasks" and task.task_type in {"security", "security_audit"}:
            # Security remediation tasks should be derived from scan evidence and
            # report synthesis, not generic planning outputs.
            inject_keys = ("report", "scan_deps", "scan_code")
        if inject_keys is None:
            inject_keys = tuple(step_outputs.keys())
        for key in inject_keys:
            val = step_outputs.get(key)
            if isinstance(val, str) and val.strip():
                parts.append("")
                parts.append(f"## Output from prior '{key}' step")
                parts.append(val.strip())

    is_fix_step = step == "implement_fix"
    review_findings = task.metadata.get("review_findings") if isinstance(task.metadata, dict) else None

    # Insert a dedicated remediation payload section for implement_fix.
    has_review_findings = bool(review_findings and isinstance(review_findings, list))
    has_verify_failure = (
        isinstance(task.metadata, dict)
        and isinstance(task.metadata.get("verify_failure"), str)
        and task.metadata.get("verify_failure", "").strip()
    )
    has_verify_output = (
        isinstance(task.metadata, dict)
        and isinstance(task.metadata.get("verify_output"), str)
        and task.metadata.get("verify_output", "").strip()
    )
    has_verify_reason_code = (
        isinstance(task.metadata, dict)
        and isinstance(task.metadata.get("verify_reason_code"), str)
        and task.metadata.get("verify_reason_code", "").strip()
    )
    retry_guidance = task.metadata.get("retry_guidance") if isinstance(task.metadata, dict) else None
    has_previous_error = False
    if isinstance(retry_guidance, dict):
        prev_error = str(retry_guidance.get("previous_error") or "").strip()
        has_previous_error = bool(prev_error)

    if is_fix_step and (
        has_review_findings or has_verify_failure or has_verify_output or has_verify_reason_code or has_previous_error
    ):
        parts.append("")
        parts.append("## Issues to fix")

    scope_contract = (
        normalize_scope_contract(task.metadata.get("scope_contract"))
        if isinstance(task.metadata, dict)
        else None
    )
    if is_fix_step and isinstance(scope_contract, dict) and scope_contract.get("mode") == "restricted":
        allowed = list(scope_contract.get("allowed_globs") or [])
        forbidden = list(scope_contract.get("forbidden_globs") or [])
        parts.append("")
        parts.append("## Scope contract")
        parts.append("This task is scope-restricted. Do NOT modify files outside allowed globs.")
        if allowed:
            parts.append("Allowed globs:")
            for pattern in allowed:
                parts.append(f"- `{pattern}`")
        if forbidden:
            parts.append("Forbidden globs:")
            for pattern in forbidden:
                parts.append(f"- `{pattern}`")
        baseline_ref = str(scope_contract.get("baseline_ref") or "").strip()
        if baseline_ref:
            parts.append(f"Baseline ref: `{baseline_ref}`")
        parts.append(
            "If verification failures are outside this scope and unchanged by your diff, "
            "report them as baseline debt rather than editing out-of-scope files."
        )

    # Include review findings for fix steps.
    if review_findings and isinstance(review_findings, list) and is_fix_step:
        parts.append("")
        parts.append("### Review findings to address")
        for finding in review_findings:
            if isinstance(finding, dict):
                sev = finding.get("severity", "medium")
                summary = finding.get("summary", "")
                file_ = finding.get("file", "")
                line_ = finding.get("line", "")
                loc = f" ({file_}:{line_})" if file_ else ""
                parts.append(f"  - [{sev}] {summary}{loc}")

    # Include review history for review and fix steps
    review_history = task.metadata.get("review_history") if isinstance(task.metadata, dict) else None
    if review_history and isinstance(review_history, list) and step in {"review", "implement_fix"}:
        parts.append("")
        parts.append("## Previous review cycle history")
        parts.append(
            "IMPORTANT: Do NOT contradict decisions from prior cycles. "
            "Do NOT re-raise findings that were already resolved. "
            "Do NOT undo fixes applied in response to earlier findings."
        )
        for cycle_entry in review_history:
            if not isinstance(cycle_entry, dict):
                continue
            attempt_num = cycle_entry.get("attempt", "?")
            decision = cycle_entry.get("decision", "?")
            parts.append(f"  Cycle {attempt_num} — decision: {decision}")
            for cf in cycle_entry.get("findings", []):
                if not isinstance(cf, dict):
                    continue
                sev = cf.get("severity", "?")
                summ = cf.get("summary", "")
                st = cf.get("status", "open")
                parts.append(f"    - [{sev}] {summ} (status: {st})")

    # Surface verify environment notes to the reviewer
    if category == "review" and isinstance(task.metadata, dict):
        env_note = task.metadata.get("verify_environment_note")
        if isinstance(env_note, str) and env_note.strip():
            parts.append("")
            parts.append("## Verification environment note")
            parts.append(
                "The verify step encountered failures caused by environment/infrastructure "
                "constraints, not code defects. The pipeline proceeded without fix attempts. "
                "Please assess whether this is acceptable for this task."
            )
            parts.append(f"  {env_note.strip()}")

    # Include plan context for task generation
    plan_for_generation = task.metadata.get("plan_for_generation") if isinstance(task.metadata, dict) else None
    if plan_for_generation and category == "task_generation":
        parts.append("")
        parts.append("## Plan to decompose into subtasks")
        parts.append(str(plan_for_generation))
        parts.append("")
        parts.append(
            "Decompose this plan into ordered subtasks. Use the depends_on field "
            "(array of task IDs) to specify execution order where needed."
        )

    if step == "generate_tasks" and task.task_type in {"security", "security_audit"}:
        parts.append("")
        parts.append("## Remediation task generation focus")
        parts.append(
            "Generate concrete remediation tasks from security findings only. "
            "Each task should map to one or more specific findings with clear "
            "scope and expected risk reduction. Use depends_on IDs where order "
            "is required (e.g., dependency upgrade before follow-up code hardening)."
        )

    # Include context for iterative plan refinement.
    if category == "planning" and step in {"plan_refine", "initiative_plan_refine"} and isinstance(task.metadata, dict):
        refine_key = "initiative_plan_refine" if step == "initiative_plan_refine" else "plan_refine"
        base_plan = str(task.metadata.get(f"{refine_key}_base") or "").strip()
        feedback = str(task.metadata.get(f"{refine_key}_feedback") or "").strip()
        instructions = str(task.metadata.get(f"{refine_key}_instructions") or "").strip()
        if base_plan:
            parts.append("")
            parts.append("## Base initiative plan" if step == "initiative_plan_refine" else "## Base plan")
            parts.append(base_plan)
        if feedback:
            parts.append("")
            parts.append("## Feedback to address")
            parts.append(feedback)
        if instructions:
            parts.append("")
            parts.append("## Additional instructions")
            parts.append(instructions)
        parts.append("")
        if step == "initiative_plan_refine":
            parts.append("Return a full rewritten initiative plan in the `initiative_plan` field.")
        else:
            parts.append("Return a full rewritten plan in the `plan` field.")
    if step in {"plan", "plan_refine"}:
        parts.append("")
        parts.append(load_prompt("partials/plan_output_format.md"))

    # Include merge conflict context for resolve_merge step
    if category == "merge_resolution" and isinstance(task.metadata, dict):
        conflict_files = task.metadata.get("merge_conflict_files")
        if isinstance(conflict_files, dict):
            parts.append("")
            parts.append("Conflicted files (with <<<<<<< / ======= / >>>>>>> markers):")
            for fpath, content in conflict_files.items():
                parts.append(f"\n--- {fpath} ---")
                parts.append(content)

        other_tasks = task.metadata.get("merge_other_tasks")
        if isinstance(other_tasks, list) and other_tasks:
            parts.append("")
            parts.append("Other task(s) whose changes conflict with this task:")
            for info in other_tasks:
                parts.append(str(info))

        current_objective = task.metadata.get("merge_current_objective")
        if isinstance(current_objective, str) and current_objective.strip():
            parts.append("")
            parts.append("Current task objective context:")
            parts.append(current_objective.strip())

        other_objectives = task.metadata.get("merge_other_objectives")
        if isinstance(other_objectives, list) and other_objectives:
            parts.append("")
            parts.append("Other task objective context (must also be preserved):")
            for info in other_objectives:
                if isinstance(info, str) and info.strip():
                    parts.append(info.strip())

        merge_attempt = int(task.metadata.get("merge_conflict_attempt") or 0)
        merge_max_attempts = int(task.metadata.get("merge_conflict_max_attempts") or 0)
        previous_error = str(task.metadata.get("merge_conflict_previous_error") or "").strip()
        if merge_attempt > 1:
            parts.append("")
            parts.append("## Retry context")
            if merge_max_attempts > 0:
                parts.append(f"Attempt: {merge_attempt} of {merge_max_attempts}")
            else:
                parts.append(f"Attempt: {merge_attempt}")
            if previous_error:
                parts.append(f"Previous attempt error: {previous_error}")
            parts.append(
                "Re-check all conflicted files carefully. Resolve every unmerged entry and remove all conflict markers."
            )

        parts.append("")
        parts.append("Edit the conflicted files to resolve all conflicts. "
                      "Ensure BOTH this task's and the other task(s)' objectives are preserved.")

    # Include verify failure context for fix steps.
    if isinstance(task.metadata, dict) and is_fix_step:
        verify_failure = task.metadata.get("verify_failure")
        if verify_failure:
            parts.append("")
            parts.append("### Verification failure")
            parts.append(f"  {verify_failure}")
        verify_reason_code = task.metadata.get("verify_reason_code")
        if isinstance(verify_reason_code, str) and verify_reason_code.strip():
            parts.append("")
            parts.append("### Verification reason code")
            parts.append(f"  {verify_reason_code.strip()}")
        verify_output = task.metadata.get("verify_output")
        if isinstance(verify_output, str) and verify_output.strip():
            parts.append("")
            parts.append("### Verification output (test/lint/typecheck logs)")
            parts.append(verify_output.strip())

    # Inject previous attempt error as a concrete issue to fix.
    if isinstance(task.metadata, dict) and is_fix_step:
        retry_guidance = task.metadata.get("retry_guidance")
        if isinstance(retry_guidance, dict):
            prev_error = str(retry_guidance.get("previous_error") or "").strip()
            if prev_error:
                parts.append("")
                parts.append("### Previous attempt error")
                parts.append(prev_error)

    # Inject one-shot human guidance selected by resume target step.
    human_guidance_block = render_human_guidance_prompt(task, step)
    if human_guidance_block:
        parts.append("")
        parts.append("## Human guidance")
        parts.append(human_guidance_block)

    # Inject style guidelines and language defaults for implementation and review steps
    if category in ("implementation", "fix", "review"):
        parts.append("")
        parts.append(load_prompt("style.md"))
        if project_languages:
            for lang in project_languages:
                lang_block = _LANGUAGE_DEFAULTS.get(lang)
                if lang_block:
                    parts.append(lang_block)

    # Inject project commands for implementation and verification steps
    if project_commands and project_languages and category in ("implementation", "fix", "verification"):
        cmds_block = _format_project_commands(project_commands, project_languages)
        if cmds_block:
            parts.append("")
            parts.append(cmds_block)
    if category == "verification" and require_prisma_verify:
        parts.append("")
        parts.append("## Required Prisma verification gates")
        parts.append("- If `prisma/schema.prisma` exists in this worktree, run Prisma checks using the local CLI (do not use bare `npx prisma`).")
        parts.append("- Required commands:")
        parts.append("  - `docker compose up -d postgres`")
        parts.append("  - wait until Postgres is healthy before migration checks")
        parts.append("  - `./node_modules/.bin/prisma format`")
        parts.append("  - `./node_modules/.bin/prisma validate`")
        parts.append("  - retry `./node_modules/.bin/prisma migrate deploy` a few times if connection is still starting")
        parts.append("  - `./node_modules/.bin/prisma migrate deploy`")
    if category == "verification" and required_verify_commands:
        parts.append("")
        parts.append("## Required verification gates")
        parts.append("- The following checks are required for the changed JS/TS workspace files in this task.")
        parts.append("- Run every command and report exit codes/evidence; do not skip them.")
        for command in required_verify_commands:
            parts.append(f"- `{command}`")

    # Inject dependency policy instruction
    dep_policy = getattr(task, "dependency_policy", "prudent") or "prudent"
    dep_instruction = _DEPENDENCY_POLICY_INSTRUCTIONS.get(category, {}).get(dep_policy)
    if dep_instruction:
        parts.append("")
        parts.append(dep_instruction)

    parts.append("")
    parts.append(guardrails)

    return "\n".join(parts)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Try to extract a JSON object from text, handling markdown fences."""
    text = text.strip()
    # Try stripping markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```) and last line (```)
        inner_lines = []
        started = False
        for line in lines:
            if not started:
                if line.strip().startswith("```"):
                    started = True
                    continue
            elif line.strip() == "```":
                break
            else:
                inner_lines.append(line)
        text = "\n".join(inner_lines).strip()

    # Find first { and last }
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _extract_json_value(text: str) -> Any | None:
    """Try to extract a top-level JSON value (object or array) from text."""
    text = text.strip()
    if not text:
        return None

    # Try stripping markdown code fences first.
    if text.startswith("```"):
        lines = text.split("\n")
        inner_lines = []
        started = False
        for line in lines:
            if not started:
                if line.strip().startswith("```"):
                    started = True
                    continue
            elif line.strip() == "```":
                break
            else:
                inner_lines.append(line)
        text = "\n".join(inner_lines).strip()

    # If the entire remaining payload is JSON, parse directly.
    try:
        value = json.loads(text)
        if isinstance(value, (dict, list)):
            return value
    except json.JSONDecodeError:
        pass

    # Fallback object extraction.
    obj = _extract_json(text)
    if obj is not None:
        return obj

    # Fallback array extraction.
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, (dict, list)) else None


def _normalize_review_findings(raw_findings: Any) -> list[dict[str, Any]]:
    """Normalize raw findings to a stable actionable schema."""
    if not isinstance(raw_findings, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue

        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue

        severity = str(item.get("severity") or "medium").strip().lower()
        if severity == "info":
            continue
        if severity not in _REVIEW_ALLOWED_SEVERITIES:
            severity = "medium"

        category = str(item.get("category") or "other").strip().lower().replace(" ", "_")
        if category not in _REVIEW_ALLOWED_CATEGORIES:
            category = "other"

        file_raw = item.get("file")
        file_path = str(file_raw).strip() if file_raw is not None else ""

        line_raw = item.get("line")
        line_num: int | None
        try:
            line_num = int(line_raw) if line_raw is not None else None
            if line_num is not None and line_num <= 0:
                line_num = None
        except (TypeError, ValueError):
            line_num = None

        suggested_fix = str(item.get("suggested_fix") or "").strip()

        status = str(item.get("status") or "open").strip().lower()
        if status in {"fixed", "closed", "done"}:
            status = "resolved"
        elif status not in {"open", "resolved"}:
            status = "open"

        normalized.append(
            {
                "severity": severity,
                "category": category,
                "summary": summary,
                "file": file_path,
                "line": line_num,
                "suggested_fix": suggested_fix,
                "status": status,
            }
        )
    return normalized


def _normalize_verify_reason_code(value: Any) -> str:
    reason = str(value or "").strip().lower()
    return reason if reason in _VERIFY_ALLOWED_REASON_CODES else "unknown"


def _format_verify_summary(summary: Any, reason_code: str) -> str | None:
    text = str(summary).strip() if summary is not None else ""
    if not text and reason_code == "unknown":
        return None
    if not text:
        return f"[reason_code={reason_code}]"
    if reason_code == "unknown":
        return text
    return f"{text} [reason_code={reason_code}]"


def _sanitize_stderr(raw: str, *, max_length: int = 500) -> str:
    """Extract meaningful error lines from worker stderr.

    CLI workers (codex, claude) emit startup noise at the top of stderr
    (system prompt fragments, MCP startup lines) and actual errors at the
    bottom.  This helper takes the tail and strips XML tool-error tags.
    """
    # Strip XML tool-error wrapper tags that leak from worker internals
    cleaned = re.sub(r"</?tool_use_error>", "", raw)
    # Take the last max_length characters — errors are at the end
    tail = cleaned.strip()[-max_length:]
    return tail


class LiveWorkerAdapter:
    """Worker adapter that dispatches to real Codex/Ollama providers."""

    def __init__(self, container: Container) -> None:
        """Initialize the LiveWorkerAdapter.

        Args:
            container (Container): Storage/service container used to resolve
                configuration, repositories, and project paths.
        """
        self._container = container
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancel_events_lock = threading.Lock()

    def signal_cancel(self, task_id: str) -> None:
        """Set the cancel event for a running task to unblock its worker immediately."""
        with self._cancel_events_lock:
            event = self._cancel_events.get(task_id)
        if event is not None:
            event.set()

    @staticmethod
    def _coerce_timeout(value: Any, default: int = _DEFAULT_STEP_TIMEOUT_SECONDS) -> int:
        try:
            timeout = int(value)
        except (TypeError, ValueError):
            return default
        return timeout if timeout >= 0 else default

    def _default_step_timeout_seconds(self) -> int:
        cfg = self._container.config.load()
        orchestrator_cfg = cfg.get("orchestrator") if isinstance(cfg, dict) else {}
        orchestrator_cfg = orchestrator_cfg if isinstance(orchestrator_cfg, dict) else {}
        return self._coerce_timeout(orchestrator_cfg.get("step_timeout_seconds"), _DEFAULT_STEP_TIMEOUT_SECONDS)

    def _timeout_for_step(self, task: Task, step: str) -> int:
        default_timeout = self._default_step_timeout_seconds()
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        overrides = metadata.get("step_timeouts")
        if isinstance(overrides, dict):
            for key in (step, _STEP_TIMEOUT_ALIASES.get(step)):
                if not key:
                    continue
                if key in overrides:
                    return self._coerce_timeout(overrides.get(key), default_timeout)

        try:
            template = PipelineRegistry().resolve_for_task_type(task.task_type)
        except Exception:
            return default_timeout

        # Only honour template-level timeouts that were explicitly set (i.e.
        # differ from the StepDef dataclass default of 600s).  Steps using the
        # default should defer to the global ``step_timeout_seconds`` config
        # value so that a user-configured default actually applies everywhere.
        step_timeouts: dict[str, int] = {}
        for sd in template.steps:
            if sd.timeout_seconds != _DEFAULT_STEP_TIMEOUT_SECONDS:
                step_timeouts[sd.name] = self._coerce_timeout(sd.timeout_seconds, default_timeout)
        for key in (step, _STEP_TIMEOUT_ALIASES.get(step)):
            if key and key in step_timeouts:
                return step_timeouts[key]
        return default_timeout

    @staticmethod
    def _heartbeat_settings(
        cfg: dict[str, Any],
        *,
        step: str = "",
        is_heartbeat_stall_retry: bool = False,
    ) -> tuple[int, int]:
        workers_cfg = cfg.get("workers") if isinstance(cfg, dict) else {}
        workers_cfg = workers_cfg if isinstance(workers_cfg, dict) else {}
        heartbeat_seconds = LiveWorkerAdapter._coerce_timeout(
            workers_cfg.get("heartbeat_seconds"), _DEFAULT_HEARTBEAT_SECONDS
        )
        # Resolve grace: per-step config → global config → built-in per-step default → 240s
        grace_by_step = workers_cfg.get("heartbeat_grace_by_step")
        grace_by_step = grace_by_step if isinstance(grace_by_step, dict) else {}
        global_grace_raw = workers_cfg.get("heartbeat_grace_seconds")
        if step and step in grace_by_step:
            heartbeat_grace_seconds = LiveWorkerAdapter._coerce_timeout(
                grace_by_step[step], _DEFAULT_HEARTBEAT_GRACE_SECONDS
            )
        elif global_grace_raw is not None:
            heartbeat_grace_seconds = LiveWorkerAdapter._coerce_timeout(
                global_grace_raw, _DEFAULT_HEARTBEAT_GRACE_SECONDS
            )
        elif step and step in _DEFAULT_HEARTBEAT_GRACE_BY_STEP:
            heartbeat_grace_seconds = _DEFAULT_HEARTBEAT_GRACE_BY_STEP[step]
        else:
            heartbeat_grace_seconds = _DEFAULT_HEARTBEAT_GRACE_SECONDS
        if is_heartbeat_stall_retry:
            heartbeat_grace_seconds = int(heartbeat_grace_seconds * _HEARTBEAT_STALL_RETRY_GRACE_MULTIPLIER)
        if heartbeat_grace_seconds < heartbeat_seconds:
            heartbeat_grace_seconds = heartbeat_seconds
        return heartbeat_seconds, heartbeat_grace_seconds

    @staticmethod
    def _human_blocker_summary(issues: list[dict[str, str]]) -> str:
        count = len(issues)
        first = issues[0].get("summary", "").strip() if issues else ""
        if not first:
            return f"Human intervention required ({count} blocking issue{'s' if count != 1 else ''})."
        suffix = "issue" if count == 1 else "issues"
        return f"Human intervention required ({count} {suffix}): {first}"

    def run_step(self, *, task: Task, step: str, attempt: int) -> StepResult:
        """Execute one pipeline step using the configured live worker provider.

        Args:
            task (Task): Persisted task record being advanced.
            step (str): Pipeline step to execute.
            attempt (int): One-based retry count for this step.

        Returns:
            StepResult: Worker result including status, summary, and optional
                structured payload fields.
        """
        return self._execute_step(task=task, step=step, attempt=attempt, persist=True)

    def run_step_ephemeral(self, *, task: Task, step: str, attempt: int) -> StepResult:
        """Execute a step without persisting task or run state updates.

        This is used for synthetic tasks where the adapter only needs a prompt
        carrier and should not mutate repositories.

        Args:
            task (Task): In-memory task payload used to build the worker prompt.
            step (str): Pipeline step to execute.
            attempt (int): One-based retry count for this step.

        Returns:
            StepResult: Worker response for the ephemeral execution path.
        """
        return self._execute_step(task=task, step=step, attempt=attempt, persist=False)

    def _execute_step(self, *, task: Task, step: str, attempt: int, persist: bool) -> StepResult:
        worktree_path = task.metadata.get("worktree_dir") if isinstance(task.metadata, dict) else None
        project_dir = Path(worktree_path) if worktree_path else self._container.project_dir

        # 1. Resolve worker
        try:
            cfg = self._container.config.load()
            runtime = get_workers_runtime_config(config=cfg, codex_command_fallback="codex exec")
            spec = resolve_worker_for_step(runtime, step)
            # Task-level provider override: explicit user choice bypasses routing
            task_provider = str(getattr(task, "worker_provider", "") or "").strip()
            if not task_provider and isinstance(task.metadata, dict):
                task_provider = str(task.metadata.get("worker_provider") or "").strip()
            if task_provider:
                if task_provider not in runtime.providers:
                    # Auto-create a transient spec for known CLI provider types
                    # that aren't explicitly configured (e.g. user picks 'claude'
                    # from the UI dropdown but hasn't added it to workers config).
                    _fallback_commands = {"codex": "codex exec", "claude": "claude -p"}
                    if task_provider in _fallback_commands:
                        runtime.providers[task_provider] = WorkerProviderSpec(
                            name=task_provider,
                            type=task_provider,  # type: ignore[arg-type]
                            command=_fallback_commands[task_provider],
                            execution_mode="host_access" if task_provider == "claude" else "sandboxed",
                        )
                    else:
                        available_names = ", ".join(sorted(runtime.providers.keys()))
                        return StepResult(status="error", summary=f"Task worker_provider '{task_provider}' not found (available: {available_names})")
                spec = runtime.providers[task_provider]
                if spec.type in {"codex", "claude"} and not spec.command:
                    return StepResult(status="error", summary=f"Worker '{spec.name}' missing required 'command'")
                if spec.type == "ollama" and (not spec.endpoint or not spec.model):
                    return StepResult(status="error", summary=f"Worker '{spec.name}' missing 'endpoint'/'model'")
            env_cfg = workers_environment_config(cfg)
            required_caps = required_capabilities_for_step(step=step, project_dir=project_dir, cfg=cfg)
            if not task_provider and env_cfg.get("capability_fallback", True) and required_caps:
                if not provider_has_capabilities(
                    provider_capabilities=spec.capabilities,
                    required_capabilities=required_caps,
                ):
                    fallback_name: str | None = None
                    fallback_spec = None
                    for candidate_name, candidate_spec in runtime.providers.items():
                        if candidate_name == spec.name:
                            continue
                        if candidate_spec.type not in {"codex", "claude", "ollama"}:
                            continue
                        if provider_has_capabilities(
                            provider_capabilities=candidate_spec.capabilities,
                            required_capabilities=required_caps,
                        ):
                            fallback_name = candidate_name
                            fallback_spec = candidate_spec
                            break
                    if fallback_spec is not None and fallback_name is not None:
                        if isinstance(task.metadata, dict):
                            task.metadata["environment_provider_fallback"] = {
                                "step": step,
                                "from_provider": spec.name,
                                "to_provider": fallback_name,
                                "required_capabilities": list(required_caps),
                                "ts": now_iso(),
                            }
                        spec = fallback_spec
            if spec.type in {"codex", "claude"}:
                task_model = str(getattr(task, "worker_model", "") or "").strip()
                if not task_model and isinstance(task.metadata, dict):
                    task_model = str(task.metadata.get("worker_model") or "").strip()
                default_model = str(getattr(runtime, "default_model", "") or "").strip() if spec.type == "codex" else ""
                effective_model = task_model or default_model or str(spec.model or "").strip()
                if effective_model and effective_model != (spec.model or ""):
                    spec = replace(spec, model=effective_model)
            available, reason = test_worker(spec)
            if not available:
                return StepResult(status="error", summary=f"Worker not available: {reason}")
        except (ValueError, KeyError) as exc:
            return StepResult(status="error", summary=f"Cannot resolve worker: {exc}")

        # 2. Environment preflight and best-effort auto-remediation.
        preflight = run_environment_preflight(step=step, project_dir=project_dir, cfg=cfg)
        if isinstance(task.metadata, dict):
            if preflight.ok:
                task.metadata.pop("environment_preflight", None)
            else:
                task.metadata["environment_preflight"] = {
                    "step": step,
                    "required_capabilities": list(preflight.required_capabilities),
                    "attempted_remediation": preflight.attempted_remediation,
                    "issues": [
                        {
                            "code": issue.code,
                            "summary": issue.summary,
                            "capability": issue.capability,
                            "recoverable": issue.recoverable,
                        }
                        for issue in preflight.issues
                    ],
                    "remediation_log": preflight.remediation_log,
                    "ts": now_iso(),
                }
        if not preflight.ok:
            issue_text = "; ".join(issue.summary for issue in preflight.issues[:4])
            remediation_text = f" Remediation: {preflight.remediation_log}" if preflight.remediation_log else ""
            return StepResult(
                status="error",
                summary=f"Environment preflight failed: {issue_text}.{remediation_text}",
            )

        # 3. Build prompt
        langs = detect_project_languages(project_dir)
        raw_commands = (cfg.get("project") or {}).get("commands") or {}
        raw_prompt_overrides = (cfg.get("project") or {}).get("prompt_overrides")
        raw_prompt_injections = (cfg.get("project") or {}).get("prompt_injections")
        prompt_overrides = _normalize_prompt_overrides(raw_prompt_overrides)
        prompt_injections = _normalize_prompt_injections(raw_prompt_injections)
        project_commands = {
            lang: cmds for lang, cmds in raw_commands.items()
            if isinstance(cmds, dict)
        } or None
        # Overlay per-task project commands (language-level replace)
        task_pc = task.project_commands
        if isinstance(task_pc, dict) and task_pc:
            if project_commands is None:
                project_commands = {}
            for lang, cmds in task_pc.items():
                if isinstance(cmds, dict) and cmds:
                    project_commands[lang] = dict(cmds)
        # Fill in defaults for detected languages with no configured commands
        venv_info = detect_python_venv(project_dir)
        if langs:
            if project_commands is None:
                project_commands = {}
            for lang in langs:
                if lang not in project_commands:
                    defaults = _DEFAULT_PROJECT_COMMANDS.get(lang)
                    if defaults:
                        cmds = dict(defaults)
                        if lang == "python" and venv_info is not None:
                            rel_prefix = str(venv_info.path.relative_to(project_dir) / "bin") if _is_subpath(venv_info.path, project_dir) else str(venv_info.bin_dir)
                            cmds = _apply_venv_to_defaults(cmds, venv_info.bin_dir, rel_prefix)
                        if lang in ("javascript", "typescript"):
                            cmds = _filter_npm_commands_by_scripts(cmds, project_dir)
                        if cmds:
                            project_commands[lang] = cmds
            if not project_commands:
                project_commands = None
        # Remove disabled auto-detected commands (user overrides are never filtered)
        disabled_commands: list[str] = list((cfg.get("project") or {}).get("disabled_commands") or [])
        if disabled_commands and project_commands:
            disabled_set = {s.strip().lower() for s in disabled_commands if isinstance(s, str)}
            user_configured_langs = set(raw_commands.keys())
            for lang in list(project_commands.keys()):
                if lang in user_configured_langs:
                    continue  # user overrides are never filtered
                filtered = {
                    step: cmd for step, cmd in project_commands[lang].items()
                    if f"{lang}.{step}" not in disabled_set
                }
                if filtered:
                    project_commands[lang] = filtered
                else:
                    del project_commands[lang]
            if not project_commands:
                project_commands = None
        # Drop auto-detected commands whose binary is not available
        if project_commands:
            user_configured_langs = set(raw_commands.keys())
            for lang in list(project_commands.keys()):
                if lang in user_configured_langs:
                    continue  # user overrides are never filtered
                filtered = {
                    step_name: cmd for step_name, cmd in project_commands[lang].items()
                    if _check_binary_available(cmd, project_dir)
                }
                if filtered:
                    project_commands[lang] = filtered
                else:
                    del project_commands[lang]
            if not project_commands:
                project_commands = None
        # Resolve relative executable paths against the *original* project dir
        # so workers in git worktrees can find binaries in gitignored dirs.
        require_prisma_verify = _step_category(step) == "verification" and _has_prisma_schema(project_dir)
        required_verify_commands: list[str] = []
        if _step_category(step) == "verification":
            required_verify_commands = _detect_required_verify_commands(project_dir)
            if required_verify_commands:
                project_commands = _inject_required_verify_commands(
                    project_commands,
                    langs or None,
                    required_verify_commands,
                )
        if require_prisma_verify:
            project_commands = _inject_prisma_verify_command_hints(project_commands, langs or None)
        if project_commands:
            project_commands = _resolve_command_paths(
                project_commands, self._container.project_dir,
            )
        prompt = build_step_prompt(
            task=task, step=step, attempt=attempt,
            project_languages=langs or None,
            project_commands=project_commands,
            require_prisma_verify=require_prisma_verify,
            required_verify_commands=required_verify_commands or None,
            prompt_overrides=prompt_overrides or None,
            prompt_injections=prompt_injections or None,
        )

        # 4. Execute
        run_dir = Path(tempfile.mkdtemp(dir=str(self._container.state_root)))
        progress_path = run_dir / "progress.json"
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        timeout_seconds = self._timeout_for_step(task, step)
        is_stall_retry = bool(
            isinstance(task.metadata, dict)
            and task.metadata.get("heartbeat_stall_recovery_attempts_by_step", {}).get(step)
        )
        heartbeat_seconds, heartbeat_grace_seconds = self._heartbeat_settings(
            cfg, step=step, is_heartbeat_stall_retry=is_stall_retry,
        )
        if not isinstance(task.metadata, dict):
            task.metadata = {}
        log_meta = {
            "step": step,
            "run_dir": str(run_dir),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "progress_path": str(progress_path),
            "started_at": now_iso(),
        }
        task.metadata["active_logs"] = log_meta
        if persist:
            self._container.tasks.upsert(task)

        def _check_task_cancelled() -> bool:
            if not persist:
                return False
            fresh = self._container.tasks.get(task.id)
            return fresh is not None and fresh.status == "cancelled"

        # Build merged env vars for subprocess workers (codex/claude only).
        # Always pass the resolved dict so auto-detected, config, and task
        # env vars reach the subprocess.
        worker_env: dict[str, str] | None = None
        if spec.type in {"codex", "claude"}:
            worker_env = resolve_env_vars(project_dir=project_dir, cfg=cfg, task=task)

        # Create a per-invocation cancel event so cancel_task() can unblock
        # the worker poll loop immediately instead of waiting up to
        # poll_interval seconds.
        cancel_event = threading.Event()
        with self._cancel_events_lock:
            self._cancel_events[task.id] = cancel_event

        try:
            result = run_worker(
                spec=spec,
                prompt=prompt,
                project_dir=project_dir,
                run_dir=run_dir,
                timeout_seconds=timeout_seconds,
                heartbeat_seconds=heartbeat_seconds,
                heartbeat_grace_seconds=heartbeat_grace_seconds,
                progress_path=progress_path,
                is_cancelled=_check_task_cancelled,
                env=worker_env,
                cancel_event=cancel_event,
            )
        except WorkerCancelledError:
            raise  # Let the orchestrator handle cancellation
        except Exception as exc:
            return StepResult(status="error", summary=f"Worker execution failed: {exc}")
        finally:
            with self._cancel_events_lock:
                self._cancel_events.pop(task.id, None)
            if not isinstance(task.metadata, dict):
                task.metadata = {}
            task.metadata.pop("active_logs", None)
            task.metadata["last_logs"] = {**log_meta, "finished_at": now_iso()}
            if persist:
                self._container.tasks.upsert(task)

        # 5. Map result
        step_result = self._map_result(result, spec, step, task, project_dir)
        raw_json = _extract_json(result.response_text) if result.response_text else None
        raw_is_json = isinstance(raw_json, dict)

        # Propagate token usage from the primary worker call.
        primary_token_usage = dict(result.token_usage) if result.token_usage else {}
        if primary_token_usage:
            step_result.token_usage = dict(primary_token_usage)

        # 6. For verification steps that fell through to default "ok"
        #    (no structured summary), run a lightweight LLM formatter to parse the
        #    freeform output into pass/fail.
        category = _step_category(step)
        if (
            category == "verification"
            and result.response_text
            and step_result.status == "ok"
            and not raw_is_json
        ):
            step_result = self._parse_verify_output(
                spec=spec,
                response_text=result.response_text,
                project_dir=project_dir,
                task=task,
            )
            # Merge primary + formatter token usage into the step total.
            fmt_usage = step_result.token_usage or {}
            merged = _merge_token_usage(primary_token_usage, fmt_usage) if (primary_token_usage or fmt_usage) else None
            step_result.token_usage = merged

        # 7. For review steps that fell through with no findings,
        #    run a lightweight LLM formatter to extract structured findings.
        if (
            category == "review"
            and result.response_text
            and step_result.status == "ok"
            and step_result.findings is None
            and not raw_is_json
        ):
            step_result = self._parse_review_output(
                spec=spec,
                response_text=result.response_text,
                project_dir=project_dir,
            )
            # Merge primary + formatter token usage into the step total.
            fmt_usage = step_result.token_usage or {}
            merged = _merge_token_usage(primary_token_usage, fmt_usage) if (primary_token_usage or fmt_usage) else None
            step_result.token_usage = merged

        # 8. For task generation steps that fell through with no
        #    generated_tasks, run a lightweight LLM formatter to extract them.
        if (
            category == "task_generation"
            and result.response_text
            and step_result.status == "ok"
            and step_result.generated_tasks is None
            and not raw_is_json
        ):
            step_result = self._parse_task_generation_output(
                spec=spec,
                response_text=result.response_text,
                project_dir=project_dir,
            )
            # Merge primary + formatter token usage into the step total.
            fmt_usage = step_result.token_usage or {}
            merged = _merge_token_usage(primary_token_usage, fmt_usage) if (primary_token_usage or fmt_usage) else None
            step_result.token_usage = merged

        # 8b. For pr_review_comment steps that produced freeform output,
        #     run a lightweight LLM formatter to extract structured comments.
        if (
            step == "pr_review_comment"
            and result.response_text
            and step_result.status == "ok"
            and step_result.comment_actions is None
            and not raw_is_json
        ):
            step_result = self._parse_comment_output(
                spec=spec,
                response_text=result.response_text,
                project_dir=project_dir,
            )
            fmt_usage = step_result.token_usage or {}
            merged = _merge_token_usage(primary_token_usage, fmt_usage) if (primary_token_usage or fmt_usage) else None
            step_result.token_usage = merged

        # 9. Check if the step signals that no further pipeline action is needed.
        if step_result.status == "ok" and _is_no_action_needed(step, step_result.summary):
            step_result = replace(step_result, no_action_needed=True)

        return step_result

    # ------------------------------------------------------------------
    # Verify-output formatter (codex / claude)
    # ------------------------------------------------------------------

    def _parse_verify_output(
        self,
        *,
        spec: Any,
        response_text: str,
        project_dir: Path,
        task: Task,
    ) -> StepResult:
        """Use a short LLM call to classify freeform verification output."""
        formatter_prompt = load_prompt("formatters/verify_output.md").format(
            output=response_text[:8000],
        )

        fmt_run_dir = Path(tempfile.mkdtemp(dir=str(self._container.state_root)))
        progress_path = fmt_run_dir / "progress.json"
        try:
            fmt_result = run_worker(
                spec=spec,
                prompt=formatter_prompt,
                project_dir=project_dir,
                run_dir=fmt_run_dir,
                timeout_seconds=0,
                heartbeat_seconds=30,
                heartbeat_grace_seconds=60,
                progress_path=progress_path,
            )
        except Exception:
            logger.debug("Verify formatter call failed; returning error")
            return StepResult(status="error", summary="Verification output formatter failed")
        finally:
            shutil.rmtree(fmt_run_dir, ignore_errors=True)

        fmt_token_usage = fmt_result.token_usage or None

        parsed = _extract_json(fmt_result.response_text or "")
        if not isinstance(parsed, dict):
            logger.debug("Verify formatter returned unparseable output")
            return StepResult(status="error", summary="Verification output formatter returned invalid JSON", token_usage=fmt_token_usage)

        status_val = str(parsed.get("status", "")).lower().strip()
        if status_val not in _VERIFY_ALLOWED_STATUSES:
            status_val = "fail"
        reason_code = _normalize_verify_reason_code(parsed.get("reason_code"))
        summary = _format_verify_summary(parsed.get("summary"), reason_code)
        if isinstance(task.metadata, dict):
            task.metadata["verify_reason_code"] = reason_code
        if status_val == "fail":
            return StepResult(status="error", summary=str(summary) if summary else response_text[:500], token_usage=fmt_token_usage)
        if status_val == "environment":
            note = str(summary) if summary else response_text[:500]
            note, env_kind, normalized_reason = _normalize_verify_environment_note(
                note=note,
                reason_code=reason_code,
                project_dir=project_dir,
                task=task,
            )
            if isinstance(task.metadata, dict):
                task.metadata["verify_environment_note"] = note
                if normalized_reason:
                    task.metadata["verify_reason_code"] = normalized_reason
                if env_kind:
                    task.metadata["verify_environment_kind"] = env_kind
                else:
                    task.metadata.pop("verify_environment_kind", None)
            return StepResult(status="ok", summary=note, token_usage=fmt_token_usage)
        return StepResult(status="ok", summary=str(summary) if summary else None, token_usage=fmt_token_usage)

    # ------------------------------------------------------------------
    # Review-output formatter (codex / claude)
    # ------------------------------------------------------------------

    def _parse_review_output(
        self,
        *,
        spec: Any,
        response_text: str,
        project_dir: Path,
    ) -> StepResult:
        """Use a short LLM call to extract structured findings from freeform review output."""
        formatter_prompt = load_prompt("formatters/review_output.md").format(
            output=response_text[:8000],
        )

        fmt_run_dir = Path(tempfile.mkdtemp(dir=str(self._container.state_root)))
        progress_path = fmt_run_dir / "progress.json"
        try:
            fmt_result = run_worker(
                spec=spec,
                prompt=formatter_prompt,
                project_dir=project_dir,
                run_dir=fmt_run_dir,
                timeout_seconds=0,
                heartbeat_seconds=30,
                heartbeat_grace_seconds=60,
                progress_path=progress_path,
            )
        except Exception:
            logger.debug("Review formatter call failed; returning error")
            return StepResult(status="error", summary="Review output formatter failed")
        finally:
            shutil.rmtree(fmt_run_dir, ignore_errors=True)

        fmt_token_usage = fmt_result.token_usage or None

        parsed = _extract_json(fmt_result.response_text or "")
        if not isinstance(parsed, dict):
            logger.debug("Review formatter returned unparseable output")
            return StepResult(status="error", summary="Review output formatter returned invalid JSON", token_usage=fmt_token_usage)

        findings = parsed.get("findings")
        human_issues = parsed.get("human_blocking_issues")
        human_blocking: list[dict[str, str]] | None = None
        if isinstance(human_issues, list) and human_issues:
            human_blocking = [
                {"summary": str(h.get("summary") or h if isinstance(h, dict) else h).strip()}
                for h in human_issues if h
            ]
            human_blocking = [h for h in human_blocking if h["summary"]] or None
        if isinstance(findings, list):
            return StepResult(status="ok", findings=_normalize_review_findings(findings), human_blocking_issues=human_blocking, token_usage=fmt_token_usage)

        return StepResult(status="error", summary="Review output formatter returned no findings field", token_usage=fmt_token_usage)

    # ------------------------------------------------------------------
    # Task-generation-output formatter (codex / claude)
    # ------------------------------------------------------------------

    def _parse_task_generation_output(
        self,
        *,
        spec: Any,
        response_text: str,
        project_dir: Path,
    ) -> StepResult:
        """Use a short LLM call to extract structured tasks from freeform generation output."""
        formatter_prompt = load_prompt("formatters/task_generation_output.md").format(
            output=response_text[:12000],
        )

        fmt_run_dir = Path(tempfile.mkdtemp(dir=str(self._container.state_root)))
        progress_path = fmt_run_dir / "progress.json"
        try:
            fmt_result = run_worker(
                spec=spec,
                prompt=formatter_prompt,
                project_dir=project_dir,
                run_dir=fmt_run_dir,
                timeout_seconds=0,
                heartbeat_seconds=30,
                heartbeat_grace_seconds=60,
                progress_path=progress_path,
            )
        except Exception:
            logger.debug("Task generation formatter call failed; returning error")
            return StepResult(status="error", summary="Task generation output formatter failed")
        finally:
            shutil.rmtree(fmt_run_dir, ignore_errors=True)

        fmt_token_usage = fmt_result.token_usage or None

        parsed = _extract_json(fmt_result.response_text or "")
        if not isinstance(parsed, dict):
            logger.debug("Task generation formatter returned unparseable output")
            return StepResult(status="error", summary="Task generation output formatter returned invalid JSON", token_usage=fmt_token_usage)

        tasks = (
            parsed.get("tasks")
            or parsed.get("subtasks")
            or parsed.get("items")
        )
        if isinstance(tasks, list):
            return StepResult(status="ok", generated_tasks=tasks, token_usage=fmt_token_usage)

        return StepResult(status="error", summary="Task generation output formatter returned no tasks list", token_usage=fmt_token_usage)

    # ------------------------------------------------------------------
    # Comment-output formatter (codex / claude)
    # ------------------------------------------------------------------

    def _parse_comment_output(
        self,
        *,
        spec: Any,
        response_text: str,
        project_dir: Path,
    ) -> StepResult:
        """Use a short LLM call to extract structured comments from freeform pr_review_comment output."""
        formatter_prompt = load_prompt("formatters/comment_output.md").format(
            output=response_text[:8000],
        )

        fmt_run_dir = Path(tempfile.mkdtemp(dir=str(self._container.state_root)))
        progress_path = fmt_run_dir / "progress.json"
        try:
            fmt_result = run_worker(
                spec=spec,
                prompt=formatter_prompt,
                project_dir=project_dir,
                run_dir=fmt_run_dir,
                timeout_seconds=0,
                heartbeat_seconds=30,
                heartbeat_grace_seconds=60,
                progress_path=progress_path,
            )
        except Exception:
            logger.debug("Comment formatter call failed; returning error")
            return StepResult(status="error", summary="Comment output formatter failed")
        finally:
            shutil.rmtree(fmt_run_dir, ignore_errors=True)

        fmt_token_usage = fmt_result.token_usage or None

        parsed = _extract_json(fmt_result.response_text or "")
        if not isinstance(parsed, dict):
            logger.debug("Comment formatter returned unparseable output")
            return StepResult(status="error", summary="Comment output formatter returned invalid JSON", token_usage=fmt_token_usage)

        comments = parsed.get("comments")
        summary = str(parsed.get("summary") or "").strip()
        proposed = str(parsed["proposed_decision"]).strip() if parsed.get("proposed_decision") else None
        if isinstance(comments, list):
            return StepResult(status="ok", comment_actions=comments, proposed_decision=proposed, summary=summary or None, token_usage=fmt_token_usage)

        return StepResult(status="error", summary="Comment output formatter returned no comments field", token_usage=fmt_token_usage)

    def _map_result(
        self,
        result: WorkerRunResult,
        spec: Any,
        step: str,
        task: Task,
        project_dir: Path,
    ) -> StepResult:
        if result.human_blocking_issues:
            return StepResult(
                status="human_blocked",
                summary=self._human_blocker_summary(result.human_blocking_issues),
                human_blocking_issues=result.human_blocking_issues,
            )
        if result.no_heartbeat:
            summary = "Worker stalled (no heartbeat or output activity)."
            if result.stderr_path:
                try:
                    err_text = Path(result.stderr_path).read_text(errors="replace").strip()
                    if err_text:
                        summary = f"{summary}\n{_sanitize_stderr(err_text)}"
                except Exception:
                    logger.debug("Failed reading stderr for no-heartbeat worker result", exc_info=True)
            return StepResult(status="error", summary=summary)
        if result.timed_out:
            summary = "Worker timed out"
            if result.stderr_path:
                try:
                    err_text = Path(result.stderr_path).read_text(errors="replace").strip()
                    if err_text:
                        summary = f"{summary}\n{_sanitize_stderr(err_text)}"
                except Exception:
                    logger.debug("Failed reading stderr for timed-out worker result", exc_info=True)
            return StepResult(status="error", summary=summary)
        if result.exit_code != 0:
            summary = f"Worker exited with code {result.exit_code}"
            if result.stderr_path:
                try:
                    err_text = Path(result.stderr_path).read_text(errors="replace").strip()
                    if err_text:
                        summary = _sanitize_stderr(err_text)
                except Exception:
                    logger.debug("Failed reading stderr for non-zero worker exit", exc_info=True)
            return StepResult(status="error", summary=summary)

        # Dependency analysis: always parse response text.
        category = _step_category(step)
        if category == "dependency_analysis" and result.response_text:
            return self._parse_dep_analysis_output(result.response_text)

        if category == "verification" and result.response_text:
            parsed = _extract_json(result.response_text)
            if isinstance(parsed, dict):
                status_val = str(parsed.get("status", "")).lower().strip()
                if status_val not in _VERIFY_ALLOWED_STATUSES:
                    return StepResult(status="error", summary="Verification output JSON has invalid status")
                reason_code = _normalize_verify_reason_code(parsed.get("reason_code"))
                verify_summary = _format_verify_summary(parsed.get("summary"), reason_code)
                if isinstance(task.metadata, dict):
                    task.metadata["verify_reason_code"] = reason_code
                if status_val == "fail":
                    return StepResult(status="error", summary=str(verify_summary) if verify_summary else "Verification failed")
                if status_val == "environment":
                    note = str(verify_summary) if verify_summary else "Verification blocked by environment constraints"
                    note, env_kind, normalized_reason = _normalize_verify_environment_note(
                        note=note,
                        reason_code=reason_code,
                        project_dir=project_dir,
                        task=task,
                    )
                    if isinstance(task.metadata, dict):
                        task.metadata["verify_environment_note"] = note
                        if normalized_reason:
                            task.metadata["verify_reason_code"] = normalized_reason
                        if env_kind:
                            task.metadata["verify_environment_kind"] = env_kind
                        else:
                            task.metadata.pop("verify_environment_kind", None)
                    return StepResult(status="ok", summary=note)
                return StepResult(status="ok", summary=str(verify_summary) if verify_summary else "")

        if category == "review" and result.response_text:
            parsed = _extract_json(result.response_text)
            if isinstance(parsed, dict):
                findings = parsed.get("findings")
                human_issues = parsed.get("human_blocking_issues")
                human_blocking: list[dict[str, str]] | None = None
                if isinstance(human_issues, list) and human_issues:
                    human_blocking = [
                        {"summary": str(h.get("summary") or h if isinstance(h, dict) else h).strip()}
                        for h in human_issues if h
                    ]
                    human_blocking = [h for h in human_blocking if h["summary"]] or None
                if isinstance(findings, list):
                    return StepResult(status="ok", findings=_normalize_review_findings(findings), human_blocking_issues=human_blocking)
                return StepResult(status="error", summary="Review output JSON missing findings array")

        if category == "pipeline_classification" and result.response_text:
            parsed = _extract_json_value(result.response_text)
            if isinstance(parsed, dict):
                return StepResult(status="ok", summary=json.dumps(parsed))
            return StepResult(status="error", summary="Pipeline classification output must be valid JSON")

        # pr_review_comment: try to parse structured comment JSON directly.
        if step == "pr_review_comment" and result.response_text:
            parsed = _extract_json(result.response_text)
            if isinstance(parsed, dict) and isinstance(parsed.get("comments"), list):
                return StepResult(
                    status="ok",
                    comment_actions=parsed["comments"],
                    proposed_decision=str(parsed["proposed_decision"]).strip() if parsed.get("proposed_decision") else None,
                    summary=str(parsed.get("summary") or "").strip() or None,
                )

        # pr_review_fix_respond: preserve structured JSON so post_comment_responses can parse it.
        # Must return before the planning category path which normalizes JSON to plain text.
        if step == "pr_review_fix_respond" and result.response_text:
            parsed = _extract_json(result.response_text)
            if isinstance(parsed, dict) and isinstance(parsed.get("addressed_comments"), list):
                return StepResult(status="ok", summary=json.dumps(parsed))

        if category == "planning" and result.response_text:
            parsed = _extract_json(result.response_text)
            if isinstance(parsed, dict):
                planning_summary: Any | None = parsed.get("analysis") if step == "analyze" else None
                planning_summary = planning_summary or (
                    parsed.get("initiative_plan") if step in {"initiative_plan", "initiative_plan_refine"} else None
                )
                planning_summary = planning_summary or parsed.get("plan") or parsed.get("summary")
                if planning_summary:
                    return StepResult(status="ok", summary=_normalize_planning_text(str(planning_summary))[:20000])
            return StepResult(status="ok", summary=_normalize_planning_text(result.response_text)[:20000])

        if category == "diagnosis" and result.response_text:
            parsed = _extract_json(result.response_text)
            if isinstance(parsed, dict):
                diagnosis_summary = parsed.get("diagnosis") or parsed.get("summary")
                if diagnosis_summary:
                    return StepResult(status="ok", summary=str(diagnosis_summary).strip()[:20000])
            return StepResult(status="ok", summary=result.response_text.strip()[:20000])

        if category == "fix" and result.response_text:
            return StepResult(status="ok", summary=result.response_text.strip()[:20000])

        if category == "task_generation" and result.response_text:
            parsed_value = _extract_json_value(result.response_text)
            if isinstance(parsed_value, list):
                return StepResult(status="ok", generated_tasks=parsed_value)
            if isinstance(parsed_value, dict):
                tasks = (
                    parsed_value.get("tasks")
                    or parsed_value.get("subtasks")
                    or parsed_value.get("items")
                )
                if isinstance(tasks, list):
                    return StepResult(status="ok", generated_tasks=tasks)

        if category == "reporting" and result.response_text:
            return StepResult(status="ok", summary=result.response_text.strip()[:20000])

        # Parse remaining structured output for ollama-specific categories.
        if spec.type == "ollama" and result.response_text:
            return self._parse_ollama_output(result.response_text, step)

        return StepResult(status="ok")

    def _parse_dep_analysis_output(self, text: str) -> StepResult:
        parsed = _extract_json(text)
        if parsed is None:
            return StepResult(status="ok", dependency_edges=[])
        edges = parsed.get("edges")
        if isinstance(edges, list):
            return StepResult(status="ok", dependency_edges=edges)
        return StepResult(status="ok", dependency_edges=[])

    def _parse_ollama_output(self, text: str, step: str) -> StepResult:
        parsed = _extract_json(text)
        if parsed is None:
            return StepResult(status="ok", summary=text[:500] if text else None)

        category = _step_category(step)

        if category == "review" or category == "scanning":
            findings = parsed.get("findings")
            if isinstance(findings, list):
                if category == "review":
                    return StepResult(status="ok", findings=_normalize_review_findings(findings))
                return StepResult(status="ok", findings=findings)

        if category == "task_generation":
            tasks = parsed.get("tasks")
            if isinstance(tasks, list):
                return StepResult(status="ok", generated_tasks=tasks)

        if category == "verification":
            status = parsed.get("status", "ok")
            summary = parsed.get("summary")
            mapped_status = "ok" if status in ("ok", "pass") else "error"
            return StepResult(status=mapped_status, summary=summary)

        # For planning, implementation, reporting, diagnosis — extract summary
        summary = parsed.get("summary") or parsed.get("plan") or parsed.get("initiative_plan") or parsed.get("diagnosis")
        return StepResult(status="ok", summary=str(summary) if summary else None)

    # ------------------------------------------------------------------
    # Run summary generator
    # ------------------------------------------------------------------

    def _run_summarize_call(
        self,
        *,
        spec: Any,
        task: Task,
        run: RunRecord,
        project_dir: Path,
        gate_context: str | None = None,
    ) -> str:
        """Use a short LLM call to produce a human-readable execution summary."""
        # Build step log section
        step_lines: list[str] = []
        for entry in run.steps:
            if not isinstance(entry, dict):
                continue
            name = entry.get("step", "?")
            status = entry.get("status", "?")
            line = f"- {name}: {status}"
            summary = entry.get("summary")
            if summary:
                line += f" — {str(summary)[:200]}"
            open_counts = entry.get("open_counts")
            if isinstance(open_counts, dict):
                line += f" (findings: {open_counts})"
            step_lines.append(line)

        # Get git diff stat
        diff_stat = ""
        try:
            result = subprocess.run(
                ["git", "diff", "--stat", "HEAD~1"],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                diff_stat = result.stdout.strip()[:4000]
        except Exception:
            logger.debug("Failed collecting git diff stat for run summary", exc_info=True)

        description_section = f"Description: {task.description[:500]}\n" if task.description else ""
        execution_log = "\n".join(step_lines) if step_lines else "(no steps recorded)"
        diff_section = f"\n## Git diff stat\n{diff_stat}" if diff_stat else ""
        error_section = f"\n## Error: {task.error}" if task.error else ""
        workdoc_snapshot = _load_workdoc_snapshot(task, project_dir)
        run_status = str(run.status or "unknown")

        if gate_context:
            role_framing = (
                f"You are writing a progress summary. The pipeline is paused at "
                f"a human approval gate ({gate_context}). Focus on what has been "
                f"accomplished so far and what decision the human needs to make to proceed."
            )
        else:
            role_framing = "You are a run-end summary writer."

        prompt = load_prompt("formatters/summarize.md").format(
            role_framing=role_framing,
            task_title=task.title,
            description_section=description_section,
            task_type=task.task_type,
            run_status=run_status,
            workdoc_snapshot=workdoc_snapshot,
            execution_log=execution_log,
            diff_section=diff_section,
            error_section=error_section,
        )

        fmt_run_dir = Path(tempfile.mkdtemp(dir=str(self._container.state_root)))
        progress_path = fmt_run_dir / "progress.json"
        try:
            fmt_result = run_worker(
                spec=spec,
                prompt=prompt,
                project_dir=project_dir,
                run_dir=fmt_run_dir,
                timeout_seconds=0,
                heartbeat_seconds=30,
                heartbeat_grace_seconds=90,
                progress_path=progress_path,
            )
        except Exception:
            logger.warning("Summarize call failed for task %s", task.id, exc_info=True)
            return ""
        finally:
            shutil.rmtree(fmt_run_dir, ignore_errors=True)

        parsed = _extract_json(fmt_result.response_text or "")
        if isinstance(parsed, dict):
            summary = parsed.get("summary")
            if summary:
                return str(summary)

        # Fall back to raw text if JSON parsing failed
        raw = (fmt_result.response_text or "").strip()
        return raw[:2000] if raw else ""

    def generate_run_summary(self, *, task: Task, run: RunRecord, project_dir: Path, gate_context: str | None = None) -> str:
        """Produce a worker-generated summary for a completed run.

        Args:
            task (Task): Task that owns the completed run.
            run (RunRecord): Completed run record to summarize.
            project_dir (Path): Project root used to resolve workdoc context.
            gate_context (str | None): Gate name when generating a gate-pause
                summary, or ``None`` for a run-end summary.

        Returns:
            str: Worker-produced summary text, or an empty string when summary
                generation is unavailable.
        """
        try:
            cfg = self._container.config.load()
            runtime = get_workers_runtime_config(config=cfg, codex_command_fallback="codex exec")
            spec = resolve_worker_for_step(runtime, "summarize")
            # Respect task-level provider override so the summarize worker
            # uses the same provider the task was (re)started with.
            task_provider = str(getattr(task, "worker_provider", "") or "").strip()
            if not task_provider and isinstance(task.metadata, dict):
                task_provider = str(task.metadata.get("worker_provider") or "").strip()
            if task_provider and task_provider in runtime.providers:
                spec = runtime.providers[task_provider]
            elif task_provider:
                _fallback_commands = {"codex": "codex exec", "claude": "claude -p"}
                if task_provider in _fallback_commands:
                    spec = WorkerProviderSpec(
                        name=task_provider,
                        type=task_provider,  # type: ignore[arg-type]
                        command=_fallback_commands[task_provider],
                        execution_mode="host_access" if task_provider == "claude" else "sandboxed",
                    )
            available, reason = test_worker(spec)
            if not available:
                logger.debug("Summary worker not available: %s", reason)
                return ""
        except Exception:
            logger.debug("Cannot resolve worker for summarize step")
            return ""

        return self._run_summarize_call(
            spec=spec,
            task=task,
            run=run,
            project_dir=project_dir,
            gate_context=gate_context,
        )

    def generate_recommended_action(
        self,
        *,
        task: Task,
        blocked_step: str,
        error_message: str,
    ) -> str:
        """Generate an LLM-powered recommended action for a blocked task.

        Uses the same worker resolution as ``generate_run_summary`` (the
        ``summarize`` routing key) so no new worker configuration is needed.

        Returns:
            str: Recommended action text, or empty string when unavailable.
        """
        try:
            cfg = self._container.config.load()
            runtime = get_workers_runtime_config(config=cfg, codex_command_fallback="codex exec")
            spec = resolve_worker_for_step(runtime, "summarize")
            # Respect task-level provider override.
            task_provider = str(getattr(task, "worker_provider", "") or "").strip()
            if not task_provider and isinstance(task.metadata, dict):
                task_provider = str(task.metadata.get("worker_provider") or "").strip()
            if task_provider and task_provider in runtime.providers:
                spec = runtime.providers[task_provider]
            elif task_provider:
                _fallback_commands = {"codex": "codex exec", "claude": "claude -p"}
                if task_provider in _fallback_commands:
                    spec = WorkerProviderSpec(
                        name=task_provider,
                        type=task_provider,  # type: ignore[arg-type]
                        command=_fallback_commands[task_provider],
                        execution_mode="host_access" if task_provider == "claude" else "sandboxed",
                    )
            available, reason = test_worker(spec)
            if not available:
                logger.debug("Recommended-action worker not available: %s", reason)
                return ""
        except Exception:
            logger.debug("Cannot resolve worker for recommended-action generation")
            return ""

        def _escape_braces(s: str) -> str:
            return s.replace("{", "{{").replace("}", "}}")

        prompt = load_prompt("formatters/recommended_action.md").format(
            task_title=_escape_braces(task.title),
            task_type=_escape_braces(task.task_type),
            blocked_step=_escape_braces(blocked_step),
            error_message=_escape_braces(error_message),
        )

        fmt_run_dir = Path(tempfile.mkdtemp(dir=str(self._container.state_root)))
        progress_path = fmt_run_dir / "progress.json"
        try:
            fmt_result = run_worker(
                spec=spec,
                prompt=prompt,
                project_dir=self._container.project_dir,
                run_dir=fmt_run_dir,
                timeout_seconds=0,
                heartbeat_seconds=30,
                heartbeat_grace_seconds=90,
                progress_path=progress_path,
            )
        except Exception:
            logger.debug("Recommended-action LLM call failed", exc_info=True)
            return ""
        finally:
            shutil.rmtree(fmt_run_dir, ignore_errors=True)

        parsed = _extract_json(fmt_result.response_text or "")
        if isinstance(parsed, dict):
            action = parsed.get("recommended_action")
            if action:
                return str(action).strip()

        return ""
