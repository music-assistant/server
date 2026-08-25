# Release Notes Generation

Release notes are generated from the exact `source_sha` selected at the start of a
release. Branch names determine the channel source, but they are never used as the notes
comparison head after the SHA is captured.

## Previous-tag selection

The workflow discovers prior releases from Git tags:

| Channel | Previous tag |
|---|---|
| Stable | Latest `X.Y.Z` tag |
| Beta | Latest `X.Y.ZbN` or `X.Y.ZrcN` tag |
| RC1 | Latest `X.Y.ZbN` tag |
| RC2+ | Previous RC for the same base version, falling back to the latest beta |
| Nightly | Latest `X.Y.Z.devN` tag |

The version currently being prepared is always excluded. This matters for retries and
for deleted release records whose Git tag still exists.

For linear histories, pull requests come from the commits between the previous tag and
`source_sha`. A stable minor branch can diverge from the previous stable patch branch.
In that case, the generator uses the merge base as its cutoff and excludes pull requests
that already shipped on the old patch branch.

## Note contents

`.github/release-notes-config.yml` defines label categories, exclusions, formatting, and
contributors. The generator:

1. Finds merged pull requests represented in the exact Git comparison.
2. Excludes pull requests merged before the comparison cutoff or already released on a
   diverged stable patch branch.
3. Categorizes changes using the release-notes configuration.
4. Extracts notes from frontend dependency-update pull requests in the same comparison.
5. Merges and deduplicates server and frontend contributors.
6. Places manually supplied important notes first.
7. Renders an optional blog post link as a banner between the channel header and the
   changes-since line.

The resulting body is finalized on the matching draft before publication. Reruns may
refresh draft notes, but release assets and the source SHA must remain exact. After
publication, the release is verified as immutable and release/asset attestations are
checked before downstream promotion.

## Verification

The focused tests cover linear and diverged comparisons:

```bash
pytest tests/test_generate_release_notes.py tests/scripts/test_release_workflow.py
```

To inspect a pending comparison manually, use the source SHA reported by auto-release:

```bash
git log PREVIOUS_TAG..SOURCE_SHA --oneline
```

For a diverged stable tag, inspect both sides from their merge base:

```bash
base=$(git merge-base PREVIOUS_TAG SOURCE_SHA)
git log "$base..SOURCE_SHA" --oneline
git log "$base..PREVIOUS_TAG" --oneline
```
