# Provider Synchronization System

## Overview

This document describes the automated synchronization system that keeps the **Kion Music** provider in sync with the **Yandex Music** provider.

## Why This System Exists

Kion Music and Yandex Music are closely related providers:
- Both use the same `yandex-music==2.2.0` Python library
- Kion is essentially Yandex Music for MTS's KION service (different API endpoint)
- Features added to Yandex Music should propagate to Kion Music

Without automation, maintaining feature parity requires:
- Manual code copying
- Careful search-and-replace operations
- Risk of missing changes or introducing bugs

This system automates the synchronization while preserving Kion-specific customizations.

## Architecture

### Components

1. **Sync Configuration** (`.github/sync-config.yml`)
   - Defines transformation rules
   - Lists files to sync
   - Specifies file-specific customizations

2. **Sync Script** (`scripts/sync_kion_from_yandex.py`)
   - Reads Yandex Music provider files
   - Applies transformations
   - Writes to Kion Music provider

3. **GitHub Actions Workflow** (`.github/workflows/sync-kion-from-yandex.yml`)
   - Triggers on Yandex Music changes
   - Runs sync script
   - Runs tests
   - Creates Pull Requests

4. **Tests**
   - Unit tests: `scripts/test_sync.py`
   - Integration tests: `tests/providers/kion_music/test_sync_integrity.py`

### How It Works

```
Yandex Music files
       ↓
   Sync Script (applies transformations)
       ↓
   Kion Music files
       ↓
   Run Tests
       ↓
   Create PR (if tests pass)
```

## Transformations

### Global Transformations

Applied to all files:

| From | To | Reason |
|------|-----|--------|
| `YandexMusicProvider` | `KionMusicProvider` | Class name |
| `YandexMusicClient` | `KionMusicClient` | Class name |
| `YandexMusicStreamingManager` | `KionMusicStreamingManager` | Class name |
| `Yandex Music service` | `KION Music (MTS) service` | Branding |
| `Yandex Music provider` | `KION Music provider` | Branding |
| `Yandex Music` | `KION Music` | Branding |
| `My Wave` | `My Mix` | Feature branding |
| `Моя волна` | `Мой Микс` | Feature branding (Russian) |
| `my_wave` | `my_mix` | Variable/constant names |
| `MY_WAVE` | `MY_MIX` | Constant names |

### File-Specific Transformations

