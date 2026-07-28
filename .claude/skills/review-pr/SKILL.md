---
name: review-pr
description: Use when asked to review a GitHub pull request, PR link is shared, or user says /review-pr
---

# Review GitHub Pull Request

Review the GitHub pull request: $ARGUMENTS.

This is a read-only review. Report findings in the console — never post comments, approve, or request changes on GitHub, and never modify the code under review.

## Steps

1. `gh pr view` for the PR details and description.
2. Create a git worktree and do everything below inside it, so the reviewer's checked-out branch stays untouched (`git worktree add --detach <path>`).
3. `gh pr checkout` in the worktree, then confirm the local commit hash matches the remote PR head.
   - **HARD STOP**: if the checkout fails, or the hashes still differ afterwards, stop and report it. No workarounds — not `git fetch`, not reviewing from the diff alone. Wait for instructions.
4. `gh pr checks` for CI status.
5. `gh pr diff` for the changes, then read the surrounding code for anything the diff alone can't settle.
6. Apply `REVIEW_STANDARDS.md`, in the same directory as this skill.

Running tests or linters isn't part of this — CI does that.

## Output Format

Comments per file and line that need attention. Skip what's already fine.

Each comment carries a severity (`[CRITICAL]`, `[PROBLEM]`, `[SUGGESTION]`), states the problem in a sentence, says why it matters when that isn't self-evident, and gives a concrete fix or snippet.

Close with an overall assessment — `approve`, `request changes`, or `comment` — followed by the findings grouped by severity.
