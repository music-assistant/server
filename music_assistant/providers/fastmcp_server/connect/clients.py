"""
AI-client catalogue used by the Connect Wizard.

Each :class:`ClientSpec` is rendered into a copy-paste config snippet by the
wizard's JavaScript: ``{{URL}}`` is replaced with the chosen MCP endpoint URL
and ``{{TOKEN}}`` with a freshly minted per-client token. The catalogue is
serialised to JSON via :func:`clients_to_json` and embedded into ``/connect/info``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ClientSpec:
    """
    Renderable description of a single AI client.

    :param id: Stable identifier used in API calls and as the per-client token name suffix.
    :param label: Human-readable name shown in the wizard tab and in token names.
    :param kind: Snippet syntax — ``json`` / ``shell`` / ``toml`` / ``yaml``.
    :param template: Snippet body with ``{{URL}}`` and ``{{TOKEN}}`` placeholders.
    :param config_path_hint: Where the user should paste the snippet (for the UI hint line).
    :param notes: Optional extra advice (transport quirks, OS gotchas).
    :param filename: Suggested download filename for the snippet.
    """

    id: str
    label: str
    kind: str
    template: str
    config_path_hint: str
    notes: str = ""
    filename: str = ""


# Templates substitute ``{{TOKEN}}`` / ``{{URL}}`` by plain string replacement
# (page.py), with no shell/JSON/YAML escaping. That is safe only because the
# token is an MA-minted JWT (alphabet ``[A-Za-z0-9_-.]``) and the URL is the
# server's own base URL — neither can contain a quote, backslash, ``$`` or
# whitespace. If MA ever issues opaque tokens from a wider alphabet, the
# shell (OpenClaw) and YAML (Hermes) templates would need escaping.
CLIENTS: tuple[ClientSpec, ...] = (
    ClientSpec(
        id="claude-code",
        label="Claude Code",
        kind="shell",
        template=(
            "claude mcp add ma {{URL}} \\\n"
            "  --transport http \\\n"
            '  --header "Authorization: Bearer {{TOKEN}}"'
        ),
        config_path_hint="Run this in any terminal.",
        filename="add-ma.sh",
    ),
    ClientSpec(
        id="claude-desktop",
        label="Claude Desktop",
        kind="json",
        template=(
            "{\n"
            '  "mcpServers": {\n'
            '    "ma": {\n'
            '      "url": "{{URL}}",\n'
            '      "headers": { "Authorization": "Bearer {{TOKEN}}" }\n'
            "    }\n"
            "  }\n"
            "}"
        ),
        config_path_hint=(
            "macOS: ~/Library/Application Support/Claude/claude_desktop_config.json · "
            "Windows: %APPDATA%/Claude/claude_desktop_config.json"
        ),
        notes="Requires Claude Desktop with native HTTP transport (≥ 0.10).",
        filename="claude_desktop_config.json",
    ),
    ClientSpec(
        id="cursor",
        label="Cursor",
        kind="json",
        template=(
            "{\n"
            '  "mcpServers": {\n'
            '    "ma": {\n'
            '      "url": "{{URL}}",\n'
            '      "headers": { "Authorization": "Bearer {{TOKEN}}" }\n'
            "    }\n"
            "  }\n"
            "}"
        ),
        config_path_hint="~/.cursor/mcp.json (global) or .cursor/mcp.json (project).",
        notes="Use the 'Add to Cursor' button for one-click install.",
        filename="mcp.json",
    ),
    ClientSpec(
        id="windsurf",
        label="Windsurf",
        kind="json",
        template=(
            "{\n"
            '  "mcpServers": {\n'
            '    "ma": {\n'
            '      "serverUrl": "{{URL}}",\n'
            '      "headers": { "Authorization": "Bearer {{TOKEN}}" }\n'
            "    }\n"
            "  }\n"
            "}"
        ),
        config_path_hint="~/.codeium/windsurf/mcp_config.json",
        filename="mcp_config.json",
    ),
    ClientSpec(
        id="vscode",
        label="VSCode (Copilot Chat)",
        kind="json",
        template=(
            "{\n"
            '  "servers": {\n'
            '    "ma": {\n'
            '      "type": "http",\n'
            '      "url": "{{URL}}",\n'
            '      "headers": { "Authorization": "Bearer {{TOKEN}}" }\n'
            "    }\n"
            "  }\n"
            "}"
        ),
        config_path_hint=".vscode/mcp.json (workspace) or User Settings JSON.",
        filename="mcp.json",
    ),
    ClientSpec(
        id="chatgpt",
        label="ChatGPT (Connectors)",
        kind="shell",
        template=(
            "# Settings → Connectors → Add custom MCP\n"
            "URL:    {{URL}}\n"
            "Auth:   Bearer {{TOKEN}}\n"
            "# ChatGPT requires a publicly reachable HTTPS URL\n"
            "# (Cloudflare Tunnel / Tailscale Funnel / nginx + Let's Encrypt)."
        ),
        config_path_hint="UI only — no file to paste.",
        notes="Public HTTPS required.",
        filename="chatgpt-mcp.txt",
    ),
    ClientSpec(
        id="codex-cli",
        label="Codex CLI",
        kind="toml",
        template=(
            "[mcp_servers.ma]\n"
            'url = "{{URL}}"\n'
            "[mcp_servers.ma.http_headers]\n"
            'Authorization = "Bearer {{TOKEN}}"'
        ),
        config_path_hint="~/.codex/config.toml",
        notes=(
            "Codex's streamable_http transport reads custom headers from "
            "`http_headers` (not `headers`)."
        ),
        filename="config.toml",
    ),
    ClientSpec(
        id="gemini-cli",
        label="Gemini CLI",
        kind="json",
        template=(
            "{\n"
            '  "mcpServers": {\n'
            '    "ma": {\n'
            '      "httpUrl": "{{URL}}",\n'
            '      "headers": { "Authorization": "Bearer {{TOKEN}}" }\n'
            "    }\n"
            "  }\n"
            "}"
        ),
        config_path_hint="~/.gemini/settings.json",
        filename="settings.json",
    ),
    ClientSpec(
        id="cline",
        label="Cline (VSCode)",
        kind="json",
        template=(
            "{\n"
            '  "mcpServers": {\n'
            '    "ma": {\n'
            '      "url": "{{URL}}",\n'
            '      "headers": { "Authorization": "Bearer {{TOKEN}}" }\n'
            "    }\n"
            "  }\n"
            "}"
        ),
        config_path_hint='VSCode command palette → "Cline: Open MCP Settings".',
        filename="cline_mcp_settings.json",
    ),
    ClientSpec(
        id="zed",
        label="Zed Editor",
        kind="json",
        template=(
            "{\n"
            '  "context_servers": {\n'
            '    "ma": {\n'
            '      "url": "{{URL}}",\n'
            '      "headers": { "Authorization": "Bearer {{TOKEN}}" }\n'
            "    }\n"
            "  }\n"
            "}"
        ),
        config_path_hint="~/.config/zed/settings.json",
        notes="Requires a recent Zed build with native remote-MCP support.",
        filename="settings.json",
    ),
    ClientSpec(
        id="openclaw",
        label="OpenClaw",
        kind="shell",
        template=(
            "openclaw mcp set ma "
            '\'{"url":"{{URL}}","transport":"streamable-http",'
            '"headers":{"Authorization":"Bearer {{TOKEN}}"}}\''
        ),
        config_path_hint="Run this in any terminal (OpenClaw CLI).",
        notes=(
            "Needs an OpenClaw build whose bundle-mcp forwards custom headers "
            "over streamable-http (the fix for issue #65590, Apr 2026)."
        ),
        filename="add-ma.sh",
    ),
    ClientSpec(
        id="hermes",
        label="Hermes",
        kind="yaml",
        template=(
            "mcp_servers:\n"
            "  ma:\n"
            '    url: "{{URL}}"\n'
            "    headers:\n"
            '      Authorization: "Bearer {{TOKEN}}"'
        ),
        config_path_hint="~/.hermes/config.yaml",
        notes=(
            "Hermes also supports per-server tool include/exclude under a "
            "`tools:` key and OAuth via `auth: oauth` (uses the server's "
            "RFC 9728 metadata)."
        ),
        filename="config.yaml",
    ),
)


def lookup_client(client_id: str) -> ClientSpec | None:
    """Return the :class:`ClientSpec` matching ``client_id``, or ``None`` if unknown."""
    for spec in CLIENTS:
        if spec.id == client_id:
            return spec
    return None


def clients_to_json() -> list[dict[str, str]]:
    """Return the catalogue as a list of plain dicts suitable for JSON serialisation."""
    return [asdict(spec) for spec in CLIENTS]
