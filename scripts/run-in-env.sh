#!/usr/bin/env bash
# Run a development tool through `uv run`, also from a git worktree.
set -euo pipefail

# A worktree has no virtualenv of its own, so `uv run` builds an incomplete one there (the
# linters live in the `test` extra, which uv never syncs) and then fails to spawn the tool.
# Point uv at the main checkout's environment instead. Syncing is disabled in that case
# because uv resolves the environment against the *current* project, which would re-point
# the shared venv's editable install at the worktree.
git_dir=$(git rev-parse --path-format=absolute --git-dir 2>/dev/null || true)
common_dir=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
if [[ -n "$common_dir" && "$git_dir" != "$common_dir" ]]; then
  main_venv="$(dirname "$common_dir")/.venv"
  if [[ -d "$main_venv" ]]; then
    export UV_PROJECT_ENVIRONMENT="$main_venv"
    export UV_NO_SYNC=1
  fi
fi

exec uv run "$@"
