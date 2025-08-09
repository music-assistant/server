"""Helper class for calling provider mixins with a common interface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Concatenate

from music_assistant.providers.nicovideo.provider_mixins import NICOVIDEO_MIXINS

if TYPE_CHECKING:
    from music_assistant.providers.nicovideo.provider import NicovideoMusicProvider
    from music_assistant.providers.nicovideo.provider_mixins.base import (
        NicovideoMusicProviderMixinBase,
    )

    type MixinMethod[T, **P] = Callable[
        Concatenate[NicovideoMusicProviderMixinBase, P], Awaitable[T | None]
    ]
    type MixinMethodGetter[T, **P] = Callable[
        [type[NicovideoMusicProviderMixinBase]], MixinMethod[T, P]
    ]


class MixinCaller:
    """Helper class to call mixins with a common interface."""

    def __init__(self, provider: NicovideoMusicProvider, is_reverse: bool = False) -> None:
        """Initialize the helper with the provider and reverse order flag."""
        self.provider = provider
        self.is_reverse = is_reverse

    def get_mixins(self) -> tuple[type[NicovideoMusicProviderMixinBase], ...]:
        """Get the list of mixin classes."""
        return NICOVIDEO_MIXINS[::-1] if self.is_reverse else NICOVIDEO_MIXINS

    async def invoke_all[T, **P](
        self,
        func_getter: MixinMethodGetter[T, P],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        """Call mixin method on all mixins without collecting results."""
        for mixin_class in self.get_mixins():
            method = func_getter(mixin_class)
            await method(self.provider, *args, **kwargs)

    async def invoke_first_valid[T, U, **P](
        self,
        default: U,
        func_getter: MixinMethodGetter[T, P],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T | U:
        """Call mixin method and return the first non-None result or default."""
        for mixin_class in self.get_mixins():
            method = func_getter(mixin_class)
            result = await method(self.provider, *args, **kwargs)
            if result is not None:
                return result
        return default

    async def invoke_first_valid_or_raise[T, **P](
        self,
        exception: Exception,
        func_getter: MixinMethodGetter[T, P],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Call mixin method and raise exception if no result is found."""
        result = await self.invoke_first_valid(None, func_getter, *args, **kwargs)
        if result is None:
            raise exception
        return result
