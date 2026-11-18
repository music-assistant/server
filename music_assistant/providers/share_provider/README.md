# Share Links

Share Links is a beta plugin for sharing individual tracks, albums and artists.
Recipients can open an item through their own Music Assistant instance and music
providers. Playlist sharing and import are not supported.

## Setup and use

1. Add **Share Links** in Music Assistant's provider settings.
2. Open a track, album or artist context menu and select **Share**. Tracks can also
   be shared from the queue or fullscreen player.
3. Copy the generated link and send it to the recipient.
4. On the share page, the recipient can follow a public music-service link or enter
   their own Music Assistant instance URL and select **Open in Music Assistant**.

The share page remembers the instance URL in the recipient's browser. The sender's
server does not need to be reachable by the recipient, but the recipient must be
able to access and sign in to their own instance.

## Finding shared music

Music Assistant first checks known external identifiers. For albums and artists,
it then tries the public links for connected music sources visible to the current
user. If necessary, it searches the recipient's library and providers. Availability
depends on the recipient's subscriptions, region and catalog; a matching item is
not guaranteed. Opening a share does not add it to the library.

## Link contents and privacy

Links contain metadata, such as the title, artist, public artwork URL and
music-service links, in their URL fragment. They do not contain audio, credentials
or the sender's Music Assistant instance URL. The metadata is compressed, not
encrypted: anyone with the link can read it. Opening public provider links or
loading artwork contacts those external services.

The default share-page address is `https://share.music-assistant.io`.
The plugin's base URL setting can point to another deployment of the share page;
enter it without a trailing slash.

The authenticated `share/item` API returns an `ItemShareInfo` with provider links
from the user's visible music sources, or the item title as text when no supported
link is available. It supports tracks, albums and artists and is available only
while the Share Links plugin is loaded. It uses the same provider URL validation
as share links and does not construct links from provider IDs.

The link format is `/#1<base64url>`, where `1` identifies the format and the
unpadded Base64url value contains raw-DEFLATE-compressed UTF-8 JSON. Both the
encoded payload (including the prefix) and the decompressed JSON have a 65,536
character/byte limit respectively. Earlier unversioned development links are not
supported.
