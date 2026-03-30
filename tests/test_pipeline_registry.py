"""Tests for pipeline templates and registry."""

import pytest
from overdrive.pipelines.registry import (
    BUILTIN_TEMPLATES,
    PipelineRegistry,
    PipelineTemplate,
    StepDef,
)


class TestPipelineTemplate:
    """Behavioral tests for built-in pipeline template definitions."""
    def test_builtin_templates_exist(self):
        """Test that builtin templates exist."""
        expected = {
            "feature", "bug_fix", "refactor", "research", "docs",
            "test", "repo_review", "security_audit", "review", "commit_review",
            "pr_review", "mr_review",
            "pr_review_comment", "mr_review_comment",
            "pr_review_summarize", "mr_review_summarize",
            "pr_review_fix_only", "pr_review_fix_respond", "mr_review_fix_respond",
            "performance", "hotfix", "spike", "chore", "plan_only", "verify_only",
        }
        assert expected == set(BUILTIN_TEMPLATES.keys())

    def test_feature_pipeline_steps(self):
        """Test that feature pipeline steps."""
        tmpl = BUILTIN_TEMPLATES["feature"]
        assert tmpl.step_names() == ["plan", "implement", "verify", "review", "commit"]
        assert tmpl.task_types == ("feature",)

    def test_bug_fix_pipeline_steps(self):
        """Test that bug fix pipeline steps."""
        tmpl = BUILTIN_TEMPLATES["bug_fix"]
        assert tmpl.step_names() == ["diagnose", "implement", "verify", "review", "commit"]
        assert "diagnose" in tmpl.step_names()

    def test_research_pipeline_steps(self):
        """Test that research pipeline steps."""
        tmpl = BUILTIN_TEMPLATES["research"]
        assert tmpl.step_names() == ["analyze"]

    def test_docs_pipeline_steps(self):
        """Test that docs pipeline steps."""
        tmpl = BUILTIN_TEMPLATES["docs"]
        assert tmpl.step_names() == ["analyze", "implement", "verify", "review", "commit"]
        assert tmpl.task_types == ("docs",)

    def test_review_pipeline_steps(self):
        """Test that review pipeline steps."""
        tmpl = BUILTIN_TEMPLATES["review"]
        assert tmpl.step_names() == ["analyze", "review"]
        assert tmpl.task_types == ("review",)

    def test_repo_review_pipeline_steps(self):
        """Test that repo review pipeline steps."""
        tmpl = BUILTIN_TEMPLATES["repo_review"]
        assert tmpl.step_names() == ["analyze", "initiative_plan", "generate_tasks"]
        assert tmpl.task_types == ("repo_review",)

    def test_performance_pipeline_steps(self):
        """Test that performance pipeline steps."""
        tmpl = BUILTIN_TEMPLATES["performance"]
        assert tmpl.step_names() == ["profile", "plan", "implement", "benchmark", "review", "commit"]
        assert tmpl.task_types == ("performance",)

    def test_pr_review_pipeline_steps(self):
        """Test that PR review pipeline has the expected 5-step sequence."""
        tmpl = BUILTIN_TEMPLATES["pr_review"]
        assert tmpl.step_names() == ["pr_review", "implement", "verify", "review", "commit"]
        assert tmpl.task_types == ("pr_review",)
        assert tmpl.metadata.get("supports_skip_to_precommit") is True

    def test_mr_review_pipeline_steps(self):
        """Test that MR review pipeline has the expected 5-step sequence."""
        tmpl = BUILTIN_TEMPLATES["mr_review"]
        assert tmpl.step_names() == ["mr_review", "implement", "verify", "review", "commit"]
        assert tmpl.task_types == ("mr_review",)
        assert tmpl.metadata.get("supports_skip_to_precommit") is True

    def test_pr_review_comment_pipeline_steps(self):
        """Test that PR review comment pipeline has fetch, review, post sequence."""
        tmpl = BUILTIN_TEMPLATES["pr_review_comment"]
        assert tmpl.step_names() == ["fetch_comments", "pr_review_comment", "post_comments"]
        assert tmpl.task_types == ("pr_review_comment",)

    def test_pr_review_summarize_pipeline_steps(self):
        """Test that PR review summarize pipeline has fetch and summarize steps."""
        tmpl = BUILTIN_TEMPLATES["pr_review_summarize"]
        assert tmpl.step_names() == ["fetch_comments", "pr_review_summarize"]
        assert tmpl.task_types == ("pr_review_summarize",)

    def test_mr_review_comment_pipeline_steps(self):
        """Test that MR review comment pipeline has fetch, review, post sequence."""
        tmpl = BUILTIN_TEMPLATES["mr_review_comment"]
        assert tmpl.step_names() == ["fetch_comments", "pr_review_comment", "post_comments"]
        assert tmpl.task_types == ("mr_review_comment",)

    def test_mr_review_summarize_pipeline_steps(self):
        """Test that MR review summarize pipeline has fetch and summarize steps."""
        tmpl = BUILTIN_TEMPLATES["mr_review_summarize"]
        assert tmpl.step_names() == ["fetch_comments", "pr_review_summarize"]
        assert tmpl.task_types == ("mr_review_summarize",)

    def test_pr_review_fix_only_pipeline_steps(self):
        """Test that PR review fix-only pipeline matches the fix_only step sequence."""
        tmpl = BUILTIN_TEMPLATES["pr_review_fix_only"]
        assert tmpl.step_names() == ["pr_review", "implement", "verify", "review", "commit"]
        assert tmpl.task_types == ("pr_review_fix_only",)
        assert tmpl.metadata.get("supports_skip_to_precommit") is True

    def test_pr_review_fix_respond_pipeline_steps(self):
        """Test that PR review fix-and-respond pipeline has the full 7-step sequence."""
        tmpl = BUILTIN_TEMPLATES["pr_review_fix_respond"]
        assert tmpl.step_names() == [
            "fetch_comments", "pr_review_fix_respond", "implement",
            "verify", "review", "post_comment_responses", "commit",
        ]
        assert tmpl.task_types == ("pr_review_fix_respond",)
        assert tmpl.metadata.get("supports_skip_to_precommit") is True

    def test_mr_review_fix_respond_pipeline_steps(self):
        """Test that MR review fix-and-respond pipeline has the full 7-step sequence."""
        tmpl = BUILTIN_TEMPLATES["mr_review_fix_respond"]
        assert tmpl.step_names() == [
            "fetch_comments", "pr_review_fix_respond", "implement",
            "verify", "review", "post_comment_responses", "commit",
        ]
        assert tmpl.task_types == ("mr_review_fix_respond",)
        assert tmpl.metadata.get("supports_skip_to_precommit") is True

    def test_pr_review_alias_matches_fix_only(self):
        """Test that existing pr_review pipeline has same steps as pr_review_fix_only."""
        assert (
            BUILTIN_TEMPLATES["pr_review"].step_names()
            == BUILTIN_TEMPLATES["pr_review_fix_only"].step_names()
        )

    def test_mr_review_alias_matches_fix_only_pattern(self):
        """Test that mr_review follows the same fix_only pattern (steps 2-5 identical)."""
        mr_steps = BUILTIN_TEMPLATES["mr_review"].step_names()
        fix_only_steps = BUILTIN_TEMPLATES["pr_review_fix_only"].step_names()
        # First step differs (mr_review vs pr_review), but remaining steps are identical
        assert mr_steps[1:] == fix_only_steps[1:]
        assert len(mr_steps) == len(fix_only_steps)

    def test_security_audit_maps_security_type(self):
        """Test that security audit maps security type."""
        tmpl = BUILTIN_TEMPLATES["security_audit"]
        assert "security" in tmpl.task_types
        assert "security_audit" in tmpl.task_types

    def test_step_names(self):
        """Test that step names."""
        tmpl = PipelineTemplate(
            id="test",
            display_name="Test",
            description="Test pipeline",
            steps=(
                StepDef(name="a"),
                StepDef(name="b"),
                StepDef(name="c"),
            ),
        )
        assert tmpl.step_names() == ["a", "b", "c"]


