# Provider Repository Workflow

Each of the 4 custom providers has its own repository for development.
This fork serves as the integration layer and upstream contribution staging area.

## Repositories

| Provider | Repo | Domain |
|----------|------|--------|
| Yandex Music | https://github.com/trudenboy/ma-provider-yandex-music | `yandex_music` |
| KION Music   | https://github.com/trudenboy/ma-provider-kion-music   | `kion_music` |
| Zvuk Music   | https://github.com/trudenboy/ma-provider-zvuk-music   | `zvuk_music` |
| MSX Bridge   | https://github.com/trudenboy/ma-provider-msx-bridge   | `msx_bridge` |

## Architecture

```
[Provider Repo]  →  (auto PR on release)  →  [trudenboy/ma-server]  →  (manual PR)  →  [upstream]
  (develop here)                              (integration & snapshot)                  (contribution)
```

**Primary development** always happens in the provider repo, not directly in this fork.

## Branch Naming

| Branch | Purpose |
|--------|---------|
| `dev` | Tracks upstream dev (auto-merged daily via sync-upstream.yml) |
| `integration/pending-upstream-prs` | All pending upstream work combined (auto-rebuilt) |
| `provider/<domain>-<version>` | Auto-created sync PR per provider release |
| `upstream/<domain>/<description>` | Upstream PR source branch (manual) |

## Day-to-Day Workflow

### When a provider releases a new version

1. `sync-to-fork.yml` auto-creates PR: `provider/<domain>-<version>` → `dev`
2. Review and merge the PR
3. `rebuild-integration.yml` auto-rebuilds `integration/pending-upstream-prs`

### When submitting to upstream

```bash
# 1. Create upstream PR branch from fork dev
git checkout dev && git pull
git checkout -b upstream/yandex_music/add-provider

# 2. Push and create PR
git push origin upstream/yandex_music/add-provider
gh pr create \
  --repo music-assistant/server \
  --base dev \
  --head "trudenboy:upstream/yandex_music/add-provider" \
  --title "feat(yandex_music): add Yandex Music provider"

# 3. rebuild-integration.yml rebuilds integration automatically on push
```

### When upstream requests changes (review feedback)

**Minor fixes** — commit directly to the upstream PR branch:
```bash
git checkout upstream/yandex_music/add-provider
# make changes
git push origin upstream/yandex_music/add-provider --force-with-lease
```

**Major changes** — develop in provider repo, release a patch, sync to fork, then cherry-pick to upstream branch:
```bash
# provider repo: fix/* → dev → main → v2.1.1 release
# sync-to-fork.yml creates PR: provider/yandex_music-2.1.1 → fork dev
# merge sync PR — <FIX_COMMIT> now in fork dev

git checkout upstream/yandex_music/add-provider
git cherry-pick <FIX_COMMIT>
git push origin upstream/yandex_music/add-provider --force-with-lease
```

### When upstream merges your PR

```bash
# 1. Delete upstream PR branch
git push origin --delete upstream/yandex_music/add-provider
# → integration auto-rebuilds without this branch

# 2. Sync fork dev with upstream (don't wait for cron)
git remote add upstream https://github.com/music-assistant/server.git 2>/dev/null || true
git fetch upstream dev
git checkout dev
git merge upstream/dev --no-edit -m "chore: sync upstream dev after merge"
git push origin dev
```

## Integration Branch

`integration/pending-upstream-prs` = `dev` + all `upstream/*` branches merged.

**Rebuilt automatically** when any `upstream/**` branch or `dev` is updated (rebuild-integration.yml).

**Manual rebuild:**
```bash
./scripts/rebuild-integration.sh
```

**Never manually cherry-pick into this branch** — use the rebuild script instead.

## Running All Providers Locally

```bash
# Docker (all 4 providers):
docker compose up

# Clean state:
docker compose down -v && docker compose up

# Non-Docker:
./scripts/run-dev.sh
```

## Provider Repo Structure

Each provider repo follows the same layout:
```
provider/              # source → music_assistant/providers/<domain>/
tests/                 # tests → tests/providers/<domain>/
pyproject.toml
README.md
DEVELOPMENT.md
CHANGELOG.md
.pre-commit-config.yaml
scripts/setup.sh
scripts/dev-server.sh
.vscode/
.github/workflows/
  test.yml
  release.yml
  sync-to-fork.yml
```

## Versioning

```
MAJOR.MINOR.PATCH

MAJOR — breaking: incompatible with new MA API version
MINOR — feature: new functionality
PATCH — bugfix, optimizations
```

Initial versions:
- Yandex Music: 2.0.0
- KION Music: 1.0.0
- Zvuk Music: 1.0.0
- MSX Bridge: 1.0.0

