# PR Review Standards

## Review Philosophy
* Only comment when you have HIGH CONFIDENCE (>80%) that an issue exists
* Be concise: one sentence per comment when possible
* Focus on actionable feedback, not observations
* When reviewing text, only comment on clarity issues if the text is genuinely confusing or could lead to errors.

## What to Analyze

Review all code changes for:
- Code quality and style consistency with the existing codebase
- Potential bugs or issues
- Performance implications
- Always check for blocking IO in async code
- Security concerns
- Test coverage
- Documentation updates if needed

## PR Title
The PR title must be a functional description of the change. It must NOT contain conventional commit prefixes such as `feat:`, `fix:`, `refactor:`, `chore:`, etc. Labels are used to categorize PRs, not the title. Flag as `[PROBLEM]` if the title uses such prefixes.

## CI context
**Important**: You review PRs immediately, before CI completes. Do not flag issues that CI will catch.

### What Our CI Checks (`.github/workflows/test.yml`)
**Lint checks:**
* SKIP=no-commit-to-branch pre-commit run --all-files
* See .pre-commit-config.yaml for the full list of checks

**Runs test:**
* pytest --durations 10 --cov-report term-missing --cov=music_assistant --cov-report=xml tests/

## Skip These (Low Value)
Do not comment on:

* Style/formatting (pre-commit handles this)
* Test failures
* Missing dependencies (ci handles this)
* Minor naming suggestions
* Suggestions to add comments
* Multiple issues in one comment
* Logging suggestions unless security-related

## Issue Categories
Categorize every issue found as one of:
- `[CRITICAL]` — must be fixed before merging (bugs, security issues, broken functionality)
- `[PROBLEM]` — should be fixed (code quality, bad patterns, missing tests)
- `[SUGGESTION]` — optional improvement (style, minor refactors, nice-to-haves)
