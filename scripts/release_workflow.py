"""
Support immutable-safe GitHub release workflows.

The command-line interface emits values to ``$GITHUB_OUTPUT`` when requested so
the release workflows can keep version and artifact validation in tested Python.
"""

# ruff: noqa: T201

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CHANNEL_PATTERNS = {
    "stable": re.compile(r"^(?P<base>\d+\.\d+\.\d+)$"),
    "beta": re.compile(r"^(?P<base>\d+\.\d+\.\d+)b(?P<number>\d+)$"),
    "rc": re.compile(r"^(?P<base>\d+\.\d+\.\d+)rc(?P<number>\d+)$"),
    "nightly": re.compile(r"^(?P<base>\d+\.\d+\.\d+)\.dev(?P<number>\d+)$"),
}
CHANNEL_BRANCHES = {
    "stable": "stable",
    "rc": "stable",
    "beta": "dev",
    "nightly": "dev",
}
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
OCI_REVISION_ANNOTATION = "org.opencontainers.image.revision"
OCI_WHEEL_ANNOTATION = "io.music-assistant.wheel.sha256"
FRONTEND_VERSION_PATTERN = re.compile(r"^(?P<base>\d+(?:\.\d+)+)(?:\.post(?P<post>\d+))?$")


class ReleaseWorkflowError(RuntimeError):
    """Report an unsafe or inconsistent release state."""


@dataclass(frozen=True)
class AutoRelease:
    """Describe the next automatic release decision."""

    version: str
    should_release: bool
    previous_tag: str | None
    commits_since: int


@dataclass(frozen=True)
class Asset:
    """Describe one exact release asset."""

    name: str
    size: int
    sha256: str


class GitRepository:
    """Read release tags and commit relationships from a Git repository."""

    def __init__(self, path: Path) -> None:
        """
        Initialize a repository reader.

        :param path: Path to the Git worktree.
        """
        self.path = path

    def resolve_commit(self, ref: str) -> str:
        """
        Resolve a Git ref to its commit SHA.

        :param ref: Commit, branch, or tag to resolve.
        """
        return self._run("rev-parse", f"{ref}^{{commit}}")

    def tags(self, pattern: re.Pattern[str]) -> list[str]:
        """
        Return version-sorted tags matching a release pattern.

        :param pattern: Full tag-name pattern to match.
        """
        output = self._run("tag", "--sort=-version:refname")
        return [tag for tag in output.splitlines() if pattern.fullmatch(tag)]

    def all_tags(self) -> set[str]:
        """Return all tag names."""
        return set(self._run("tag").splitlines())

    def commits_since(self, tag: str | None, source_sha: str, *, allow_diverged: bool) -> int:
        """
        Count source commits after a previous release tag.

        :param tag: Previous release tag, or ``None`` for the full source history.
        :param source_sha: Exact release source commit.
        :param allow_diverged: Use the merge base when the tag is on a diverged branch.
        """
        if tag is None:
            return int(self._run("rev-list", "--count", source_sha))

        result = self._run_result("merge-base", "--is-ancestor", tag, source_sha)
        if result.returncode == 0:
            base = tag
        elif result.returncode == 1 and allow_diverged:
            base = self._run("merge-base", tag, source_sha)
        elif result.returncode == 1:
            msg = f"Previous {tag} is not an ancestor of source commit {source_sha}"
            raise ReleaseWorkflowError(msg)
        else:
            raise ReleaseWorkflowError(result.stderr.strip() or f"Unable to compare {tag}")
        return int(self._run("rev-list", "--count", f"{base}..{source_sha}"))

    def _run(self, *args: str) -> str:
        result = self._run_result(*args)
        if result.returncode:
            raise ReleaseWorkflowError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result.stdout.strip()

    def _run_result(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(self.path), *args],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
        )


def channel_branch(channel: str) -> str:
    """
    Return the source branch for a release channel.

    :param channel: Release channel name.
    """
    try:
        return CHANNEL_BRANCHES[channel]
    except KeyError as err:
        raise ReleaseWorkflowError(f"Unsupported release channel: {channel}") from err