**`api_client.py`:**
- Preserves KION API endpoint: `music.mts.ru/ya_api` (vs Yandex's `api.music.yandex.net`)

**`constants.py`:**
- Renames My Wave constants to My Mix equivalents

**`__init__.py`:**
- Marks experimental features with `default_value=False`
- Adds experimental warnings to feature descriptions

**`manifest.json`:**
- Updates domain: `kion_music`
- Updates name: `KION Music`
- Updates description and documentation URLs

### What's NOT Transformed

**Library imports are preserved:**
```python
from yandex_music import Client, Track
```

Both providers use the same `yandex_music` library, so imports remain unchanged.

## Usage

### Manual Sync

```bash
# Dry run (see what would change without writing)
python scripts/sync_kion_from_yandex.py --dry-run

# Actually sync files
python scripts/sync_kion_from_yandex.py

# Verbose output
python scripts/sync_kion_from_yandex.py --verbose
```

### Automatic Sync (CI/CD)

The sync runs automatically when:
- Changes are pushed to `integration/pending-upstream-prs` branch
- Changes affect `music_assistant/providers/yandex_music/**`

The workflow:
1. Detects Yandex Music changes
2. Runs sync script
3. Runs Kion tests
4. Creates PR to upstream (`music-assistant/server`)

## Testing

### Run Unit Tests

```bash
pytest scripts/test_sync.py -v
```

Tests cover:
- ✅ Transformation rules (class names, branding)
- ✅ My Wave → My Mix rebranding
- ✅ Experimental features disabled by default
- ✅ API endpoint preservation
- ✅ Library import preservation
- ✅ Manifest transformations

### Run Sync Validation Tests

```bash
pytest tests/providers/kion_music/test_sync_integrity.py -v
```

Tests verify:
- ✅ No Yandex branding in Kion code
- ✅ My Mix branding correct
- ✅ Experimental features disabled
- ✅ KION API endpoint preserved
- ✅ Manifest correct
- ✅ Library imports preserved

## Experimental Features

New advanced features from Yandex Music are marked as **experimental** in Kion Music:

**Features:**
- My Mix (My Wave equivalent) - AI-powered recommendations
- Rotor stations
- Similar tracks recommendations

**Implementation:**
- Disabled by default (`default_value=False`)
- Marked with ⚠️ Experimental in descriptions
- Require user opt-in to enable

**Rationale:**
- KION API compatibility uncertain
- Conservative rollout approach
- Easy to enable if proven stable

## Pull Request Workflow

### Automatic PRs

When Yandex Music changes, the workflow creates **two separate PRs** to `music-assistant/server`:

**PR #1: Kion Music Sync**
- Contains synced Kion Music changes
- Labels: `auto-sync`, `kion_music`, `experimental-features`
- Includes test results
- Review checklist provided

**PR #2: Yandex Music Updates**
- Contains direct Yandex Music changes (if any)
- Labels: `yandex_music`, `requires-review`
- Independent from Kion PR

### Review Checklist

Before merging Kion sync PRs, verify:

- [ ] Code changes correct for Kion
- [ ] API endpoint preserved (`music.mts.ru/ya_api`)
- [ ] My Wave → My Mix rebranding applied
- [ ] Experimental features disabled by default
- [ ] Tests pass
- [ ] No Kion-specific customizations lost

## Configuration

### Adding New Transformations

Edit `.github/sync-config.yml`:

```yaml
transformations:
  - pattern: 'SourcePattern'
    replacement: 'TargetPattern'
```

**Order matters!** More specific patterns must come before general ones:

```yaml
# ✅ Correct order
- pattern: 'Yandex Music service'    # Specific
  replacement: 'KION Music (MTS) service'
- pattern: 'Yandex Music'            # General
  replacement: 'KION Music'

# ❌ Wrong order (general pattern would match first)
- pattern: 'Yandex Music'
  replacement: 'KION Music'
- pattern: 'Yandex Music service'
  replacement: 'KION Music (MTS) service'
```

### File-Specific Transformations

For transformations that only apply to certain files:

```yaml
file_transformations:
  api_client.py:
    - pattern: 'OLD_CONSTANT = "value"'
      replacement: 'NEW_CONSTANT = "value"'
```

### Excluding Features

If KION API doesn't support a feature:

```yaml
exclude_features:
  - "unsupported_feature_name"
```

The sync script will skip code containing excluded keywords.

## Troubleshooting

### Sync Creates Incorrect Changes

**Check transformation order:**
```bash
python scripts/sync_kion_from_yandex.py --verbose --dry-run
```

More specific patterns must come before general ones.

### Tests Fail After Sync

**Run tests locally:**
```bash
pytest tests/providers/kion_music/ -v
```

Check if:
- API endpoint accidentally changed
- Library imports broken
- Experimental features not disabled

### Workflow Doesn't Trigger

**Verify path filter:**
```yaml
paths:
  - 'music_assistant/providers/yandex_music/**'
```

Only triggers on Yandex Music changes.

### PR Not Created

**Check workflow permissions:**
```yaml
permissions:
  contents: write
  pull-requests: write
```

Requires `GITHUB_TOKEN` with PR creation permissions.

## Maintenance

### Regular Tasks

1. **Review sync PRs** when they're created
2. **Update transformation rules** if branding changes
3. **Monitor test failures** for new edge cases
4. **Update documentation** when adding features

### When to Update Config

- **New class added**: Add to `transformations`
- **New file added**: Add to `sync_files`
- **Branding change**: Update relevant transformations
- **KION API divergence**: Add to `exclude_features`

## Statistics

**Files synced:** 7 (provider.py, api_client.py, parsers.py, streaming.py, constants.py, __init__.py, manifest.json)

**Transformations per sync:** ~44 replacements

**Test coverage:**
- 13 unit tests
- 10 integration tests

**Time saved:** ~4 hours manual → ~15 minutes automated

## Future Improvements

Potential enhancements:

1. **Conflict detection**: Warn if Kion-specific changes would be overwritten
2. **Partial sync**: Sync only changed files (not all 7)
3. **Rollback mechanism**: Easy revert if sync breaks something
4. **Metrics dashboard**: Track sync success rate, review time
5. **Smarter transformations**: Use AST parsing instead of string replacement

## Related Documentation

- [CLAUDE.md](../CLAUDE.md) - Project development guidelines
- [Yandex Music Provider](../music_assistant/providers/yandex_music/)
- [Kion Music Provider](../music_assistant/providers/kion_music/)
- [GitHub Actions Workflows](../.github/workflows/)

## Support

For issues or questions:
- Check test output: `pytest scripts/test_sync.py -v`
- Review sync logs: Check GitHub Actions workflow run
- Open issue: [GitHub Issues](https://github.com/music-assistant/server/issues)
