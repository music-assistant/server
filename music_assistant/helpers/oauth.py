"""Small shared helpers for OAuth setup flows that use the MA hosted callback bounce."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from music_assistant.models.setup_flow import SetupFlowError

# Fixed https redirect URI that OAuth providers which only allow pre-registered
# redirect URIs (Spotify, Google, Microsoft, ...) have on file. The page hosted
# there forwards the browser to the local (session specific) callback URL that we
# smuggle along in the OAuth `state` parameter.
HOSTED_CALLBACK_URL = "https://music-assistant.io/callback"

# Deadline for the browser part of an OAuth flow. Bounded so the client shows a
# countdown and a consent that never comes back (window closed, callback blocked)
# ends the flow instead of leaving the user with a spinner.
OAUTH_STEP_TIMEOUT = 10 * 60


def hosted_bounce_redirect(callback_url: str) -> tuple[str, str]:
    """
    Return the (redirect_uri, state) pair for a hosted-bounce OAuth authorize URL.

    The redirect_uri is the fixed MA callback page the provider has pre-registered;
    it forwards the browser to the flow's local callback URL, carried in `state`.

    :param callback_url: The setup session's local callback URL (session.callback_url).
    """
    return HOSTED_CALLBACK_URL, callback_url


def authorization_code_from_params(params: dict[str, str]) -> str:
    """
    Return the authorization code from OAuth callback params, or raise SetupFlowError.

    :param params: The merged callback query/body params returned by session.external().
    """
    code = params.get("code")
    # an older (cached) hosted relay page forwards a literal "null" code on denied consent
    if not code or code == "null":
        error = params.get("error") or "no authorization code returned"
        raise SetupFlowError(f"Authorization failed: {error}")
    return code


def authorization_code_from_url(url: str) -> str:
    """
    Return the authorization code from a redirect URL the user pasted back into a form.

    For providers whose OAuth client only accepts loopback redirect URIs, the browser cannot
    reach Music Assistant and the user copies the URL it landed on instead.

    :param url: The full redirect URL, as pasted by the user.
    :raises SetupFlowError: When the URL carries no usable authorization code.
    """
    query = parse_qs(urlparse(url.strip()).query)
    return authorization_code_from_params(
        {key: values[0] for key, values in query.items() if values}
    )
