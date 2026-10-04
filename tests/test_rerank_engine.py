import asyncio
import json

import httpx
import pytest

from research_engine.merge.rerank import rerank

pytestmark = pytest.mark.asyncio


def items(count=4):
    return [{"handle": f"url:{number}", "title": f"Title {number}", "snippet": f"Evidence {number}"} for number in range(count)]


@pytest.mark.parametrize("backend", ["infinity", "jina", "cohere", "voyage"])
async def test_api_backends_reorder_top_n_preserve_tail_and_sources(backend):
    original = items()

    def transport(request):
        payload = json.loads(request.content)
        assert len(payload["documents"]) == 3
        assert payload["top_n"] == 3
        assert request.headers["Authorization"] == "Bearer fixture-key"
        ranking = [{"index": index, "relevance_score": 1 - position / 10} for position, index in enumerate([2, 0, 1])]
        return httpx.Response(200, json={"data" if backend == "voyage" else "results": ranking})

    diagnostics = {}
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await rerank("query", original, {
            "enabled": True, "backend": backend, "api_key": "fixture-key", "top_n": 3, "diagnostics": diagnostics,
        }, client=client)
    assert result == [original[2], original[0], original[1], original[3]]
    assert result[0] is original[2]
    assert diagnostics == {"backend": backend, "status": "applied", "documents": 3}


@pytest.mark.parametrize("ranking", [
    None, [], {}, [{"index": True}], [{"index": -1}], [{"index": 99}], [{"index": 1.0}],
    [{"index": 1, "score": "bad"}], [{"index": 1, "score": True}], ["bad"],
])
async def test_malformed_rankings_preserve_original_order(ranking):
    original = items()
    diagnostics = {}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": ranking}))) as client:
        result = await rerank("query", original, {"enabled": True, "diagnostics": diagnostics}, client=client)
    assert result == original
    assert diagnostics["status"] == "fallback"


async def test_partial_and_duplicate_rankings_never_drop_evidence():
    original = items()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": [{"index": 2}, {"index": 2}]}))) as client:
        result = await rerank("query", original, {"enabled": True}, client=client)
    assert result == [original[2], original[0], original[1], original[3]]


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_http_failure_keeps_original_order(status):
    original = items()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as client:
        assert await rerank("query", original, {"enabled": True}, client=client) == original


async def test_disabled_backend_does_not_make_requests():
    def transport(request):
        raise AssertionError("Disabled reranking must not call its backend")

    original = items()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        assert await rerank("query", original, client=client) == original


async def test_timeout_keeps_original_order():
    async def transport(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={"results": [{"index": 1}]})

    original = items()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        assert await rerank("query", original, {"enabled": True, "timeout_s": 0.01}, client=client) == original


async def test_cancellation_is_propagated():
    def transport(request):
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(asyncio.CancelledError):
            await rerank("query", items(), {"enabled": True}, client=client)


async def test_local_backend_dispatch_and_permutation_validation(monkeypatch):
    import importlib
    module = importlib.import_module("research_engine.merge.rerank")
    monkeypatch.setattr(module, "_local_ranking", lambda *args: [{"index": 1, "score": 0.5}, {"index": 0, "score": 0.2}])
    original = items()
    result = await rerank("query", original, {"enabled": True, "backend": "fastembed", "top_n": 2})
    assert result == [original[1], original[0], original[2], original[3]]


async def test_fastembed_score_iterator_is_sorted_and_validated(monkeypatch):
    import importlib
    module = importlib.import_module("research_engine.merge.rerank")

    class Model:
        def rerank(self, query, documents):
            return iter([0.2, 0.9, 0.3])

    monkeypatch.setattr(module, "_local_model", lambda *args: Model())
    ranking = module._local_ranking("query", ["a", "b", "c"], {})
    assert [result["index"] for result in ranking] == [1, 2, 0]
