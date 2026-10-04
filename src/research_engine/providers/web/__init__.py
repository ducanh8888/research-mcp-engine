"""Built-in web providers, exposed as concrete instances to the registry."""

from .brave import BraveProvider
from .duckduckgo import DuckDuckGoProvider
from .exa import ExaProvider
from .firecrawl import FirecrawlProvider
from .jina import JinaProvider
from .serper import SerperProvider
from .tavily import TavilyProvider
from .trafilatura import TrafilaturaProvider

PROVIDERS = [ExaProvider(), FirecrawlProvider(), TavilyProvider(), BraveProvider(), SerperProvider(),
             JinaProvider(), TrafilaturaProvider(), DuckDuckGoProvider()]
