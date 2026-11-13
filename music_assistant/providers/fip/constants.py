"""Constants for the FIP provider."""

from typing import TypedDict

import aiohttp

# Radio France's now-playing service, the one behind the players on radiofrance.fr.
LIVE_URL = "https://api.radiofrance.fr/livemeta/live/{api_id}/webrf_webradio_player"

# Radio France's older now-playing service. It names a song sooner but has skipped one now and
# then, so it only stands in while the other answers with its placeholder.
PULL_URL = "https://api.radiofrance.fr/livemeta/pull/{api_id}"

# Radio France's Icecast streams, AAC at 192 kbit/s.
STREAM_URL = "https://icecast.radiofrance.fr/{slug}-hifi.aac"

# Radio France's image service. An image is only served in one of its fixed sizes: a list
# entry takes a small one, a player showing what plays now a large one.
IMAGE_URL = "https://www.radiofrance.fr/pikapi/images/{image_id}/400x400"
NOW_PLAYING_IMAGE_URL = "https://www.radiofrance.fr/pikapi/images/{image_id}/1000x1000"

# Seconds the audio trails the service's clock: the service times a song by the studio,
# while listeners hear it after the stream's own delay plus the player's buffer.
STREAM_DELAY = 8

# How often (seconds) the player queue asks for now-playing data while a station plays.
STREAM_METADATA_UPDATE_INTERVAL = 10

# How often (seconds) to ask while the song on display should already be over: the
# service names a new song 20 to 40 seconds after it started.
OVERDUE_UPDATE_INTERVAL = 3

# An entry on display counts as on air until this long (seconds) after its end: until
# then neither the service's placeholder nor an entry that started earlier replaces it.
ON_AIR_GRACE = 60

# Per-request timeout for the now-playing service.
API_TIMEOUT = aiohttp.ClientTimeout(total=5)


class FipStation(TypedDict):
    """Type definition for a FIP station."""

    api_id: int
    name: str
    description: str
    image_id: str


# The station id doubles as the Icecast mount without its underscores ("fip_rock" plays
# "fiprock"). The descriptions are the stations' own, as the service gives them.
FIP_STATIONS: dict[str, FipStation] = {
    "fip": {
        "api_id": 7,
        "name": "FIP",
        "description": "La radio la plus éclectique du monde",
        "image_id": "7eee98cb-3f59-4a3b-b921-6a4be85af542",
    },
    "fip_rock": {
        "api_id": 64,
        "name": "FIP Rock",
        "description": (
            "De Bowie à Radiohead, de Lou Reed à Miossec, "
            "notre mix rock des genres et des générations"
        ),
        "image_id": "cda37279-f985-4017-bc03-2dad643c7306",
    },
    "fip_jazz": {
        "api_id": 65,
        "name": "FIP Jazz",
        "description": (
            "Bebop, afro jazz, salsa, bossa, blues, tout le jazz, ses cousins et ses cousines"
        ),
        "image_id": "2de59c50-9912-401e-9d1f-95a2f0b66567",
    },
    "fip_groove": {
        "api_id": 66,
        "name": "FIP Groove",
        "description": (
            "Gardez le rythme avec notre sélection dance ou Motown, funk, trip-hop, R'n'B et disco"
        ),
        "image_id": "95cf76c3-26bc-404e-be1c-b81666188c3f",
    },
    "fip_world": {
        "api_id": 69,
        "name": "FIP Monde",
        "description": (
            "Un tour du monde en quelques heures, une épopée musicale qui traverse les continents"
        ),
        "image_id": "524fabe1-7645-4847-aa93-932d9835de67",
    },
    "fip_nouveautes": {
        "api_id": 70,
        "name": "FIP Nouveautés",
        "description": (
            "On ne les entend que sur FIP mais demain… "  # codespell:ignore entend
            "Toutes nos découvertes sont ici"
        ),
        "image_id": "9f85ac9d-1151-429a-9645-d030e92b441f",
    },
    "fip_reggae": {
        "api_id": 71,
        "name": "FIP Reggae",
        "description": "Des Toots à Biga Ranx, des Clash à DJ Vadim, un voyage vers Zion et au-delà",
        "image_id": "a242c7c5-bf1e-468b-9cb9-2154550f8be7",
    },
    "fip_electro": {
        "api_id": 74,
        "name": "FIP Electro",
        "description": (
            "De Air à Soulwax, de Superpoze à Tosca, "
            "gardez le kick avec notre sélection électronique"
        ),
        "image_id": "bf85964f-395a-4e6a-beb4-6cc0590d20bb",
    },
    "fip_metal": {
        "api_id": 77,
        "name": "FIP Metal",
        "description": (
            "Le mix Metal éclectique de Black Sabbath à Deafheaven, de Gojira à Rammstein"
        ),
        "image_id": "a3406ba2-41d4-4a94-89a2-c45470d606ff",
    },
    "fip_pop": {
        "api_id": 78,
        "name": "FIP Pop",
        "description": "De l'indie-rock à la synth-wave, de la brit-pop à la scène française",
        "image_id": "867b3aac-d06c-4f79-85a3-fc1565526889",
    },
    "fip_hiphop": {
        "api_id": 95,
        "name": "FIP Hip-Hop",
        "description": (
            "Une programmation qui explore le passé, le présent et le futur du hip-hop, "
            "des Last Poets à Little Simz, de Nujabes à Alpha Wann"
        ),
        "image_id": "5c52d959-7ea2-4c9e-b298-5aab893a9b86",
    },
    "fip_sacre_francais": {
        "api_id": 96,
        "name": "FIP Sacré français !",
        "description": "La webradio préférée des Francophiles et Anglophobes !",
        "image_id": "1641a25c-e8c8-49f8-b5e1-cffd8b722231",
    },
    "fip_cultes": {
        "api_id": 709,
        "name": "FIP Cultes",
        "description": "Une webradio entièrement consacrée aux titres devenus intemporels",
        "image_id": "d16aab34-0e5c-439f-a364-31f7e0ac4abb",
    },
}
