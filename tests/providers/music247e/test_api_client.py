"""Test 24-7 (247e) GraphQL pagination."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, call

import pytest

from music_assistant.providers.music247e.api_client import Music247eAPIClient
from music_assistant.providers.music247e.constants import PAGE_SIZE

if TYPE_CHECKING:
    from music_assistant.providers.music247e.api_client import JsonLike
    from music_assistant.providers.music247e.provider import Music247eProvider


@pytest.mark.parametrize(
    ("page_infos", "page_cap", "warning"),
    [
        pytest.param([{"hasNextPage": False}], 3, None, id="last-page"),
        pytest.param(
            [{"hasNextPage": True, "endCursor": "first"}, {"hasNextPage": False}],
            3,
            None,
            id="multiple-pages",
        ),
        pytest.param([{"hasNextPage": True}], 3, "cursor", id="missing-cursor"),
        pytest.param([{"hasNextPage": True, "endCursor": None}], 3, "cursor", id="null-cursor"),
        pytest.param([{"hasNextPage": True, "endCursor": ""}], 3, "cursor", id="empty-cursor"),
        pytest.param(
            [
                {"hasNextPage": True, "endCursor": "first"},
                {"hasNextPage": True, "endCursor": "first"},
            ],
            3,
            "cursor",
            id="repeated-cursor",
        ),
        pytest.param(
            [
                {"hasNextPage": True, "endCursor": "first"},
                {"hasNextPage": True, "endCursor": "second"},
                {"hasNextPage": True, "endCursor": "first"},
            ],
            4,
            "cursor",
            id="cursor-cycle",
        ),
        pytest.param(
            [
                {"hasNextPage": True, "endCursor": "first"},
                {"hasNextPage": True, "endCursor": "second"},
            ],
            2,
            "maximum number of pages",
            id="page-cap",
        ),
        pytest.param(
            [{"hasNextPage": True, "endCursor": "first"}],
            1,
            "maximum number of pages",
            id="single-page-cap",
        ),
        pytest.param(
            [
                {"hasNextPage": True, "endCursor": "first"},
                {"hasNextPage": False},
            ],
            2,
            None,
            id="normal-completion-at-cap",
        ),
    ],
)
async def test_paginate_graphql_termination(
    provider: Music247eProvider,
    monkeypatch: pytest.MonkeyPatch,
    page_infos: list[JsonLike],
    page_cap: int,
    warning: str | None,
) -> None:
    """Yield every fetched page and stop without requesting beyond the termination point."""
    provider.auth = Mock()
    client = Music247eAPIClient(provider)
    logger = Mock()
    monkeypatch.setattr(client, "logger", logger)
    monkeypatch.setattr(
        "music_assistant.providers.music247e.api_client.MAX_PAGES_PAGINATED", page_cap
    )
    items = [{"id": f"track{index}"} for index in range(len(page_infos))]
    post_graphql = AsyncMock(
        side_effect=[
            {"data": {"tracks": {"items": [item], "pageInfo": page_info}}}
            for item, page_info in zip(items, page_infos, strict=True)
        ]
    )
    monkeypatch.setattr(client, "post_graphql", post_graphql)
    variables = {"id": "playlist1"}

    result = [
        item async for item in client.paginate_graphql("query", variables, ["data", "tracks"])
    ]

    assert result == items
    cursors = [None, *(page_info["endCursor"] for page_info in page_infos[:-1])]
    assert post_graphql.await_args_list == [
        call("query", {"id": "playlist1", "first": PAGE_SIZE, "after": cursor})
        for cursor in cursors
    ]
    assert variables == {"id": "playlist1"}
    if warning is None:
        logger.warning.assert_not_called()
    else:
        logger.warning.assert_called_once()
        assert warning in logger.warning.call_args.args[0]
