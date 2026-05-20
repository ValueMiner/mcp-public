"""ValueMiner MCP Desktop Extension server.

This server is designed for Claude Desktop MCPB packaging. It runs over stdio,
loads the authenticated user's available ValueMiner tools from the ValueMiner
API, and proxies MCP tool calls back to the corresponding API endpoints.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server


SERVER_NAME = "valueminer"
SERVER_VERSION = "1.0.0"

VALUEMINER_API_URL = os.getenv(
    "VALUEMINER_API_URL",
    os.getenv("GO_API_URL", "https://apigo.develop.valueminer.eu"),
).rstrip("/")
VALUEMINER_API_TOKEN = os.getenv(
    "VALUEMINER_API_TOKEN",
    os.getenv("CLIENT_SECRET_TOKEN", ""),
).strip()
VALUEMINER_BA_HEADER = os.getenv("VALUEMINER_BA_HEADER", os.getenv("BA_HEADER", "767"))
TOOL_CACHE_TTL_SECONDS = int(os.getenv("VALUEMINER_TOOL_CACHE_TTL", "300"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("VALUEMINER_HTTP_TIMEOUT", "120"))

app = Server(SERVER_NAME)

_http_client: httpx.AsyncClient | None = None
_tool_cache: dict[str, "ValueMinerTool"] = {}
_tool_cache_loaded_at = 0.0
_tool_cache_key = ""
_tool_reload_lock = asyncio.Lock()


@dataclass(frozen=True)
class ValueMinerTool:
    """Internal representation of one API-provided ValueMiner tool."""

    name: str
    api_name: str
    description: str
    endpoint: str
    method: str
    input_schema: dict[str, Any]


def _log(message: str) -> None:
    """Write diagnostics to stderr so stdout remains valid MCP JSON-RPC."""

    print(f"[valueminer-mcp] {message}", file=sys.stderr, flush=True)


def _http() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=HTTP_TIMEOUT_SECONDS,
                write=10.0,
                pool=10.0,
            ),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=8),
            headers={"User-Agent": f"ValueMiner-MCP/{SERVER_VERSION}"},
        )
    return _http_client


def _cache_key() -> str:
    token_hash = hashlib.sha256(VALUEMINER_API_TOKEN.encode()).hexdigest()[:16]
    return f"{VALUEMINER_API_URL}:{VALUEMINER_BA_HEADER}:{token_hash}"


def _strip_html(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"<[^<]+>", "", value).strip()


def _safe_tool_name(name: str, used_names: set[str]) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "valueminer_tool"

    candidate = cleaned
    counter = 2
    while candidate in used_names:
        candidate = f"{cleaned}_{counter}"
        counter += 1
    used_names.add(candidate)
    return candidate


def _default_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Optional text, prompt, or query content for the ValueMiner tool.",
            },
            "nodeId": {
                "type": "integer",
                "description": "Optional ValueMiner node identifier.",
            },
        },
        "additionalProperties": True,
    }


def _normalize_input_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return _default_input_schema()

    normalized = dict(schema)
    normalized.setdefault("type", "object")
    normalized.setdefault("properties", {})

    if not isinstance(normalized["properties"], dict):
        normalized["properties"] = {}

    normalized.setdefault("additionalProperties", True)
    return normalized


def _payload_defaults_from_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Extract non-secret default payload values from the tool schema, if any."""

    defaults: dict[str, Any] = {}
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return defaults

    for key, value in properties.items():
        if isinstance(value, dict) and "default" in value:
            defaults[key] = value["default"]
        elif not isinstance(value, dict):
            defaults[key] = value
    return defaults


def _tool_annotations(method: str) -> Any | None:
    annotation_model = getattr(types, "ToolAnnotations", None)
    if annotation_model is None:
        return None

    http_method = method.upper()
    kwargs = {
        "openWorldHint": True,
        "readOnlyHint": http_method == "GET",
        "destructiveHint": http_method in {"DELETE", "PATCH", "PUT"},
        "idempotentHint": http_method in {"GET", "PUT", "DELETE"},
    }

    try:
        return annotation_model(**kwargs)
    except TypeError:
        return None


def _mcp_tool(tool: ValueMinerTool) -> types.Tool:
    kwargs: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
    }

    annotations = _tool_annotations(tool.method)
    if annotations is not None:
        kwargs["annotations"] = annotations

    try:
        return types.Tool(**kwargs)
    except TypeError:
        kwargs.pop("annotations", None)
        return types.Tool(**kwargs)


def _text_result(payload: Any, *, is_error: bool = False) -> types.CallToolResult:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    structured = payload if isinstance(payload, dict) else {"result": payload}
    kwargs: dict[str, Any] = {
        "content": [types.TextContent(type="text", text=text)],
        "isError": is_error,
    }

    if not is_error:
        kwargs["structuredContent"] = structured

    try:
        return types.CallToolResult(**kwargs)
    except TypeError:
        kwargs.pop("structuredContent", None)
        return types.CallToolResult(**kwargs)


