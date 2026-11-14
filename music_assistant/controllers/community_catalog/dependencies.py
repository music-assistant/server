"""Python requirements of community providers, installed next to the server's own packages."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
import re
import shutil
import sys
import sysconfig
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from music_assistant_models.errors import MusicAssistantError

from music_assistant.helpers.process import check_output

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# uv resolves against the indexes the server's own packages are installed from (see
# requirements_all.txt and the Dockerfile), so a build such as torch `+cpu` keeps its own
# requirements; tests point it at locally built wheels
_UV_INDEX_ARGS: list[str] = [
    "--extra-index-url",
    "https://download.pytorch.org/whl/cpu",
    "--index-strategy",
    "unsafe-best-match",
]
_LOCK_FILE = "requirements.lock"
_INPUTS_HASH_PREFIX = "# inputs: "
_ENVIRONMENT_PREFIX = "# python: "
# a wheel with native code only fits the Python version and platform it was installed for
_ENVIRONMENT = f"{sys.implementation.cache_tag}-{sysconfig.get_platform()}"
# uv exits with 1 when the requirements cannot be resolved; any other failure is its own
_EXIT_CONFLICT = 1
# a package name with optional extras and version specifiers; never a URL or an option for uv
_PLAIN_REQUIREMENT = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._, -]+\])?"
    r"(\s*(==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9.*+!_-]+"
    r"(\s*,\s*(==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9.*+!_-]+)*)?"
)


class DependencyConflict(MusicAssistantError):
    """The requirements of a community provider cannot be installed."""

    def __init__(self, domain: str, culprit: str, cause: str) -> None:
        """
        Initialize the error.

        :param domain: The provider whose requirements are refused.
        :param culprit: What they clash with: "server", the domain of another provider, or the
            provider's own domain when its requirements cannot be installed at all.
        :param cause: The explanation of the resolver.
        """
        clash = "cannot be installed" if culprit == domain else f"clash with {culprit}"
        super().__init__(f"Requirements of {domain} {clash}: {cause}")
        self.domain = domain
        self.culprit = culprit


class DependenciesUnavailable(MusicAssistantError):
    """uv failed or the package index could not be reached."""


@dataclass(frozen=True, slots=True)
class ResolvedDependencies:
    """The pinned requirements of all community providers and what they were resolved for."""

    pins: tuple[str, ...]
    inputs_hash: str
    environment: str


class CommunityDependencies:
    """The folder that holds the Python requirements of all community providers."""

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Initialize the dependency folder.

        :param mass: The Music Assistant instance, whose cache folder holds the dependencies.
        """
        self.path = os.path.join(mass.cache_path, "community_deps")
        self._lock_path = os.path.join(self.path, _LOCK_FILE)

    async def resolve(self, requirements: Mapping[str, Sequence[str]]) -> ResolvedDependencies:
        """
        Resolve the requirements of all community providers against the server's own packages.

        The result holds only what the server does not have. Pins of the installed set are kept
        unless a requirement forces a change. When the server's packages, its Python and the
        requirements are as they were at the last sync, the installed set is returned without
        uv. Nothing on disk changes.

        :param requirements: The requirements per provider domain. When two providers clash,
            the later one is refused.
        :raises DependencyConflict: A provider's requirements cannot be installed.
        :raises DependenciesUnavailable: uv failed or the package index could not be reached.
        """
        for domain, reqs in requirements.items():
            for req in reqs:
                if not _PLAIN_REQUIREMENT.fullmatch(req):
                    raise DependencyConflict(domain, domain, f"{req!r} is not a package name")
        constraints = await asyncio.to_thread(_server_constraints)
        union = sorted({req for reqs in requirements.values() for req in reqs})
        inputs_hash = hashlib.sha256("\n".join([*constraints, "", *union]).encode()).hexdigest()
        current = await asyncio.to_thread(self._read_lock)
        if (inputs_hash, _ENVIRONMENT) == (current.inputs_hash, current.environment):
            return current
        if not union:
            return ResolvedDependencies((), inputs_hash, _ENVIRONMENT)
        workdir = await asyncio.to_thread(tempfile.mkdtemp, prefix="community_deps_")
        try:
            output = os.path.join(workdir, _LOCK_FILE)
            await asyncio.to_thread(_write_lines, os.path.join(workdir, "server.txt"), constraints)
            # the pins already in the output file are kept where possible
            await asyncio.to_thread(_write_lines, output, current.pins)
            if (cause := await self._compile(workdir, union, "-o", output)) is not None:
                await self._raise_conflict(workdir, requirements, cause)
            server = {pin.split("==")[0] for pin in constraints}
            # the server's own packages are part of the resolve, and are left out of the result
            pins = tuple(
                pin
                for pin in _pins(await asyncio.to_thread(_read_lines, output))
                if _canonical_name(pin.split("==")[0]) not in server
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, workdir, True)
        return ResolvedDependencies(pins, inputs_hash, _ENVIRONMENT)

    async def touches_imported(self, pins: Sequence[str]) -> bool:
        """
        Return whether installing the pins would replace or remove a package that is imported.

        Such a change only takes effect after a restart of the server.

        :param pins: The pins to install, as returned by `resolve`.
        """
        return await asyncio.to_thread(self._touches_imported, {_canonical_pin(p) for p in pins})

    async def sync(self, resolved: ResolvedDependencies) -> None:
        """
        Make the dependency folder hold exactly the resolved pins.

        :param resolved: The result of `resolve`.
        :raises DependenciesUnavailable: uv failed or the package index could not be reached.
        """
        current = await asyncio.to_thread(self._read_lock)
        if resolved == current:
            return
        new_lock = f"{self._lock_path}.new"
        header = (
            f"{_INPUTS_HASH_PREFIX}{resolved.inputs_hash}",
            f"{_ENVIRONMENT_PREFIX}{resolved.environment}",
        )
        await asyncio.to_thread(_write_lines, new_lock, [*header, *resolved.pins])
        reinstall = resolved.environment != current.environment
        if reinstall or resolved.pins != current.pins:
            args = ["--target", self.path, "--allow-empty-requirements"]
            if reinstall:
                # what is in the folder was installed for another Python or platform
                args.append("--reinstall")
            if (cause := await self._uv("sync", new_lock, *args)) is not None:
                raise DependenciesUnavailable(cause)
            # a package new in a folder that is already on sys.path is only found after this
            importlib.invalidate_caches()
        await asyncio.to_thread(os.replace, new_lock, self._lock_path)

    def activate(self) -> None:
        """Make the installed requirements importable; the server's own packages keep precedence."""
        if self.path not in sys.path:
            # appended, not added as a site dir: no .pth file of a dependency is executed
            sys.path.append(self.path)

    async def _compile(
        self, workdir: str, requirements: Sequence[str], *args: str, constrained: bool = True
    ) -> str | None:
        """
        Resolve requirements and return the explanation of uv when they clash, else None.

        :param workdir: The folder with the input files of this resolve.
        :param requirements: The requirements to resolve.
        :param args: Extra arguments for `uv pip compile`.
        :param constrained: Whether the server's packages constrain the resolve.
        """
        req_file = await asyncio.to_thread(_write_temp_lines, workdir, requirements)
        if constrained:
            args = (*args, "--constraint", os.path.join(workdir, "server.txt"))
        return await self._uv("compile", req_file, "--no-header", "--no-annotate", *args)

    async def _raise_conflict(
        self, workdir: str, requirements: Mapping[str, Sequence[str]], cause: str
    ) -> NoReturn:
        """
        Raise a DependencyConflict that names the provider to refuse and what it clashes with.

        :param workdir: The folder with the input files of this resolve.
        :param requirements: The requirements per provider domain, earlier ones take precedence.
        :param cause: The explanation of uv for the clash of the whole set.
        """
        accepted: dict[str, Sequence[str]] = {}
        for domain, reqs in requirements.items():
            if (clash := await self._compile(workdir, reqs)) is not None:
                own = await self._compile(workdir, reqs, constrained=False)
                culprit = domain if own is not None else "server"
                raise DependencyConflict(domain, culprit, own or clash)
            earlier = [req for other in accepted.values() for req in other]
            if earlier and await self._compile(workdir, [*earlier, *reqs]) is not None:
                # the first earlier provider that makes the clash appear is the culprit
                combined: list[str] = []
                for other, other_reqs in accepted.items():
                    combined.extend(other_reqs)
                    if (clash := await self._compile(workdir, [*combined, *reqs])) is not None:
                        raise DependencyConflict(domain, other, clash)
            accepted[domain] = reqs
        # every part resolves while the whole set did not: the index changed in between
        raise DependenciesUnavailable(cause)

    async def _uv(self, command: str, *args: str) -> str | None:
        """
        Run a `uv pip` command for the server's interpreter and return its output on a conflict.

        :param command: The `uv pip` subcommand.
        :param args: The arguments of the subcommand.
        :raises DependenciesUnavailable: uv failed for another reason than a conflict.
        """
        code, output = await check_output(
            sys.executable,
            "-m",
            "uv",
            "pip",
            command,
            *args,
            "--python",
            sys.executable,
            "--no-config",
            "--no-cache",
            "--only-binary",
            ":all:",
            *_UV_INDEX_ARGS,
        )
        if code == 0:
            return None
        text = output.decode(errors="replace").strip()
        if code == _EXIT_CONFLICT:
            return text
        msg = f"uv pip {command} failed with exit code {code}: {text}"
        raise DependenciesUnavailable(msg)

    def _read_lock(self) -> ResolvedDependencies:
        """Return the installed pins and what they were resolved for (blocking)."""
        try:
            lines = _read_lines(self._lock_path)
        except FileNotFoundError:
            lines = []
        header = {
            prefix: line.removeprefix(prefix)
            for line in lines
            for prefix in (_INPUTS_HASH_PREFIX, _ENVIRONMENT_PREFIX)
            if line.startswith(prefix)
        }
        return ResolvedDependencies(
            _pins(lines), header.get(_INPUTS_HASH_PREFIX, ""), header.get(_ENVIRONMENT_PREFIX, "")
        )

    def _touches_imported(self, keep: set[str]) -> bool:
        """Return whether a module is imported from a package that is not in keep (blocking)."""
        changing = [
            dist
            for dist in distributions(path=[self.path])
            if _canonical_pin(f"{dist.name}=={dist.version}") not in keep
        ]
        files = {
            os.path.normpath(str(dist.locate_file(f)))
            for dist in changing
            for f in dist.files or ()
        }
        return any(
            isinstance(file := getattr(module, "__file__", None), str)
            and os.path.normpath(file) in files
            for module in list(sys.modules.values())
        )


