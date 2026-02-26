---
name: review-pr
description: Review a GitHub pull request and provide feedback comments
---

# Review GitHub Pull Request

Please review the GitHub pull request: $ARGUMENTS.

Follow these steps:
1. Use 'gh pr view' to get the PR details and description.
2. Use 'gh pr diff' to see all the changes in the PR.
3. Use 'gh pr checks' to see the status of CI checks.
4. Apply the review standards defined in `REVIEW_STANDARDS.md` (located in the same directory as this skill).
6. Generate constructive review comments in the CONSOLE. DO NOT POST TO GITHUB YOURSELF.

IMPORTANT:
- If the local commit does not match the pr one, checkout the PR locally using 'gh pr checkout'.
- CRITICAL: If 'gh pr checkout' fails for ANY reason, you MUST immediately STOP.
    - Do NOT attempt any workarounds (git fetch, alternative methods, etc.).
    - Do NOT proceed with the review using only diffs.
    - ALERT about the failure and WAIT for instructions.
    - This is a hard requirement - no exceptions.
- When checked out locally, ensure the local commit hash matches the remote one.
    - CRITICAL: if the commits don't match, you MUST immediately STOP.
- DO NOT make any changes to the code
- Be constructive and specific in your comments
- Suggest improvements where appropriate
- Only provide review feedback in the CONSOLE. DO NOT ACT ON GITHUB.
- No need to run tests or linters, just review the code changes.

Output format:
- List specific comments per file/line that need attention
- Do not list things that are already correct
- In the end, summarize with an overall assessment (approve, request changes, or comment) and list of changes suggested, if any.
  - Example output:
    ```
    Overall assessment: request changes
    - [CRITICAL] Memory leak in music_assistant/components/sensor/my_sensor.py
    - [PROBLEM] Inefficient algorithm in music_assistant/helpers/data_processing.py
    - [SUGGESTION] Improve variable naming in music_assistant/helpers/config_validation.py
    ```

Output Comment Format
1. State the problem (1 sentence)
2. Why it matters (1 sentence, if needed)
3. Suggested fix (snippet or specific action)

Example:
This could generate a `KeyError` if `"name"` does not exist in the `dict`. Consider using `.get("name")` or adding a check.
