---
applyTo: "**"
---

<!-- Generated; additive to copilot-instructions.md + AGENTS.md. -->
# Pull request description

The "What does this implement/fix?" description must be short, specific, and in the author's own words — a few sentences on what actually changed and, for a fix, the concrete problem it resolves. The project's [AI Policy](https://github.com/music-assistant/.github/blob/main/AI_POLICY.md) requires this ("keep responses to the minimum needed to communicate your intent"; review any AI-assisted summary for technical accuracy), and the PR template asks for a "quick description."

Raise a `[PROBLEM]` when the description:

- is a long AI-style essay, or a speculative root-cause narrative (often wrong), instead of a concise statement of the actual change;
- adds sections beyond the PR template (e.g. Summary, Changes, Testing, Impact) — the template is used as-is, and bypassing or inflating it violates the AI Policy;
- is vague or generic, or does not match what the diff actually changes.

When you raise it, say: "This needs to more clearly describe the issue you're having and what it's fixing," name any extra template sections to remove, and you may offer one concise rewrite (a few sentences) the author can validate and use. Judge the description, not the author.