## Upstream PR Lifecycle

### State Machine

```
STATE 1: Development
  provider repo:  feature/* → dev (active development)
  fork:           unchanged, no upstream/* branch
  Commands:       cd ma-provider-<name> && git checkout -b feature/x && pytest tests/

                  ↓  release workflow → tag → GitHub Release

STATE 2: Released, sync PR open in fork
  provider repo:  main @ vX.Y.Z
  fork:           PR open: provider/<domain>-X.Y.Z → dev
  Commands:       gh pr view --repo trudenboy/ma-server

                  ↓  merge sync PR

STATE 3: Synced to fork (integration & E2E testing)
  provider repo:  main @ vX.Y.Z
  fork dev:       contains vX.Y.Z code
  integration:    unchanged (no upstream/* yet)
  ⚠️ Can stay here indefinitely — run E2E tests, accumulate releases
  Commands:       ./scripts/dev-server.sh  (from provider repo)
                  docker compose up        (from fork)

                  ↓  create upstream PR branch + open PR

STATE 4: Upstream PR open (pending review)
  fork:           upstream/<domain>/<desc> branch exists
  upstream:       PR open: trudenboy:upstream/... → music-assistant:dev
  integration:    = dev + upstream/* branch (auto-rebuilt)
  Commands:       gh pr view --repo music-assistant/server
                  git push origin upstream/... --force-with-lease  (for review fixes)

                  ↓  upstream merged

STATE 5: Cleanup
  Commands:       git push origin --delete upstream/<domain>/<desc>
                  # then: sync fork dev with upstream (see above)
```

### Cleanup after upstream merge

```bash
# Delete upstream PR branch → triggers auto-rebuild of integration
git push origin --delete upstream/yandex_music/add-provider

# Sync fork dev immediately (don't wait for cron)
git remote add upstream https://github.com/music-assistant/server.git 2>/dev/null || true
git fetch upstream dev
git checkout dev
git merge upstream/dev --no-edit -m "chore: sync upstream dev after merge"
git push origin dev
```

## E2E Checklist: Yandex Music

Run before creating an upstream PR:

### Setup
- [ ] Provider connects successfully (token accepted, no error on load)
- [ ] No errors in MA logs at startup

### Browse
- [ ] Browse → Yandex Music opens
- [ ] Liked Tracks displayed
- [ ] My Wave available
- [ ] Picks & Mixes shows playlists

### Search
- [ ] Track search returns results
- [ ] Artist search works
- [ ] Album search works

### Playback
- [ ] Track plays (AAC/MP3 quality)
- [ ] Track plays (FLAC quality, if premium)
- [ ] Seek works correctly
- [ ] No stall mid-track

### Library sync
- [ ] Liked Tracks sync to library
- [ ] Album art loads

## Troubleshooting

**sync-upstream.yml failed with merge conflict**

Upstream changed files that overlap with provider code. Resolve manually:
```bash
git checkout dev
git remote add upstream https://github.com/music-assistant/server.git 2>/dev/null || true
git fetch upstream dev
git merge upstream/dev
# resolve conflicts
git push origin dev
```

**rebuild-integration.sh: conflict merging upstream/***

Two upstream/* branches conflict with each other:
```bash
git checkout integration/pending-upstream-prs
git status  # see conflicting files
# resolve conflicts
git add .
git commit
git push origin integration/pending-upstream-prs:integration/pending-upstream-prs --force
```

Note: use the explicit refspec `branch:branch` rather than `--force-with-lease` here,
as the worktree-based rebuild script may not have the remote tracking ref set up.

**sync-to-fork.yml: FORK_SYNC_PAT expired**

Renew the PAT (needs `contents:write` on `trudenboy/ma-server`) then update the secret
in all 4 provider repos:
```bash
gh secret set FORK_SYNC_PAT --body "$NEW_PAT" --repo trudenboy/ma-provider-yandex-music
gh secret set FORK_SYNC_PAT --body "$NEW_PAT" --repo trudenboy/ma-provider-kion-music
gh secret set FORK_SYNC_PAT --body "$NEW_PAT" --repo trudenboy/ma-provider-zvuk-music
gh secret set FORK_SYNC_PAT --body "$NEW_PAT" --repo trudenboy/ma-provider-msx-bridge
```

**sync-to-fork.yml ran but no PR created**

This is normal when the provider files haven't changed since the last sync.
`create-pull-request` action skips PR creation if the diff is empty.

**gh repo sync fails when run manually on fork**

`gh repo sync` does a fast-forward only — it fails when fork `dev` has extra commits
(provider syncs) not present in upstream. Use `git merge upstream/dev` instead
(as in `sync-upstream.yml`).
