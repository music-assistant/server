"""
Fail when a provider leaves the server without the community catalog taking its users along.

A provider that moves out of the server is listed in ``MOVED_PROVIDER_DOMAINS``: a server that has
it set up installs it from the community catalog after the upgrade. That only works when its folder
is gone and the index of every channel of the community catalog the change reaches offers a version that passed.

Without arguments (pre-commit) it checks, offline, that no moved provider still has a manifest.

In CI it also reads the changed files of a pull request from stdin, one per line as the GitHub API
lists them: status, path and the previous path of a renamed file, separated by tabs. When the
change removes a provider or changes the moved list, it asks the community catalog: a provider
that moves must be offered with a version that passed on nightly, beta and the channel of the next
beta (for a pull request into ``stable``: the channel of its next release), and a removed provider
the catalog expects to move must be in the moved list. Only then does it use the network, and a
lookup that fails fails the check.

Usage:
    uv run -m scripts.check_moved_providers
    <changed files> | uv run -m scripts.check_moved_providers --base-branch dev --base-sha <sha>
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from scripts.release_workflow import next_release_line

# ruff: noqa: T201

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDERS_FOLDER = "music_assistant/providers"
MOVED_FILE = "music_assistant/controllers/community_catalog/constants.py"
MOVED_NAME = "MOVED_PROVIDER_DOMAINS"
# the index a server reads (controllers/community_catalog/index.py)
INDEX_URL = "https://catalog.music-assistant.io/v1/{channel}.json"
RESERVED_URL = (
    "https://raw.githubusercontent.com/music-assistant/community-catalog/main/reserved_domains.json"
)
SOURCE_URL = "https://raw.githubusercontent.com/music-assistant/server/{sha}/{path}"
SERVER_REPOSITORY = "https://github.com/music-assistant/server.git"
TIMEOUT = 15
_MANIFEST = re.compile(rf"{PROVIDERS_FOLDER}/([a-z][a-z0-9_]*)/manifest\.json")


class LookupFailedError(Exception):
    """The community catalog or the server repository could not be asked."""


class NotFoundError(LookupFailedError):
    """The community catalog has no such file."""


def parse_moved(source: str) -> frozenset[str]:
    """
    Return the domains of ``MOVED_PROVIDER_DOMAINS`` in the source of the constants module.

    :param source: The source of ``community_catalog/constants.py``.
    :raises ValueError: The source does not assign the list as ``frozenset()`` of string literals.
    """
    for node in ast.walk(ast.parse(source)):
        targets: list[ast.expr]
        if isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        else:
            continue
        if not any(isinstance(target, ast.Name) and target.id == MOVED_NAME for target in targets):
            continue
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
            and len(value.args) <= 1
            and not value.keywords
        ):
            domains = ast.literal_eval(value.args[0]) if value.args else ()
            if isinstance(domains, set | list | tuple) and all(isinstance(d, str) for d in domains):
                return frozenset(domains)
        break
    msg = f"{MOVED_FILE}: {MOVED_NAME} is not a frozenset of domains"
    raise ValueError(msg)


def offline_problems(root: Path = REPO_ROOT) -> list[str]:
    """
    Return the moved providers that the server still ships.

    :param root: The root of the server repository.
    """
    moved = parse_moved((root / MOVED_FILE).read_text(encoding="utf-8"))
    # the manifest, not the folder: git leaves the compiled files of a removed provider behind
    return [
        f"{domain}: is in {MOVED_NAME} but {PROVIDERS_FOLDER}/{domain}/manifest.json still exists"
        for domain in sorted(moved)
        if (root / PROVIDERS_FOLDER / domain / "manifest.json").is_file()
    ]


def parse_changes(lines: Iterable[str]) -> list[tuple[str, str]]:
    """
    Return the status and path of each changed file, a renamed file as its old path removed.

    :param lines: The changed files, each as status, path and previous path separated by tabs.
    """
    changes: list[tuple[str, str]] = []
    for line in lines:
        status, _, rest = line.partition("\t")
        path, _, previous = rest.partition("\t")
        if status == "renamed" and previous:
            changes += [("removed", previous), ("added", path)]
        elif path:
            changes.append((status, path))
    return changes


def online_problems(
    changes: Iterable[tuple[str, str]], base_branch: str, base_sha: str, head_moved: frozenset[str]
) -> list[str]:
    """
    Return what the community catalog says against the providers a pull request moves.

    Asks nothing when the change removes no provider and leaves the moved list alone.

    :param changes: The status and path of every changed file.
    :param base_branch: The branch the pull request goes into.
    :param base_sha: The commit of that branch the pull request is compared with.
    :param head_moved: The moved list of the pull request.
    :raises LookupFailedError: The community catalog or the server repository did not answer.
    """
    changes = list(changes)
    removed = {
        match.group(1)
        for status, path in changes
        if status == "removed" and (match := _MANIFEST.fullmatch(path))
    }
    list_changed = any(path == MOVED_FILE for _, path in changes)
    if not removed and not list_changed:
        return []
    base_moved = head_moved
    if list_changed:
        base_source = _fetch(SOURCE_URL.format(sha=base_sha, path=MOVED_FILE))
        base_moved = parse_moved(base_source.decode())
    problems: list[str] = []
    if dropped := removed - head_moved:
        moving = set(json.loads(_fetch(RESERVED_URL)).get("moving", []))
        problems += [
            f"{domain}: the community catalog takes it over; add it to {MOVED_NAME}"
            for domain in sorted(dropped & moving)
        ]
    if moving_now := sorted((removed & head_moved) | (head_moved - base_moved)):
        for channel in channels_for(base_branch):
            problems += _index_problems(channel, moving_now)
    return problems


def channels_for(base_branch: str) -> list[str]:
    """
    Return the channels of the community catalog a change to a branch reaches.

    :param base_branch: The branch the change goes into.
    :raises LookupFailedError: The tags of the server repository could not be read.
    """
    release = next_release_line(base_branch, _server_tags())
    return [release] if base_branch == "stable" else ["nightly", "beta", release]


def main(argv: list[str] | None = None) -> int:
    """
    Run the check and return the exit code.

    :param argv: The arguments; without ``--base-branch`` only the offline check runs.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--base-branch", help="the branch the pull request goes into")
    parser.add_argument("--base-sha", help="the commit of that branch the pull request is based on")
    args = parser.parse_args(argv)
    if bool(args.base_branch) != bool(args.base_sha):
        parser.error("--base-branch and --base-sha go together")
    problems = offline_problems()
    if args.base_branch:
        changes = parse_changes(sys.stdin.read().splitlines())
        head_moved = parse_moved((REPO_ROOT / MOVED_FILE).read_text(encoding="utf-8"))
        try:
            problems += online_problems(changes, args.base_branch, args.base_sha, head_moved)
        except LookupFailedError as err:
            problems.append(f"could not ask the community catalog, run the check again: {err}")
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


