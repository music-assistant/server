"""Artist converter for nicovideo objects."""

from __future__ import annotations

from music_assistant_models.enums import ImageType, LinkType
from music_assistant_models.media_items import (
    Artist,
    MediaItemImage,
    MediaItemLink,
    MediaItemMetadata,
)
from niconico.objects.user import NicoUser
from niconico.objects.video import Owner

from music_assistant.providers.nicovideo.converters.base import NicovideoConverterBase
from music_assistant.providers.nicovideo.converters.utilities import NicovideoUrlPath


class NicovideoArtistConverter(NicovideoConverterBase):
    """Handles artist conversion for nicovideo."""

    def convert_by_owner_or_user(self, owner_or_user: Owner | NicoUser) -> Artist:
        """Convert an Owner or NicoUser into an Artist."""
        item_id = str(owner_or_user.id_)
        name = str(
            owner_or_user.name if isinstance(owner_or_user, Owner) else owner_or_user.nickname
        )
        icon_url = (
            owner_or_user.icon_url
            if isinstance(owner_or_user, Owner)
            else owner_or_user.icons.large
        )

        # Determine URL path based on owner type
        url_path: NicovideoUrlPath = "user"  # Default for users and NicoUser
        if isinstance(owner_or_user, Owner) and owner_or_user.owner_type == "channel":
            url_path = "channel"

        artist = Artist(
            item_id=item_id,
            provider=self.provider.lookup_key,
            name=name,
            metadata=MediaItemMetadata(
                description=owner_or_user.description
                if isinstance(owner_or_user, NicoUser)
                else None,
            ),
            provider_mappings=self.helper.create_provider_mapping(
                item_id=item_id,
                url_path=url_path,
            ),
        )

        # Add icon image if available
        if icon_url:
            artist.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=icon_url,
                    provider=self.provider.lookup_key,
                    remotely_accessible=True,
                )
            )

        # Add links to artist metadata
        artist.metadata.links = {
            MediaItemLink(
                type=LinkType.WEBSITE,
                url=f"https://www.nicovideo.jp/{url_path}/{item_id}",
            )
        }
        if isinstance(owner_or_user, NicoUser):
            # Add SNS links if available
            for sns in owner_or_user.sns:
                artist.metadata.links.add(
                    MediaItemLink(
                        type=LinkType(sns.type_),
                        url=sns.url,
                    )
                )

        return artist
