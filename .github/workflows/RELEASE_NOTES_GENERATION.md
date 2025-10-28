# Release Notes Generation - Channel-Specific Behavior

## Overview

The release workflow generates release notes **specific to each channel** by leveraging Release Drafter's `filter-by-commitish` feature. This ensures that:

- **Stable** releases only show commits from the `stable` branch
- **Beta** releases only show commits from the `dev` branch since the last beta
- **Nightly** releases only show commits from the `dev` branch since the last nightly

The workflow uses the **release notes configuration** (`.github/release-notes-config.yml`) for label-based categorization and formatting.

## How It Works

### 1. Filter by Branch (commitish)

The `.github/release-notes-config.yml` file includes:

```yaml
filter-by-commitish: true
```

This tells Release Drafter to only consider releases that have the same `target_commitish` (branch) when calculating the commit range. Combined with setting the `commitish` parameter to the appropriate branch:

- **Stable releases**: Use `commitish: stable` → Only sees releases created from `stable` branch
- **Beta releases**: Use `commitish: dev` → Only sees releases created from `dev` branch
- **Nightly releases**: Use `commitish: dev` → Only sees releases created from `dev` branch

### 2. Previous Release Detection

The workflow also manually identifies the previous release for context headers using tag patterns:

#### Stable Channel
- **Pattern**: `^[0-9]+\.[0-9]+\.[0-9]+$` (e.g., `2.1.0`, `2.0.5`)
- **Branch**: `stable`
- **Finds**: Latest stable release (no suffix)

#### Beta Channel
- **Pattern**: `^[0-9]+\.[0-9]+\.[0-9]+b[0-9]+$` (e.g., `2.1.0b1`, `2.1.0b2`)
- **Branch**: `dev`
- **Finds**: Latest beta release (`bN` suffix)

#### Nightly Channel
- **Pattern**: `^[0-9]+\.[0-9]+\.[0-9]+\.dev[0-9]+$` (e.g., `2.1.0.dev20251023`)
- **Branch**: `dev`
- **Finds**: Latest nightly release (`.devYYYYMMDD` suffix)

### 3. Release Notes Generation

Release Drafter automatically:

1. **Finds commit range**: Determines commits between the previous release (same branch) and HEAD
2. **Extracts PRs**: Identifies all merged pull requests in that range
3. **Categorizes by labels**: Applies the category rules from `.github/release-notes-config.yml`:
   - ⚠ Breaking Changes (`breaking-change` label)
   - 🚀 New Providers (`new-provider` label)
   - 🚀 Features and enhancements (`feature`, `enhancement`, `new-feature` labels)
   - 🐛 Bugfixes (`bugfix` label)
   - 🧰 Maintenance (`ci`, `documentation`, `maintenance`, `dependencies` labels)
4. **Lists contributors**: Adds all unique contributors from the PRs

The workflow then enhances these notes by:
- Adding a context header showing the previous release
- Extracting and appending frontend changes from frontend update PRs
- Merging and deduplicating contributors from both server and frontend

### 4. What This Means

#### ✅ Stable Release Notes
- Include **only commits since the last stable release**
- **Do NOT include** beta or nightly commits that happened in between
- Example: `2.0.5` → `2.1.0` only shows stable branch commits

#### ✅ Beta Release Notes
- Include **only commits since the last beta release**
- **Do NOT include** nightly commits
- **Do NOT include** stable commits from stable branch
- Example: `2.1.0b2` → `2.1.0b3` only shows dev branch commits since b2

#### ✅ Nightly Release Notes
- Include **only commits since the last nightly release**
- **Do NOT include** beta or stable releases in between
- Example: `2.1.0.dev20251022` → `2.1.0.dev20251023` only shows dev branch commits since yesterday

## Release Notes Configuration

✅ The workflow uses a **custom release notes generator** that reads `.github/release-notes-config.yml`:

```yaml
# .github/release-notes-config.yml
change-template: '- $TITLE (by @$AUTHOR in #$NUMBER)'

categories:
  - title: "⚠ Breaking Changes"
    labels: ['breaking-change']
  # ... more categories
```

This approach ensures:
- **Full control over commit range** (explicit previous tag parameter)
- **No mysterious failures** (clear Python code you can debug)
- **Consistent formatting** (same config format as Release Drafter used)
- **Branch-based separation** (stable vs dev commits via explicit tag comparison)

The configuration includes:
- Category definitions (labels → section headers)
- Category titles and emoji
- Excluded contributors (bots)
- PR title format
- Collapse settings for long categories

## Example Release Notes Format

```markdown
## 📦 Beta Release

_Changes since [2.1.0b1](https://github.com/music-assistant/server/releases/tag/2.1.0b1)_

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
