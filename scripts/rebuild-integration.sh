#!/usr/bin/env bash
set -euo pipefail

# Save current branch to restore after script
ORIG_BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "")

git fetch origin

# Rebuild from fork's dev (already merged with upstream via sync-upstream.yml)
git checkout -B integration/pending-upstream-prs origin/dev

# awk is more robust than sed for stripping leading whitespace + "origin/" prefix
BRANCHES=$(git branch -r | grep 'origin/upstream/' | awk '{print $1}' | sed 's|^origin/||' || true)

if [[ -z "$BRANCHES" ]]; then
  echo "→ No upstream/* branches — integration = dev"
  git push origin integration/pending-upstream-prs:integration/pending-upstream-prs --force
else
  for branch in $BRANCHES; do
    echo "→ Merging $branch"
    git merge "origin/$branch" --no-edit \
      -m "chore: merge $branch into integration" || {
      echo "ERROR: conflict merging $branch — resolve manually, then re-run"
      git merge --abort
      # Restore original branch before exiting
      [[ -n "$ORIG_BRANCH" ]] && git checkout "$ORIG_BRANCH" 2>/dev/null || true
      exit 1
    }
  done
  git push origin integration/pending-upstream-prs:integration/pending-upstream-prs --force
  echo "✓ Rebuilt: dev + $(echo "$BRANCHES" | wc -l | tr -d ' ') upstream branches"
fi

# Restore original branch (CI: ORIG_BRANCH is empty on detached HEAD — skip)
[[ -n "$ORIG_BRANCH" ]] && git checkout "$ORIG_BRANCH" || true