def validate_version(channel: str, version: str) -> None:
    """
    Validate a version against its release channel.

    :param channel: Release channel name.
    :param version: Version to validate.
    """
    try:
        pattern = CHANNEL_PATTERNS[channel]
    except KeyError as err:
        raise ReleaseWorkflowError(f"Unsupported release channel: {channel}") from err
    if not pattern.fullmatch(version):
        raise ReleaseWorkflowError(f"Version {version} is invalid for the {channel} channel")


def determine_auto_release(
    repository: GitRepository,
    channel: str,
    source_sha: str,
    *,
    now: datetime | None = None,
) -> AutoRelease:
    """
    Calculate the next channel version from Git tags and commit ancestry.

    :param repository: Source Git repository.
    :param channel: Release channel name.
    :param source_sha: Exact release source commit.
    :param now: Timestamp used for nightly versions.
    """
    source_sha = repository.resolve_commit(source_sha)
    stable_tags = repository.tags(CHANNEL_PATTERNS["stable"])
    beta_tags = repository.tags(CHANNEL_PATTERNS["beta"])
    rc_tags = repository.tags(CHANNEL_PATTERNS["rc"])
    nightly_tags = repository.tags(CHANNEL_PATTERNS["nightly"])
    all_tags = repository.all_tags()

    latest_stable = _first(stable_tags)
    latest_beta = _first(beta_tags)
    latest_rc = _first(rc_tags)
    latest_nightly = _first(nightly_tags)

    if channel == "stable":
        previous_tag = latest_stable
        commits_since = repository.commits_since(previous_tag, source_sha, allow_diverged=True)
        version = _next_stable(latest_stable)
    elif channel == "beta":
        previous_tag = latest_beta
        commits_since = repository.commits_since(previous_tag, source_sha, allow_diverged=False)
        version = _next_beta(latest_beta, latest_stable)
    elif channel == "rc":
        version = _next_rc(latest_rc, latest_beta)
        version_match = CHANNEL_PATTERNS["rc"].fullmatch(version)
        assert version_match is not None
        rc_base = version_match.group("base")
        latest_rc_match = CHANNEL_PATTERNS["rc"].fullmatch(latest_rc or "")
        if latest_rc_match and latest_rc_match.group("base") == rc_base:
            previous_tag = latest_rc
        else:
            previous_tag = latest_beta
        commits_since = repository.commits_since(previous_tag, source_sha, allow_diverged=False)
    elif channel == "nightly":
        previous_tag = latest_nightly
        commits_since = repository.commits_since(previous_tag, source_sha, allow_diverged=False)
        version = _next_nightly(
            latest_nightly,
            latest_stable,
            now or datetime.now(UTC),
        )
    else:
        raise ReleaseWorkflowError(f"Unsupported release channel: {channel}")

    version = _ensure_unique(version, channel, all_tags)
    return AutoRelease(
        version=version,
        should_release=channel != "nightly" or commits_since >= 2,
        previous_tag=previous_tag,
        commits_since=commits_since,
    )


def previous_release_tag(
    repository: GitRepository,
    channel: str,
    version: str,
) -> str | None:
    """
    Return the previous tag used as the release-notes base.

    :param repository: Source Git repository.
    :param channel: Release channel name.
    :param version: Version currently being prepared.
    """
    validate_version(channel, version)
    if channel == "stable":
        tags = repository.tags(CHANNEL_PATTERNS["stable"])
    elif channel == "beta":
        combined_pattern = re.compile(r"^\d+\.\d+\.\d+(?:b|rc)\d+$")
        tags = repository.tags(combined_pattern)
    elif channel == "nightly":
        tags = repository.tags(CHANNEL_PATTERNS["nightly"])
    else:
        match = CHANNEL_PATTERNS["rc"].fullmatch(version)
        assert match is not None
        rc_number = int(match.group("number"))
        if rc_number > 1:
            previous_rc = f"{match.group('base')}rc{rc_number - 1}"
            if previous_rc in repository.all_tags():
                return previous_rc
        tags = repository.tags(CHANNEL_PATTERNS["beta"])
    return next((tag for tag in tags if tag != version), None)