class TestPipelineRegistry:
    """Behavioral tests for registry lookup and mutation semantics."""
    def test_list_templates(self):
        """Test that list templates."""
        reg = PipelineRegistry()
        templates = reg.list_templates()
        assert len(templates) == 25

    def test_get_template(self):
        """Test that get template."""
        reg = PipelineRegistry()
        tmpl = reg.get("feature")
        assert tmpl.id == "feature"

    def test_get_unknown_raises(self):
        """Test that get unknown raises."""
        reg = PipelineRegistry()
        with pytest.raises(KeyError, match="Unknown pipeline"):
            reg.get("nonexistent")

    def test_resolve_for_task_type(self):
        """Test that resolve for task type."""
        reg = PipelineRegistry()
        assert reg.resolve_for_task_type("feature").id == "feature"
        assert reg.resolve_for_task_type("bug").id == "bug_fix"
        assert reg.resolve_for_task_type("refactor").id == "refactor"
        assert reg.resolve_for_task_type("research").id == "research"
        assert reg.resolve_for_task_type("docs").id == "docs"
        assert reg.resolve_for_task_type("test").id == "test"
        assert reg.resolve_for_task_type("security").id == "security_audit"
        assert reg.resolve_for_task_type("review").id == "review"
        assert reg.resolve_for_task_type("performance").id == "performance"
        assert reg.resolve_for_task_type("initiative_plan").id == "plan_only"
        assert reg.resolve_for_task_type("decompose").id == "plan_only"
        assert reg.resolve_for_task_type("pr_review").id == "pr_review"
        assert reg.resolve_for_task_type("mr_review").id == "mr_review"

    def test_resolve_new_review_task_types(self):
        """Test that all 4 new review task types resolve correctly."""
        reg = PipelineRegistry()
        assert reg.resolve_for_task_type("pr_review_comment").id == "pr_review_comment"
        assert reg.resolve_for_task_type("pr_review_summarize").id == "pr_review_summarize"
        assert reg.resolve_for_task_type("pr_review_fix_only").id == "pr_review_fix_only"
        assert reg.resolve_for_task_type("pr_review_fix_respond").id == "pr_review_fix_respond"

    def test_resolve_unknown_type_defaults_to_feature(self):
        """Test that resolve unknown type defaults to feature."""
        reg = PipelineRegistry()
        assert reg.resolve_for_task_type("unknown_type").id == "feature"

    def test_register_custom(self):
        """Test that register custom."""
        reg = PipelineRegistry()
        custom = PipelineTemplate(
            id="custom",
            display_name="Custom",
            description="Custom pipeline",
            steps=(StepDef(name="plan"), StepDef(name="implement")),
            task_types=("custom_type",),
        )
        reg.register(custom)
        assert reg.get("custom").id == "custom"
        assert reg.resolve_for_task_type("custom_type").id == "custom"

    def test_unregister(self):
        """Test that unregister."""
        reg = PipelineRegistry()
        reg.unregister("research")
        with pytest.raises(KeyError):
            reg.get("research")
