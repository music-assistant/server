# ruff: noqa: RUF001
"""
TTS pronunciation hints for Alice's text-to-speech.

Yandex Alice's TTS gives passable Russian pronunciation out of the box but
often mangles foreign artist / band names — "Metallica" becomes
"мэ-та-ли-ка" (Latin-letters → English-phonemes path), "Coldplay" becomes
"чол-дплай", etc. We intercept by emitting a Cyrillic transliteration with
a `+` stress marker into ``response.tts`` while keeping ``response.text``
clean (the user reads the original; Alice speaks the cleaned-up form).

Two tables:

* ``WORD_REPLACEMENTS`` — single-word, applied via the per-word regex in
  ``dialogs._tts_for``. Includes both Russian response words (stress
  hints) and foreign single-word artist names (Cyrillic transliteration).
* ``PHRASE_REPLACEMENTS`` — multi-word, applied via whole-string
  substitution before the word regex. Required because the per-word
  regex can't match phrases like "Iron Maiden" or "Pink Floyd".

Entries are LOWERCASE keys; case is restored at substitution time.
Add via PR — opportunistically, when a real-user log shows Alice
mangling a name. Don't bulk-import — every entry is config debt.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Single-word replacements
# ---------------------------------------------------------------------------

WORD_REPLACEMENTS: dict[str, str] = {
    # ----- Russian response words (P0.2 — stress hints) -----
    "включаю": "включ+аю",
    "ставлю": "ст+авлю",
    "пауза": "п+ауза",
    "продолжаю": "продолж+аю",
    "следующая": "сл+едующая",
    "предыдущая": "пред+ыдущая",
    "громче": "гр+омче",
    "тише": "т+ише",
    "громкость": "гр+омкость",
    "колонке": "кол+онке",
    "колонку": "кол+онку",
    # ----- Foreign artists (single word) -----
    # Add here when a real log shows mispronunciation. Order: alpha by key.
    "abba": "+абба",
    "adele": "ад+эль",
    "aerosmith": "+аэросмит",
    "beatles": "б+итлз",
    "beyonce": "бэй+онсэ",
    "blur": "блёр",
    "coldplay": "к+олдплей",
    "depeche": "деп+еш",
    "drake": "дрейк",
    "eminem": "эмин+эм",
    "evanescence": "иванэсс+энс",
    "gorillaz": "горил+ас",
    "imagine": "имадж+ин",
    "kiss": "кисс",
    "madonna": "мад+онна",
    "metallica": "мет+аллика",
    "muse": "мьюз",
    "nirvana": "нирв+ана",
    "oasis": "о+азис",
    "queen": "квин",
    "radiohead": "р+адиохед",
    "rammstein": "р+амштайн",
    "rihanna": "рих+анна",
    "scorpions": "ск+орпионс",
    "skillet": "ск+иллет",
    "sting": "стинг",  # codespell:ignore sting
}


# ---------------------------------------------------------------------------
# Multi-word phrase replacements (applied before per-word substitution)
# ---------------------------------------------------------------------------

PHRASE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    # Order: longer phrases first to avoid sub-string clashes
    # (e.g. "red hot chili peppers" must beat any "red"-prefixed entry).
    ("red hot chili peppers", "ред хот ч+или п+эпперс"),
    ("imagine dragons", "имадж+ин др+агонс"),
    ("arctic monkeys", "+арктик м+анкис"),
    ("billie eilish", "б+илли +айлиш"),
    ("black sabbath", "блэк с+аббат"),
    ("foo fighters", "фу ф+айтерс"),
    ("guns n roses", "ганз эн р+оузес"),
    ("iron maiden", "+айрон м+эйден"),
    ("lady gaga", "л+эди г+ага"),
    ("led zeppelin", "лед цеппел+ин"),
    ("linkin park", "л+инкин парк"),
    ("pink floyd", "пинк фл+ойд"),
    ("bruno mars", "бр+уно марс"),
    ("daft punk", "дафт панк"),
    ("ed sheeran", "эд ш+иран"),
    ("taylor swift", "т+эйлор свифт"),
)