def is_current_release(
    repository: GitRepository,
    channel: str,
    version: str,
) -> tuple[bool, str | None]:
    """
    Check whether a release is the newest version for its rolling channel.

    :param repository: Source Git repository.
    :param channel: Release channel name.
    :param version: Release version to compare.
    """
    validate_version(channel, version)
    if channel == "stable":
        pattern = CHANNEL_PATTERNS["stable"]
    elif channel == "nightly":
        pattern = CHANNEL_PATTERNS["nightly"]
    else:
        pattern = re.compile(r"^\d+\.\d+\.\d+(?:b|rc)\d+$")
    tags = repository.tags(pattern)
    latest_tag = max(tags, key=_release_version_key, default=None)
    return latest_tag is None or _release_version_key(version) >= _release_version_key(
        latest_tag
    ), latest_tag


def compare_release_versions(current: str, requested: str) -> str:
    """
    Compare a deployed release version with a requested release version.

    :param current: Version currently deployed to an alias.
    :param requested: Version requested for promotion.
    """
    return _version_relation(
        _release_version_key(current),
        _release_version_key(requested),
    )


def compare_frontend_versions(current: str, requested: str) -> str:
    """
    Compare a deployed frontend version with a requested frontend version.

    :param current: Frontend version currently configured for the channel.
    :param requested: Frontend version requested for deployment.
    """
    current_base, current_post = _frontend_version_parts(current)
    requested_base, requested_post = _frontend_version_parts(requested)
    width = max(len(current_base), len(requested_base))
    current_key = (*current_base, *((0,) * (width - len(current_base))), current_post)
    requested_key = (*requested_base, *((0,) * (width - len(requested_base))), requested_post)
    return _version_relation(current_key, requested_key)


def select_release(release_pages: object, version: str) -> dict[str, Any] | None:
    """
    Return the only release whose tag exactly matches a version.

    :param release_pages: Paginated responses from the GitHub releases API.
    :param version: Exact release tag to select.
    """
    if not isinstance(release_pages, list):
        raise ReleaseWorkflowError("GitHub releases response does not contain pages")

    matches: list[dict[str, Any]] = []
    for page in release_pages:
        if not isinstance(page, list):
            raise ReleaseWorkflowError("GitHub releases response contains an invalid page")
        for release in page:
            if not isinstance(release, dict):
                raise ReleaseWorkflowError("GitHub releases response contains invalid metadata")
            if release.get("tag_name") == version:
                matches.append(release)

    if len(matches) > 1:
        release_ids = ", ".join(str(release.get("id", "unknown")) for release in matches)
        raise ReleaseWorkflowError(f"Multiple releases match exact tag {version}: {release_ids}")
    if not matches:
        return None

    release = matches[0]
    release_id = release.get("id")
    if not isinstance(release_id, int) or isinstance(release_id, bool) or release_id <= 0:
        raise ReleaseWorkflowError(f"Release {version} does not contain a valid id")
    return release


def inspect_assets(
    version: str,
    *,
    directory: Path | None = None,
    release_json: Path | None = None,
) -> tuple[Asset, Asset]:
    """
    Validate and describe the exact wheel and source distribution.

    :param version: Release version.
    :param directory: Optional directory containing downloaded assets.
    :param release_json: Optional GitHub release API response.
    """
    expected_names = _expected_asset_names(version)
    api_assets: dict[str, Asset] | None = None
    local_assets: dict[str, Asset] | None = None

    if release_json is not None:
        release = json.loads(release_json.read_text(encoding="utf-8"))
        raw_assets = release.get("assets")
        if not isinstance(raw_assets, list):
            raise ReleaseWorkflowError("Release response does not contain an asset list")
        if len(raw_assets) != len(expected_names):
            raise ReleaseWorkflowError(
                f"Release must contain exactly these assets: {', '.join(expected_names)}"
            )
        api_assets = {}
        for raw_asset in raw_assets:
            if not isinstance(raw_asset, dict):
                raise ReleaseWorkflowError("Release response contains invalid asset metadata")
            name = raw_asset.get("name")
            size = raw_asset.get("size")
            state = raw_asset.get("state")
            digest = raw_asset.get("digest")
            if (
                not isinstance(name, str)
                or not isinstance(size, int)
                or state != "uploaded"
                or not isinstance(digest, str)
                or not SHA256_PATTERN.fullmatch(digest)
                or size <= 0
            ):
                raise ReleaseWorkflowError(f"Release asset metadata is invalid: {name!r}")
            if name in api_assets:
                raise ReleaseWorkflowError(f"Release contains duplicate asset {name}")
            api_assets[name] = Asset(name, size, digest.removeprefix("sha256:"))
        if set(api_assets) != set(expected_names):
            raise ReleaseWorkflowError(
                f"Release must contain exactly these assets: {', '.join(expected_names)}"
            )

    if directory is not None:
        files = sorted(path for path in directory.iterdir() if path.is_file())
        if {path.name for path in files} != set(expected_names):
            raise ReleaseWorkflowError(
                f"Asset directory must contain exactly: {', '.join(expected_names)}"
            )
        local_assets = {
            path.name: Asset(path.name, path.stat().st_size, _sha256(path)) for path in files
        }
        if any(asset.size <= 0 for asset in local_assets.values()):
            raise ReleaseWorkflowError("Release assets must not be empty")

    assets = local_assets or api_assets
    if assets is None:
        raise ReleaseWorkflowError("An asset directory or release response is required")
    if local_assets is not None and api_assets is not None and local_assets != api_assets:
        raise ReleaseWorkflowError("Downloaded assets do not match GitHub release metadata")
    return assets[expected_names[0]], assets[expected_names[1]]


