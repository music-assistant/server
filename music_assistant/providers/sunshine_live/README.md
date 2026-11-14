# Sunshine Live

Plays the channels of [Sunshine Live](https://www.sunshine-live.de), a German electronic music
station, as radio stations. Every channel uses the best stream the station lists for it, now 192 kbit/s MP3. The song
title of a song comes from the stream itself, as it follows the audio. During a show the stream only
names the station, so the name of the show comes from the list of the station. A cover from that list
is only used when it belongs to the song the stream names.

## The channel list

The channels, their streams and their images come from the public list behind the player on the
website of the station (`https://www.sunshine-live.de/streams/api`), so new channels show up by
themselves. The list is kept in the cache for a minute, and an older list is used while the
station can't be reached. The id of a channel is the `station_id` of the station.

## Logo and channel images

The provider icons (`icon.png`, `icon_dark.png`, `icon_monochrome.png`) are made from the logo on
the [brand assets page of Sunshine Live](https://mamapapacola.com/brand-assets/sunshine-live). The
monochrome icon is made from the white and black version of the logo. The channel images are loaded
from the image server of the station and are not part of this repository.

[© sunshine live](https://mamapapacola.com/brand-assets/sunshine-live)

The logo and the channel images belong to sunshine live GmbH & Co. KG. Music Assistant is not
connected with the station.
