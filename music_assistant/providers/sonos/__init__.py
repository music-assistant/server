"""Player provider for Sonos speakers."""

from .sonos import SonosProvider


async def async_setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = SonosProvider()
    await mass.async_register_provider(prov)
