Music Assistant
==================================

[![pypi_badge](https://img.shields.io/pypi/v/music_assistant.svg)](https://pypi.python.org/pypi/music_assistant)

**Music Assistant Server**


Music Assistant is a free, opensource Media library manager that connects to your streaming services and a wide range of connected speakers. The server is the beating heart, the core of Music Assistant and must run on an always-on device like a Raspberry Pi, a NAS or an Intel NUC or alike.



**Documentation and support**

For issues, please go to [the issue tracker](https://github.com/music-assistant/hass-music-assistant/issues).

For feature requests, please see [feature requests](https://github.com/music-assistant/hass-music-assistant/discussions/categories/feature-requests-and-ideas).

____________


## Running the server

Music Assistant can be operated as a complete standalone product but it is actually tailored to use side by side with Home Assistant, it is meant with automation in mind, hence our recommended installation method is to run the server as Home assistant Add-on.


### Preferred method: Home Assistant Add-on

By far the most convenient way to run the Music Assistant Server is to install the Music Assistant Add-on:

1. Add the Music Assistant repository to your Home Assistant instance.
2. Install the Music Assistant add-on.

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmusic-assistant%2Fhome-assistant-addon)


### Alternative method: Docker image

An alternative way to run the Music Assistant server is by running the docker image:

```
docker run --network host --privileged -v <dir>:/data ghcr.io/music-assistant/server
```

You must run the docker container with host network mode. The data volume is `/data` - replace `<dir>` with a writable directory to ensure the data volume persists between updates.
If you want access to your local music files from within MA, make sure to also mount that, e.g. /media.
Note that accessing remote (SMB) shares can be done from within MA itself using the SMB File provider (but requires the privileged flag).

____________

### Notes:

- Because the server heavily relies on multicast techniques like mDNS and uPNP to discover players in your network it MUST be run in the same Layer 2 network as your player devices.

- The server itself hosts a very simple webserver to stream audio to devices. This webinterface must be run at HTTP (so no HTTPS) and accessible by IP-address from local players. See the server's logging at startup if it correctly auto-detected the local IP.

- The server itself hosts a websocket API and a JSON RPC API which is more or less compatible with LMS. The Music Assistant fronted communicates with the server using the websockets API.

- The webinterface of the server can be reached on the tcp port 8095 by default (unless that port is occupied, then it picks the next available port). See the server's logging to confirm the port.


## Support, documentation
Because this project originated out of a Home Assistant integration, we maintain all our documentatation and enduser support in a separate reposity:
https://github.com/music-assistant/hass-music-assistant


[repository-badge]: https://img.shields.io/badge/Add%20repository%20to%20my-Home%20Assistant-41BDF5?logo=home-assistant&style=for-the-badge
[repository-url]: https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmusic-assistant%2Fhome-assistant-addon
