Music Assistant
==================================

**Music Assistant Server**

Music Assistant is a free, opensource Media library manager that connects to your streaming services and a wide range of connected speakers. The server is the beating heart, the core of Music Assistant and must run on an always-on device like a Raspberry Pi, a NAS or an Intel NUC or alike.

**Documentation and support**

Documentation https://music-assistant.io

Beta Documentation https://beta.music-assistant.io

For issues, please go to [the issue tracker](https://github.com/music-assistant/support/issues).

For feature requests, please see [feature requests](https://github.com/music-assistant/support/discussions/categories/feature-requests-and-ideas).

____________


## Running the server

Music Assistant can be operated as a complete standalone product but it is actually tailored to use side by side with Home Assistant, it is meant with automation in mind, hence our recommended installation method is to run the server as a Home assistant Add-on.


### Installation Instructions

See here https://music-assistant.io/installation/

[repository-badge]: https://img.shields.io/badge/Add%20repository%20to%20my-Home%20Assistant-41BDF5?logo=home-assistant&style=for-the-badge
[repository-url]: https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmusic-assistant%2Fhome-assistant-addon

Note that although Music Assistant's main code is written in python, it has multiple dependencies on external/OS components such as ffmpeg and custom binaries and it is therefore not possible to run it as standalone pypi package. The only available installation method to run the Music Assistant server is by running the Docker container or the Home Assistant add-on.

---

[![A project from the Open Home Foundation](https://www.openhomefoundation.org/badges/ohf-project.png)](https://www.openhomefoundation.org/)
