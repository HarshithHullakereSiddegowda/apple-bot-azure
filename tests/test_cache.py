"""Requirements cache: when a saved checklist is reused, and when it must be re-extracted."""
from datetime import datetime, timezone

from tender import tools
from tender.tools import CHAT_DEPLOYMENT, REQUIREMENTS_PROMPT_VERSION, _stale_reason

SAVED = {"prompt_version": REQUIREMENTS_PROMPT_VERSION, "model": CHAT_DEPLOYMENT,
         "extracted_at": "2026-10-18T10:01:31+00:00", "conditions": ["ISO 9001"]}
BEFORE = datetime(2026, 10, 1, tzinfo=timezone.utc)    # documents ingested before extraction
AFTER = datetime(2026, 10, 20, tzinfo=timezone.utc)    # documents re-ingested after extraction


def test_valid_checklist_is_reused():
    assert _stale_reason(SAVED, BEFORE) == ""


def test_each_reason_to_re_extract():
    assert _stale_reason(None, None) == "none saved"
    assert _stale_reason({**SAVED, "prompt_version": "req-v0"}, BEFORE) == "prompt changed"
    assert _stale_reason({**SAVED, "model": "other-model"}, BEFORE) == "model changed"
    assert _stale_reason(SAVED, AFTER) == "documents re-ingested"


def test_hit_returns_saved_without_reading_documents(monkeypatch):
    monkeypatch.setattr(tools.store, "get_json", lambda tid, name: dict(SAVED))
    monkeypatch.setattr(tools.store, "modified_at", lambda tid, name: BEFORE)
    monkeypatch.setattr(tools, "_tender_chunks", lambda tid: (_ for _ in ()).throw(AssertionError("LLM path ran")))
    result = tools.extract_requirements("T-009")
    assert result["cached"] is True and result["conditions"] == ["ISO 9001"]


def test_refresh_skips_the_cache(monkeypatch):
    monkeypatch.setattr(tools.store, "get_json", lambda tid, name: dict(SAVED))
    monkeypatch.setattr(tools, "_tender_chunks", lambda tid: [])   # miss path → "no ingested documents"
    result = tools.extract_requirements("T-009", refresh=True)
    assert "error" in result and "cached" not in result
