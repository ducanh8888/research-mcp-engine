"""Provider contracts and real failure shapes, without live credentials or network."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest

from research_engine.providers.base import Capability, CallContext, ErrorKind, ProviderError
from research_engine.providers.web import PROVIDERS
from research_engine.providers.web.brave import BraveProvider
from research_engine.providers.web.duckduckgo import DuckDuckGoProvider, parse_results
from research_engine.providers.web.exa import ExaProvider
from research_engine.providers.web.firecrawl import FirecrawlProvider
from research_engine.providers.web.jina import JinaProvider
from research_engine.providers.web.serper import SerperProvider
from research_engine.providers.web.tavily import TavilyProvider
from research_engine.providers.web.trafilatura import TrafilaturaProvider


class Context(CallContext):
    def __init__(self, client: httpx.AsyncClient, provider: str, options: dict[str, Any] | None = None):
        super().__init__(client=client, credentials={"api_key": "unit-key"}, account_id=7,
                         provider=provider, deadline=time.monotonic() + 30, options=options or {})
        self.calls: list[dict[str, Any]] = []
        self.validated: list[str] = []

    async def validate_url(self, url: str):
        self.validated.append(url)
        if "127.0.0.1" in url:
            raise ProviderError(ErrorKind.TARGET, "Private target rejected", block_capability=False)

    async def request(self, method: str, url: str, **kwargs: Any):
        self.calls.append({"method": method, "url": url, **kwargs})
        kwargs.pop("retries", None)
        kwargs.pop("max_bytes", None)
        return await self.client.request(method, url, **kwargs)


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_builtin_web_instances():
    assert {provider.name for provider in PROVIDERS} == {
        "exa", "firecrawl", "tavily", "brave", "serper", "jina", "trafilatura", "duckduckgo"
    }
    assert all(not isinstance(provider, type) for provider in PROVIDERS)


@pytest.mark.parametrize(("provider", "payload"), [
    (ExaProvider(), {"results": [{"title": "Missing URL"}]}),
    (FirecrawlProvider(), {"success": True, "data": {"web": [{"title": "Missing URL"}]}}),
    (TavilyProvider(), {"results": [{"title": "Missing URL"}]}),
    (BraveProvider(), {"web": {"results": [{"title": "Missing URL"}]}}),
    (SerperProvider(), {"organic": [{"title": "Missing URL"}]}),
])
async def test_nonempty_results_without_source_urls_are_not_false_empty(provider, payload):
    async with client_for(lambda request: httpx.Response(200, json=payload)) as client:
        with pytest.raises(ProviderError) as caught:
            await provider.call(Capability.WEB_SEARCH, {"query": "retrieval"}, Context(client, provider.name))
    assert caught.value.kind == ErrorKind.TRANSIENT


async def test_exa_search_payload_and_provenance():
    def handler(request):
        assert request.url.path == "/search"
        assert request.headers["x-api-key"] == "unit-key"
        body = json.loads(request.content)
        assert body["type"] == "fast"
        assert body["contents"] == {"highlights": True}
        assert body["includeDomains"] == ["example.com"]
        assert body["numResults"] == 2
        return httpx.Response(200, json={"results": [{"title": "Source", "url": "https://example.com/a",
                               "highlights": ["First excerpt", "Second excerpt"], "author": "A. Author",
                               "publishedDate": "2026-09-01"}], "costDollars": {"total": 0.005}})
    async with client_for(handler) as client:
        result = await ExaProvider().call(Capability.WEB_SEARCH,
                                         {"query": "retrieval", "limit": 2, "include_domains": ["example.com"]},
                                         Context(client, "exa", {"search_type": "fast"}))
    hit = result.hits[0]
    assert (hit.provider, hit.account, hit.rank, hit.year) == ("exa", 7, 1, 2026)
    assert hit.authors == ["A. Author"]
    assert hit.snippet == "First excerpt\nSecond excerpt"
    assert hit.raw["highlights"] == ["First excerpt", "Second excerpt"]
    assert result.usage == {"total": 0.005}


async def test_exa_removed_category_is_request_specific():
    async with client_for(lambda request: pytest.fail("Unexpected request")) as client:
        ctx = Context(client, "exa", {"category": "github"})
        with pytest.raises(ProviderError) as caught:
            await ExaProvider().call(Capability.DEVELOPER_SEARCH, {"query": "retrieval"}, ctx)
    assert caught.value.kind == ErrorKind.BAD_REQUEST
    assert caught.value.block_capability is False


async def test_exa_contents_has_freshness_and_source_text():
    def handler(request):
        assert json.loads(request.content) == {"urls": ["https://example.com/a"], "text": True,
                                               "maxAgeHours": -1.0}
        return httpx.Response(200, json={"results": [{"url": "https://example.com/a", "text": "Source text"}]})
    async with client_for(handler) as client:
        ctx = Context(client, "exa", {"max_age_hours": -1})
        result = await ExaProvider().call(Capability.WEB_READ, {"url": "https://example.com/a"}, ctx)
    assert ctx.validated == ["https://example.com/a"]
    assert result.document.text == "Source text"
    assert result.document.kind == "page"


@pytest.mark.parametrize("provider", [ExaProvider(), FirecrawlProvider(), TavilyProvider(),
                                    BraveProvider(), SerperProvider()])
@pytest.mark.parametrize(("status", "body", "kind"), [
    (401, {"error": "invalid api key"}, ErrorKind.AUTH),
    (402, {"error": "payment required"}, ErrorKind.EXHAUSTED),
    (429, {"error": "request rate exceeded"}, ErrorKind.RATE_LIMITED),
    (503, {"error": "service overloaded"}, ErrorKind.TRANSIENT),
])
async def test_http_failure_classification(provider, status, body, kind):
    async with client_for(lambda request: httpx.Response(status, json=body, headers={"Retry-After": "12"})) as client:
        with pytest.raises(ProviderError) as caught:
            await provider.call(Capability.WEB_SEARCH, {"query": "retrieval"}, Context(client, provider.name))
    assert caught.value.kind == kind
    assert caught.value.retry_after == 12


@pytest.mark.parametrize("status", [432, 433])
async def test_tavily_custom_credit_limits(status):
    async with client_for(lambda request: httpx.Response(status, json={"detail": {"error": "limit exceeded"}})) as client:
        with pytest.raises(ProviderError) as caught:
            await TavilyProvider().call(Capability.WEB_SEARCH, {"query": "retrieval"}, Context(client, "tavily"))
    assert caught.value.kind == ErrorKind.EXHAUSTED


async def test_tavily_legacy_budget_429_is_not_request_throttling():
    async with client_for(lambda request: httpx.Response(429, json={"detail": {"error": "Usage limit exceeded"}})) as client:
        with pytest.raises(ProviderError) as caught:
            await TavilyProvider().call(Capability.WEB_SEARCH, {"query": "retrieval"}, Context(client, "tavily"))
    assert caught.value.kind == ErrorKind.EXHAUSTED


async def test_error_message_redacts_reflected_credentials():
    async with client_for(lambda request: httpx.Response(401, json={"error": "Invalid key unit-key"})) as client:
        with pytest.raises(ProviderError) as caught:
            await ExaProvider().call(Capability.WEB_SEARCH, {"query": "retrieval"}, Context(client, "exa"))
    assert "unit-key" not in str(caught.value)


async def test_ddg_invalid_page_does_not_start_local_cooldown():
    provider = DuckDuckGoProvider()
    async with client_for(lambda request: httpx.Response(200, text=ddg_html())) as client:
        ctx = Context(client, "duckduckgo")
        with pytest.raises(ProviderError) as caught:
            await provider.call(Capability.WEB_SEARCH, {"query": "retrieval", "page": 2}, ctx)
        result = await provider.call(Capability.WEB_SEARCH, {"query": "retrieval"}, ctx)
    assert caught.value.kind == ErrorKind.BAD_REQUEST and not caught.value.block_capability
    assert len(ctx.calls) == 1 and len(result.hits) == 1


async def test_serper_credit_error_on_400():
    async with client_for(lambda request: httpx.Response(400, json={"message": "Not enough credits"})) as client:
        with pytest.raises(ProviderError) as caught:
            await SerperProvider().call(Capability.WEB_SEARCH, {"query": "retrieval"}, Context(client, "serper"))
    assert caught.value.kind == ErrorKind.EXHAUSTED


async def test_brave_monthly_quota_and_request_rate_differ():
    async with client_for(lambda request: httpx.Response(429, json={"error": {
            "code": "SUBSCRIPTION_QUOTA_LIMIT_REACHED"}})) as client:
        with pytest.raises(ProviderError) as caught:
            await BraveProvider().call(Capability.WEB_SEARCH, {"query": "retrieval"}, Context(client, "brave"))
    assert caught.value.kind == ErrorKind.EXHAUSTED


@pytest.mark.parametrize(("used", "kind"), [(2000, ErrorKind.EXHAUSTED), (3, ErrorKind.RATE_LIMITED)])
async def test_brave_quota_counters_distinguish_monthly_budget_from_burst(used, kind):
    body = {"error": {"code": "RATE_LIMITED", "detail": "Rate limit exceeded",
            "meta": {"quota_current": used, "quota_limit": 2000, "rate_current": 1, "rate_limit": 1}},
            "query": "monthly pricing"}
    async with client_for(lambda request: httpx.Response(429, json=body)) as client:
        with pytest.raises(ProviderError) as caught:
            await BraveProvider().call(Capability.WEB_SEARCH, {"query": "retrieval"}, Context(client, "brave"))
    assert caught.value.kind == kind


async def test_tavily_reflected_query_does_not_change_rate_failure_kind():
    body = {"error": "Request rate limit exceeded", "query": "usage limit study"}
    async with client_for(lambda request: httpx.Response(429, json=body)) as client:
        with pytest.raises(ProviderError) as caught:
            await TavilyProvider().call(Capability.WEB_SEARCH, {"query": "retrieval"}, Context(client, "tavily"))
    assert caught.value.kind == ErrorKind.RATE_LIMITED


async def test_brave_language_country_and_news():
    def handler(request):
        assert request.url.path == "/res/v1/news/search"
        assert request.url.params["search_lang"] == "zh-hant"
        assert request.url.params["country"] == "TW"
        assert request.url.params["count"] == "20"
        assert request.url.params["freshness"] == "pw"
        return httpx.Response(200, json={"results": [{"title": "News", "url": "https://example.com/a",
                                                     "description": "Actual source excerpt"}]})
    async with client_for(handler) as client:
        result = await BraveProvider().call(Capability.NEWS_SEARCH,
            {"query": "retrieval", "language": "zh-TW", "country": "tw", "limit": 50,
             "freshness": "week"}, Context(client, "brave"))
    assert result.hits[0].title == "News"


async def test_tavily_never_promotes_generated_answer():
    def handler(request):
        body = json.loads(request.content)
        assert body["include_answer"] is False
        assert body["country"] == "thailand"
        assert body["language"] == "th"
        return httpx.Response(200, json={"results": [{"title": "Source", "url": "https://example.com/a",
                                                     "content": "Original excerpt"}],
                                         "answer": "Generated prose", "usage": {"credits": 1}})
    async with client_for(handler) as client:
        result = await TavilyProvider().call(Capability.WEB_SEARCH,
            {"query": "retrieval", "country": "TH", "language": "th"}, Context(client, "tavily"))
    assert result.hits[0].snippet == "Original excerpt"
    assert "Generated prose" not in result.model_dump_json()


async def test_tavily_country_news_refusal_does_not_disable_capability():
    async with client_for(lambda request: pytest.fail("Unexpected request")) as client:
        with pytest.raises(ProviderError) as caught:
            await TavilyProvider().call(Capability.NEWS_SEARCH,
                                       {"query": "retrieval", "country": "US"}, Context(client, "tavily"))
    assert caught.value.kind == ErrorKind.BAD_REQUEST
    assert caught.value.block_capability is False


async def test_tavily_extract_failure_is_target_only():
    async with client_for(lambda request: httpx.Response(200, json={"results": [],
            "failed_results": [{"url": "https://example.com/a", "error": "unavailable"}]})) as client:
        with pytest.raises(ProviderError) as caught:
            await TavilyProvider().call(Capability.WEB_READ, {"url": "https://example.com/a"},
                                       Context(client, "tavily"))
    assert caught.value.kind == ErrorKind.TARGET
    assert caught.value.block_capability is False


async def test_serper_news_keeps_raw_credit_usage():
    def handler(request):
        assert request.url.path == "/news"
        assert request.headers["X-API-KEY"] == "unit-key"
        body = json.loads(request.content)
        assert body["hl"] == "en" and body["gl"] == "us" and body["tbs"] == "qdr:d"
        return httpx.Response(200, json={"news": [{"title": "News", "link": "https://example.com/a",
                                                  "snippet": "Excerpt", "date": "2026-09-01"}], "credits": 1})
    async with client_for(handler) as client:
        result = await SerperProvider().call(Capability.NEWS_SEARCH,
            {"query": "retrieval", "language": "en-US", "country": "US", "freshness": "day"},
            Context(client, "serper"))
    assert result.usage == {"credits": 1}
    assert result.hits[0].year == 2026


async def test_firecrawl_developer_uses_current_contract():
    def handler(request):
        assert request.method == "POST" and request.url.path == "/v2/search/developer"
        body = json.loads(request.content)
        assert body["k"] == 3 and body["passages"] == 1 and type(body["passages"]) is int
        assert body["types"] == ["doc"]
        return httpx.Response(200, json={"results": [{"type": "doc", "url": "https://example.com/a",
            "passages": [{"text": "Source passage"}]}], "creditsUsed": 1})
    async with client_for(handler) as client:
        result = await FirecrawlProvider().call(Capability.DEVELOPER_SEARCH,
            {"query": "retrieval", "kind": "docs", "limit": 3}, Context(client, "firecrawl"))
    assert result.hits[0].title == "https://example.com/a"
    assert result.hits[0].snippet == "Source passage"


async def test_firecrawl_v2_news_array():
    def handler(request):
        assert json.loads(request.content)["sources"] == ["news"]
        return httpx.Response(200, json={"success": True, "data": {"news": [{"title": "News",
                               "url": "https://example.com/a", "description": "Source excerpt"}]}, "creditsUsed": 1})
    async with client_for(handler) as client:
        result = await FirecrawlProvider().call(Capability.NEWS_SEARCH, {"query": "retrieval"},
                                               Context(client, "firecrawl"))
    assert result.hits[0].snippet == "Source excerpt"


async def test_firecrawl_map_object_links():
    async with client_for(lambda request: httpx.Response(200, json={"success": True, "links": [
            {"url": "https://example.com/a", "title": "A"}, {"url": "https://example.com/a"},
            {"url": "javascript:bad"}]})) as client:
        ctx = Context(client, "firecrawl")
        result = await FirecrawlProvider().call(Capability.SITE_MAP, {"url": "https://example.com"}, ctx)
    assert result.urls == ["https://example.com/a"]
    assert ctx.validated == ["https://example.com"]


async def test_firecrawl_start_is_bounded_and_not_retried():
    def handler(request):
        body = json.loads(request.content)
        assert body["maxDiscoveryDepth"] == 2 and body["limit"] == 25
        assert body["allowExternalLinks"] is False and body["ignoreRobotsTxt"] is False
        return httpx.Response(200, json={"success": True, "id": "job-1"})
    async with client_for(handler) as client:
        ctx = Context(client, "firecrawl")
        ref = await FirecrawlProvider().start(Capability.SITE_CRAWL, {"url": "https://example.com"}, ctx)
    assert ref == "job-1" and len(ctx.calls) == 1 and ctx.calls[0]["retries"] == 0


async def test_firecrawl_start_timeout_is_ambiguous():
    def handler(request):
        raise httpx.ReadTimeout("submission lost", request=request)
    async with client_for(handler) as client:
        with pytest.raises(ProviderError) as caught:
            await FirecrawlProvider().start(Capability.SITE_CRAWL, {"url": "https://example.com"},
                                            Context(client, "firecrawl"))
    assert caught.value.kind == ErrorKind.TRANSIENT and caught.value.ambiguous_start is True


async def test_firecrawl_completed_job_collects_pagination():
    def handler(request):
        row = {"markdown": "Page " + ("two" if request.url.query else "one"),
               "metadata": {"sourceURL": "https://example.com/" + ("b" if request.url.query else "a")}}
        if request.url.query:
            return httpx.Response(200, json={"success": True, "data": [row]})
        return httpx.Response(200, json={"success": True, "status": "completed", "data": [row],
            "next": "https://api.firecrawl.dev/v2/crawl/job-1?skip=1", "creditsUsed": 2})
    async with client_for(handler) as client:
        result = await FirecrawlProvider().poll(Context(client, "firecrawl"), "job-1")
    assert result.status == "completed"
    assert [doc.text for doc in result.result.documents] == ["Page one", "Page two"]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_firecrawl_native_job_failure(status):
    async with client_for(lambda request: httpx.Response(200, json={"success": False,
            "status": status, "error": "upstream failure"})) as client:
        result = await FirecrawlProvider().poll(Context(client, "firecrawl"), "job-1")
    assert result.status == status and result.error == "upstream failure"


async def test_firecrawl_completed_job_omits_target_errors_and_unsafe_sources():
    payload = {"success": True, "status": "completed", "data": [
        {"markdown": "Private source", "metadata": {"sourceURL": "http://127.0.0.1/private"}},
        {"markdown": "Not found", "metadata": {"sourceURL": "https://example.com/missing", "statusCode": 404}},
        {"markdown": "Source text", "metadata": {"sourceURL": "https://example.com/a", "statusCode": 200}},
    ]}
    async with client_for(lambda request: httpx.Response(200, json=payload)) as client:
        result = await FirecrawlProvider().poll(Context(client, "firecrawl"), "job-1")
    assert [doc.text for doc in result.result.documents] == ["Source text"]
    assert len(result.result.raw["failed_pages"]) == 2
    assert "Private source" not in result.result.model_dump_json()


async def test_firecrawl_missing_native_job_fails_clearly():
    async with client_for(lambda request: httpx.Response(404, json={"error": "job expired"})) as client:
        result = await FirecrawlProvider().poll(Context(client, "firecrawl"), "job-1")
    assert result.status == "failed"


async def test_firecrawl_pagination_cannot_forward_key_to_another_host():
    async with client_for(lambda request: httpx.Response(200, json={"success": True, "status": "completed",
            "data": [], "next": "https://other.example/crawl/job-1"})) as client:
        ctx = Context(client, "firecrawl")
        with pytest.raises(ProviderError) as caught:
            await FirecrawlProvider().poll(ctx, "job-1")
    assert caught.value.kind == ErrorKind.TARGET and len(ctx.calls) == 1


async def test_firecrawl_cancel_uses_delete():
    def handler(request):
        assert request.method == "DELETE"
        return httpx.Response(200, json={"success": True})
    async with client_for(handler) as client:
        assert await FirecrawlProvider().cancel(Context(client, "firecrawl"), "job-1")


@pytest.mark.parametrize("provider", [ExaProvider(), FirecrawlProvider(), TavilyProvider(),
                                    JinaProvider(), TrafilaturaProvider()])
async def test_every_remote_reader_validates_target_before_network(provider):
    async with client_for(lambda request: pytest.fail("Private target reached network")) as client:
        ctx = Context(client, provider.name)
        with pytest.raises(ProviderError) as caught:
            await provider.call(Capability.WEB_READ, {"url": "http://127.0.0.1/private"}, ctx)
    assert caught.value.kind == ErrorKind.TARGET and not ctx.calls


async def test_jina_keyless_json_source():
    def handler(request):
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"code": 200, "data": {"url": "https://example.com/a",
                               "content": "Source text", "title": "Title", "usage": {"tokens": 4}}})
    async with client_for(handler) as client:
        ctx = Context(client, "jina")
        ctx.credentials = {}
        result = await JinaProvider().call(Capability.WEB_READ, {"url": "https://example.com/a"}, ctx)
    assert result.document.text == "Source text" and result.usage == {"tokens": 4}


async def test_jina_text_fallback_strips_reader_wrapper():
    async with client_for(lambda request: httpx.Response(200, text="Title: Example\nURL Source: https://example.com\n\nMarkdown Content:\nOriginal source text")) as client:
        result = await JinaProvider().call(Capability.WEB_READ, {"url": "https://example.com"},
                                         Context(client, "jina"))
    assert result.document.text == "Original source text"


async def test_jina_target_block_is_not_account_auth_failure():
    async with client_for(lambda request: httpx.Response(403, json={"name": "SecurityCompromiseError"})) as client:
        with pytest.raises(ProviderError) as caught:
            await JinaProvider().call(Capability.WEB_READ, {"url": "https://example.com/a"}, Context(client, "jina"))
    assert caught.value.kind == ErrorKind.TARGET and caught.value.block_capability is False


async def test_trafilatura_extracts_source_html():
    paragraph = "Original source paragraph about retrieval and information quality. " * 30
    html = "<html><body><article><h1>Source</h1><p>" + paragraph + "</p></article></body></html>"
    async with client_for(lambda request: httpx.Response(200, text=html, headers={"Content-Type": "text/html"})) as client:
        result = await TrafilaturaProvider().call(Capability.WEB_READ, {"url": "https://example.com/a"},
                                                  Context(client, "trafilatura"))
    assert "Original source paragraph" in result.document.text
    assert "<html>" not in result.document.text


async def test_trafilatura_rejects_pdf_for_reader_fallback():
    async with client_for(lambda request: httpx.Response(200, content=b"%PDF-1.7 fixture")) as client:
        with pytest.raises(ProviderError) as caught:
            await TrafilaturaProvider().call(Capability.WEB_READ, {"url": "https://example.com/a.pdf"},
                                             Context(client, "trafilatura"))
    assert caught.value.kind == ErrorKind.TARGET and caught.value.block_capability is False


def ddg_html(target="https://example.com/a?x=a%2Fb"):
    href = "/l/?" + urlencode({"uddg": target})
    return f'<div class="result"><a class="result__a" href="{href}">Source</a><div class="result__snippet">Excerpt</div></div>'


def test_ddg_unwraps_once_and_excludes_ads():
    html = '<div class="result result--ad"><a class="result__a" href="https://ad.example">Ad</a></div>' + ddg_html()
    rows = parse_results(html)
    assert rows == [{"title": "Source", "url": "https://example.com/a?x=a%2Fb", "snippet": "Excerpt"}]


def test_ddg_true_empty_is_success():
    assert parse_results('<div class="no-results">No results</div>') == []


@pytest.mark.parametrize("html", ["<html>CAPTCHA</html>", "<html>anomaly.js</html>"])
def test_ddg_challenge_is_truthful_rate_limit(html):
    with pytest.raises(ProviderError) as caught:
        parse_results(html)
    assert caught.value.kind == ErrorKind.RATE_LIMITED


def test_ddg_parser_drift_is_not_false_empty_success():
    with pytest.raises(ProviderError) as caught:
        parse_results('<div class="new-result">Unrecognized markup</div>')
    assert caught.value.kind == ErrorKind.TRANSIENT


async def test_ddg_keyless_search_and_local_cooldown():
    provider = DuckDuckGoProvider()
    async with client_for(lambda request: httpx.Response(200, text=ddg_html())) as client:
        ctx = Context(client, "duckduckgo")
        ctx.credentials = {}
        result = await provider.call(Capability.WEB_SEARCH, {"query": "retrieval"}, ctx)
        with pytest.raises(ProviderError) as caught:
            await provider.call(Capability.WEB_SEARCH, {"query": "another query"}, ctx)
    assert result.hits[0].provider == "duckduckgo"
    assert caught.value.kind == ErrorKind.RATE_LIMITED and caught.value.retry_after > 40
    assert len(ctx.calls) == 1


async def test_ddg_202_is_a_service_limit_without_retry():
    async with client_for(lambda request: httpx.Response(202, text="challenge")) as client:
        ctx = Context(client, "duckduckgo")
        with pytest.raises(ProviderError) as caught:
            await DuckDuckGoProvider().call(Capability.WEB_SEARCH, {"query": "retrieval"}, ctx)
    assert caught.value.kind == ErrorKind.RATE_LIMITED and len(ctx.calls) == 1
    assert ctx.calls[0]["retries"] == 0
