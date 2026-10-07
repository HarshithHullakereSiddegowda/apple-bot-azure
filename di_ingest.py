"""Layout-aware ingestion with Azure AI Document Intelligence: text, tables and figures.

    PDF -> prebuilt-layout (Markdown + figures) -> section-aware chunks
        text    paragraphs packed up to ~1,200 chars, cut only between paragraphs, prefixed with [Section]
        table   one whole table per chunk (large tables split by rows, header repeated)
        figure  cropped image -> GPT-4.1-mini description -> chunk
        page headers / footers / page numbers dropped
    -> embeddings -> its own index (default "apple-support-di"), so it can be A/B-tested against pypdf chunking.

Usage:
    python di_ingest.py --pages 9-13 --dry-run   # analyze a few pages, print chunks, no indexing
    python di_ingest.py                          # whole PDF -> index apple-support-di

Costs (paid S0 tier): layout ~$10 per 1,000 pages; figure descriptions ~$0.0003 each.
Results are cached in data/di/, so re-running with the same pages costs nothing.
"""
import argparse
import base64
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeOutputOption, AnalyzeResult, DocumentContentFormat
from azure.core.credentials import AzureKeyCredential
from azure.search.documents.indexes.models import SimpleField, SearchFieldDataType

from config import CHAT_DEPLOYMENT, EMBED_DIMENSIONS, aoai, index_client
from ingest import DEFAULT_SOURCE, PDF_PATH, build_index

DI_INDEX = "apple-support-di"
CACHE = Path("data/di")
TEXT_CHUNK_CHARS = 1200      # pack paragraphs up to this size
TABLE_CHUNK_CHARS = 3000     # split bigger tables by rows
MIN_FIGURE_INCHES = 1.2      # skip figures smaller than this in both directions (icons)
NOISE_ROLES = {"pageHeader", "pageFooter", "pageNumber"}
HEADING_ROLES = {"title", "sectionHeading"}

FIGURE_PROMPT = (
    "Describe this figure from a device manual so it can be found by search. "
    "Section: '{section}'. Caption: '{caption}'. State what it shows, any steps in order, and every visible "
    "label. Only describe what is visible; do not add facts. Max 90 words."
)


# ── 1. Analyze (cached) ───────────────────────────────────────────────────────

def analyze(pdf_path: str, pages: str | None) -> tuple[AnalyzeResult, dict[str, bytes]]:
    """Run prebuilt-layout once and download every figure in the same run (figures expire after 24 h)."""
    tag = (pages or "all").replace(",", "_")
    result_file, fig_dir = CACHE / f"layout_{tag}.json", CACHE / f"figures_{tag}"
    if result_file.exists():
        print(f"Using cached analysis {result_file}")
        figures = {p.stem: p.read_bytes() for p in fig_dir.glob("*.png")}
        return AnalyzeResult(json.loads(result_file.read_text())), figures

    client = DocumentIntelligenceClient(os.environ["DI_ENDPOINT"], AzureKeyCredential(os.environ["DI_KEY"]))
    print(f"Analyzing {pdf_path} (pages: {pages or 'all'}) with prebuilt-layout...")
    with open(pdf_path, "rb") as f:
        poller = client.begin_analyze_document(
            "prebuilt-layout", body=f, pages=pages,
            output_content_format=DocumentContentFormat.MARKDOWN,
            output=[AnalyzeOutputOption.FIGURES],
        )
    result = poller.result()
    result_id = poller.details["operation_id"]

    figures: dict[str, bytes] = {}
    for fig in result.figures or []:
        if _big_enough(fig):
            figures[fig.id] = b"".join(client.get_analyze_result_figure(
                model_id="prebuilt-layout", result_id=result_id, figure_id=fig.id))

    fig_dir.mkdir(parents=True, exist_ok=True)
    result_file.write_text(json.dumps(result.as_dict()))
    for fid, data in figures.items():
        (fig_dir / f"{fid}.png").write_bytes(data)
    print(f"Analyzed {len(result.pages)} pages; downloaded {len(figures)} of {len(result.figures or [])} figures")
    return result, figures


