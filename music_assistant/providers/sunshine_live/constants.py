"""Constants for the Sunshine Live provider."""

from typing import Final, TypedDict

# The last part names who links to the stream, this is the one of the station's public list.
STREAM_URL: Final[str] = "https://stream.sunshine-live.de/{path}/mp3-192/stream.sunshine-live.de/"
STREAM_BIT_RATE: Final[int] = 192
STREAM_SAMPLE_RATE: Final[int] = 44100

_IMAGES: Final[str] = "https://images.sunshine-live.de/files"


class Channel(TypedDict):
    """One channel of the station."""

    name: str
    # the name of the channel on the stream server, which is case sensitive
    path: str
    image: str


# The channels are listed by hand, as the station offers no public list of them. Their ids are
# stored in libraries, so they stay the same when a name or a stream path changes.
CHANNELS: Final[dict[str, Channel]] = {
    "sunshine_live": {
        "name": "Sunshine Live",
        "path": "live",
        "image": f"{_IMAGES}/streams/hg/sunshine-sunshine-logo_bg.png",
    },
    "sunshine_live_ch": {
        "name": "Sunshine Live CH",
        "path": "schweiz",
        "image": f"{_IMAGES}/2022-12/1080x1080.jpg",
    },
    "80s": {
        "name": "Sunshine Live 80s",
        "path": "80er",
        "image": f"{_IMAGES}/streams/hg/Die80er_bg.png",
    },
    "90s": {
        "name": "Sunshine Live 90s",
        "path": "90er",
        "image": f"{_IMAGES}/streams/hg/90er_bg.png",
    },
    "2000s": {
        "name": "Sunshine Live 2000s",
        "path": "2000er",
        "image": f"{_IMAGES}/streams/hg/2000er_bg.png",
    },
    "2010s": {
        "name": "Sunshine Live 2010s",
        "path": "2010er",
        "image": f"{_IMAGES}/2024-05/2010er.jpg",
    },
    "afro_house": {
        "name": "Sunshine Live Afro House",
        "path": "afrohouse",
        "image": f"{_IMAGES}/2024-08/afrohouse_1x1_clean.jpg",
    },
    "blue": {
        "name": "Sunshine Live Blue",
        "path": "Blue",
        "image": f"{_IMAGES}/2023-08/blue.jpg",
    },
    "bounce": {
        "name": "Sunshine Live Bounce",
        "path": "bounce",
        "image": f"{_IMAGES}/2025-12/bounce-p.jpg",
    },
    "calm_flow": {
        "name": "Sunshine Live Calm Flow by Karmaloft",
        "path": "calmflow",
        "image": f"{_IMAGES}/2025-07/calmflow_plain_1.jpg",
    },
    "chillout": {
        "name": "Sunshine Live Chillout",
        "path": "sp8",
        "image": f"{_IMAGES}/streams/hg/chillout_bg.png",
    },
    "classics": {
        "name": "Sunshine Live Classics",
        "path": "classics",
        "image": f"{_IMAGES}/streams/hg/classics_bg.png",
    },
    "drum_and_bass": {
        "name": "Sunshine Live Drum & Bass",
        "path": "dnb",
        "image": f"{_IMAGES}/streams/hg/drumnbass_bg.png",
    },
    "edm": {
        "name": "Sunshine Live EDM",
        "path": "edm",
        "image": f"{_IMAGES}/streams/hg/edm_bg.png",
    },
    "electrique_cafe": {
        "name": "Sunshine Live Électrique Café",
        "path": "electriquecafe",
        "image": f"{_IMAGES}/2024-04/electriquecafe_clean.jpg",
    },
    "eurodance": {
        "name": "Sunshine Live Eurodance",
        "path": "eurodance",
        "image": f"{_IMAGES}/streams/hg/eurodance_bg.png",
    },
    "festival": {
        "name": "Sunshine Live Festival",
        "path": "festival",
        "image": f"{_IMAGES}/streams/hg/festival_bg.png",
    },
    "hardcore": {
        "name": "Sunshine Live Hardcore",
        "path": "Hardcore",
        "image": f"{_IMAGES}/2023-08/hardcore.jpg",
    },
    "hardstyle": {
        "name": "Sunshine Live Hardstyle",
        "path": "hardstyle",
        "image": f"{_IMAGES}/2023-05/hardstyle.jpg",
    },
    "hardtechno": {
        "name": "Sunshine Live Hardtechno",
        "path": "Hardtechno",
        "image": f"{_IMAGES}/2023-08/hardtechno.jpg",
    },
    "house": {
        "name": "Sunshine Live House",
        "path": "house",
        "image": f"{_IMAGES}/streams/hg/house_bg.png",
    },
    "ibiza": {
        "name": "Sunshine Live Ibiza",
        "path": "ibiza",
        "image": f"{_IMAGES}/streams/hg/ibiza_bg.png",
    },
    "lounge": {
        "name": "Sunshine Live Lounge",
        "path": "lounge",
        "image": f"{_IMAGES}/streams/hg/lounge_bg.png",
    },
    "mayday": {
        "name": "Sunshine Live Mayday",
        "path": "mayday",
        "image": f"{_IMAGES}/streams/hg/mayday_bg.png",
    },
    "melodic_beats": {
        "name": "Sunshine Live Melodic Beats",
        "path": "MelodicB",
        "image": f"{_IMAGES}/2023-10/melodic_beats.jpg",
    },
    "mix_mission": {
        "name": "Sunshine Live Mix Mission",
        "path": "mixmission",
        "image": f"{_IMAGES}/2023-08/mixmission.jpg",
    },
    "most_wanted": {
        "name": "Sunshine Live Most Wanted",
        "path": "mostwanted",
        "image": f"{_IMAGES}/2026-10/channel_grafiken_most_wanted_ohne_text_mit_rahmen.png",
    },
    "nature_one": {
        "name": "Sunshine Live Nature One",
        "path": "natureone",
        "image": f"{_IMAGES}/streams/hg/natureone_bg.png",
    },
    "schranz": {
        "name": "Sunshine Live Schranz",
        "path": "schranz",
        "image": f"{_IMAGES}/2025-12/schranz-p.jpg",
    },
    "techno": {
        "name": "Sunshine Live Techno",
        "path": "techno",
        "image": f"{_IMAGES}/streams/hg/techno_bg.png",
    },
    "time_warp": {
        "name": "Sunshine Live Time Warp",
        "path": "timewarp",
        "image": f"{_IMAGES}/2025-05/timewarp_1x1_hg.jpg",
    },
    "trance": {
        "name": "Sunshine Live Trance",
        "path": "trance",
        "image": f"{_IMAGES}/streams/hg/trance_bg.png",
    },
    "women_of_techno": {
        "name": "Sunshine Live Women of Techno",
        "path": "technoqueens",
        "image": f"{_IMAGES}/2023-05/femalepower.jpg",
    },
    "workout": {
        "name": "Sunshine Live Workout",
        "path": "workout",
        "image": f"{_IMAGES}/streams/hg/workout_bg.png",
    },
    "xmas_beats": {
        "name": "Sunshine Live X-mas Beats",
        "path": "xmasbeats",
        "image": f"{_IMAGES}/2024-11/xmas_1x1_ologo.png",
    },
}
