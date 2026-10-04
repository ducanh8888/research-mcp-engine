"""Direct academic provider registry. Hosted MCP providers are registered separately."""

from .arxiv import ArxivProvider
from .crossref import CrossrefProvider
from .consensus_api import ConsensusAPIProvider
from .elicit_api import ElicitAPIProvider
from .openalex import OpenAlexProvider
from .scite import SciteRestProvider
from .semantic_scholar import SemanticScholarProvider

PROVIDERS = [OpenAlexProvider, CrossrefProvider, SemanticScholarProvider, ArxivProvider, SciteRestProvider,
             ConsensusAPIProvider, ElicitAPIProvider]

__all__ = ["PROVIDERS", "OpenAlexProvider", "CrossrefProvider", "SemanticScholarProvider",
           "ArxivProvider", "SciteRestProvider", "ConsensusAPIProvider", "ElicitAPIProvider"]
