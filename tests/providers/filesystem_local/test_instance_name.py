"""Tests for the name postfix that tells Local files sources apart."""

from unittest.mock import MagicMock

import pytest

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.providers.filesystem_local import LocalFileSystemProvider

DOMAIN = "filesystem_local"
INSTANCE_ID = f"{DOMAIN}--own"


def _create_provider(base_path: str, sibling_paths: dict[str, str]) -> LocalFileSystemProvider:
    """
    Build a provider on a folder, next to other sources stored with their own folders.

    :param base_path: The folder of the provider.
    :param sibling_paths: The folder of each other source, keyed by instance id.
    """
    providers = {
        INSTANCE_ID: {"domain": DOMAIN},
        "other_domain--x": {"domain": "other_domain"},
        **{instance_id: {"domain": DOMAIN} for instance_id in sibling_paths},
    }
    paths = {**sibling_paths, INSTANCE_ID: base_path, "other_domain--x": base_path}
    mass = MagicMock()
    mass.config.get = MagicMock(
        side_effect=lambda key, default=None: providers if key == CONF_PROVIDERS else default
    )
    mass.config.get_provider_setup_value = MagicMock(
        side_effect=lambda instance_id, _key, default=None: paths.get(instance_id, default)
    )
    config = MagicMock()
    config.instance_id = INSTANCE_ID
    config.get_value = MagicMock(side_effect=lambda _key, default=None: default)
    manifest = MagicMock()
    manifest.domain = DOMAIN
    return LocalFileSystemProvider(mass, manifest, config, base_path=base_path)


@pytest.mark.parametrize(
    ("base_path", "sibling_paths", "postfix"),
    [
        ("/media/music", {}, "music"),
        ("/media/music", {"a": "/media/audiobooks"}, "music"),
        ("/media/nas/music", {"a": "/media/usb/music"}, "nas/music"),
        ("/data/nas/music", {"a": "/media/nas/music", "b": "/media/usb"}, "data/nas/music"),
        ("/music", {"a": "/media/music"}, "/music"),
    ],
)
def test_postfix_is_the_shortest_unique_tail(
    base_path: str, sibling_paths: dict[str, str], postfix: str
) -> None:
    """The postfix is the folder name, with as many parent folders as needed to be unique."""
    provider = _create_provider(base_path, sibling_paths)
    assert provider.instance_name_postfix == postfix
