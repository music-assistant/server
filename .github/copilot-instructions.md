# PR Review Standards

## Review Philosophy
- Only comment when confident an issue exists
- Be concise: one sentence per comment when possible
- Focus on actionable feedback, not observations
- When reviewing text, only comment on clarity issues if the text is genuinely confusing or could lead to errors
- If you're uncertain whether something is an issue, don't comment

## What to Analyze

Review all code changes for:
- Code quality and style consistency with the existing codebase
- Potential bugs or issues
- Performance implications
- Blocking IO in async code
- Security concerns
- Test coverage
- Documentation updates if needed

## New Provider Reviews

When the PR adds a new provider (new files under `music_assistant/providers/`), use the relevant demo provider as ground truth:

| Provider type | Demo reference |
|---|---|
| Music source | `_demo_music_provider` |
| Player | `_demo_player_provider` |
| Plugin | `_demo_plugin_provider` |
| Audio analysis | `_demo_audio_analysis_provider` |

- Flag any deviations from the requirements and patterns outlined in the demo provider as `[PROBLEM]` or `[CRITICAL]` depending on severity.
- Provider icons (e.g. icon.svg) are allowed to be 5KB max. If larger, flag as a critical.

## Project standards
Respect the project standards as outlined in AGENTS.md. Any deviations must be raised as `[PROBLEM]`.

## Helper Function Reuse

Check if existing helper functions in `music_assistant/helpers/` cover what new code is doing. Providers should use shared helpers instead of reimplementing logic. Example: if a music provider does PLS parsing, it should use `parse_pls` from `music_assistant.helpers.playlists` instead of writing its own.

## PR Title

The PR title must be a functional description of the change. It must NOT contain conventional commit prefixes such as `feat:`, `fix:`, `refactor:`, `chore:`, etc. Labels categorize PRs, not the title. Flag as `[PROBLEM]` if the title uses such prefixes.

## Existing Review Comments

Check if existing review comments on the PR have been addressed. Flag unaddressed comments.

## CI Context

You review PRs immediately, before CI completes. Do not flag issues that CI will catch.

### What CI Checks (`.github/workflows/test.yml`)
**Lint:** `SKIP=no-commit-to-branch pre-commit run --all-files` (see `.pre-commit-config.yaml` for full list)
**Tests:** `pytest --durations 10 --cov-report term-missing --cov=music_assistant --cov-report=xml tests/`

## Skip These (Low Value)

Do not comment on:
- Style/formatting (pre-commit handles this)
- Test failures (CI catches this)
- Missing dependencies (CI catches this)
- Minor naming suggestions
- Suggestions to add comments
- Logging suggestions unless security-related

## Issue Categories

Categorize every issue as:
- `[CRITICAL]` — must fix before merging (bugs, security issues, broken functionality)
- `[PROBLEM]` — should fix (code quality, bad patterns, missing tests)
- `[SUGGESTION]` — optional improvement (style, minor refactors, nice-to-haves)

## Output

- Post inline comments on GitHub for every `[CRITICAL]` and `[PROBLEM]` issue found.
- Do NOT post `[SUGGESTION]` items to GitHub.
- Do not list things that are already correct.

## Output Comment Format

1. State the severity ([CRITICAL], [PROBLEM], [SUGGESTION])
2. State the problem (1 sentence)
3. Why it matters (1 sentence, if needed)
4. Suggested fix (snippet or specific action)

Example:
This could generate a `KeyError` if `"name"` does not exist in the `dict`. Consider using `.get("name")` or adding a check.
