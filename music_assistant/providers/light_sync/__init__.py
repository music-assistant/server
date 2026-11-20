"""
Shared base for the light sync plugin providers (Hue Lights Sync, ...).

Not a loadable provider itself (no manifest.json). `analyzer.AudioAnalyzer` turns the
Sendspin visualizer stream (beats, peaks, spectrum, album colors) into one color per
light channel; each provider only sends those colors to its own lights.
"""