def verify_oci_manifest(
    manifest: dict[str, Any],
    source_sha: str,
    wheel_sha256: str,
) -> tuple[str, list[str]]:
    """
    Validate the exact multi-platform image manifest and provenance annotations.

    :param manifest: Formatted Buildx manifest JSON.
    :param source_sha: Exact source commit expected in the image annotations.
    :param wheel_sha256: Wheel digest expected in the image annotations.
    """
    digest = manifest.get("digest")
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        raise ReleaseWorkflowError("OCI manifest does not contain a valid digest")
    annotations = manifest.get("annotations")
    if not isinstance(annotations, dict):
        raise ReleaseWorkflowError("OCI manifest does not contain provenance annotations")
    if annotations.get(OCI_REVISION_ANNOTATION) != source_sha:
        raise ReleaseWorkflowError("OCI manifest source SHA does not match the release")
    if annotations.get(OCI_WHEEL_ANNOTATION) != wheel_sha256:
        raise ReleaseWorkflowError("OCI manifest wheel digest does not match the release")

    raw_manifests = manifest.get("manifests")
    if not isinstance(raw_manifests, list):
        raise ReleaseWorkflowError("OCI image is not a multi-platform manifest")
    runtime_manifests: dict[str, str] = {}
    for item in raw_manifests:
        if not isinstance(item, dict):
            raise ReleaseWorkflowError("OCI manifest contains an invalid descriptor")
        platform = item.get("platform")
        child_digest = item.get("digest")
        if not isinstance(platform, dict) or not isinstance(child_digest, str):
            raise ReleaseWorkflowError("OCI manifest descriptor is incomplete")
        operating_system = platform.get("os")
        architecture = platform.get("architecture")
        if operating_system == "unknown" and architecture == "unknown":
            continue
        if not SHA256_PATTERN.fullmatch(child_digest):
            raise ReleaseWorkflowError("OCI child manifest digest is invalid")
        platform_name = f"{operating_system}/{architecture}"
        if platform_name in runtime_manifests:
            raise ReleaseWorkflowError(f"OCI image contains duplicate platform {platform_name}")
        runtime_manifests[platform_name] = child_digest
    expected_platforms = {"linux/amd64", "linux/arm64"}
    if set(runtime_manifests) != expected_platforms:
        raise ReleaseWorkflowError("OCI image must contain linux/amd64 and linux/arm64")
    return digest, [runtime_manifests[platform] for platform in sorted(expected_platforms)]


