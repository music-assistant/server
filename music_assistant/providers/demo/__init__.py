"""Demo/test providers."""

from .demo_playerprovider import DemoPlayerProvider


async def async_setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = DemoPlayerProvider()
    await mass.async_register_provider(prov)
