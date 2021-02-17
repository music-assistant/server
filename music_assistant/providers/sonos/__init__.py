"""Player provider for Sonos speakers."""

from .sonos import SonosProvider


async def setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = SonosProvider()
    await mass.register_provider(prov)
