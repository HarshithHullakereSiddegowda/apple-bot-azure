"""Ingestion: PDF -> chunks -> embeddings -> Azure AI Search index.

Used two ways:
    python ingest.py --dry-run   # chunk the default PDF only, print stats, no Azure calls
    python ingest.py             # create/update the index and (re)index the default PDF
    index_pdf(...)               # called by POST /upload in app.py for user-uploaded PDFs

Every chunk carries `source` (which document) and `page` (for citations).
Re-indexing a source deletes its old chunks first, so there are no stale or duplicate chunks.
"""
import argparse
import re
import time
from typing import BinaryIO

from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)
from pypdf import PdfReader

PDF_PATH = "data/apple-support.pdf"
DEFAULT_SOURCE = "iPhone User Guide"
CHUNK_SIZE = 1000     # characters (~250 tokens)
CHUNK_OVERLAP = 200   # characters repeated between neighbouring chunks
MIN_CHUNK_CHARS = 50  # skip near-empty fragments (cover pages, stray headers)
BATCH_SIZE = 16       # chunks per embedding call / upload


# ── Chunking ──────────────────────────────────────────────────────────────────

def clean(text: str) -> str:
    """Collapse PDF line breaks and repeated spaces into single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def chunk(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    step = size - overlap
    return [text[i:i + size] for i in range(0, len(text), step)]


def slugify(name: str) -> str:
    """'My Guide (v2).pdf' -> 'my-guide-v2'. Used as the chunk-id prefix for a source."""
    name = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "doc"


def load_chunks(pdf: str | BinaryIO = PDF_PATH, source: str = DEFAULT_SOURCE,
                id_prefix: str = "") -> list[dict]:
    """Chunk a PDF (path or file object). Ids: '{prefix}p{page}-{n}'.

    The default guide keeps the original un-prefixed ids (p56-1), so citations and eval history
    stay stable. Uploaded documents get a prefix so their ids never collide with it.
    """
    docs = []
    for page_no, page in enumerate(PdfReader(pdf).pages, start=1):
        for j, piece in enumerate(chunk(clean(page.extract_text() or ""))):
            if len(piece) >= MIN_CHUNK_CHARS:
                docs.append({"id": f"{id_prefix}p{page_no}-{j}", "content": piece,
                             "page": page_no, "source": source})
    return docs


# ── The index (the shelf) ─────────────────────────────────────────────────────

def build_index(index_name: str, dimensions: int) -> SearchIndex:
    return SearchIndex(
        name=index_name,
        fields=[
            SimpleField(name="id", type=SearchFieldDataType.String, key=True),
            SearchableField(name="content", type=SearchFieldDataType.String),          # keyword (BM25)
            SimpleField(name="page", type=SearchFieldDataType.Int32,
                        filterable=True, sortable=True),
            SimpleField(name="source", type=SearchFieldDataType.String,                # which document
                        filterable=True, facetable=True),
            SearchField(name="embedding",                                               # meaning (vector)
                        type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                        searchable=True,
                        vector_search_dimensions=dimensions,
                        vector_search_profile_name="vec"),
        ],
        vector_search=VectorSearch(
            algorithms=[HnswAlgorithmConfiguration(name="hnsw")],
            profiles=[VectorSearchProfile(name="vec", algorithm_configuration_name="hnsw")],
        ),
        semantic_search=SemanticSearch(configurations=[SemanticConfiguration(
            name="sem",
            prioritized_fields=SemanticPrioritizedFields(
                content_fields=[SemanticField(field_name="content")]
            ),
        )]),
    )


# ── Embed + upload ────────────────────────────────────────────────────────────

def odata_quote(value: str) -> str:
    """Escape a string for an OData filter literal ('O''Brien')."""
    return "'" + value.replace("'", "''") + "'"


def delete_source(source: str) -> int:
    """Remove every chunk of one document. Returns how many were deleted."""
    from config import search_client

    ids = [r["id"] for r in search_client.search(
        search_text="*", filter=f"source eq {odata_quote(source)}", select=["id"], top=1000)]
    if ids:
        search_client.delete_documents([{"id": i} for i in ids])
    return len(ids)


def upload(docs: list[dict], quiet: bool = False, client=None) -> None:
    """Embed and upload in batches. client: a SearchClient for another index; default is the app's index."""
    from config import embed, search_client

    search_client = client or search_client
    for i in range(0, len(docs), BATCH_SIZE):
        batch = docs[i:i + BATCH_SIZE]
        vectors = embed([d["content"] for d in batch])
        for d, vec in zip(batch, vectors, strict=True):  # fail loudly if a vector is missing
            d["embedding"] = vec
        results = search_client.upload_documents(batch)
        failed = [r.key for r in results if not r.succeeded]
        if failed:
            raise RuntimeError(f"Upload failed for: {failed}")
        if not quiet:
            print(f"  uploaded {min(i + BATCH_SIZE, len(docs))}/{len(docs)}", end="\r")
    if not quiet:
        print()


def index_pdf(pdf: str | BinaryIO, source: str, id_prefix: str = "") -> dict:
    """Replace one document in the index: delete its old chunks, then chunk, embed and upload."""
    t0 = time.perf_counter()
    docs = load_chunks(pdf, source, id_prefix)
    if not docs:
        raise ValueError("No extractable text found (is this a scanned PDF?)")
    removed = delete_source(source)
    upload(docs, quiet=True)
    return {
        "source": source,
        "chunks": len(docs),
        "pages": len({d["page"] for d in docs}),
        "replaced_chunks": removed,
        "seconds": round(time.perf_counter() - t0, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="chunk only, no Azure calls")
    args = parser.parse_args()

    docs = load_chunks()
    lengths = [len(d["content"]) for d in docs]
    pages = len({d["page"] for d in docs})
    print(f"Chunks: {len(docs)} from {pages} pages "
          f"(size {CHUNK_SIZE}, overlap {CHUNK_OVERLAP}); "
          f"avg {sum(lengths) // len(lengths)} chars, est. {sum(lengths) // 4:,} tokens to embed")

    if args.dry_run:
        sample = docs[len(docs) // 3]
        print(f"\nSample chunk {sample['id']} (page {sample['page']}):\n{sample['content']}")
        return

    from config import EMBED_DIMENSIONS, INDEX, index_client

    index_client.create_or_update_index(build_index(INDEX, EMBED_DIMENSIONS))
    print(f"Index '{INDEX}' ready")
    print(index_pdf(PDF_PATH, DEFAULT_SOURCE))


if __name__ == "__main__":
    main()
