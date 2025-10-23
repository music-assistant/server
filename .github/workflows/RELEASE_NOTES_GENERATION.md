# Release Notes Generation - Channel-Specific Behavior

## Overview

The release workflow generates release notes **specific to each channel** by only including commits since the last release of that same channel. It uses your **Release Drafter configuration** (`.github/release-drafter.yml`) for label-based categorization.

## How It Works

### 1. Previous Release Detection

The workflow identifies the previous release for each channel using tag patterns:

#### Stable Channel
- **Pattern**: `^[0-9]+\.[0-9]+\.[0-9]+$` (e.g., `2.1.0`, `2.0.5`)
- **Branch**: `stable`
- **Finds**: Latest stable release (no suffix)

#### Beta Channel
- **Pattern**: `^[0-9]+\.[0-9]+\.[0-9]+\.b[0-9]+$` (e.g., `2.1.0.b1`, `2.1.0.b2`)
- **Branch**: `dev`
- **Finds**: Latest beta release (`.bN` suffix)

#### Nightly Channel
- **Pattern**: `^[0-9]+\.[0-9]+\.[0-9]+\.dev[0-9]+$` (e.g., `2.1.0.dev20251023`)
- **Branch**: `dev`
- **Finds**: Latest nightly release (`.devYYYYMMDD` suffix)

### 2. Release Notes Generation

The workflow generates notes in three steps:

1. **Find PRs in commit range**: Extracts PR numbers from merge commits between the previous tag and HEAD
2. **Categorize by labels**: Applies the category rules from `.github/release-drafter.yml`:
   - ⚠ Breaking Changes (`breaking-change` label)
   - 🚀 New Providers (`new-provider` label)
   - 🚀 Features and enhancements (`feature`, `enhancement`, `new-feature` labels)
   - 🐛 Bugfixes (`bugfix` label)
   - 🧰 Maintenance (`ci`, `documentation`, `maintenance`, `dependencies` labels)
3. **Add contributors**: Lists all unique contributors from the PRs

### 3. What This Means

#### ✅ Stable Release Notes
- Include **only commits since the last stable release**
- **Do NOT include** beta or nightly commits that happened in between
- Example: `2.0.5` → `2.1.0` only shows stable branch commits

#### ✅ Beta Release Notes
- Include **only commits since the last beta release**
- **Do NOT include** nightly commits
- **Do NOT include** stable commits from stable branch
- Example: `2.1.0.b2` → `2.1.0.b3` only shows dev branch commits since b2

#### ✅ Nightly Release Notes
- Include **only commits since the last nightly release**
- **Do NOT include** beta or stable releases in between
- Example: `2.1.0.dev20251022` → `2.1.0.dev20251023` only shows dev branch commits since yesterday

## Release Drafter Configuration

✅ The workflow **uses your `.github/release-drafter.yml` configuration** for:
- Category definitions (labels → section headers)
- Category titles and emoji
- Excluded contributors (bots)
- PR title format

The workflow manually implements the categorization logic to ensure channel-specific commit ranges while preserving your custom formatting.

## Example Release Notes Format

```markdown
## 📦 Beta Release

_Changes since [2.1.0.b1](https://github.com/music-assistant/server/releases/tag/2.1.0.b1)_

### ⚠ Breaking Changes

- Major API refactoring (by @contributor1 in #123)

### 🚀 Features and enhancements

- Add new audio processor (by @contributor2 in #124)
- Improve queue management (by @contributor3 in #125)

### 🐛 Bugfixes

- Fix playback issue (by @contributor1 in #126)

### 🧰 Maintenance and dependency bumps

- Update dependencies (by @dependabot in #127)
- Improve CI pipeline (by @contributor2 in #128)

## :bow: Thanks to our contributors

Special thanks to the following contributors who helped with this release:

@contributor1, @contributor2, @contributor3
```

## Testing

To verify channel-specific release notes:

1. **Create a beta release** after a stable release:
   ```bash
   # Should only show commits on dev branch since last beta
   # Should NOT include stable branch commits
   ```

2. **Create a nightly release** after a beta release:
   ```bash
   # Should only show commits since yesterday's nightly
   # Should NOT include beta release notes
   ```

3. **Create a stable release** after multiple betas:
   ```bash
   # Should only show commits on stable branch since last stable
   # Should NOT include any beta or nightly commits
   ```

## Verification Commands

```bash
# Check what will be in next stable release
git log $(git tag | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)..stable --oneline

# Check what will be in next beta release
git log $(git tag | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.b[0-9]+$' | sort -V | tail -1)..dev --oneline

# Check what will be in next nightly release
git log $(git tag | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.dev[0-9]+$' | sort -V | tail -1)..dev --oneline
```

## Testing

To verify channel-specific release notes:

1. **Create a beta release** after a stable release:
   ```bash
   # Should only show commits on dev branch since last beta
   # Should NOT include stable branch commits
   ```

2. **Create a nightly release** after a beta release:
   ```bash
   # Should only show commits since yesterday's nightly
   # Should NOT include beta release notes
   ```

3. **Create a stable release** after multiple betas:
   ```bash
   # Should only show commits on stable branch since last stable
   # Should NOT include any beta or nightly commits
   ```

## Verification Commands

```bash
# Check what will be in next stable release
git log $(git tag | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)..stable --oneline

# Check what will be in next beta release
git log $(git tag | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.b[0-9]+$' | sort -V | tail -1)..dev --oneline

# Check what will be in next nightly release
git log $(git tag | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.dev[0-9]+$' | sort -V | tail -1)..dev --oneline
```
