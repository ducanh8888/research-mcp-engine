"""Bounded PDF downloads with an isolated, killable parser subprocess."""

from __future__ import annotations

import asyncio
import json
import sys

from research_engine.providers.base import ErrorKind, ProviderError
from research_engine.providers.url_guard import safe_fetch
from research_engine.server.schemas import Document


async def read_pdf(ctx, url: str) -> Document:
    response = await safe_fetch(ctx.client, url, deadline=ctx.deadline)
    if response.status_code == 404:
        raise ProviderError(ErrorKind.TARGET, "PDF target was not found", status_code=404, block_capability=False)
    if response.status_code != 200:
        raise ProviderError(ErrorKind.TRANSIENT, f"PDF retrieval failed (HTTP {response.status_code})")
    if not response.content.startswith(b"%PDF"):
        raise ProviderError(ErrorKind.TARGET, "Source is not a PDF", block_capability=False)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "research_engine.providers.pdf", "--extract",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(min(15, ctx.remaining())):
            stdout, _ = await process.communicate(response.content)
        if process.returncode != 0:
            raise ProviderError(ErrorKind.TARGET, "PDF could not be parsed", block_capability=False)
        parsed = json.loads(stdout)
        return Document(url=url, text=parsed["text"], source=ctx.provider, kind="fulltext")
    except TimeoutError as error:
        raise ProviderError(ErrorKind.TRANSIENT, "PDF parser exceeded its deadline") from error
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def _extract() -> None:
    import io
    from pypdf import PdfReader
    data = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
    if len(data) > 16 * 1024 * 1024:
        raise ValueError("PDF exceeds size limit")
    reader = PdfReader(io.BytesIO(data), strict=True)
    parts, size = [], 0
    for page in list(reader.pages)[:150]:
        text = page.extract_text() or ""
        text = text[:max(0, 1_000_000 - size)]
        parts.append(text)
        size += len(text)
        if size >= 1_000_000:
            break
    print(json.dumps({"text": "\n\n".join(parts), "pages": len(parts)}))


if __name__ == "__main__":
    _extract()
