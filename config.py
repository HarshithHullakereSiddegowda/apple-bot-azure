"""Single source of configuration and clients. Every other module imports from here.

Auth: if AOAI_KEY / SEARCH_KEY are set (local .env), use keys. If they are not set (Azure Container
Apps), use the managed identity via DefaultAzureCredential: no secrets in the app at all.
"""
import os

from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

INDEX = os.environ.get("INDEX_NAME", "apple-support")
APP_API_KEY = os.environ.get("APP_API_KEY", "")  # required in X-API-Key for /upload; empty = uploads always 401
CHAT_DEPLOYMENT = "chat"    # gpt-4.1-mini, GlobalStandard
EMBED_DEPLOYMENT = "embed"  # text-embedding-3-small, Standard (regional, Australia East)
EMBED_DIMENSIONS = 1536     # must match vector_search_dimensions in the index

_aoai_key = os.environ.get("AOAI_KEY", "")
_search_key = os.environ.get("SEARCH_KEY", "")
# Created only when a key is missing. Locally it resolves to your `az login`; in Azure, to the managed identity.
_credential = DefaultAzureCredential() if not (_aoai_key and _search_key) else None

AUTH_MODE = "keys" if _credential is None else "managed_identity"

# Azure OpenAI v1 API: the standard OpenAI client pointed at the Azure endpoint, no api_version needed.
# api_key accepts a string (key) or a callable that returns a fresh Entra ID token on every request.
aoai = OpenAI(
    base_url=f"{os.environ['AOAI_ENDPOINT'].rstrip('/')}/openai/v1/",
    api_key=_aoai_key or get_bearer_token_provider(
        _credential, "https://cognitiveservices.azure.com/.default"),
)

_search_cred = AzureKeyCredential(_search_key) if _search_key else _credential
index_client = SearchIndexClient(os.environ["SEARCH_ENDPOINT"], _search_cred)


def search_client_for(index: str) -> SearchClient:
    """A search client for any index on the same service (used to A/B a second index)."""
    return SearchClient(os.environ["SEARCH_ENDPOINT"], index, _search_cred)


search_client = search_client_for(INDEX)


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Returns one vector per input, in order."""
    resp = aoai.embeddings.create(model=EMBED_DEPLOYMENT, input=texts)
    return [d.embedding for d in resp.data]