def _big_enough(fig) -> bool:
    xs, ys = fig.bounding_regions[0].polygon[0::2], fig.bounding_regions[0].polygon[1::2]
    return (max(xs) - min(xs)) >= MIN_FIGURE_INCHES or (max(ys) - min(ys)) >= MIN_FIGURE_INCHES


# ── 2. Describe figures (cached, parallel) ────────────────────────────────────

def describe_figures(figures: dict[str, bytes], context: dict[str, tuple[str, str]], tag: str) -> dict[str, str]:
    cache_file = CACHE / f"figure_descriptions_{tag}.json"
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    todo = [fid for fid in figures if fid not in cache]

    def one(fid: str) -> tuple[str, str]:
        section, caption = context.get(fid, ("", ""))
        b64 = base64.b64encode(figures[fid]).decode()
        # Batch job: retry 429s with backoff, honouring Azure's Retry-After header.
        r = aoai.with_options(max_retries=8).chat.completions.create(
            model=CHAT_DEPLOYMENT, temperature=0, max_tokens=200,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": FIGURE_PROMPT.format(section=section, caption=caption or "none")},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "low"}},
            ]}],
        )
        return fid, r.choices[0].message.content.strip()

    if todo:
        print(f"Describing {len(todo)} figures with {CHAT_DEPLOYMENT} (vision)...")
        # 3 workers stays under the deployment's tokens-per-minute limit; save after each one so a
        # failure never loses finished (paid-for) descriptions.
        failed = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(one, fid): fid for fid in todo}
            for n, fut in enumerate(as_completed(futures), start=1):
                try:
                    fid, text = fut.result()
                    cache[fid] = text
                    cache_file.write_text(json.dumps(cache, indent=2))
                except Exception as e:  # one bad figure must not stop the batch
                    failed.append(futures[fut])
                    print(f"  figure {futures[fut]} failed: {type(e).__name__}")
                print(f"  described {n}/{len(todo)}", end="\r")
        print()
        if failed:
            print(f"{len(failed)} figures not described (re-run to retry them): {failed}")
    return cache


# ── 3. Section-aware chunking ─────────────────────────────────────────────────

def _span(obj) -> tuple[int, int]:
    s = obj.spans[0]
    return s.offset, s.offset + s.length


def _page(obj) -> int:
    return obj.bounding_regions[0].page_number if obj.bounding_regions else 0


def _split_table(md: str) -> list[str]:
    """Keep small tables whole; split big HTML tables by rows, repeating the header row."""
    if len(md) <= TABLE_CHUNK_CHARS:
        return [md]
    rows = re.findall(r"<tr>.*?</tr>", md, flags=re.S)
    if len(rows) < 2:
        return [md[i:i + TABLE_CHUNK_CHARS] for i in range(0, len(md), TABLE_CHUNK_CHARS)]
    header, parts, current = rows[0], [], []
    for row in rows[1:]:
        if current and len(header) + sum(map(len, current)) + len(row) > TABLE_CHUNK_CHARS:
            parts.append(current)
            current = []
        current.append(row)
    parts.append(current)
    return ["<table>\n" + header + "\n" + "\n".join(p) + "\n</table>" for p in parts]


