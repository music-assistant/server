---
name: review-pr
description: Use when asked to review a GitHub pull request, PR link is shared, or user says /review-pr
---

# Review GitHub Pull Request

Review the GitHub pull request: $ARGUMENTS.

## Steps

1. Run `gh pr view` to get PR details and description.
2. Run `gh pr diff` to see all changes.
3. Compare the local commit hash with the remote PR head commit.
   - If they don't match, run `gh pr checkout` to check out the PR locally.
   - **HARD STOP**: If `gh pr checkout` fails for ANY reason, STOP immediately. Do NOT attempt workarounds (git fetch, alternative methods). Do NOT proceed with the review using only diffs. Alert about the failure and wait for instructions.
   - After checkout, verify the local commit hash matches the remote one. If they still don't match, STOP.
4. Run `gh pr checks` to see CI status.
5. Apply the review standards defined in `REVIEW_STANDARDS.md` (located in the same directory as this skill).
6. Output review feedback to the CONSOLE only. DO NOT act on GitHub (no posting comments, no approving, no requesting changes).

## Output Format

List specific comments per file/line that need attention. Do not list things that are already correct.

Each comment:
1. State the severity ([CRITICAL], [PROBLEM], [SUGGESTION])
2. State the problem (1 sentence)
3. Why it matters (1 sentence, if needed)
4. Suggested fix (snippet or specific action)

Example:
> This could generate a `KeyError` if `"name"` does not exist in the `dict`. Consider using `.get("name")` or adding a check.

End with an overall assessment and summary:

```
Overall assessment: <approve | request changes | comment>
- [CRITICAL] <issue>
- [PROBLEM] <issue>
- [SUGGESTION] <improvement>
```

## Constraints

- DO NOT make any changes to the code.
- No need to run tests or linters, just review the code changes.
