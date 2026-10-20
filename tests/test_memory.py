"""Long-term memory: save, recall by meaning, duplicates, validation, human-only forget, safe concurrent writes."""
from types import SimpleNamespace as NS

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from tender import memory

TOPICS = {"price": ("$", "200k", "price", "value"), "place": ("canberra", "travel", "far"),
          "cyber": ("cyber", "security")}


def fake_embed(texts):
    """Topic vectors: same topic → cosine ≈ 1, different topic → ≈ 0."""
    return [[float(any(w in t.lower() for w in words)) for words in TOPICS.values()] + [0.01] for t in texts]


class FakeContainer:
    """Behaves like a Blob container for one file, including ETag checks."""

    def __init__(self):
        self.data, self.etag, self.before_save = None, 0, None

    def download_blob(self, name):
        if self.data is None:
            raise ResourceNotFoundError("no blob")
        return NS(readall=lambda: self.data, properties=NS(etag=str(self.etag)))

    def upload_blob(self, name, data, overwrite, etag=None, match_condition=None):
        if self.before_save:                       # simulate another writer saving first
            hook, self.before_save = self.before_save, None
            hook()
        if not overwrite and self.data is not None:
            raise ResourceExistsError("exists")
        if etag is not None and etag != str(self.etag):
            raise ResourceModifiedError("412 precondition failed")
        self.data, self.etag = data.encode(), self.etag + 1


@pytest.fixture
def box(monkeypatch):
    c = FakeContainer()
    monkeypatch.setattr(memory.store, "container", lambda: c)
    monkeypatch.setattr(memory, "embed", fake_embed)
    return c


def test_remember_then_recall_by_meaning(box):
    memory.remember("We don't bid below $200k", source="user:thread_1")
    hits = memory.recall("Is a tender with a value of $150k worth it?")
    assert [h["text"] for h in hits] == ["We don't bid below $200k"] and hits[0]["score"] > 0.9
    assert memory.recall("cyber security certifications") == []      # unrelated → below the threshold


def test_same_memory_is_not_saved_twice(box):
    first = memory.remember("We don't bid below $200k", source="user:thread_1")
    again = memory.remember("Never bid under $200k value", source="user:thread_2")
    assert again["duplicate"] is True and again["id"] == first["id"]
    assert len(memory.list_memories()) == 1


def test_bad_memories_are_rejected(box):
    for text, source in (("   ", "user:t"), ("x" * 301, "user:t"), ("We avoid Canberra", "")):
        with pytest.raises(ValueError):
            memory.remember(text, source=source)


def test_only_a_person_can_forget(box):
    m = memory.remember("Travel to Canberra is too far for us", source="user:thread_1")
    with pytest.raises(PermissionError):
        memory.forget(m["id"], by="agent")
    assert memory.forget(m["id"], by="human:Jane Smith")["forgotten"] == m["id"]
    assert memory.list_memories() == []
    with pytest.raises(KeyError):
        memory.forget(m["id"], by="human:Jane Smith")


def test_recall_with_no_memories_skips_the_embedding_call(box, monkeypatch):
    monkeypatch.setattr(memory, "embed", lambda texts: pytest.fail("embedding API called"))
    assert memory.recall("anything") == []


def test_concurrent_write_is_retried_not_lost(box):
    memory.remember("We don't bid below $200k", source="user:thread_1")
    box.before_save = lambda: memory.remember("Travel to Canberra is too far", source="user:thread_2")
    memory.remember("We are strong in cyber security", source="user:thread_3")
    assert len(memory.list_memories()) == 3                         # nobody's memory was overwritten


def test_listing_hides_embeddings(box):
    memory.remember("We don't bid below $200k", source="user:thread_1")
    assert "embedding" not in memory.list_memories()[0]