async def _fetch_tools() -> dict[str, ValueMinerTool]:
    if not VALUEMINER_API_TOKEN:
        raise RuntimeError(
            "ValueMiner API token is not configured. Set VALUEMINER_API_TOKEN in the extension settings."
        )

    client = _http()
    response = await client.get(
        f"{VALUEMINER_API_URL}/api/v6/mcp",
        headers={
            "Authorization": f"Bearer {VALUEMINER_API_TOKEN}",
            "BA": VALUEMINER_BA_HEADER,
        },
    )
    response.raise_for_status()

    raw_tools = response.json().get("tools", {}).get("data", [])
    if not isinstance(raw_tools, list):
        raise RuntimeError("ValueMiner API returned an invalid tools payload.")

    used_names: set[str] = set()
    tools: dict[str, ValueMinerTool] = {}

    for item in raw_tools:
        if not isinstance(item, dict):
            continue

        endpoint = item.get("endpoint")
        if not endpoint:
            continue

        api_name = str(item.get("name") or "ValueMiner tool")
        safe_name = _safe_tool_name(api_name, used_names)
        description = _strip_html(item.get("description")) or "Run a ValueMiner tool."
        input_schema = _normalize_input_schema(item.get("inputSchema"))
        method = str(item.get("method") or "POST").upper()

        tools[safe_name] = ValueMinerTool(
            name=safe_name,
            api_name=api_name,
            description=description,
            endpoint=str(endpoint),
            method=method,
            input_schema=input_schema,
        )

    return tools


async def _ensure_tools_loaded(*, force: bool = False) -> dict[str, ValueMinerTool]:
    global _tool_cache, _tool_cache_loaded_at, _tool_cache_key

    now = time.monotonic()
    current_key = _cache_key()
    cache_is_warm = (
        _tool_cache
        and _tool_cache_key == current_key
        and now - _tool_cache_loaded_at < TOOL_CACHE_TTL_SECONDS
    )
    if cache_is_warm and not force:
        return _tool_cache

    async with _tool_reload_lock:
        now = time.monotonic()
        cache_is_warm = (
            _tool_cache
            and _tool_cache_key == current_key
            and now - _tool_cache_loaded_at < TOOL_CACHE_TTL_SECONDS
        )
        if cache_is_warm and not force:
            return _tool_cache

        _tool_cache = await _fetch_tools()
        _tool_cache_loaded_at = time.monotonic()
        _tool_cache_key = current_key
        _log(f"loaded {len(_tool_cache)} ValueMiner tools")
        return _tool_cache


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    try:
        tools = await _ensure_tools_loaded()
    except Exception as exc:
        _log(f"could not load tools: {exc}")
        return [
            types.Tool(
                name="valueminer_configuration_status",
                description="Check whether the ValueMiner MCP extension is configured correctly.",
                inputSchema={"type": "object", "properties": {}},
            )
        ]

    return [_mcp_tool(tool) for tool in tools.values()]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
    if name == "valueminer_configuration_status":
        try:
            await _ensure_tools_loaded(force=True)
            return _text_result({"status": "ok", "message": "ValueMiner tools loaded successfully."})
        except Exception as exc:
            return _text_result({"status": "error", "message": str(exc)}, is_error=True)

    try:
        tools = await _ensure_tools_loaded()
        tool = tools[name]
    except KeyError:
        return _text_result(f"Unknown ValueMiner tool: {name}", is_error=True)
    except Exception as exc:
        _log(f"tool cache failed: {exc}")
        return _text_result(str(exc), is_error=True)

    args = arguments or {}
    if not isinstance(args, dict):
        return _text_result("Tool arguments must be a JSON object.", is_error=True)

    payload = _payload_defaults_from_schema(tool.input_schema)
    payload.update(args)

    endpoint = tool.endpoint if tool.endpoint.startswith("/") else f"/{tool.endpoint}"

    try:
        response = await _http().request(
            method=tool.method,
            url=f"{VALUEMINER_API_URL}{endpoint}",
            json=payload if tool.method not in {"GET", "HEAD"} else None,
            params=payload if tool.method in {"GET", "HEAD"} else None,
            headers={"Authorization": f"Bearer {VALUEMINER_API_TOKEN}"},
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:2000]
        _log(f"{tool.name} returned HTTP {exc.response.status_code}: {body}")
        return _text_result(
            {
                "error": "ValueMiner API request failed.",
                "tool": tool.api_name,
                "status_code": exc.response.status_code,
                "details": body,
            },
            is_error=True,
        )
    except Exception as exc:
        _log(f"{tool.name} request failed: {exc}")
        return _text_result(
            {"error": "ValueMiner API request failed.", "tool": tool.api_name, "details": str(exc)},
            is_error=True,
        )

    if not response.content:
        return _text_result({"status": "ok"})

    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        return _text_result(response.json())

    return _text_result(response.text)


async def main() -> None:
    global _http_client
    try:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name=SERVER_NAME,
                    server_version=SERVER_VERSION,
                    capabilities=app.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        if _http_client is not None and not _http_client.is_closed:
            await _http_client.aclose()
            _http_client = None


if __name__ == "__main__":
    asyncio.run(main())