def update_addon_release(
    config_path: Path,
    changelog_path: Path,
    *,
    version: str,
    release_date: str,
    notes: str,
    retain: int = 3,
) -> None:
    """
    Update an add-on channel to one canonical release entry.

    :param config_path: Add-on ``config.yaml`` path.
    :param changelog_path: Add-on ``CHANGELOG.md`` path.
    :param version: Server version and image tag.
    :param release_date: Canonical release date in ``DD.MM.YYYY`` format.
    :param notes: Published server release notes.
    :param retain: Total number of distinct changelog releases to retain.
    """
    if retain < 1:
        raise ReleaseWorkflowError("At least one changelog release must be retained")
    config = config_path.read_text(encoding="utf-8")
    config, replacements = re.subn(
        r"^version: .+$",
        f"version: {version}",
        config,
        count=1,
        flags=re.MULTILINE,
    )
    if replacements != 1:
        raise ReleaseWorkflowError(f"Expected one version field in {config_path}")
    config_path.write_text(config, encoding="utf-8")

    existing = changelog_path.read_text(encoding="utf-8") if changelog_path.exists() else ""
    blocks = _changelog_blocks(existing)
    previous_blocks: list[str] = []
    seen_versions = {version}
    for block_version, block in blocks:
        if block_version in seen_versions:
            continue
        seen_versions.add(block_version)
        previous_blocks.append(block)
    new_block = f"# [{version}] - {release_date}\n\n{notes.strip()}"
    content = "\n\n\n".join([new_block, *previous_blocks[: retain - 1]]) + "\n"
    changelog_path.write_text(content, encoding="utf-8")


def _next_stable(latest_stable: str | None) -> str:
    if latest_stable is None:
        return "0.1.0"
    major, minor, patch = _version_tuple(latest_stable)
    return f"{major}.{minor}.{patch + 1}"


def _next_beta(latest_beta: str | None, latest_stable: str | None) -> str:
    if latest_beta is not None:
        match = CHANNEL_PATTERNS["beta"].fullmatch(latest_beta)
        assert match is not None
        beta_base = _version_tuple(match.group("base"))
        if latest_stable is not None:
            stable = _version_tuple(latest_stable)
            if stable[:2] >= beta_base[:2]:
                return f"{stable[0]}.{stable[1] + 1}.0b1"
        return f"{match.group('base')}b{int(match.group('number')) + 1}"
    if latest_stable is not None:
        major, minor, _patch = _version_tuple(latest_stable)
        return f"{major}.{minor + 1}.0b1"
    return "0.1.0b1"


def _next_rc(latest_rc: str | None, latest_beta: str | None) -> str:
    if latest_beta is None:
        return "0.1.0rc1"
    beta_match = CHANNEL_PATTERNS["beta"].fullmatch(latest_beta)
    assert beta_match is not None
    beta_base = beta_match.group("base")
    if latest_rc is not None:
        rc_match = CHANNEL_PATTERNS["rc"].fullmatch(latest_rc)
        assert rc_match is not None
        if rc_match.group("base") == beta_base:
            return f"{beta_base}rc{int(rc_match.group('number')) + 1}"
    return f"{beta_base}rc1"


def _next_nightly(
    latest_nightly: str | None,
    latest_stable: str | None,
    now: datetime,
) -> str:
    if latest_stable is None:
        base = "0.1.0"
    else:
        major, minor, _patch = _version_tuple(latest_stable)
        base = f"{major}.{minor + 1}.0"
    serial = int(now.astimezone(UTC).strftime("%Y%m%d%H"))
    if latest_nightly is not None:
        match = CHANNEL_PATTERNS["nightly"].fullmatch(latest_nightly)
        assert match is not None
        if match.group("base") == base:
            serial = max(serial, int(match.group("number")) + 1)
    return f"{base}.dev{serial}"


def _ensure_unique(version: str, channel: str, tags: set[str]) -> str:
    while version in tags:
        match = CHANNEL_PATTERNS[channel].fullmatch(version)
        assert match is not None
        base = match.group("base")
        if channel == "stable":
            major, minor, patch = _version_tuple(base)
            version = f"{major}.{minor}.{patch + 1}"
        elif channel == "beta":
            version = f"{base}b{int(match.group('number')) + 1}"
        elif channel == "rc":
            version = f"{base}rc{int(match.group('number')) + 1}"
        else:
            version = f"{base}.dev{int(match.group('number')) + 1}"
    return version


def _version_tuple(version: str) -> tuple[int, int, int]:
    match = CHANNEL_PATTERNS["stable"].fullmatch(version)
    if match is None:
        raise ReleaseWorkflowError(f"Invalid base version: {version}")
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


