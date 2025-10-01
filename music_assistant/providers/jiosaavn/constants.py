"""Constants for JioSaavn Music Provider."""

from typing import Final

BASE_URL: Final[str] = "https://www.jiosaavn.com/api.php"

# API Endpoints
SEARCH_ENDPOINT: Final[str] = "search.getResults"
SONG_DETAILS_ENDPOINT: Final[str] = "song.getDetails"
ALBUM_DETAILS_ENDPOINT: Final[str] = "content.getAlbumDetails"
ARTIST_DETAILS_ENDPOINT: Final[str] = "artist.getArtistPageDetails"
PLAYLIST_DETAILS_ENDPOINT: Final[str] = "playlist.getDetails"

DEFAULT_HEADERS: Final[dict[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}
