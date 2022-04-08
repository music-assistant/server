Music Assistant
==================================

.. image:: https://img.shields.io/pypi/v/music_assistant.svg
        :target: https://pypi.python.org/pypi/music_assistant


**Music Assistant**


Python library for managing online and offline music sources and orchestrate playback to devices.

Features:

- Supports multiple music sources through a provider implementation.
- Currently implemented music providers are Spotify, Qobuz, Tune-In Radio and local filesystem
- More music providers can be easily added.
- Auto matches music on different providers (track linking)
- Fetches metadata for extended artist information
- Keeps track of the entire musis library in a compact database
- Provides all the plumbing to actually playback music to players.


This library has been created mainly for the Home Assistant integration but can be used stand-alone as well in other projects.
See the examples folder for some simple code samples or have a look at the [Home Assistant Integration](https://github.com/music-assistant/hass-music-assistant) for discovering the full potential.
