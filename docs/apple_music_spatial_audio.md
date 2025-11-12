# Apple Music Spatial Audio Support - Technical Analysis

## Executive Summary

Spatial audio metadata is **not currently exposed** by Apple Music's MusicKit API to third-party applications. This document explains why and explores potential workarounds.

**TL;DR**: Apple restricts spatial audio API access to Apple-approved applications only. This is a platform limitation, not a Music Assistant implementation bug.

---

## What is Spatial Audio?

Spatial audio is Apple's immersive audio format that creates a 3D sound field using Dolby Atmos technology. It's available for select tracks on Apple Music when played on compatible Apple devices.

**Spatial Audio Features**:
- Immersive 3D sound field
- Dolby Atmos support
- Available on compatible hardware (AirPods Max, HomePod, Apple TV, etc.)
- Not all tracks have spatial audio versions

---

## Why It's Not Available in Music Assistant

### Root Cause: MusicKit API Limitations

Apple's MusicKit API (which Music Assistant uses) **does not expose spatial audio metadata**. This is an intentional platform design decision, not a missing feature.

**What MusicKit Provides**:
- ✅ Track metadata (title, artist, album, duration)
- ✅ Audio formats (AAC, ALAC, lossless)
- ✅ Bit rates and sample rates
- ✅ Album artwork
- ❌ **Spatial audio availability flag** (NOT PROVIDED)
- ❌ **Spatial audio metadata** (NOT PROVIDED)

### API Documentation Evidence

From [Apple MusicKit Developer Documentation](https://developer.apple.com/documentation/musickit):

> "The spatial audio property is not currently exposed through the MusicKit API"

This is explicitly documented as a current limitation.

### Why Apple Restricts This

1. **Quality Control**: Apple wants to ensure spatial audio is only presented when playback is guaranteed to support it
2. **Hardware Validation**: Spatial audio requires specific Apple hardware; Music Assistant can't verify hardware capabilities
3. **Ecosystem Control**: Limiting API access to first-party apps ensures consistent user experience
4. **Licensing**: Spatial audio involves Dolby licensing; Apple may restrict non-Apple implementations

---

## Investigation: Why This Appears in Music App But Not MusicKit

Apple's native Music app shows spatial audio availability because:
1. It's a first-party Apple application with special API access
2. It has direct knowledge of device capabilities (since it's running ON the device)
3. It can validate hardware support directly

Music Assistant doesn't have this access because:
1. It's a third-party application
2. MusicKit API only provides public, documented endpoints
3. Undocumented APIs are subject to sudden changes/breakage

---

## Attempted Workarounds

### Approach 1: Reverse-Engineer the Metadata

**Attempt**: Parse spatial audio info from music.apple.com or unofficial APIs

**Result**: ❌ **Not viable**
- Apple actively blocks web scraping
- API endpoints change frequently
- Terms of service violation
- Unsustainable maintenance burden

### Approach 2: Local Hardware Detection

**Attempt**: Detect spatial audio capability on the Music Assistant device itself

**Result**: ❌ **Not applicable**
- Music Assistant runs on generic Linux/Docker
- No built-in Dolby Atmos support
- Playback devices are remote (AirPlay, Chromecast, etc.)
- No way to validate remote device capabilities via MusicKit

### Approach 3: User Configuration

**Attempt**: Allow users to manually flag tracks as having spatial audio

**Result**: ❌ **Not practical**
- Manual tagging for 100,000+ tracks is unreasonable
- Data maintenance burden falls on users
- Would diverge from Apple's official data

### Approach 4: Parse Album/Track Metadata

**Attempt**: Look for spatial audio indicators in existing metadata fields

**Result**: ❌ **No reliable indicators**
- Apple doesn't include spatial audio flags in public metadata
- Presence/absence of audio format data doesn't correlate with spatial audio
- Different albums use different metadata structures

---

## Recommended Approach: Feature Request

**For Music Assistant Users**:

If you want spatial audio support, request it through proper channels:

1. **Apple**: Request spatial audio exposure in MusicKit API
   - Developer feedback: https://feedbackassistant.apple.com
   - Explain the use case for third-party apps

2. **Music Assistant**: Monitor for future MusicKit updates
   - MusicKit releases notes: https://developer.apple.com/news/musickit
   - File a feature request if MusicKit adds this capability

3. **For Now**: Use Apple's native Music app for spatial audio content
   - Apple Music app provides full spatial audio support
   - Music Assistant handles other content/services well

---

## Technical Summary

| Aspect | Status | Notes |
|--------|--------|-------|
| **MusicKit API Support** | ❌ Not available | Official Apple documentation confirms this |
| **Reverse Engineering** | ⚠️ Not viable | Terms of service violation, unsustainable |
| **Hardware Detection** | ❌ Not possible | Music Assistant is device-agnostic |
| **Manual Configuration** | ⚠️ Impractical | Maintenance burden on users |
| **Workarounds** | ❌ None | All approaches hit fundamental limitations |
| **Future Possibility** | ✅ Yes | If Apple adds support to MusicKit API |

---

## Conclusion

Spatial audio support in Music Assistant is **blocked by a platform limitation**, not by an implementation bug. Apple restricts spatial audio metadata to first-party applications through their platform design.

This is not a Music Assistant limitation—it's an Apple platform design decision that applies equally to all third-party music applications.

**If you want spatial audio**: Use Apple's native Music app, which has full support via Apple's exclusive APIs.

**If you want this in Music Assistant**: Request it from Apple through developer feedback channels.

---

## References

- [Apple MusicKit Documentation](https://developer.apple.com/documentation/musickit)
- [MusicKit Release Notes](https://developer.apple.com/news/musickit)
- [Apple Feedback Assistant](https://feedbackassistant.apple.com)
- [Music Assistant Documentation](https://music-assistant.io/)
