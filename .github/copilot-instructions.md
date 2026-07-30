# PR Review Standards

## Philosophy

Comment only where you're confident there's a real issue — if you're uncertain whether something is a problem, don't raise it. Actionable feedback, not observations. One sentence where one sentence does the job. On documentation or user-facing text, flag wording only when it's genuinely confusing or could mislead someone into an error.

## What to review

Correctness and bugs, blocking IO in async code, security, performance, test coverage, docs the change makes wrong, and consistency with the surrounding code. `AGENTS.md` holds the project standards — deviations from it are at least `[PROBLEM]`.

## Linked issues

If the PR references an issue, read it before reviewing the diff. Then judge two things: whether the change actually resolves the reported problem, and whether it does so in the most elegant way available. A fix that works but patches a symptom, sits at the wrong layer, or adds a special case where the existing design already had a seam for it is a `[PROBLEM]` — say which approach would be cleaner and why. A change that leaves part of the issue unaddressed, or addresses something else entirely, is `[CRITICAL]`.

## Reuse over reimplementation

Check whether `music_assistant/helpers/` already covers what new code does; providers are expected to use the shared helpers rather than reimplement. A music provider parsing PLS itself instead of calling `parse_pls` from `music_assistant.helpers.playlists` is a `[PROBLEM]`.

## New providers

When the PR adds files under `music_assistant/providers/`, the matching demo provider is the ground truth — it's the annotated template that encodes the required structure, lifecycle and config schema:

| Provider type | Reference |
|---|---|
| Music source | `music_assistant/providers/_demo_music_provider` |
| Player | `music_assistant/providers/_demo_player_provider` |
| Plugin | `music_assistant/providers/_demo_plugin_provider` |
| Audio analysis | `music_assistant/providers/_demo_audio_analysis_provider` |

Read the demo provider alongside the new one, then flag deviations from its requirements and patterns as `[PROBLEM]` or `[CRITICAL]` depending on what breaks. Provider icons (`icon.svg`) are capped at 5KB — anything larger is `[CRITICAL]`.

## Don't duplicate CI

Reviews happen before CI finishes, which makes it tempting to flag what CI will catch on its own — formatting, lint, test failures, missing dependencies. Those comments cost the author a round trip and tell them nothing they won't hear from CI a minute later; `.pre-commit-config.yaml` and `.github/workflows/test.yml` are the authority on that class of problem. Spend the review on what CI can't see.

## PR title

A functional description of the change, with no conventional-commit prefix (`feat:`, `fix:`, `refactor:`, `chore:`, ...) — labels do the categorizing. A prefixed title is a `[PROBLEM]`.

## Existing review comments

Flag earlier review comments on the PR that haven't been addressed.

## Severity

- `[CRITICAL]` — must fix before merge: bugs, security issues, broken functionality
- `[PROBLEM]` — should fix: bad patterns, missing tests, standards deviations
- `[SUGGESTION]` — optional: minor refactors, nice-to-haves

## Output

Post an inline comment on GitHub for every `[CRITICAL]` and `[PROBLEM]`. Do not post `[SUGGESTION]` items, and don't comment on what's already fine.

Each comment carries its severity, states the problem in a sentence, says why it matters when that isn't self-evident, and gives a concrete fix or snippet.
