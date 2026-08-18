"""Constants for the Metadata Controller."""

from __future__ import annotations

# Map of normalised imageproxy `fmt` values to standards-compliant MIME types.
# Used both to validate the client-supplied `fmt` query parameter and to set
# the Content-Type on the response.
_IMAGEPROXY_CONTENT_TYPES: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "svg": "image/svg+xml",
}

LOCALES = {
    "af_ZA": "African",
    "ar_AE": "Arabic (United Arab Emirates)",
    "ar_EG": "Arabic (Egypt)",
    "ar_SA": "Saudi Arabia",
    "bg_BG": "Bulgarian",
    "cs_CZ": "Czech",
    "zh_CN": "Chinese",
    "zh_TW": "Chinese (Traditional)",
    "hr_HR": "Croatian",
    "da_DK": "Danish",
    "de_DE": "German",
    "el_GR": "Greek",
    "en_AU": "English (AU)",
    "en_US": "English (US)",
    "en_GB": "English (UK)",
    "es_ES": "Spanish",
    "et_EE": "Estonian",
    "fi_FI": "Finnish",
    "fr_FR": "French",
    "hu_HU": "Hungarian",
    "is_IS": "Icelandic",
    "it_IT": "Italian",
    "lt_LT": "Lithuanian",
    "lv_LV": "Latvian",
    "ja_JP": "Japanese",
    "ko_KR": "Korean",
    "nl_NL": "Dutch",
    "nb_NO": "Norwegian Bokmål",
    "pl_PL": "Polish",
    "pt_PT": "Portuguese",
    "ro_RO": "Romanian",
    "ru_RU": "Russian",
    "sk_SK": "Slovak",
    "sl_SI": "Slovenian",
    "sr_RS": "Serbian",
    "sv_SE": "Swedish",
    "tr_TR": "Turkish",
    "uk_UA": "Ukrainian",
}

DEFAULT_LANGUAGE = "en_US"

# Radio stream artwork cache settings
CACHE_CATEGORY_RADIO_ARTWORK = 101

CACHE_EXPIRATION_RADIO_ARTWORK = 86400 * 90  # 90 days

CACHE_EXPIRATION_RADIO_ARTWORK_MISS = 86400 * 7  # 7 days

AD_DETECTION_PHRASES = ("asset link", "asset stop", "asset spot", "advert", "promo")

REFRESH_INTERVAL = 60 * 60 * 24 * 90  # 90 days

CONF_ENABLE_ONLINE_METADATA = "enable_online_metadata"

CONF_PREFER_LOCAL_GENRES = "prefer_local_genres"

CONF_ENABLE_RADIO_METADATA_LOOKUP = "enable_radio_metadata_lookup"

MISSING_ARTIST_METADATA_SCAN_TASK_ID = "metadata_missing_artist_metadata_scan_v2"

PLAYLIST_METADATA_SCAN_TASK_ID = "metadata_playlist_metadata_scan_v2"

THUMB_CACHE_CLEANUP_TASK_ID = "metadata_thumb_cache_cleanup_v2"

ALBUM_RECONCILIATION_TASK_ID = "metadata_album_reconciliation_v1"

METADATA_LOOKUP_TASK_ID_PREFIX = "metadata_lookup"

METADATA_SCAN_BATCH_SIZE = 5

CONF_THUMB_CACHE_MAX_SIZE = "thumb_cache_max_size"

DEFAULT_THUMB_CACHE_MAX_SIZE_MB = 500

# Image-id system: maps a sha256(provider+path) hash to the (provider, path) tuple
# so the imageproxy can be addressed by an opaque short id instead of a long
# query string carrying the raw (often URL-shaped) path. The high category
# number matches the convention used elsewhere in this controller and avoids
# collisions with providers that use low category integers under the default
# cache namespace.
CACHE_CATEGORY_IMAGE_IDS = 102

# Bounds each of the in-memory image-id maps (forward memo, reverse LRU and
# persisted markers). The maps share their key/id string objects, so the
# combined worst-case footprint stays in the single-digit MB range. Overflow is
# harmless: an evicted entry merely costs one re-hash and/or one cache-db probe
# on the next encounter.
_IMAGE_ID_LRU_MAX = 10000

# 1 year; a stored mapping is refreshed once the row burns through half its TTL
_IMAGE_ID_CACHE_TTL = 86400 * 365

# Sizes accepted by the imageproxy. 0 means "no resize". The set is small enough
# to bound PIL memory + thumbnail cache cardinality; expand if a real use case appears.
_ALLOWED_IMAGEPROXY_SIZES = frozenset({0, 80, 160, 256, 512, 1024})

# Human-readable form of the allowed sizes, used in error responses.
_ALLOWED_IMAGEPROXY_SIZES_STR = ", ".join(str(size) for size in sorted(_ALLOWED_IMAGEPROXY_SIZES))

_IMAGEPROXY_PATH_PREFIX = "/imageproxy/"