def _index_problems(channel: str, domains: Iterable[str]) -> list[str]:
    """
    Return the domains the index of a channel does not offer with a version that passed.

    :param channel: The channel of the community catalog.
    :param domains: The domains that move.
    :raises LookupFailedError: The index exists but could not be fetched.
    """
    try:
        index: dict[str, Any] = json.loads(_fetch(INDEX_URL.format(channel=channel)))
    except NotFoundError:
        return [f"the community catalog has no index for the {_channel_name(channel)} channel yet"]
    providers = index.get("providers", {})
    return [
        f"{domain}: the community catalog index of the {_channel_name(channel)} channel "
        "offers no version that passed"
        for domain in domains
        if not (
            (entry := providers.get(domain))
            and entry.get("status") == "listed"
            and entry.get("latest") is not None
        )
    ]


def _channel_name(channel: str) -> str:
    """
    Return a channel as a contributor reads it: nightly, beta or stable <major>.<minor>.

    :param channel: The channel of the community catalog.
    """
    return f"stable {channel}" if channel[:1].isdigit() else channel


def _server_tags() -> list[str]:
    """
    Return the tags of the server repository.

    :raises LookupFailedError: git could not read them.
    """
    try:
        output = subprocess.run(  # noqa: S603
            ["git", "ls-remote", "--tags", "--refs", SERVER_REPOSITORY],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=TIMEOUT * 2,
        ).stdout
    except (OSError, subprocess.SubprocessError) as err:
        raise LookupFailedError(str(err)) from err
    return [line.rsplit("refs/tags/", 1)[-1] for line in output.splitlines()]


def _fetch(url: str) -> bytes:
    """
    Return the body of a URL.

    :param url: The URL.
    :raises NotFoundError: It answered 404.
    :raises LookupFailedError: It did not answer with a body in time.
    """
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:  # noqa: S310 - fixed https URLs
            body: bytes = response.read()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            raise NotFoundError(url) from err
        msg = f"{url}: {err}"
        raise LookupFailedError(msg) from err
    except (OSError, ValueError) as err:
        msg = f"{url}: {err}"
        raise LookupFailedError(msg) from err
    return body


if __name__ == "__main__":
    sys.exit(main())
