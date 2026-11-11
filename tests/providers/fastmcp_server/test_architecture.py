"""Static architecture boundaries for capabilities, policy, and execution."""

from __future__ import annotations

import ast
from pathlib import Path

from music_assistant.providers.fastmcp_server import capabilities as capabilities_module

PROVIDER_ROOT = Path(capabilities_module.__file__).parent


def test_capability_is_the_only_policy_vocabulary() -> None:
    """Reintroducing the former Tag domain or changing the 26 strings must fail."""
    assert [str(item) for item in capabilities_module.Capability] == [
        "query:library",
        "query:queue",
        "query:players",
        "query:metadata",
        "control:playback",
        "control:volume",
        "control:players",
        "control:media",
        "edit:library",
        "edit:queue",
        "edit:playlists",
        "edit:favorites",
        "delete:library",
        "delete:queue",
        "delete:playlists",
        "delete:favorites",
        "debug:inspect",
        "debug:logs",
        "debug:events",
        "debug:providers",
        "config:read",
        "config:write:provider",
        "config:write:core",
        "config:write:player",
        "config:write:secret",
        "system:admin",
    ]
    assert not (PROVIDER_ROOT / "tags.py").exists()


def test_provider_has_no_runtime_assertions() -> None:
    """Authorization and bridge invariants must fail explicitly in optimized Python."""
    offenders: list[str] = []
    for path in PROVIDER_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text())
        if any(isinstance(node, ast.Assert) for node in ast.walk(tree)):
            offenders.append(str(path.relative_to(PROVIDER_ROOT)))

    assert offenders == []


def test_policy_layers_have_no_reverse_imports() -> None:
    """Lower policy layers must never import configuration composition."""
    forbidden = {
        "capabilities.py": {"policy", "policy_config", "config"},
        "policy.py": {"policy_config", "config"},
        "policy_config.py": {"config"},
    }
    offenders: list[str] = []
    for filename, denied in forbidden.items():
        path = PROVIDER_ROOT / filename
        assert path.exists()
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level and node.module in denied:
                offenders.append(f"{filename}->{node.module}")

    assert offenders == []


def test_provider_internal_import_graph_is_acyclic() -> None:
    """Runtime and type-only provider imports must not recreate layer cycles."""
    modules = {
        ".".join(("provider", *path.relative_to(PROVIDER_ROOT).with_suffix("").parts)).removesuffix(
            ".__init__"
        ): path
        for path in PROVIDER_ROOT.rglob("*.py")
    }
    edges: dict[str, set[str]] = {module: set() for module in modules}
    for module, path in modules.items():
        package = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            prefix = package[: len(package) - node.level + 1]
            targets = (
                [".".join((*prefix, *node.module.split(".")))]
                if node.module
                else [".".join((*prefix, alias.name)) for alias in node.names]
            )
            edges[module].update(target for target in targets if target in modules)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str, trail: tuple[str, ...]) -> None:
        if module in visiting:
            start = trail.index(module)
            cycle = " -> ".join((*trail[start:], module))
            raise AssertionError(f"provider import cycle: {cycle}")
        if module in visited:
            return
        visiting.add(module)
        for dependency in sorted(edges[module]):
            visit(dependency, (*trail, module))
        visiting.remove(module)
        visited.add(module)

    for module in sorted(modules):
        visit(module, ())


def test_dynamic_api_compatibility_module_is_gone() -> None:
    """Catalog generation and execution are imported directly, not through a facade."""
    assert not (PROVIDER_ROOT / "dynamic_api.py").exists()


def test_provider_has_no_custom_opentelemetry_layer() -> None:
    """Observability stays deferred without provider-owned OTel code or transport counters."""
    assert not (PROVIDER_ROOT / "telemetry.py").exists()

    forbidden = ("opentelemetry", "from .telemetry", "traced(", "PayloadSizeCounter")
    offenders = [
        str(path.relative_to(PROVIDER_ROOT))
        for path in PROVIDER_ROOT.rglob("*.py")
        if any(marker in path.read_text() for marker in forbidden)
    ]
    assert offenders == []
