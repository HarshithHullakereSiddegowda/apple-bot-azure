"""FastAPI service: chat page, grounded Q&A with citations, document upload.

    GET  /            chat page (static/index.html)
    GET  /health      liveness check, no auth
    GET  /documents   documents in the index with chunk counts        (public, rate-limited)
    POST /ask         question -> retrieve chunks -> grounded answer   (public, rate-limited)
    POST /upload      PDF -> chunk -> embed -> index                   (X-API-Key only)

Run locally:
    uvicorn app:app --port 8010 --reload       # then open http://localhost:8010
"""
import io
import json
import logging
import os
import secrets
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from config import APP_API_KEY, CHAT_DEPLOYMENT, aoai, search_client
from ratelimit import RateLimiter, client_ip, visitor_id
from retrieval import MODES, retrieve

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("apple-rag")

app = FastAPI(title="Apple Support RAG on Azure")
STATIC = Path(__file__).resolve().parent / "static"

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB
MAX_UPLOAD_PAGES = 200               # keeps one upload to ~1-2 minutes and protects the 50 MB Free tier

# The rules the writer must follow. Excerpts are labelled with their document and page.
SYSTEM_PROMPT = (
    "You are a support assistant. Your only source is the excerpts below, each labelled with its "
    "document name and page number.\n"
    "Rules:\n"
    "1. Answer ONLY from the excerpts. Do not use outside knowledge.\n"
    "2. Use numbered steps for procedures.\n"
    "3. Cite what you used, like (iPhone User Guide, page 32).\n"
    "4. If the excerpts do not contain the answer, reply exactly: "
    "\"I couldn't find that in the documents.\"\n"
    "5. Do not invent prices, dates, model names or settings that are not in the excerpts."
)


# ── Auth ──────────────────────────────────────────────────────────────────────

def require_api_key(x_api_key: str = Header(default="")) -> str:
    """Uploads only. Fail closed: no configured key, or a wrong key, means 401. compare_digest avoids timing leaks."""
    if APP_API_KEY and secrets.compare_digest(x_api_key, APP_API_KEY):
        return "api-key"
    raise HTTPException(401, "Missing or invalid X-API-Key header")


# ── Rate limits (public routes) ───────────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


ask_per_visitor = RateLimiter([(_env_int("ASK_PER_MINUTE", 10), 60), (_env_int("ASK_PER_DAY", 100), 86_400)])
ask_global = RateLimiter([(_env_int("ASK_GLOBAL_PER_DAY", 1000), 86_400)])  # cost ceiling, ~$0.70/day
docs_per_visitor = RateLimiter([(_env_int("DOCS_PER_MINUTE", 30), 60)])


def _too_many(retry_after: int, message: str) -> HTTPException:
    return HTTPException(429, f"{message} Try again in {retry_after} s.", headers={"Retry-After": str(retry_after)})


def limit_ask(request: Request) -> str:
    """Per-visitor and global limits for /ask. Returns a hashed visitor id for the logs."""
    ip = client_ip(request)
    if (wait := ask_per_visitor.check(ip)) is not None:
        raise _too_many(wait, "Too many questions from you.")
    if (wait := ask_global.check("global")) is not None:
        raise _too_many(wait, "The demo's daily question limit has been reached.")
    return f"visitor:{visitor_id(ip)}"


def limit_docs(request: Request) -> None:
    if (wait := docs_per_visitor.check(client_ip(request))) is not None:
        raise _too_many(wait, "Too many requests.")


# ── Models ────────────────────────────────────────────────────────────────────

class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000, examples=["How do I pair a Bluetooth device?"])
    mode: str = Field(default="hybrid_rerank", examples=MODES)
    k: int = Field(default=5, ge=1, le=10)
    source: str | None = Field(default=None, description="Limit to one document; omit to search all")


class Citation(BaseModel):
    source: str
    page: int


class AskResponse(BaseModel):
    answer: str
    pages: list[int]
    citations: list[Citation]
    mode: str
    latency_ms: int
    request_id: str


# ── Generation ────────────────────────────────────────────────────────────────

def build_user_prompt(question: str, hits: list[dict]) -> str:
    excerpts = "\n\n".join(
        f"[{h.get('source', 'document')}, page {h['page']}]\n{h['content']}" for h in hits)
    return f"Excerpts:\n{excerpts}\n\nQuestion: {question}"


def generate(question: str, hits: list[dict]):
    """The writer step. Shared by /ask and the evals, so the exam tests the real code."""
    return aoai.chat.completions.create(
        model=CHAT_DEPLOYMENT,
        temperature=0,
        max_tokens=500,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(question, hits)},
        ],
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/documents", dependencies=[Depends(limit_docs)])
def documents() -> list[dict]:
    """Every document in the index and how many chunks it has (Azure AI Search facets)."""
    facets = search_client.search(search_text="*", facets=["source,count:100"], top=0).get_facets()
    return [{"source": f["value"], "chunks": f["count"]} for f in facets.get("source", [])]


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest, caller: str = Depends(limit_ask)) -> AskResponse:
    if req.mode not in MODES:
        raise HTTPException(422, f"mode must be one of {MODES}")

    request_id, t0 = str(uuid.uuid4()), time.perf_counter()

    hits = retrieve(req.question, req.mode, req.k, source=req.source)
    t_retrieved = time.perf_counter()
    if not hits:
        raise HTTPException(404, "No relevant content found")

    resp = generate(req.question, hits)
    t_done = time.perf_counter()

    latency_ms = int((t_done - t0) * 1000)
    log.info(json.dumps({
        "event": "ask",
        "request_id": request_id,
        "caller": caller,
        "mode": req.mode,
        "source_filter": req.source,
        "latency_ms": latency_ms,
        "retrieval_ms": int((t_retrieved - t0) * 1000),
        "generation_ms": int((t_done - t_retrieved) * 1000),
        "pages": [h["page"] for h in hits],
        "tokens_in": resp.usage.prompt_tokens,
        "tokens_out": resp.usage.completion_tokens,
    }))

    citations = sorted({(h.get("source", "unknown"), h["page"]) for h in hits})
    return AskResponse(
        answer=resp.choices[0].message.content,
        pages=sorted({h["page"] for h in hits}),
        citations=[Citation(source=s, page=p) for s, p in citations],
        mode=req.mode,
        latency_ms=latency_ms,
        request_id=request_id,
    )


@app.post("/upload")
def upload_document(file: UploadFile = File(...), caller: str = Depends(require_api_key)) -> dict:
    """Index a new PDF (or replace one with the same file name). Synchronous: ~1-2 min for 200 pages."""
    from pypdf import PdfReader

    from ingest import index_pdf, slugify

    name = Path(file.filename or "").name
    if not name.lower().endswith(".pdf"):
        raise HTTPException(415, "Only PDF files are supported")

    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)")
    if not data.startswith(b"%PDF"):
        raise HTTPException(415, "File is not a valid PDF")

    try:
        pages = len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        raise HTTPException(422, "Could not read the PDF")
    if pages > MAX_UPLOAD_PAGES:
        raise HTTPException(413, f"Too many pages ({pages}, max {MAX_UPLOAD_PAGES})")

    source = name[:-4].strip()[:100] or "Uploaded document"
    try:
        result = index_pdf(io.BytesIO(data), source, id_prefix=slugify(name) + "-")
    except ValueError as e:
        raise HTTPException(422, str(e))

    log.info(json.dumps({"event": "upload", "caller": caller, **result, "bytes": len(data)}))
    return result
