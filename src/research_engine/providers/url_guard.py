"""Fail-closed public-target guard, adapted from research-mcp's _url_guard.py."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from research_engine.providers.base import ErrorKind, ProviderError


@dataclass(frozen=True)
class SafeTarget:
    url: str
    hostname: str
    addresses: tuple[str, ...]


async def validate_url(url: str) -> SafeTarget:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Public HTTP(S) URLs without credentials are required")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        hostname = parsed.hostname.encode("idna").decode("ascii")
        try:
            addresses = (str(ipaddress.ip_address(hostname)),)
        except ValueError:
            resolved = await asyncio.to_thread(socket.getaddrinfo, hostname, port, 0, socket.SOCK_STREAM)
            addresses = tuple(sorted({item[4][0] for item in resolved}))
        if not addresses:
            raise ValueError("Target DNS returned no addresses")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global or ip.is_multicast:
                raise ValueError("Private, reserved, loopback and tailnet addresses are denied")
        normalized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
        return SafeTarget(normalized, hostname, addresses)
    except (ValueError, UnicodeError, socket.gaierror, OSError) as error:
        raise ProviderError(ErrorKind.TARGET, "Target URL is unavailable or disallowed", block_capability=False) from error


async def safe_fetch(client: httpx.AsyncClient, url: str, *, deadline: float,
                     max_bytes: int = 16 * 1024 * 1024, max_redirects: int = 5,
                     method: str = "GET", **kwargs) -> httpx.Response:
    """Pin the validated address for each connection and revalidate every redirect."""
    import time
    current = url
    for _ in range(max_redirects + 1):
        async with asyncio.timeout_at(deadline):
            target = await validate_url(current)
            parsed = urlsplit(target.url)
            address = target.addresses[0]
            host = f"[{address}]" if ":" in address else address
            if parsed.port is not None:
                host += f":{parsed.port}"
            pinned = urlunsplit((parsed.scheme, host, parsed.path, parsed.query, ""))
            headers = dict(kwargs.pop("headers", {}))
            headers["Host"] = parsed.netloc
            extensions = {**kwargs.pop("extensions", {}), "sni_hostname": target.hostname}
            try:
                async with client.stream(method, pinned, headers=headers, extensions=extensions,
                                         follow_redirects=False, timeout=max(0.01, deadline - time.monotonic()),
                                         **kwargs) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ProviderError(ErrorKind.TARGET, "Redirect has no location", block_capability=False)
                        current = urljoin(target.url, location)
                        # Credentials never follow redirects to other targets.
                        method = "GET" if response.status_code == 303 else method
                        continue
                    length = response.headers.get("content-length")
                    if length and length.isdigit() and int(length) > max_bytes:
                        raise ProviderError(ErrorKind.TARGET, "Source exceeds the download limit", block_capability=False)
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise ProviderError(ErrorKind.TARGET, "Source exceeds the download limit", block_capability=False)
                        chunks.append(chunk)
                    return httpx.Response(response.status_code, headers=response.headers, content=b"".join(chunks),
                                          request=httpx.Request(method, target.url))
            except httpx.RequestError as error:
                raise ProviderError(ErrorKind.TRANSIENT, "Source connection failed") from error
    raise ProviderError(ErrorKind.TARGET, "Source exceeded the redirect limit", block_capability=False)
