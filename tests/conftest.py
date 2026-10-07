"""Test setup: fake Azure settings so unit tests never touch real services or need keys.

Set before any app module is imported. load_dotenv() does not override variables that are already set,
so a local .env with real keys is ignored during tests.
"""
import os
import sys
from pathlib import Path

os.environ.update({
    "AOAI_ENDPOINT": "https://unit-test.openai.azure.com",
    "AOAI_KEY": "unit-test-key",
    "SEARCH_ENDPOINT": "https://unit-test.search.windows.net",
    "SEARCH_KEY": "unit-test-key",
    "INDEX_NAME": "unit-test-index",
    "APP_API_KEY": "unit-test-app-key",
})

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))
