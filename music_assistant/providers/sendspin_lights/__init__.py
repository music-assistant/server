"""
Shared base for light plugins that follow playback through Sendspin.

Not a loadable provider itself (no manifest.json). base.py holds the SendspinLightBridge
class that concrete light plugins subclass; analyzer.py turns the visualizer stream (beats,
peaks, spectrum, album colors) into one color per light channel, with the light-agnostic
types in models.py.
"""
