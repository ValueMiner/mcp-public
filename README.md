# ValueMiner MCP Desktop Extension

This folder contains the public-ready ValueMiner MCPB package source for Claude
Desktop.

## What it does

- Runs a local stdio MCP server for Claude Desktop.
- Reads the user's ValueMiner API token from Claude extension settings.
- Loads available tools from `GET /api/v6/mcp`.
- Exposes those tools to Claude through MCP.
- Proxies tool calls back to the configured ValueMiner API.

## Configuration

The extension asks users for:

- `ValueMiner API token`: a bearer token for their ValueMiner account.
- `ValueMiner API URL`: the API base URL.
- `ValueMiner BA header`: the business-account header used when loading tools.

No secrets are committed to the repository or bundled into the `.mcpb` file.

## Build

From this directory:

```bash
mcpb pack
```

That creates `valueminer.mcpb`, which is the file to upload to Anthropic's MCP
server submission form.

If the MCPB CLI is not installed, install it from the official MCPB package
instructions, then run the same command again.

## Local smoke test

From this directory:

```bash
VALUEMINER_API_TOKEN=your-token uv run python server/mcp_server.py
```

The process should wait for MCP stdio messages. Claude Desktop normally starts
and communicates with it automatically.