def build_chunks(result: AnalyzeResult, descriptions: dict[str, str], source: str) -> list[dict]:
    content = result.content
    tables = [(_span(t), _page(t), t) for t in result.tables or []]
    figures = [(_span(f), _page(f), f) for f in result.figures or []]
    inside = [span for span, _, _ in tables + figures]

    def in_block(offset: int) -> bool:
        return any(a <= offset < b for a, b in inside)

    # One ordered stream of elements by position in the document.
    elements = []
    for p in result.paragraphs or []:
        start, _ = _span(p)
        if (p.role or "") in NOISE_ROLES or in_block(start):
            continue
        kind = "heading" if (p.role or "") in HEADING_ROLES else "text"
        elements.append((start, kind, _page(p), p.content))
    for (start, end), page, _ in tables:
        elements.append((start, "table", page, content[start:end]))
    for (start, _), page, fig in figures:
        elements.append((start, "figure", page, fig))
    elements.sort(key=lambda e: e[0])

    chunks: list[dict] = []
    section, buf, buf_page = "", [], 0

    def add(kind: str, page: int, body: str) -> None:
        label = f"[{section}]\n" if section else ""
        chunks.append({"content": label + body, "page": page, "section": section,
                       "content_type": kind, "source": source})

    def flush() -> None:
        nonlocal buf
        if buf:
            add("text", buf_page, "\n".join(buf))
            buf = []

    for _, kind, page, payload in elements:
        if kind == "heading":
            flush()
            section = payload.strip()
        elif kind == "text":
            if buf and sum(map(len, buf)) + len(payload) > TEXT_CHUNK_CHARS:
                flush()
            if not buf:
                buf_page = page
            buf.append(payload)
        elif kind == "table":
            flush()
            for part in _split_table(payload):
                add("table", page, part)
        elif kind == "figure":
            desc = descriptions.get(payload.id)
            if desc:  # small icons were never downloaded or described
                caption = payload.caption.content if payload.caption else ""
                add("figure", page, (f"Figure: {caption}\n" if caption else "Figure:\n") + desc)
    flush()

    for n, c in enumerate(chunks):
        c["id"] = f"di-p{c['page']}-{n}"
    return [c for c in chunks if len(c["content"]) >= 30]


def figure_context(result: AnalyzeResult) -> dict[str, tuple[str, str]]:
    """Section heading and caption for each figure, given to the vision model as context."""
    heads = sorted((_span(p)[0], p.content) for p in result.paragraphs or [] if (p.role or "") in HEADING_ROLES)
    ctx = {}
    for fig in result.figures or []:
        start = _span(fig)[0]
        section = next((h for off, h in reversed(heads) if off <= start), "")
        ctx[fig.id] = (section, fig.caption.content if fig.caption else "")
    return ctx


# ── 4. Index ──────────────────────────────────────────────────────────────────

def ensure_index(name: str) -> None:
    """Rebuild from empty: chunk ids are positional, so leftovers from a previous run would become duplicates."""
    if name in set(index_client.list_index_names()):
        index_client.delete_index(name)
    index = build_index(name, EMBED_DIMENSIONS)
    index.fields += [
        SimpleField(name="section", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="content_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
    ]
    index_client.create_or_update_index(index)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", help="page range like 9-13 (default: whole PDF)")
    parser.add_argument("--dry-run", action="store_true", help="analyze + chunk only, no indexing")
    parser.add_argument("--index", default=DI_INDEX)
    args = parser.parse_args()

    result, figures = analyze(PDF_PATH, args.pages)
    tag = (args.pages or "all").replace(",", "_")
    descriptions = describe_figures(figures, figure_context(result), tag)
    chunks = build_chunks(result, descriptions, DEFAULT_SOURCE)

    by_type = {t: sum(c["content_type"] == t for c in chunks) for t in ("text", "table", "figure")}
    sizes = [len(c["content"]) for c in chunks]
    print(f"Chunks: {len(chunks)} {by_type}, avg {sum(sizes) // max(len(sizes), 1)} chars, "
          f"{len({c['section'] for c in chunks})} sections")

    if args.dry_run:
        for t in ("text", "table", "figure"):
            sample = next((c for c in chunks if c["content_type"] == t), None)
            if sample:
                print(f"\n--- sample {t} chunk {sample['id']} (page {sample['page']}) ---\n{sample['content'][:600]}")
        return

    from config import search_client_for
    from ingest import upload

    ensure_index(args.index)
    upload(chunks, client=search_client_for(args.index))
    print(f"Indexed {len(chunks)} chunks into '{args.index}'")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(".env")
    main()