def _server_constraints() -> list[str]:
    """Return the distributions of the server's own environment as sorted pins (blocking)."""
    paths = sysconfig.get_paths()
    versions = {
        _canonical_name(dist.name): dist.version
        for dist in distributions(path=list({paths["purelib"], paths["platlib"]}))
    }
    return sorted(f"{name}=={version}" for name, version in versions.items())


def _canonical_name(name: str) -> str:
    """Return the normalized form of a distribution name (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _canonical_pin(pin: str) -> str:
    """Return a `name==version` pin with its name normalized."""
    name, _, version = pin.partition("==")
    return f"{_canonical_name(name)}=={version}"


def _read_lines(path: str) -> list[str]:
    """Return the stripped lines of a file (blocking)."""
    with open(path, encoding="utf-8") as file:
        return [line.strip() for line in file]


def _pins(lines: Iterable[str]) -> tuple[str, ...]:
    """Return the pins of the lines of a requirements file."""
    return tuple(line for line in lines if line and not line.startswith("#"))


def _write_lines(path: str, lines: Iterable[str]) -> None:
    """Write lines to a file, creating its folder when needed (blocking)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        file.writelines(f"{line}\n" for line in lines)


def _write_temp_lines(folder: str, lines: Iterable[str]) -> str:
    """Write lines to a new file in folder and return its path (blocking)."""
    fd, path = tempfile.mkstemp(suffix=".in", dir=folder)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.writelines(f"{line}\n" for line in lines)
    return path
