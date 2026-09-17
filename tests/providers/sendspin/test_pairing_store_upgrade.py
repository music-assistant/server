"""Tests that a pairing store written before the pair-method rename still loads."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aiosendspin.models.types import PairMethod
from aiosendspin.noise.keys import b64url_encode
from aiosendspin.noise.trust_store import FileServerPairingStore

if TYPE_CHECKING:
    from pathlib import Path

# Shape written by aiosendspin 9.1.1, before the pair methods were renamed to the
# `*_pairing_code` names. A user upgrading from a released Music Assistant holds this.
_LEGACY_STORE = {
    "records": {
        "client-pin": {
            "psk_id": "psk-pin",
            "psk": b64url_encode(bytes(range(32))),
            "client_id": "client-pin",
            "pair_methods": ["dynamic_pin"],
            "created_at": "2026-01-01T00:00:00+00:00",
            "owner": None,
        },
        "client-static": {
            "psk_id": "psk-static",
            "psk": b64url_encode(bytes(range(1, 33))),
            "client_id": "client-static",
            "pair_methods": ["static_pin"],
            "created_at": "2026-01-02T00:00:00+00:00",
            "owner": "user-1",
        },
    },
    "staged_pairing_psks": {},
    "trusted_unpaired_clients": {},
}


def _write_legacy_store(path: Path) -> None:
    """Write the 9.1.1-shaped pairing store to ``path``."""
    path.write_text(json.dumps(_LEGACY_STORE), encoding="utf-8")


async def test_legacy_pair_methods_load_under_the_new_names(tmp_path: Path) -> None:
    """A store holding the pre-rename method names opens and maps onto the new ones."""
    store_path = tmp_path / "pairing_store.json"
    _write_legacy_store(store_path)

    store = await FileServerPairingStore.open(store_path)

    pin_record = await store.record_by_client_id("client-pin")
    assert pin_record is not None
    assert pin_record.pair_methods == [PairMethod.DYNAMIC_PAIRING_CODE]
    static_record = await store.record_by_client_id("client-static")
    assert static_record is not None
    assert static_record.pair_methods == [PairMethod.STATIC_PAIRING_CODE]
    # the rest of the record has to survive the mapping untouched
    assert static_record.owner == "user-1"
    assert static_record.psk_id == "psk-static"


async def test_legacy_store_is_rewritten_with_the_new_names(tmp_path: Path) -> None:
    """Once the upgraded store saves, the old names are gone from the file."""
    store_path = tmp_path / "pairing_store.json"
    _write_legacy_store(store_path)
    store = await FileServerPairingStore.open(store_path)

    await store.remove_record("client-static")

    saved = json.loads(store_path.read_text(encoding="utf-8"))
    methods = saved["records"]["client-pin"]["pair_methods"]
    assert methods == [PairMethod.DYNAMIC_PAIRING_CODE.value]