def _release_version_key(version: str) -> tuple[int, int, int, int, int]:
    for stage, channel in enumerate(("nightly", "beta", "rc", "stable")):
        match = CHANNEL_PATTERNS[channel].fullmatch(version)
        if match is None:
            continue
        major, minor, patch = _version_tuple(match.group("base"))
        number = int(match.groupdict().get("number") or 0)
        return major, minor, patch, stage, number
    raise ReleaseWorkflowError(f"Unsupported release version: {version}")


def _frontend_version_parts(version: str) -> tuple[tuple[int, ...], int]:
    match = FRONTEND_VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise ReleaseWorkflowError(f"Unsupported frontend version: {version}")
    base = tuple(int(part) for part in match.group("base").split("."))
    post = int(match.group("post")) if match.group("post") is not None else -1
    return base, post


def _version_relation(current: tuple[int, ...], requested: tuple[int, ...]) -> str:
    if current > requested:
        return "newer"
    if current < requested:
        return "older"
    return "equal"


def _first(values: list[str]) -> str | None:
    return values[0] if values else None


def _expected_asset_names(version: str) -> tuple[str, str]:
    return (
        f"music_assistant-{version}-py3-none-any.whl",
        f"music_assistant-{version}.tar.gz",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _changelog_blocks(content: str) -> list[tuple[str, str]]:
    header_pattern = re.compile(r"(?m)^# \[([^\]]+)\] - .+$")
    matches = list(header_pattern.finditer(content))
    if not matches:
        if content.strip():
            raise ReleaseWorkflowError("Existing changelog has no release headings")
        return []
    if content[: matches[0].start()].strip():
        raise ReleaseWorkflowError("Existing changelog has content before its first release")
    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        blocks.append((match.group(1), content[match.start() : end].strip()))
    return blocks


def _write_outputs(
    values: Mapping[str, str | int | bool | None],
    output_path: Path | None,
) -> None:
    lines = [
        f"{key}={str(value).lower() if isinstance(value, bool) else '' if value is None else value}"
        for key, value in values.items()
    ]
    if output_path is not None:
        with output_path.open("a", encoding="utf-8") as file_handle:
            file_handle.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def _asset_outputs(assets: tuple[Asset, Asset]) -> dict[str, str | int]:
    wheel, source = assets
    return {
        "wheel_name": wheel.name,
        "wheel_size": wheel.size,
        "wheel_sha256": wheel.sha256,
        "sdist_name": source.name,
        "sdist_size": source.size,
        "sdist_sha256": source.sha256,
    }


def _configure_release_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--version", required=True)
    parser.add_argument("--releases-json", type=Path, required=True)
    parser.add_argument("--release-json", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    branch_parser = subparsers.add_parser("branch")
    branch_parser.add_argument("--channel", required=True)
    branch_parser.add_argument("--github-output", type=Path)

    validate_parser = subparsers.add_parser("validate-version")
    validate_parser.add_argument("--channel", required=True)
    validate_parser.add_argument("--version", required=True)

    auto_parser = subparsers.add_parser("auto-version")
    auto_parser.add_argument("--channel", required=True)
    auto_parser.add_argument("--source-sha", required=True)
    auto_parser.add_argument("--repository", type=Path, default=Path.cwd())
    auto_parser.add_argument("--timestamp")
    auto_parser.add_argument("--github-output", type=Path)

    previous_parser = subparsers.add_parser("previous-tag")
    previous_parser.add_argument("--channel", required=True)
    previous_parser.add_argument("--version", required=True)
    previous_parser.add_argument("--repository", type=Path, default=Path.cwd())
    previous_parser.add_argument("--github-output", type=Path)

    current_parser = subparsers.add_parser("current-version")
    current_parser.add_argument("--channel", required=True)
    current_parser.add_argument("--version", required=True)
    current_parser.add_argument("--repository", type=Path, default=Path.cwd())
    current_parser.add_argument("--github-output", type=Path)

    release_order_parser = subparsers.add_parser("compare-release-versions")
    release_order_parser.add_argument("--current", required=True)
    release_order_parser.add_argument("--requested", required=True)
    release_order_parser.add_argument("--github-output", type=Path)

    frontend_order_parser = subparsers.add_parser("compare-frontend-versions")
    frontend_order_parser.add_argument("--current", required=True)
    frontend_order_parser.add_argument("--requested", required=True)
    frontend_order_parser.add_argument("--github-output", type=Path)

    _configure_release_parser(subparsers.add_parser("select-release"))

    assets_parser = subparsers.add_parser("assets")
    assets_parser.add_argument("--version", required=True)
    assets_parser.add_argument("--directory", type=Path)
    assets_parser.add_argument("--release-json", type=Path)
    assets_parser.add_argument("--github-output", type=Path)

    manifest_parser = subparsers.add_parser("verify-manifest")
    manifest_parser.add_argument("--manifest-json", type=Path, required=True)
    manifest_parser.add_argument("--source-sha", required=True)
    manifest_parser.add_argument("--wheel-sha256", required=True)
    manifest_parser.add_argument("--github-output", type=Path)

    addon_parser = subparsers.add_parser("update-addon")
    addon_parser.add_argument("--config", type=Path, required=True)
    addon_parser.add_argument("--changelog", type=Path, required=True)
    addon_parser.add_argument("--version", required=True)
    addon_parser.add_argument("--release-date", required=True)
    addon_parser.add_argument("--notes", type=Path, required=True)
    addon_parser.add_argument("--retain", type=int, default=3)
    return parser


def main() -> int:
    """Run a release workflow helper command."""
    args = _build_parser().parse_args()
    try:
        if args.command == "branch":
            _write_outputs({"branch": channel_branch(args.channel)}, args.github_output)
        elif args.command == "validate-version":
            validate_version(args.channel, args.version)
        elif args.command == "auto-version":
            timestamp = datetime.fromisoformat(args.timestamp) if args.timestamp else None
            decision = determine_auto_release(
                GitRepository(args.repository),
                args.channel,
                args.source_sha,
                now=timestamp,
            )
            _write_outputs(
                {
                    "version": decision.version,
                    "should_release": decision.should_release,
                    "previous_tag": decision.previous_tag,
                    "commits_since": decision.commits_since,
                },
                args.github_output,
            )
        elif args.command == "previous-tag":
            tag = previous_release_tag(
                GitRepository(args.repository),
                args.channel,
                args.version,
            )
            _write_outputs({"previous_tag": tag}, args.github_output)
        elif args.command == "current-version":
            is_current, latest_tag = is_current_release(
                GitRepository(args.repository),
                args.channel,
                args.version,
            )
            _write_outputs(
                {
                    "is_current": is_current,
                    "latest_channel_tag": latest_tag,
                },
                args.github_output,
            )
        elif args.command == "compare-release-versions":
            _write_outputs(
                {
                    "current_relation": compare_release_versions(
                        args.current,
                        args.requested,
                    )
                },
                args.github_output,
            )
        elif args.command == "compare-frontend-versions":
            _write_outputs(
                {
                    "current_relation": compare_frontend_versions(
                        args.current,
                        args.requested,
                    )
                },
                args.github_output,
            )
        elif args.command == "select-release":
            release_pages = json.loads(args.releases_json.read_text(encoding="utf-8"))
            release = select_release(release_pages, args.version)
            if release is None:
                args.release_json.unlink(missing_ok=True)
                _write_outputs(
                    {"release_exists": False, "release_id": None},
                    args.github_output,
                )
            else:
                args.release_json.write_text(
                    json.dumps(release, indent=2) + "\n",
                    encoding="utf-8",
                )
                _write_outputs(
                    {"release_exists": True, "release_id": release["id"]},
                    args.github_output,
                )
        elif args.command == "assets":
            assets = inspect_assets(
                args.version,
                directory=args.directory,
                release_json=args.release_json,
            )
            _write_outputs(_asset_outputs(assets), args.github_output)
        elif args.command == "verify-manifest":
            manifest = json.loads(args.manifest_json.read_text(encoding="utf-8"))
            digest, runtime_digests = verify_oci_manifest(
                manifest,
                args.source_sha,
                args.wheel_sha256,
            )
            _write_outputs(
                {
                    "digest": digest,
                    "runtime_digests": " ".join(runtime_digests),
                },
                args.github_output,
            )
        elif args.command == "update-addon":
            update_addon_release(
                args.config,
                args.changelog,
                version=args.version,
                release_date=args.release_date,
                notes=args.notes.read_text(encoding="utf-8"),
                retain=args.retain,
            )
        return 0
    except (OSError, ReleaseWorkflowError, ValueError, json.JSONDecodeError) as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
