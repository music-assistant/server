"""Reviewed AI-client connection catalogue used by the Connect Wizard."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ConnectionMethod:
    """One ordered setup path for a client."""

    id: str
    label: str
    kind: str
    template: str
    config_path_hint: str
    notes: str = ""
    filename: str = ""
    action: str = "copy"


@dataclass(frozen=True, slots=True)
class ClientSpec:
    """One target client and its recommended-first connection methods."""

    id: str
    label: str
    methods: tuple[ConnectionMethod, ...]


def _json_method(
    method_id: str,
    label: str,
    template: str,
    hint: str,
    filename: str,
    notes: str = "",
) -> ConnectionMethod:
    """Build one downloadable JSON configuration method."""
    return ConnectionMethod(
        id=method_id,
        label=label,
        kind="json",
        template=template,
        config_path_hint=hint,
        notes=notes,
        filename=filename,
        action="download",
    )


def _method(
    method_id: str,
    label: str,
    kind: str,
    template: str,
    hint: str,
    notes: str = "",
    filename: str = "",
) -> ConnectionMethod:
    """Build one copyable command or instruction method."""
    return ConnectionMethod(
        id=method_id,
        label=label,
        kind=kind,
        template=template,
        config_path_hint=hint,
        notes=notes,
        filename=filename,
        action="download" if kind in {"json", "toml", "yaml"} else "copy",
    )


_CLAUDE_CONFIG = (
    "{\n"
    '  "mcpServers": {\n'
    '    "ma": {\n'
    '      "type": "http",\n'
    '      "url": "{{URL}}",\n'
    '      "headers": { "Authorization": "Bearer {{TOKEN}}" }\n'
    "    }\n"
    "  }\n"
    "}"
)
_CURSOR_CONFIG = (
    "{\n"
    '  "mcpServers": {\n'
    '    "ma": {\n'
    '      "url": "{{URL}}",\n'
    '      "headers": { "Authorization": "Bearer {{TOKEN}}" }\n'
    "    }\n"
    "  }\n"
    "}"
)
_OPENCODE_CONFIG = (
    "{\n"
    '  "$schema": "https://opencode.ai/config.json",\n'
    '  "mcp": {\n'
    '    "ma": {\n'
    '      "type": "remote",\n'
    '      "url": "{{URL}}",\n'
    '      "enabled": true,\n'
    '      "oauth": false,\n'
    '      "headers": { "Authorization": "Bearer {{TOKEN}}" }\n'
    "    }\n"
    "  }\n"
    "}"
)
_DEVIN_CONFIG = (
    "{\n"
    '  "mcpServers": {\n'
    '    "ma": {\n'
    '      "url": "{{URL}}",\n'
    '      "transport": "http",\n'
    '      "headers": { "Authorization": "Bearer {{TOKEN}}" }\n'
    "    }\n"
    "  }\n"
    "}"
)
_CASCADE_CONFIG = (
    "{\n"
    '  "mcpServers": {\n'
    '    "ma": {\n'
    '      "serverUrl": "{{URL}}",\n'
    '      "headers": { "Authorization": "Bearer {{TOKEN}}" }\n'
    "    }\n"
    "  }\n"
    "}"
)
_VSCODE_CONFIG = (
    "{\n"
    '  "servers": {\n'
    '    "ma": {\n'
    '      "type": "http",\n'
    '      "url": "{{URL}}",\n'
    '      "headers": { "Authorization": "Bearer {{TOKEN}}" }\n'
    "    }\n"
    "  }\n"
    "}"
)
_COPILOT_CONFIG = (
    "{\n"
    '  "mcpServers": {\n'
    '    "ma": {\n'
    '      "type": "http",\n'
    '      "url": "{{URL}}",\n'
    '      "headers": { "Authorization": "Bearer {{TOKEN}}" },\n'
    '      "tools": ["*"]\n'
    "    }\n"
    "  }\n"
    "}"
)
_GEMINI_CONFIG = (
    "{\n"
    '  "mcpServers": {\n'
    '    "ma": {\n'
    '      "httpUrl": "{{URL}}",\n'
    '      "headers": { "Authorization": "Bearer {{TOKEN}}" }\n'
    "    }\n"
    "  }\n"
    "}"
)
_CLINE_CONFIG = (
    "{\n"
    '  "mcpServers": {\n'
    '    "ma": {\n'
    '      "type": "streamableHttp",\n'
    '      "url": "{{URL}}",\n'
    '      "headers": { "Authorization": "Bearer {{TOKEN}}" },\n'
    '      "disabled": false,\n'
    '      "autoApprove": []\n'
    "    }\n"
    "  }\n"
    "}"
)
_ROO_CODE_CONFIG = (
    "{\n"
    '  "mcpServers": {\n'
    '    "ma": {\n'
    '      "type": "streamable-http",\n'
    '      "url": "{{URL}}",\n'
    '      "headers": { "Authorization": "Bearer {{TOKEN}}" },\n'
    '      "disabled": false,\n'
    '      "alwaysAllow": []\n'
    "    }\n"
    "  }\n"
    "}"
)
_ZED_CONFIG = (
    "{\n"
    '  "context_servers": {\n'
    '    "ma": {\n'
    '      "url": "{{URL}}",\n'
    '      "headers": { "Authorization": "Bearer {{TOKEN}}" }\n'
    "    }\n"
    "  }\n"
    "}"
)
_OPENHANDS_CONFIG = (
    "{\n"
    '  "mcpServers": {\n'
    '    "ma": {\n'
    '      "url": "{{URL}}",\n'
    '      "transport": "http",\n'
    '      "headers": { "Authorization": "Bearer {{TOKEN}}" },\n'
    '      "enabled": true\n'
    "    }\n"
    "  }\n"
    "}"
)


CLIENTS: tuple[ClientSpec, ...] = (
    ClientSpec(
        "claude-code",
        "Claude Code",
        (
            _method(
                "cli",
                "CLI command",
                "shell",
                "claude mcp add --scope user --transport http ma {{URL}} \\\n"
                '  --header "Authorization: Bearer {{TOKEN}}"',
                "Run in any terminal; installs for all projects.",
                filename="add-ma.sh",
            ),
            _json_method(
                "project-config",
                "Project config",
                _CLAUDE_CONFIG,
                ".mcp.json in the project root.",
                ".mcp.json",
                "Claude Code asks for approval when the project is first opened.",
            ),
        ),
    ),
    ClientSpec(
        "cursor",
        "Cursor",
        (
            _json_method(
                "user-config",
                "User config",
                _CURSOR_CONFIG,
                "~/.cursor/mcp.json; available in all projects.",
                "mcp.json",
            ),
            _json_method(
                "project-config",
                "Project config",
                _CURSOR_CONFIG,
                ".cursor/mcp.json in the project root.",
                "mcp.json",
            ),
        ),
    ),
    ClientSpec(
        "opencode",
        "OpenCode",
        (
            _json_method(
                "user-config",
                "User config",
                _OPENCODE_CONFIG,
                "~/.config/opencode/opencode.json; available in all projects.",
                "opencode.json",
                "OAuth is disabled because this method supplies a dedicated MA token.",
            ),
            _json_method(
                "project-config",
                "Project config",
                _OPENCODE_CONFIG,
                "opencode.json in the project root.",
                "opencode.json",
            ),
        ),
    ),
    ClientSpec(
        "windsurf",
        "Windsurf / Devin",
        (
            _json_method(
                "devin-user",
                "Devin user config",
                _DEVIN_CONFIG,
                "~/.config/devin/mcp_config.json; current Windsurf/Devin Local.",
                "mcp_config.json",
            ),
            _json_method(
                "devin-project",
                "Devin project config",
                _DEVIN_CONFIG,
                ".devin/mcp_config.local.json in the project root.",
                "mcp_config.local.json",
                "The local file is private and intended for credentials.",
            ),
            _json_method(
                "legacy-cascade",
                "Legacy Cascade",
                _CASCADE_CONFIG,
                "~/.codeium/windsurf/mcp_config.json.",
                "mcp_config.json",
                "Use only for legacy Cascade; new tabs use Devin Local.",
            ),
        ),
    ),
    ClientSpec(
        "vscode",
        "VS Code (Copilot Chat)",
        (
            _json_method(
                "user-config",
                "User profile",
                _VSCODE_CONFIG,
                "Run 'MCP: Open User Configuration' and merge this entry.",
                "mcp.json",
            ),
            _json_method(
                "workspace-config",
                "Workspace config",
                _VSCODE_CONFIG,
                ".vscode/mcp.json in the workspace.",
                "mcp.json",
            ),
        ),
    ),
    ClientSpec(
        "github-copilot-cli",
        "GitHub Copilot CLI",
        (
            _method(
                "cli",
                "CLI command",
                "shell",
                "copilot mcp add --transport http \\\n"
                '  --header "Authorization: Bearer {{TOKEN}}" \\\n'
                '  --tools "*" ma {{URL}}',
                "Run in any terminal; writes the user configuration.",
                filename="add-ma.sh",
            ),
            _method(
                "interactive",
                "Interactive form",
                "text",
                "/mcp add\nServer Name: ma\nServer Type: HTTP\nURL: {{URL}}\n"
                'HTTP Headers: {"Authorization":"Bearer {{TOKEN}}"}\nTools: *',
                "Enter /mcp add in Copilot CLI, fill these values, then press Ctrl+S.",
            ),
            _json_method(
                "user-config",
                "User config",
                _COPILOT_CONFIG,
                "~/.copilot/mcp-config.json.",
                "mcp-config.json",
            ),
            _json_method(
                "project-config",
                "Project config",
                _COPILOT_CONFIG,
                ".mcp.json in the repository; project servers require folder trust.",
                ".mcp.json",
            ),
        ),
    ),
    ClientSpec(
        "codex-cli",
        "Codex CLI",
        (
            _method(
                "cli",
                "CLI command",
                "shell",
                "export MA_MCP_TOKEN='{{TOKEN}}'\n"
                "codex mcp add ma --url {{URL}} --bearer-token-env-var MA_MCP_TOKEN",
                "Run in a terminal; persist MA_MCP_TOKEN in your secret manager for later sessions.",
                filename="add-ma.sh",
            ),
            _method(
                "user-config",
                "User config",
                "toml",
                '[mcp_servers.ma]\nurl = "{{URL}}"\n'
                'bearer_token_env_var = "MA_MCP_TOKEN"\n'
                "# Set MA_MCP_TOKEN={{TOKEN}} in the Codex environment.",
                "~/.codex/config.toml; merge this table and set the environment variable.",
                filename="config.toml",
            ),
        ),
    ),
    ClientSpec(
        "gemini-cli",
        "Gemini CLI",
        (
            _method(
                "cli",
                "CLI command",
                "shell",
                "gemini mcp add --scope user --transport http \\\n"
                '  --header "Authorization: Bearer {{TOKEN}}" ma {{URL}}',
                "Run in any terminal; installs for the user scope.",
                filename="add-ma.sh",
            ),
            _json_method(
                "user-config",
                "User config",
                _GEMINI_CONFIG,
                "~/.gemini/settings.json.",
                "settings.json",
            ),
            _json_method(
                "project-config",
                "Project config",
                _GEMINI_CONFIG,
                ".gemini/settings.json in the project root.",
                "settings.json",
            ),
        ),
    ),
    ClientSpec(
        "cline",
        "Cline (VS Code)",
        (
            _json_method(
                "user-config",
                "MCP settings JSON",
                _CLINE_CONFIG,
                "Open Cline > MCP Servers > Configure MCP Servers.",
                "cline_mcp_settings.json",
            ),
            _method(
                "cli-wizard",
                "CLI wizard",
                "text",
                "Run: cline mcp\nAdd a remote Streamable HTTP server named ma.\n"
                "URL: {{URL}}\nAuthorization header: Bearer {{TOKEN}}",
                "The interactive wizard writes Cline's shared global MCP configuration.",
            ),
        ),
    ),
    ClientSpec(
        "roo-code",
        "Roo Code",
        (
            _json_method(
                "global-config",
                "Global config",
                _ROO_CODE_CONFIG,
                "Open Roo Code MCP settings and select 'Edit Global MCP'.",
                "mcp_settings.json",
            ),
            _json_method(
                "project-config",
                "Project config",
                _ROO_CODE_CONFIG,
                ".roo/mcp.json in the project root.",
                "mcp.json",
            ),
        ),
    ),
    ClientSpec(
        "zed",
        "Zed Editor",
        (
            _method(
                "settings-ui",
                "Settings UI",
                "text",
                "Open Settings > AI > MCP Servers > Add Remote Server.\n"
                "Name: ma\nURL: {{URL}}\nAuthorization header: Bearer {{TOKEN}}",
                "Use the native MCP Settings UI; edit the generated JSON if headers are not shown.",
            ),
            _json_method(
                "user-config",
                "User config",
                _ZED_CONFIG,
                "Open 'zed: open settings file' and merge context_servers.ma.",
                "settings.json",
            ),
            _json_method(
                "project-config",
                "Project config",
                _ZED_CONFIG,
                ".zed/settings.json in the project root.",
                "settings.json",
            ),
        ),
    ),
    ClientSpec(
        "openclaw",
        "OpenClaw",
        (
            _method(
                "cli",
                "CLI command",
                "shell",
                "openclaw mcp add ma --url {{URL}} \\\n"
                "  --transport streamable-http \\\n"
                '  --header "Authorization: Bearer {{TOKEN}}"',
                "Run in any terminal, then verify with 'openclaw mcp doctor ma --probe'.",
                filename="add-ma.sh",
            ),
            _method(
                "user-config",
                "User config",
                "json",
                '{\n  mcp: {\n    servers: {\n      ma: {\n        url: "{{URL}}",\n'
                '        transport: "streamable-http",\n'
                '        headers: { Authorization: "Bearer {{TOKEN}}" },\n'
                "      },\n    },\n  },\n}",
                "~/.openclaw/openclaw.json (JSON5).",
                filename="openclaw.json",
            ),
        ),
    ),
    ClientSpec(
        "openhands",
        "OpenHands CLI",
        (
            _method(
                "cli",
                "CLI command",
                "shell",
                "openhands mcp add ma --transport http \\\n"
                '  --header "Authorization: Bearer {{TOKEN}}" \\\n'
                "  {{URL}}",
                "Run in any terminal; writes ~/.openhands/mcp.json.",
                filename="add-ma.sh",
            ),
            _json_method(
                "user-config",
                "User config",
                _OPENHANDS_CONFIG,
                "~/.openhands/mcp.json.",
                "mcp.json",
            ),
        ),
    ),
    ClientSpec(
        "hermes",
        "Hermes",
        (
            _method(
                "cli",
                "CLI setup",
                "text",
                "Run: hermes mcp add ma --url {{URL}} --auth header\n"
                "When prompted for the token, enter: {{TOKEN}}\n"
                "Then verify with: hermes mcp test ma",
                "Hermes stores the token in the active profile's .env file.",
            ),
            _method(
                "user-config",
                "User config",
                "yaml",
                'mcp_servers:\n  ma:\n    url: "{{URL}}"\n    headers:\n'
                '      Authorization: "Bearer {{TOKEN}}"',
                "~/.hermes/config.yaml in the active profile.",
                filename="config.yaml",
            ),
            _json_method(
                "desktop-editor",
                "Desktop editor",
                _CURSOR_CONFIG,
                "Open the Hermes Desktop MCP JSON editor and paste this configuration.",
                "mcp.json",
            ),
        ),
    ),
    ClientSpec(
        "custom",
        "Custom",
        (
            _method(
                "parameters",
                "Connection parameters",
                "text",
                "Server name: ma\n"
                "Transport: Streamable HTTP\n"
                "URL: {{URL}}\n"
                "Header name: Authorization\n"
                "Header value: Bearer {{TOKEN}}",
                "Copy these values into any client that supports remote Streamable HTTP MCP.",
                "Use the selected Network or Loopback URL as appropriate for that client.",
            ),
        ),
    ),
)


def lookup_client(client_id: str) -> ClientSpec | None:
    """Return the client matching ``client_id``, or ``None`` if unknown."""
    return next((spec for spec in CLIENTS if spec.id == client_id), None)


def clients_to_json() -> list[dict[str, Any]]:
    """Return the catalogue as plain dictionaries for JSON serialisation."""
    return [asdict(spec) for spec in CLIENTS]
