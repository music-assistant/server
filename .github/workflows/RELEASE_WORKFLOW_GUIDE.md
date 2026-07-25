# Release Workflow Guide

This document explains how releases are created for Music Assistant.

## Overview

Releases are built around GitHub's **immutable releases** setting. Once a release
is published, its tag, notes and assets can never change — so the workflow does
all of its validation, building and verification *before* publishing, and only
publishes once, atomically. Two workflows work together:

- **`auto-release.yml`** — resolves a channel to its source branch, decides the
  next version from existing Git tags, and triggers `release.yml` with the exact
  commit to release. Runs nightly on a schedule, or manually for beta/rc/stable.
- **`release.yml`** — the reusable workflow that validates, builds, publishes and
  promotes a single release. Can also be triggered directly for one-off releases.

## How to Create a Release

### Automatically (recommended)
Nightly releases are created automatically every day at 05:00 Europe/Amsterdam
time, provided there are at least 2 new commits on `dev` since the last nightly.

### Manually
1. Go to the **Actions** tab in GitHub
2. Select **"Auto Release"** (computes the next version for you) or
   **"Create Release"** (lets you specify an exact version)
3. Click **"Run workflow"** and pick the channel: `nightly`, `beta`, `rc` or `stable`
4. For **"Create Release"**, also fill in the version number (see formats below)

## Version Format Requirements

| Channel | Format | Example | Source branch |
|---|---|---|---|
| stable | `X.Y.Z` | `2.1.0` | `stable` |
| rc | `X.Y.ZrcN` | `2.1.0rc1` | `stable` |
| beta | `X.Y.ZbN` | `2.1.0b1` | `dev` |
| nightly | `X.Y.Z.devN` | `2.1.0.dev2025102305` | `dev` |

Stable releases bump the patch version. Beta and nightly versions are always one
minor version ahead of the latest stable release. RC versions are based on the
latest beta of the same base version.

## Release State Machine

Every release goes through the same sequence, keyed off the **exact commit SHA**
resolved at the start of the run (not a moving branch tip), so a rerun always
targets the same code:

1. **Resolve source** — pins the exact commit to release.
2. **Verify immutable releases are enabled** — fails the run immediately if the
   repository setting is off, before anything else happens.
3. **Validate version** — checks the version format matches the channel.
4. **Create the release tag** — creates it if missing, or confirms an existing
   tag points at the same commit (rejects conflicting tags/versions).
5. **Run tests** — the full test suite against the exact commit.
6. **Build the package and Docker image, draft the release** — builds the wheel
   and sdist, generates release notes, creates (or resumes) a **draft** release
   pinned to the tag, uploads the built assets, and builds+pushes a single exact
   image tag (`ghcr.io/music-assistant/server:$VERSION`) — no rolling aliases yet.
7. **Recheck and publish** — rechecks the immutable setting one more time, then
   publishes the release exactly once, and verifies the published release is
   immutable, matches the exact commit, and its assets are intact.
8. **Promote rolling tags** — retags the *same* image digest (no rebuild) as the
   channel's rolling aliases (e.g. `stable`, `latest`, `2.1`, `2`).
9. **Update downstream repositories** — bumps the Home Assistant add-on and
   notifies `app.music-assistant.io` of the new frontend version.

If a run fails partway through, simply rerun it: steps already completed
(tag, draft, assets, published release) are detected and reused rather than
redone, and a run that failed after publishing resumes from the downstream
update steps only. A published release that is somehow not immutable is never
reused — release a new version instead.

## Credentials

- Operations on this repository (tags, releases, assets, container images) use
  the default `GITHUB_TOKEN`.
- Cross-repository and administrative operations (checking the immutable
  setting, updating the add-on repository, notifying the frontend app) use
  short-lived tokens minted per job from the `music-assistant-bot` GitHub App,
  each scoped to only the repository and permission it needs.

## Docker Image Tags

### Stable
`X.Y.Z`, `X.Y`, `X`, `stable`, `latest`

### Beta / RC
`X.Y.ZbN` / `X.Y.ZrcN`, `beta`

### Nightly
`X.Y.Z.devN`, `nightly`

The exact version tag is built and pushed first; the rolling aliases above are
applied afterwards by retagging that same image digest — they never trigger a
separate build.

## Troubleshooting

### "Enable release immutability" error
The repository's immutable-releases setting (Settings → General → Releases) is
off. Enable it — the workflow refuses to run without it.

### "Tag already exists and points at a different commit"
The version you asked for was already released from a different commit. Bump
the version number to release the new commit.

### "Release is already published but is not immutable"
A release for that version exists but predates the immutable-releases setting
(or was created outside this workflow). Use a new version number instead of
reusing it.

### Docker build fails
Check that the base image version (configured in `release.yml`'s `env` section)
exists and that the `Dockerfile` is compatible with the version being built.
