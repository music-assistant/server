"""Deezer's Pipe GraphQL client, on the rate limit of the provider instance."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from deezer_python_gql import DeezerGQLClient, GraphQLClientHttpError
from music_assistant_models.errors import RateLimited

from music_assistant.helpers.throttle_retry import throttle_with_retries

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from deezer_python_gql import GQLResponse

    from music_assistant.helpers.throttle_retry import ThrottlerManager


class ThrottledGQLClient(DeezerGQLClient):
    """GraphQL client that sends every request on a throttler and retries a rate limited one."""

    def __init__(
        self,
        arl: str,
        session: ClientSession,
        throttler: ThrottlerManager,
        account_id: str | None = None,
    ) -> None:
        """
        Initialize the client.

        :param arl: Deezer ARL cookie of the account.
        :param session: The HTTP session to send the requests on.
        :param throttler: Throttler shared with the other clients of the account.
        :param account_id: Family profile to act as, None for the account itself.
        """
        super().__init__(arl=arl, session=session, account_id=account_id)
        self.throttler = throttler
        self.logger = logging.getLogger(__name__)

    @throttle_with_retries
    async def execute(
        self,
        /,
        query: str,
        operation_name: str | None = None,
        variables: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> GQLResponse:
        """
        Execute a GraphQL query against the Pipe API.

        :param query: The GraphQL query string.
        :param operation_name: Optional operation name for multi-operation documents.
        :param variables: Optional query variables.
        :param kwargs: Additional keyword arguments passed to the session request.
        :raises RetriesExhausted: When Deezer keeps rate limiting the account.
        """
        try:
            response = await super().execute(query, operation_name, variables, **kwargs)
        except GraphQLClientHttpError as err:
            # the token refresh raises its HTTP errors, the query returns them in its response
            if err.status_code == 429:
                raise RateLimited("Deezer rate limit") from err
            raise
        if response.status == 429:
            raise RateLimited("Deezer rate limit")
        return response
