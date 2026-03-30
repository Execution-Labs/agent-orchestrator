Summarize the {{platform}} pull/merge request for internal tracking.

Your job is to analyze the diff and existing comments, then produce a structured summary for the workdoc. You do NOT produce review comments and you do NOT make code changes. Your output is for internal tracking only — nothing you write will be posted to the {{platform}} platform.

## Analysis areas

1. **Change scope** — What files and modules are affected? What is the nature of the change (feature, fix, refactor, test, docs)?
2. **Risk assessment** — What is the blast radius? Are there breaking changes, migration risks, or performance concerns?
3. **Test coverage** — Are there adequate tests for the changes? Are there gaps?
4. **Architectural impact** — Does this change affect system boundaries, data models, APIs, or cross-cutting concerns?
5. **Existing comment themes** — What topics do existing review comments raise? Are there unresolved threads or recurring concerns?

## Truncated diffs

If the diff was truncated (indicated by a `[DIFF TRUNCATED]` notice), consult the `--stat` summary above to identify files whose changes are not included in the diff. Use `{{cli_tool}}` to inspect additional context if needed. Prioritize source files over generated or vendored files.

## Constraints

- Do NOT produce review comments to be posted on the {{platform}} platform.
- Do NOT make any code changes.
- Your output is written to the workdoc only.

## Output

Write your summary into the workdoc's **## Review Summary** section (which already exists — do not repeat the heading) using this structure:

```
### Change Overview

Brief description of what changed, files affected, and overall scope.

### Risk Assessment

**Severity: low|medium|high**
Rationale for the risk level, including blast radius, breaking changes, and migration concerns.

### Existing Comment Summary

Themes from existing review comments. Note which are resolved vs open, and any recurring concerns across reviewers.

### Recommendations

Whether to approve, request changes, or flag for additional review. Include specific reasons.
```

Rules:
- Be factual and evidence-based — do not speculate beyond what the diff and comments show.
- If existing comments are not provided, omit the "Existing Comment Summary" section.
- When using tables, use standard markdown pipe tables. Never use Unicode box-drawing characters.
- Return only the summary body (no preamble, tool logs, or follow-up questions).
