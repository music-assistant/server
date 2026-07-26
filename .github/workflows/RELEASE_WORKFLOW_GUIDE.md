# Release Workflow Guide

The server release workflows publish one verified source commit as an immutable GitHub
release and an exact multi-architecture GHCR image before changing any rolling channel.
The server package is attached to GitHub releases only; it is not published to PyPI.

## Release channels

| Channel | Source branch | Version format | GitHub release |
|---|---|---|---|
| Stable | `stable` | `X.Y.Z` | Release |
| RC | `stable` | `X.Y.ZrcN` | Prerelease |
| Beta | `dev` | `X.Y.ZbN` | Prerelease |
| Nightly | `dev` | `X.Y.Z.devN` | Prerelease |

Auto-release checks out the channel branch explicitly and captures its full commit SHA.
That `source_sha` is passed to the reusable release workflow and remains the source for
tests, release notes, package contents, the Git tag, the exact container image, and
frontend-version extraction even if the branch advances during the run.

Automatic versions come from Git tags, not release records. The workflow validates the
previous channel tag's relationship to `source_sha` and counts its Git commit range.
Deleted release records therefore cannot make a tag/version reusable. Stable automatic
releases continue to increment the patch component only. Nightlies require at least two
commits after the previous nightly tag.

## Publishing sequence

Only one release workflow runs at a time, and queued runs are never cancelled.

1. Resolve the channel branch and exact `source_sha`.
2. Require the repository's immutable-release setting using a short-lived GitHub App
   token with Administration read access.
3. If the version is not already published, run the full test workflow against
   `source_sha`.
4. Build the wheel and source distribution, or recover the exact two verified assets
   from a matching draft.
5. Create an annotated exact tag with `github-actions[bot]`, then create or update only a
   draft whose version, tag, and target SHA match. Replace incomplete draft assets, then
   verify both asset names, sizes, upload state, and SHA-256 digests.
6. Build and push only `ghcr.io/music-assistant/server:$VERSION`. The image index must
   contain `linux/amd64` and `linux/arm64`, identify `source_sha` and the wheel digest,
   and produce one captured OCI digest. An existing exact tag is verified and never
   overwritten.
7. Revalidate the draft, recheck the immutable-release setting, and publish once.
8. Require an immutable release, the tag at `source_sha`, matching release assets,
   successful release and asset attestations, and the same exact OCI digest.
9. Promote that digest without rebuilding to the channel aliases:
   - Stable: `X.Y`, `X`, `stable`, `latest`
   - RC and beta: `beta`
   - Nightly: `nightly`
   Each alias's current amd64 and arm64 image labels are checked independently. An alias
   that already points to a newer release is never moved backward.
10. Deterministically update the matching Home Assistant add-on and dispatch the
    frontend version from `source_sha` to `app.music-assistant.io`.

The exact image tag is already the full version (`X.Y.Z`, `X.Y.ZbN`, `X.Y.ZrcN`, or
`X.Y.Z.devN`). Rolling aliases are never pushed before the immutable release verifies.
Normal channel releases are forward-only. This workflow does not define a legacy-branch
backport process for publishing an older version after a newer channel release.

## Starting a release

The scheduled workflow checks nightly releases automatically. For a manual version:

1. Run **Auto Release** from the `dev` branch.
2. Select `stable`, `rc`, `beta`, or `nightly`.
3. Add important notes when needed.

Auto-release calculates the next version and invokes **Create Release** with its captured
source SHA. **Create Release** can also be dispatched directly with an explicit version;
for direct runs it resolves and freezes the current channel branch head itself unless you
pass `source_sha` to recover an exact draft or published release source.

Do not create or publish a GitHub release manually. A draft created outside the workflow
is accepted only when its exact tag name and target SHA match; conflicting tags,
published mutable releases, and mismatched drafts fail closed.

## Authentication

Same-repository tags, releases, assets, attestations, and GHCR writes use the job's
built-in `GITHUB_TOKEN` and `github-actions[bot]`.

Administrative and cross-repository work uses a fresh installation token in each job
from the private `music-assistant-bot` GitHub App. Tokens are never passed between jobs
and are restricted to one repository and the permission needed by that job.

The server repository must define:

- Repository variable `MUSIC_ASSISTANT_BOT_CLIENT_ID`
- Repository secret `MUSIC_ASSISTANT_BOT_PRIVATE_KEY`

The App installation must include these selected repositories:

| Repository | Token permission used by this workflow |
|---|---|
| `music-assistant/server` | Administration: read |
| `music-assistant/appvars` | Contents: read |
| `music-assistant/home-assistant-addon` | Contents: write |
| `music-assistant/app.music-assistant.io` | Contents: write |

The App itself must be approved for Administration read and Contents write. Each minted
token is downscoped from those installation permissions. The workflow verifies that the
token's App slug is `musicassistant-bot` and its installation ID is `146062122`.

## Recovery

Rerun the failed workflow with the same version and `source_sha`.

If you need to resume an exact draft or published release after the branch has advanced,
reuse the workflow-created source SHA from the matching tag or draft target commit:

```bash
gh workflow run release.yml --ref dev -f version=2.10.0.dev2026072510 -f channel=nightly -f source_sha=a087405a28d2c0991803dbd9c037dc76fd05a631
```

Use the exact commit recorded by the workflow-created tag or draft. There is no moving
branch recovery path.

- **Before publication:** The matching draft remains mutable. Complete assets are
  downloaded and reused byte-for-byte; incomplete assets are replaced. If the exact
  image already exists, its source, wheel digest, platforms, and OCI digest must match,
  otherwise the rerun stops without overwriting it. Run-scoped build artifacts remain
  available to GitHub's **Re-run failed jobs** path. A crash after exact tag creation but
  before draft creation can resume only when the annotated tag has the workflow's exact
  source marker and `github-actions[bot]` identity.
- **After publication:** Tests, package builds, draft mutation, and publication are
  skipped. The immutable release, assets, attestations, tag, and exact image are verified
  again, then rolling aliases and downstream updates resume. If a newer release already
  superseded this version, verification still runs but older downstream state is not
  restored.

An immutable release with incorrect contents cannot be repaired. Publish a new version
that supersedes it. A published mutable release is also never adopted by this workflow.

The add-on changelog update removes duplicate entries for the same version, prepends one
canonical entry using the GitHub publication date, and retains three distinct releases.
The add-on repository's default branch is resolved through GitHub, and non-fast-forward
updates retry a bounded pull/rebase/push sequence.

Frontend recovery reads the target repository's `channels.json` first. Equal or newer
frontend state suppresses the dispatch; an older state receives a payload containing a
stable `server@$VERSION` idempotency key, channel, frontend and server versions, source
SHA, and image digest. The receiver does not yet persist the idempotency key itself, so
an immediate rerun while the first dispatch is still in flight can enqueue a replacement;
the receiver's per-channel concurrency cancels the older in-flight run.

## Rollout

Perform rollout in this order:

1. Merge the support hardening PR for existing `music-assistant-bot` token usage.
2. Add/approve the App's Administration read and Contents write permissions and the four
   selected repositories above.
3. Merge the server release-workflow PR.
4. Enable immutable releases for `music-assistant/server`.
5. Run a unique nightly canary and verify its release, two assets, exact multi-arch image,
   rolling `nightly` alias, add-on update, and frontend dispatch.

Keep the immutable-release setting disabled until the server PR has merged. Enabling it
against the previous publish-first workflow can strand incomplete public releases.
