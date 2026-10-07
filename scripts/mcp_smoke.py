#!/usr/bin/env python3
"""Check a running endpoint through the actual MCP HTTP transport."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


def read_token(path: Path | None) -> str:
    if path is None:
        token = os.environ.get("RESEARCH_ENGINE_TOKEN", "").strip()
    else:
        value = path.read_text(encoding="utf-8").strip()
        if value.startswith("{"):
            bootstrap = json.loads(value)
            token = next(
                (bootstrap[key] for key in ("client_token", "mcp_token", "token")
                 if isinstance(bootstrap.get(key), str)),
                "",
            )
        else:
            token = value
    if not token or "\n" in token or "\r" in token:
        raise ValueError("Provide a token file or set RESEARCH_ENGINE_TOKEN")
    return token


async def smoke(args: argparse.Namespace, token: str) -> dict:
    url = urlsplit(args.url)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        raise ValueError("--url must be an absolute HTTP(S) URL without credentials")
    health_url = urlunsplit((url.scheme, url.netloc, "/health", "", ""))
    async with httpx.AsyncClient(timeout=args.timeout, follow_redirects=False) as http:
        health = await http.get(health_url)
        health.raise_for_status()
        health_data = health.json()
    transport = StreamableHttpTransport(args.url, headers={"Authorization": f"Bearer {token}"})
    async with Client(transport, timeout=args.timeout, init_timeout=args.timeout) as client:
        tools = await client.list_tools()
        names = sorted(tool.name for tool in tools)
        missing = sorted(set(args.expect) - set(names))
        if missing:
            raise RuntimeError("Expected tools missing: " + ", ".join(missing))
        result = {"url": args.url, "health": health_data, "tools": names}
        if args.tool:
            arguments = json.loads(args.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("--arguments must be a JSON object")
            call = await client.call_tool(args.tool, arguments)
            data = json.loads(call.content[0].text)
            result["call"] = {"tool": args.tool, "data": data}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--expect", action="append", default=["web_search", "web_read"])
    parser.add_argument("--tool", help="Optionally call one tool after discovery")
    parser.add_argument("--arguments", default="{}", help="JSON arguments for --tool")
    args = parser.parse_args()
    token = ""
    try:
        token = read_token(args.token_file)
        result = asyncio.run(smoke(args, token))
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        message = str(exc).replace(token, "[redacted]") if token else str(exc)
        print(f"MCP smoke failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
