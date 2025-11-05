# Custom Release Notes Generator

## Overview

We've replaced Release Drafter with a custom GitHub Action that generates release notes with full control over commit ranges and PR categorization.

## Why Custom Solution?

Release Drafter's `filter-by-commitish` feature proved unreliable:
- Inconsistent handling of branch references (`dev` vs `refs/heads/dev`)
- No way to explicitly specify the previous release tag
- Would sometimes include entire git history instead of just changes since last release
- Caused "body too long" errors when it tried to include all commits

## How It Works

### 1. Custom Action: `.github/actions/generate-release-notes/`

This is a complete, self-contained GitHub Action that handles everything:

**Inputs:**
- `version`: The version being released
- `previous-tag`: The previous release tag to compare against (optional)
- `branch`: The branch being released from
- `channel`: Release channel (stable/beta/nightly)
- `github-token`: GitHub token for API access

**Outputs:**
- `release-notes`: Complete release notes including server changes, frontend changes, and merged contributors

**What it does internally:**
1. Generates base release notes from server PRs
2. Extracts frontend changes from frontend update PRs
3. Merges contributors from both server and frontend
4. Returns complete, formatted release notes ready to publish

### 2. Python Script: `generate_notes.py`

The script:
1. **Loads configuration** from `.github/release-notes-config.yml`
2. **Fetches PRs** between the previous tag and current branch HEAD using GitHub API
3. **Categorizes PRs** based on labels:
   - ⚠ Breaking Changes
   - 🚀 New Providers
   - 🚀 Features and enhancements
   - 🐛 Bugfixes
   - 🧰 Maintenance and dependency bumps
4. **Extracts contributors** excluding bots
5. **Formats notes** using templates from config

### 3. Workflow Integration

The release workflow is now incredibly simple:
1. **Detects previous tag** using channel-specific patterns
2. **Calls the action** with one step - that's it!
3. **Creates GitHub release** with the complete notes from the action

All the complexity (server PRs, frontend PRs, contributor merging) is handled inside the reusable action.

## Benefits

✅ **Full control** - We explicitly specify which tag to compare against
✅ **Reliable** - No mysterious "entire history" issues
✅ **Consistent** - Uses same config format as Release Drafter
✅ **Faster** - Only fetches the PRs we need
✅ **Maintainable** - Clear Python code instead of black-box action
✅ **Flexible** - Easy to customize formatting or add features

## Configuration

The generator reads `.github/release-notes-config.yml`:

```yaml
change-template: '- $TITLE (by @$AUTHOR in #$NUMBER)'

exclude-contributors:
  - dependabot
  - dependabot[bot]
  # ... more bots

categories:
  - title: "⚠ Breaking Changes"
    labels:
      - 'breaking-change'
  - title: "🚀 Features and enhancements"
    labels:
      - 'feature'
      - 'enhancement'
  # ... more categories

template: |
  $CHANGES

  ## :bow: Thanks to our contributors

  Special thanks to the following contributors who helped with this release:

  $CONTRIBUTORS
```

## Example Output

```markdown
## 📦 Nightly Release

_Changes since [2.7.0.dev20251022](https://github.com/music-assistant/server/releases/tag/2.7.0.dev20251022)_

### 🚀 Features and enhancements

- Add new audio processor (by @contributor1 in #123)
- Improve queue management (by @contributor2 in #124)

### 🐛 Bugfixes

- Fix playback issue (by @contributor1 in #125)

## 🎨 Frontend Changes

- Add dark mode toggle
- Improve mobile layout
- Fix typo in settings

## :bow: Thanks to our contributors

Special thanks to the following contributors who helped with this release:

@contributor1, @contributor2, @frontend-contributor
```

## Testing

To test locally:
```bash
cd .github/actions/generate-release-notes

# Set environment variables
export GITHUB_TOKEN="your_token"
export VERSION="2.7.0.dev20251024"
export PREVIOUS_TAG="2.7.0.dev20251023"
export BRANCH="dev"
export CHANNEL="nightly"
export GITHUB_REPOSITORY="music-assistant/server"

# Run the script
python3 generate_notes.py
```

## Maintenance

To modify release notes formatting:
1. Edit `.github/release-notes-config.yml` to change categories, labels, or templates
2. Edit `generate_notes.py` if you need to change the generation logic
3. No changes needed to the main workflow unless adding new features

## Configuration File Format

The configuration file (`.github/release-notes-config.yml`) uses the same format as Release Drafter used to:
- ✅ Same configuration structure
- ✅ Same output formatting
- ✅ Same contributor exclusion logic
- ✅ Same label-based categorization

But now we have **full control** over the commit range and complete visibility into the generation process.
