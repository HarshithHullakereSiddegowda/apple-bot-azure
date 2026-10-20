"""Long-term memory for the Bid Analyst: facts the bid manager states and past human bid decisions.

    Short-term memory = the Foundry thread (one conversation).
    Long-term memory  = this module (every conversation): retrieval over saved memories, i.e. RAG over our own past.

    remember(text)   save one memory with its embedding, who it came from and when
    recall(query)    the top-k memories closest in MEANING to the query (cosine similarity of embeddings)
    forget(id, by)   delete one; a human action, like recording a bid decision

Memory is CONTEXT, not truth: a tender's status lives in status.json (state), never here. Memories are written only
from what a user says or what a person decided, never from tender documents (memory poisoning).

Storage: one JSON file in the tenders container, `_memory/memories.json`. Fine for hundreds of memories; at
thousands, move to an Azure AI Search index behind the same remember/recall functions. Writes check the blob's ETag,
so two writers can never silently overwrite each other.

    python -m tender.memory list
    python -m tender.memory forget m-1a2b3c4d "Jane Smith"
"""
import json
import math
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Literal

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from config import embed
from tender import store
from tender.telemetry import tracer

BLOB = "_memory/memories.json"   # one file in the existing `tenders` container (no new Azure resource)
MAX_CHARS = 300                  # a memory is one short fact, not a document
TOP_K = 3
MIN_SCORE = 0.30                 # from eval_memory.py: recall 0.93 / precision 0.50; a missed rule costs more than
                                 # one extra memory (top-3 cap, agent names what it used). Re-run as memories grow.
DUPLICATE_SCORE = 0.95           # this close to an existing memory = the same memory

Kind = Literal["fact", "decision"]

# Set by the agent loop for one turn: which conversation, and what the user actually typed. remember_fact reads it.
current_turn: ContextVar[dict | None] = ContextVar("current_turn", default=None)


# ── storage: read-modify-write guarded by the blob's ETag ─────────────────────

def _load() -> tuple[list[dict], str | None]:
    """All memories + the blob's ETag (None when nothing has been saved yet)."""
    try:
        blob = store.container().download_blob(BLOB)
        return json.loads(blob.readall()), blob.properties.etag
    except ResourceNotFoundError:
        return [], None


def _save(memories: list[dict], etag: str | None) -> None:
    """Write back only if nobody changed the file since it was loaded; otherwise Azure refuses (HTTP 412/409)."""
    data = json.dumps(memories)
    if etag is None:
        store.container().upload_blob(BLOB, data, overwrite=False)
    else:
        store.container().upload_blob(BLOB, data, overwrite=True, etag=etag,
                                      match_condition=MatchConditions.IfNotModified)


def _update(change):
    """Load → change(memories) → save. `change` returns (result, changed). Retries if another writer got in first."""
    for _ in range(3):
        memories, etag = _load()
        result, changed = change(memories)
        if not changed:
            return result
        try:
            _save(memories, etag)
            return result
        except (ResourceModifiedError, ResourceExistsError):
            continue
    raise RuntimeError("memory store is busy; try again")


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True)) / (math.hypot(*a) * math.hypot(*b))


def _public(m: dict) -> dict:
    """A memory without its embedding (1,536 numbers nobody needs to read)."""
    return {k: v for k, v in m.items() if k != "embedding"}


# ── the three operations ──────────────────────────────────────────────────────

def remember(text: str, kind: Kind = "fact", source: str = "", tender_id: str | None = None) -> dict:
    """Save one memory. `source` says who it came from (e.g. "user:thread_abc" or "human:Jane Smith")."""
    text = " ".join(text.split())
    if not text:
        raise ValueError("a memory needs text")
    if len(text) > MAX_CHARS:
        raise ValueError(f"memory is {len(text)} characters; keep it to one fact under {MAX_CHARS}")
    if not source:
        raise ValueError("every memory needs a source: who said or decided it")
    with tracer.start_as_current_span("memory.remember") as span:
        span.set_attribute("memory.kind", kind)
        vector = [round(v, 5) for v in embed([text])[0]]
        record = {"id": f"m-{uuid.uuid4().hex[:8]}", "kind": kind, "text": text, "tender_id": tender_id,
                  "source": source, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "embedding": vector}

        def add(memories: list[dict]):
            same = next((m for m in memories if m["kind"] == kind and m.get("tender_id") == tender_id
                         and _cosine(m["embedding"], vector) >= DUPLICATE_SCORE), None)
            if same:
                return {**_public(same), "duplicate": True}, False
            memories.append(record)
            return _public(record), True

        result = _update(add)
        span.set_attribute("memory.duplicate", bool(result.get("duplicate")))
        return result


def recall(query: str, k: int = TOP_K, min_score: float = MIN_SCORE) -> list[dict]:
    """The k memories most similar in meaning to `query`, best first, each with its similarity score."""
    with tracer.start_as_current_span("memory.recall") as span:
        memories, _ = _load()
        span.set_attribute("memory.count", len(memories))
        if not memories:                       # nothing saved: skip the embedding call entirely
            span.set_attribute("memory.hits", 0)
            return []
        q = embed([query])[0]
        scored = sorted(((_cosine(q, m["embedding"]), m) for m in memories), key=lambda p: p[0], reverse=True)
        hits = [{**_public(m), "score": round(s, 3)} for s, m in scored[:k] if s >= min_score]
        span.set_attributes({"memory.hits": len(hits), "memory.top_score": round(scored[0][0], 3)})
        return hits


def forget(memory_id: str, by: str) -> dict:
    """Delete one memory. Human-only: `by` must be "human:<name>" (the agent has no tool for this)."""
    if not (by.startswith("human:") and len(by) > len("human:")):
        raise PermissionError(f"forgetting a memory is a human action; signature {by!r} is not a person")

    def drop(memories: list[dict]):
        keep = [m for m in memories if m["id"] != memory_id]
        if len(keep) == len(memories):
            raise KeyError(f"no memory {memory_id!r}")
        memories[:] = keep
        return {"forgotten": memory_id, "by": by}, True

    return _update(drop)


def list_memories() -> list[dict]:
    return [_public(m) for m in _load()[0]]


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["list"]:
        for m in list_memories():
            print(f"{m['id']}  {m['kind']:<8} {m['created_at']}  {m['source']:<28} {m['text']}")
    elif args[:1] == ["forget"] and len(args) == 3:
        print(forget(args[1], f"human:{args[2]}"))
    else:
        print(__doc__.split("\n\n")[-1])


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(".env")
    main()
