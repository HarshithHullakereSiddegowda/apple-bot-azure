"""Retrieval: four ways to find the best chunks for a question.

    keyword        BM25 over `content`                       (exact words)
    vector         HNSW nearest neighbours over `embedding`  (meaning)
    hybrid         both, merged with Reciprocal Rank Fusion  (words + meaning)
    hybrid_rerank  hybrid, then the semantic ranker re-reads the top 50 (best precision)

Usage:
    python retrieval.py                       # compare all modes on sample questions
    python retrieval.py "your question here"  # compare all modes on one question
"""
import sys

from azure.search.documents.models import VectorizedQuery

from config import embed, search_client

MODES = ["keyword", "vector", "hybrid", "hybrid_rerank"]
K_NEAREST = 50  # vector candidates handed to the merge / reranker


def retrieve(question: str, mode: str = "hybrid_rerank", k: int = 5,
             source: str | None = None, client=None) -> list[dict]:
    """Return the top-k chunks as dicts: id, source, page, content, score.

    source: limit the search to one document (exact name); None searches every document.
    client: a SearchClient for another index (e.g. tender-docs); default is the app's index.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")

    options: dict = {}
    if source:
        options["filter"] = "source eq '" + source.replace("'", "''") + "'"
    if mode != "vector":
        options["search_text"] = question                      # keyword half
    if mode != "keyword":
        options["vector_queries"] = [VectorizedQuery(          # meaning half
            vector=embed([question])[0],
            k_nearest_neighbors=K_NEAREST,
            fields="embedding",
        )]
        if source:
            options["vector_filter_mode"] = "preFilter"        # filter BEFORE finding neighbours
    if mode == "hybrid_rerank":
        options["query_type"] = "semantic"                     # reranker on top
        options["semantic_configuration_name"] = "sem"

    results = (client or search_client).search(top=k, select=["id", "source", "page", "content"], **options)
    return [
        {
            "id": r["id"],
            "source": r.get("source") or "unknown",
            "page": r["page"],
            "content": r["content"],
            # reranker score (0-4) when reranking, otherwise the search score
            "score": r.get("@search.reranker_score") or r["@search.score"],
        }
        for r in results
    ]


def compare(question: str, k: int = 3) -> None:
    print(f"\nQ: {question}")
    for mode in MODES:
        hits = retrieve(question, mode, k)
        summary = "  ".join(f"p{h['page']}({h['score']:.2f})" for h in hits)
        print(f"  {mode:14} {summary}")


if __name__ == "__main__":
    questions = sys.argv[1:] or [
        "How do I pair a Bluetooth device with my iPhone?",   # wording matches the manual
        "my phone keeps dying too fast",                      # paraphrase, no shared words
        "How do I save a webpage to read later?",             # synonym: "Reading List"
    ]
    for q in questions:
        compare(q)
