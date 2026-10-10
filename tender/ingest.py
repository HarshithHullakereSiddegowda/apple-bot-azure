"""Ingest a tender's documents from Blob into the AI Search index `tender-docs`.

    tenders/{tender_id}/source/*.pdf ─► Document Intelligence (layout: text, tables, figures)
                                     ─► section-aware chunks, each with source = tender_id
                                     ─► embeddings ─► index "tender-docs"

One index for all tenders; per-tender isolation by filtering on `source`. Re-ingesting a tender replaces its chunks.

    python -m tender.ingest T-001 T-009
"""
import sys
import time

from config import EMBED_DIMENSIONS, index_client, search_client_for
from di_ingest import analyze, build_chunks, describe_figures, figure_context
from ingest import BATCH_SIZE, build_index, odata_quote, upload
from tender import store

TENDER_INDEX = "tender-docs"


def ensure_index() -> None:
    """Create the index once. Never deleted here: it holds every tender."""
    if TENDER_INDEX in set(index_client.list_index_names()):
        return
    from azure.search.documents.indexes.models import SearchFieldDataType, SimpleField

    index = build_index(TENDER_INDEX, EMBED_DIMENSIONS)
    index.fields += [
        SimpleField(name="section", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="content_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="document", type=SearchFieldDataType.String, filterable=True),
    ]
    index_client.create_or_update_index(index)


def _delete_tender_chunks(tender_id: str) -> int:
    client = search_client_for(TENDER_INDEX)
    ids = [r["id"] for r in client.search(search_text="*", filter=f"source eq {odata_quote(tender_id)}",
                                          select=["id"], top=1000)]
    for i in range(0, len(ids), 500):
        client.delete_documents([{"id": x} for x in ids[i:i + 500]])
    return len(ids)


def ingest_tender(tender_id: str) -> dict:
    """Replace one tender's chunks in `tender-docs` with a fresh layout-aware ingestion of its source PDFs."""
    t0 = time.perf_counter()
    sources = [s for s in store.list_sources(tender_id) if s.lower().endswith(".pdf")]
    if not sources:
        return {"tender_id": tender_id, "chunks": 0, "note": "no source documents in Blob"}

    ensure_index()
    chunks: list[dict] = []
    for name in sources:
        tag = f"tender_{tender_id}_{name.removesuffix('.pdf')}"
        result, figures = analyze(store.get_file(tender_id, f"source/{name}"), pages=None, tag=tag)
        descriptions = describe_figures(figures, figure_context(result), tag)
        for n, c in enumerate(build_chunks(result, descriptions, source=tender_id)):
            c["id"] = f"{tender_id}-{name.removesuffix('.pdf')}-p{c['page']}-{n}".replace(".", "_")
            c["document"] = name
            chunks.append(c)

    replaced = _delete_tender_chunks(tender_id)
    upload(chunks, quiet=True, client=search_client_for(TENDER_INDEX))
    summary = {
        "tender_id": tender_id,
        "documents": sources,
        "chunks": len(chunks),
        "by_type": {t: sum(c["content_type"] == t for c in chunks) for t in ("text", "table", "figure")},
        "replaced_chunks": replaced,
        "seconds": round(time.perf_counter() - t0, 1),
    }
    store.put_json(tender_id, "ingest.json", summary)   # audit record next to the tender
    return summary


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(".env")
    assert BATCH_SIZE > 0
    for tid in sys.argv[1:] or store.list_tenders():
        print(ingest_tender(tid))
