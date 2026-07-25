# Release Workflow Guide

## Prerequisites

Before running a release:

- Make `MUSIC_ASSISTANT_BOT_CLIENT_ID` and `MUSIC_ASSISTANT_BOT_PRIVATE_KEY` available
  to this repository as an Actions variable and secret.
- Grant the `musicassistant-bot` GitHub App Administration read, Contents read/write,
  Issues write, and Pull requests write permissions.
- Select `server`, `appvars`, `home-assistant-addon`, and
  `app.music-assistant.io` in installation `146062122`, then approve the installation
  permission changes.
- Enable **immutable releases** for `music-assistant/server` only after this workflow
  change is merged.
- Keep same-repository release, tag, asset, and GHCR writes on the workflow
  `GITHUB_TOKEN`.

Each job mints a repository-scoped installation token with only the permissions it
needs. Tokens expire within one hour and are revoked when their job ends. The workflow
only reads the immutable-release setting; it never enables or changes it.

## Release Sources

Every release uses one full source commit SHA for tests, notes, distributions, the Git
tag, the container image, and frontend version extraction.

Release tooling is checked out from the workflow commit separately from the release
source. This allows a stable patch to build stable code while still using the current
release action and OCI verification logic.

| Channel | Source branch | Version format | Rolling container aliases |
|---|---|---|---|
| Stable | `stable` | `X.Y.Z` | `X.Y`, `X`, `stable`, `latest` |
| RC | `stable` | `X.Y.ZrcN` | `beta` |
| Beta | `dev` | `X.Y.ZbN` | `beta` |
| Nightly | `dev` | `X.Y.Z.devYYYYMMDDHH` | `nightly` |

`auto-release.yml` checks out the channel branch, captures its SHA, calculates a
version, and passes both values to `release.yml`. A direct **Create Release** run
resolves the selected channel branch once. Branch movement after that point cannot
change the release contents.

Automatic version discovery uses strict Git tag formats and commit ancestry rather
than release creation dates:

- Nightlies require at least two commits after the latest reachable nightly tag.
- Beta and RC numbers advance from their strict channel tags.
- Stable automation only increments the latest stable patch version.
- A planned stable minor promotion must use **Create Release** with an explicit
  version.

Tags remain authoritative even if a release record is missing, so deleted records and
reserved immutable versions cannot be selected again.

## Publication Sequence

1. Mint scoped App tokens for `server` settings and `appvars`, verify installation
   `146062122`, and read the immutable-release setting.
2. Resolve the exact branch SHA and inspect the requested version.
3. For a new release or matching draft, run the repository test workflow.
4. Build a deterministic wheel and source distribution. The server is not uploaded to
   PyPI; both files are GitHub release assets, and the wheel is also installed into the
   container image.
5. Generate final release notes and create or resume an exact matching draft.
6. Replace assets while the release is still a draft, then verify their names, sizes,
   uploaded state, upload identity, and SHA-256 digests.
7. Build and push only `ghcr.io/music-assistant/server:$VERSION`, then verify its
   amd64/arm64 manifest, source SHA, version, wheel digest, and OCI digest.
8. Bind the exact OCI digest into the draft's release metadata.
9. Mint a fresh Administration-read App token and recheck the immutable-release
   setting immediately before publishing the draft.
10. Create or validate the bot-authored annotated tag containing the source SHA, exact
    OCI digest, and wheel digest, then publish the draft once.
11. Require `isImmutable=true`, the exact source tag, matching asset digests, and valid
   `gh release verify` and `gh release verify-asset` attestations.
12. Promote the verified exact OCI digest to rolling aliases without rebuilding.
13. Update the Home Assistant add-on and dispatch the matching frontend version to
    `app.music-assistant.io` with fresh repository-scoped App tokens.

No public GitHub release exists if tests, package creation, draft upload, or exact image
creation fails. Rolling aliases and downstream repositories are changed only after the
immutable release passes verification.

## Retry and Recovery

Release concurrency is non-cancelling, keeps the maximum pending queue, and serializes
RC and beta publication because they share the same rolling channel.

| Existing state | Retry behavior |
|---|---|
| No release | Build distributions, prepare a draft, build the exact image, and publish. |
| Matching draft | Rebuild from the same source and pinned app-secrets commit, replace draft assets, and resume before publication. |
| Published immutable release | Skip tests, builds, and all asset mutation; verify the release and exact image, then resume aliases and downstream updates. |
| Conflicting draft, tag, or mutable release | Stop without changing it. |
| Invalid immutable release | Stop and create a new version; immutable releases are never repaired or reused. |

The exact GHCR version tag is write-once. A retry verifies and reuses it only when its
digest matches the immutable annotated release tag and its source and wheel labels
match. Each rolling alias and each downstream update is idempotent, so a
post-publication retry can safely resume incomplete rollout work. Retrying an older
release never replaces newer rolling aliases or downgrades downstream channels. A
manual patch for an older stable line may still advance its own `X.Y` alias while
newer `X`, `stable`, and `latest` aliases remain unchanged.
The frontend dispatcher also compares the currently deployed frontend version and
ignores a stale downgrade request.

## Manual Releases

Open **Actions** on the `dev` workflow revision, select **Create Release**, and provide:

- the version in the format for its channel;
- the channel;
- optional important notes.

The immutable-release setting must already be enabled. Do not create the tag or
release manually.

## External Rollout

1. Merge this workflow change while immutable releases remain disabled.
2. Update the `musicassistant-bot` App permissions and selected repositories listed
   above.
3. Approve the installation changes and confirm the Client ID variable and private-key
   secret are available to `server`.
4. Enable immutable releases for `music-assistant/server`.
5. Run the intended release version. Do not pre-create its tag or GitHub release.

## Troubleshooting

- **App token error:** confirm the Client ID/private key belong to `musicassistant-bot`,
  installation `146062122` contains the target repository, and its requested
  permissions were approved.
- **Immutable releases disabled:** enable the repository setting, then rerun.
- **Tag or draft conflict:** inspect the reported version and use a new version unless
  it is the workflow's matching draft.
- **Published verification failure:** do not edit or reuse the release; supersede it.
- **Downstream failure:** rerun the same version. The immutable release and exact image
  are verified and skipped before rollout resumes.
