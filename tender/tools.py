"""Agent 2 (Bid Analyst) tools. Each tool is a plain Python function: JSON-serialisable in, JSON-serialisable out.

The Foundry agent decides WHICH tool to call; these functions decide WHAT the tool is allowed to do.
"""
import os
from pathlib import Path

from tender import store

PROFILE_PATH = Path(os.environ.get("TENDER_PROFILE", Path(__file__).parent / "data" / "horsell_profile.md"))


def load_profile() -> str:
    """Company capability profile (local file, not committed). Falls back to the fictional demo profile."""
    path = PROFILE_PATH if PROFILE_PATH.exists() else Path(__file__).parent / "data" / "company_profile.md"
    return path.read_text()


# ── Tool 1: get_tender ────────────────────────────────────────────────────────

def get_tender(tender_id: str) -> dict:
    """Everything known about a tender: notice, status, documents, and which analysis steps have run. No LLM."""
    notice = store.get_json(tender_id, "notice.json")
    if notice is None:
        return {"error": f"Unknown tender {tender_id!r}", "known_tenders": store.list_tenders()}
    status = store.get_json(tender_id, "status.json") or {}
    ingest = store.get_json(tender_id, "ingest.json")
    score = store.get_json(tender_id, "score.json")
    return {
        "tender_id": tender_id,
        "title": notice["title"],
        "agency": notice["agency"],
        "type": notice.get("type"),
        "closes": notice.get("closes"),
        "estimated_value_aud": notice.get("estimated_value_aud"),
        "description": notice["description"],
        "status": status.get("status"),
        "documents": store.list_sources(tender_id),
        "ingested": bool(ingest and ingest.get("chunks")),
        "has_requirements": store.get_json(tender_id, "requirements.json") is not None,
        "score": {k: score[k] for k in ("score", "verdict")} if score else None,
    }


# ── Tool 2: score_fit ─────────────────────────────────────────────────────────

import html  # noqa: E402
import re  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from typing import Literal  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402

from config import CHAT_DEPLOYMENT, aoai, search_client_for  # noqa: E402

TENDER_INDEX = "tender-docs"
SCORE_PROMPT_VERSION = "score-v4.3"
SHORT_NOTICE_WORDS = 25   # recorded as a review signal only. A word-count rule was tried in v4/v4.1 and removed:
#                           every threshold fixed one case and broke another (overfitting to a small label set).

# Certifications that commonly appear as mandatory conditions. If a tender requires one and the profile does not
# state it as held, a "strong" verdict is downgraded by code, whatever the model said.
CERTIFICATIONS = ["ISO 9001", "ISO 27001", "IRAP", "NV2", "PV"]

SCORE_SYSTEM = """You assess whether a company should bid for a government tender.
Compare the TENDER with the COMPANY PROFILE and fill in the scorecard.

Rubric:
- strong (75-100): the requested service is a core capability and nothing mandatory is missing.
- moderate (50-74): partly in scope, or capability is adjacent rather than core.
- poor (0-49): out of scope for this company (wrong type of service, scale or sector).
- insufficient_information: the tender does not say what service is wanted. Use score 0. Do not guess low.
- confirm_before_bidding: capability is strong, but a MANDATORY requirement (certification, clearance, insurance,
  headcount) is not stated as held in the profile.
Out of scope always means poor, even if mandatory requirements are also missing.

mandatory_checks: one entry for EACH mandatory condition that the TENDER TEXT explicitly states (words such as
must, required, mandatory, condition for participation). Copy the tender's exact words into tender_quote.
Never add requirements the tender text does not state (for example ABN or insurance when not mentioned).
If the tender states no mandatory conditions, return an empty list.
- met = "yes" if the profile states the capability, credential or experience, even in different words; put the
  supporting profile text in evidence.
- met = "no" if the profile says it is not held.
- met = "unknown" if the profile is silent about it.

service_line: the main thing being bought, mapped to EXACTLY one name from SERVICE LINES, or "none".
Use "none" when the core work is a different discipline (for example research, evaluation, clinical care,
construction, goods supply), even if it shares general skills such as project management or writing.
Adjacent general consulting is "none".

scale_fit:
- within_reach: individuals, small teams, task orders, panels or discrete packages.
- prime_scale: a large multi-disciplinary facility, multi-year prime contract, or a large standing team.
- unclear: the tender does not say.

Rules: use only facts in the TENDER and PROFILE. Each reason and gap must be specific (name the service,
certification or requirement). Never claim the company holds something the profile does not state."""


class MandatoryCheck(BaseModel):
    requirement: str
    tender_quote: str = Field(description="Exact words from the tender stating this requirement")
    met: Literal["yes", "no", "unknown"]
    evidence: str = Field(description="Profile text supporting 'yes'; empty otherwise")


class FitScore(BaseModel):
    service_line: str = Field(description="Exact name from SERVICE LINES, or 'none'")
    scale_fit: Literal["within_reach", "prime_scale", "unclear"]
    score: int = Field(description="0-100")
    verdict: Literal["strong", "moderate", "poor", "insufficient_information", "confirm_before_bidding"]
    reasons: list[str]
    gaps: list[str]
    mandatory_checks: list[MandatoryCheck]
    recommendation: str


def _mandatory_conditions(tender_id: str) -> str:
    """Mandatory conditions from the tender documents (empty if not ingested).

    Single source of truth: the complete list in requirements.json when extract_requirements has run;
    otherwise a quick top-3 search over the ingested chunks.
    """
    req = store.get_json(tender_id, "requirements.json")
    if req and req.get("conditions"):
        return "Conditions for participation (mandatory):\n" + "\n".join(
            f"[{tender_id}, page {c['page']}] {c['id'] or '-'}: {c['tender_quote']}" for c in req["conditions"])
    from retrieval import retrieve

    try:
        hits = retrieve("conditions for participation mandatory requirements certification clearance",
                        "hybrid", k=3, source=tender_id, client=search_client_for(TENDER_INDEX))
    except Exception:
        return ""
    return "\n\n".join(f"[{h['source']}, page {h['page']}]\n{h['content']}" for h in hits)


def _unproven_certifications(tender_text: str, profile: str) -> list[str]:
    """Deterministic check: certifications the tender mentions that the profile does not state as held."""
    missing = []
    for cert in CERTIFICATIONS:
        if re.search(rf"\b{re.escape(cert)}\b", tender_text):
            lines = [ln for ln in profile.splitlines() if cert in ln]
            held = any(not re.search(r"not (stated|held)|absent|\bno\b", ln, re.IGNORECASE) for ln in lines)
            if not held:
                missing.append(cert)
    return missing


def _norm(text: str) -> str:
    """Compare text as words only: HTML tags (table cells) and entities removed, case and punctuation ignored."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _quoted_in(quote: str, text: str) -> bool:
    """True if the quote (ignoring case, punctuation and spacing) appears in the text. Short quotes don't count."""
    q = _norm(quote)
    return len(q) >= 12 and q in _norm(text)


def service_lines(profile: str) -> list[str]:
    """Service-line names from the profile's '## Service lines' list, e.g. '1. **Publications development…**'."""
    section = re.search(r"## Service lines\n(.*?)(\n## |\Z)", profile, re.S)
    if not section:
        return []
    return [m.strip().rstrip(".") for m in re.findall(r"^\s*\d+\.\s+\*\*(.+?)\*\*", section.group(1), re.M)]


def _match_line(chosen: str, lines: list[str]) -> str | None:
    """The profile service line the model chose (tolerant of case and punctuation), or None."""
    c = _norm(chosen)
    if not c or c == "none":
        return None
    return next((ln for ln in lines if _norm(ln) == c or _norm(ln).startswith(c) or c.startswith(_norm(ln))), None)


def _final_verdict(model_verdict: str, score: int, blockers: list[str], *, short_notice: bool = False,  # noqa: ARG001
                   in_service_line: bool | None = None, scale_fit: str = "unclear") -> tuple[str, list[str]]:
    """Deterministic precedence. Returns (verdict, rules that fired) so every decision is explainable."""
    # "Can't tell what's being bought" wins over "no service line": a wrong need_info costs a 30-second look,
    # a wrong reject hides a possible opportunity for good.
    if model_verdict == "insufficient_information":
        return model_verdict, ["model_insufficient_information"]
    if in_service_line is False:      # core work isn't one of the company's service lines
        return "poor", ["no_matching_service_line"]
    if scale_fit == "prime_scale":
        return "poor", ["prime_scale"]
    if score < 50:
        return "poor", ["score_below_50"]
    if blockers:
        return "confirm_before_bidding", ["mandatory_blockers"]
    return ("strong", ["score_75_plus"]) if score >= 75 else ("moderate", ["score_50_74"])


def score_fit(tender_id: str) -> dict:
    """Score one tender against the company profile. Saves score.json and moves status to 'scored'."""
    notice = store.get_json(tender_id, "notice.json")
    if notice is None:
        return {"error": f"Unknown tender {tender_id!r}"}
    profile = load_profile()
    conditions = _mandatory_conditions(tender_id)
    tender_text = (f"Title: {notice['title']}\nAgency: {notice['agency']}\nType: {notice.get('type')}\n"
                   f"Category: {notice.get('category')}\nDescription: {notice['description']}")
    if conditions:
        tender_text += f"\n\nFrom the tender documents:\n{conditions}"

    lines = service_lines(profile)
    lines_text = "\n".join(f"- {ln}" for ln in lines) or "- (profile lists none; answer 'none')"
    resp = aoai.chat.completions.parse(
        model=CHAT_DEPLOYMENT, temperature=0, response_format=FitScore,
        messages=[{"role": "system", "content": SCORE_SYSTEM},
                  {"role": "user", "content": f"TENDER:\n{tender_text}\n\nSERVICE LINES:\n{lines_text}"
                                              f"\n\nCOMPANY PROFILE:\n{profile}"}],
    )
    result = resp.choices[0].message.parsed.model_dump()

    # AI gathers evidence; code decides.
    # 1. Grounding check: keep only conditions whose quote really appears in the tender text (drops invented ones).
    grounded, dropped = [], []
    for c in result["mandatory_checks"]:
        (grounded if _quoted_in(c["tender_quote"], tender_text) else dropped).append(c)
    result["mandatory_checks"], result["dropped_checks"] = grounded, dropped
    # 2. Blockers = grounded conditions not evidenced in the profile + certification guard.
    blockers = [f"{c['requirement']} (profile: {c['met']})" for c in grounded if c["met"] != "yes"]
    for cert in _unproven_certifications(tender_text, profile):
        if not any(cert in b for b in blockers):
            blockers.append(f"{cert} required by the tender (profile: not stated)")
    result["mandatory_blockers"] = blockers
    # 3. Service line, scale and notice length, decided by code.
    matched = _match_line(result["service_line"], lines)
    result["service_line_matched"] = matched
    result["short_notice"] = len(notice["description"].split()) < SHORT_NOTICE_WORDS and not conditions
    result["model_verdict"] = result["verdict"]
    result["verdict"], result["rules_fired"] = _final_verdict(
        result["model_verdict"], result["score"], blockers, short_notice=result["short_notice"],
        in_service_line=(matched is not None) if lines else None, scale_fit=result["scale_fit"])
    result["guard_applied"] = result["verdict"] != result["model_verdict"]

    result.update({"tender_id": tender_id, "prompt_version": SCORE_PROMPT_VERSION, "model": CHAT_DEPLOYMENT,
                   "used_documents": bool(conditions),
                   "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "tokens": resp.usage.total_tokens})
    store.put_json(tender_id, "score.json", result)
    if store.get_status(tender_id) in ("new", "scored"):
        store.set_status(tender_id, "scored", by="agent2.score_fit", note=f"{result['verdict']} ({result['score']})")
    return result


# ── Tool 3: extract_requirements ──────────────────────────────────────────────

REQUIREMENTS_PROMPT_VERSION = "req-v1"
MAX_TENDER_CHARS = 60_000   # ~15k tokens; larger packs need section-targeted extraction (not needed for MVP)

REQUIREMENTS_SYSTEM = """You extract the response requirements from a government tender into a checklist.
Read ALL excerpts. Extract every item; do not summarise or merge rows.

- conditions: every mandatory condition for participation (exclusion if not met).
- evaluation_criteria: every criterion with its weighting as a number (e.g. 35 for "35%"); null if not stated.
- response_parts: every part the response must contain, with its page limit as written (e.g. "15 pages").
- key_dates: every milestone with its date as written. closing_time: the tender closing date and time as written.
- scope_items: each service or deliverable in the scope.
- pricing_items: each role or item the pricing schedule asks to be priced.

For every item, copy the tender's exact words into tender_quote and give the page number of the excerpt.
Use only the excerpts. If something is not stated, leave the list empty or the field null."""


class Condition(BaseModel):
    id: str | None
    text: str
    tender_quote: str
    page: int


class Criterion(BaseModel):
    number: str | None
    name: str
    weight_percent: float | None
    tender_quote: str
    page: int


class ResponsePart(BaseModel):
    part: str
    content: str
    page_limit: str | None
    tender_quote: str
    page: int


class KeyDate(BaseModel):
    milestone: str
    date_text: str
    tender_quote: str
    page: int


class PricingItem(BaseModel):
    role: str
    unit: str | None
    tender_quote: str


class Requirements(BaseModel):
    conditions: list[Condition]
    evaluation_criteria: list[Criterion]
    response_parts: list[ResponsePart]
    key_dates: list[KeyDate]
    closing_time: str | None
    scope_items: list[str]
    pricing_items: list[PricingItem]


def _tender_chunks(tender_id: str) -> list[dict]:
    """Every chunk of one tender, in document order (page, then position)."""
    from ingest import odata_quote

    rows = search_client_for(TENDER_INDEX).search(
        search_text="*", filter=f"source eq {odata_quote(tender_id)}", top=1000,
        select=["id", "page", "content", "content_type", "section"])
    def order(r):
        n = re.search(r"-(\d+)$", r["id"])
        return (r["page"], int(n.group(1)) if n else 0)
    return sorted(rows, key=order)


def _table_rows_with_header(chunks: list[dict], header_word: str) -> int | None:
    """Data rows in the tender table whose header row contains header_word (e.g. 'weight'). None if no such table."""
    for c in chunks:
        if c["content_type"] != "table":
            continue
        rows = re.findall(r"<tr>(.*?)</tr>", c["content"], re.S)
        if rows and header_word in rows[0].lower():
            return len(rows) - 1
    return None


def check_requirements(req: dict, chunks: list[dict], tender_text: str) -> tuple[dict, list[str]]:
    """Deterministic checks on the extracted checklist. Returns (cleaned requirements, warnings)."""
    warnings, dropped = [], 0
    for key in ("conditions", "evaluation_criteria", "response_parts", "key_dates", "pricing_items"):
        kept = [item for item in req[key] if _quoted_in(item["tender_quote"], tender_text)]
        dropped += len(req[key]) - len(kept)
        req[key] = kept
    if dropped:
        warnings.append(f"{dropped} item(s) dropped: quote not found in the tender")

    weights = [c["weight_percent"] for c in req["evaluation_criteria"]]
    if weights and all(w is not None for w in weights) and abs(sum(weights) - 100) > 0.5:
        warnings.append(f"criteria weights sum to {sum(weights):g}%, not 100%")

    expected_rows = _table_rows_with_header(chunks, "weight")
    if expected_rows is not None and expected_rows != len(req["evaluation_criteria"]):
        warnings.append(f"criteria table has {expected_rows} rows but {len(req['evaluation_criteria'])} were extracted")

    if not req["closing_time"]:
        warnings.append("no closing time found")
    return req, warnings


def extract_requirements(tender_id: str) -> dict:
    """Compliance checklist from ALL of a tender's ingested chunks. Saves requirements.json."""
    chunks = _tender_chunks(tender_id)
    if not chunks:
        return {"error": f"No ingested documents for {tender_id!r}; run ingest_tender first."}
    tender_text = "\n\n".join(f"[page {c['page']}]\n{c['content']}" for c in chunks)
    truncated = len(tender_text) > MAX_TENDER_CHARS
    tender_text = tender_text[:MAX_TENDER_CHARS]

    resp = aoai.chat.completions.parse(
        model=CHAT_DEPLOYMENT, temperature=0, response_format=Requirements,
        messages=[{"role": "system", "content": REQUIREMENTS_SYSTEM},
                  {"role": "user", "content": f"TENDER {tender_id} EXCERPTS:\n{tender_text}"}],
    )
    req, warnings = check_requirements(resp.choices[0].message.parsed.model_dump(), chunks, tender_text)
    if truncated:
        warnings.append(f"tender text truncated at {MAX_TENDER_CHARS:,} characters")
    req.update({"tender_id": tender_id, "warnings": warnings, "prompt_version": REQUIREMENTS_PROMPT_VERSION,
                "model": CHAT_DEPLOYMENT, "chunks_read": len(chunks),
                "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tokens": resp.usage.total_tokens})
    store.put_json(tender_id, "requirements.json", req)
    return req


# ── Tool 4: ask_tender ────────────────────────────────────────────────────────

ASK_PROMPT_VERSION = "ask-v2"
NOT_FOUND = "This tender's documents don't say."

ASK_SYSTEM = f"""You answer questions about ONE government tender for a bid team.
Use ONLY the excerpts, each labelled [tender, page N] or [tender, notice].
- Answer in 1-4 sentences or a short list. Quote exact figures, dates and requirements.
- Cite every fact like (T-001, page 2) or (T-001, notice).
- If the excerpts do not contain the answer, reply exactly: "{NOT_FOUND}"
- Never use outside knowledge or guess."""


def _cited_pages(answer: str) -> set[int]:
    """Page numbers cited in the answer, e.g. '(T-001, page 2)' or 'pages 2 and 3'."""
    pages = set()
    for run in re.findall(r"pages?\s+((?:\d+(?:\s*(?:,|and|&|-)\s*)?)+)", answer, re.IGNORECASE):
        pages.update(int(n) for n in re.findall(r"\d+", run))
    return pages


def check_citations(answer: str, retrieved_pages: set[int]) -> list[str]:
    """Deterministic: flag citations to pages the model never saw, and factual answers with no citation."""
    warnings = []
    unseen = _cited_pages(answer) - retrieved_pages
    if unseen:
        warnings.append(f"cites page(s) {sorted(unseen)} that were not retrieved")
    if NOT_FOUND not in answer and not _cited_pages(answer) and "notice" not in answer.lower():
        warnings.append("answer has no citation")
    return warnings


def ask_tender(tender_id: str, question: str) -> dict:
    """Grounded Q&A over one tender's documents (falls back to the notice text if none are ingested)."""
    from retrieval import retrieve

    notice = store.get_json(tender_id, "notice.json")
    if notice is None:
        return {"error": f"Unknown tender {tender_id!r}", "known_tenders": store.list_tenders()}
    hits = retrieve(question, "hybrid_rerank", k=5, source=tender_id, client=search_client_for(TENDER_INDEX))
    # The public notice is always one excerpt: it carries facts the documents often omit (value, agency, type).
    value = notice.get("estimated_value_aud")
    notice_excerpt = (f"[{tender_id}, notice]\nTitle: {notice['title']}\nAgency: {notice['agency']}\n"
                      f"Type: {notice.get('type')}\nCloses: {notice.get('closes')}\n"
                      f"Estimated value: {f'AUD {value:,}' if value else 'not stated'}\n{notice['description']}")
    excerpts = "\n\n".join([notice_excerpt] + [f"[{tender_id}, page {h['page']}]\n{h['content']}" for h in hits])
    resp = aoai.chat.completions.create(
        model=CHAT_DEPLOYMENT, temperature=0, max_tokens=400,
        messages=[{"role": "system", "content": ASK_SYSTEM},
                  {"role": "user", "content": f"EXCERPTS:\n{excerpts}\n\nQUESTION: {question}"}],
    )
    answer = resp.choices[0].message.content.strip()
    pages = {h["page"] for h in hits}
    result = {"tender_id": tender_id, "question": question, "answer": answer,
              "source": "documents" if hits else "notice", "retrieved_pages": sorted(pages),
              "found": NOT_FOUND not in answer, "warnings": check_citations(answer, pages) if hits else [],
              "prompt_version": ASK_PROMPT_VERSION, "tokens": resp.usage.total_tokens}

    log = store.get_json(tender_id, "qa_log.json") or {"entries": []}
    log["entries"].append({k: result[k] for k in ("question", "answer", "retrieved_pages", "found", "warnings")}
                          | {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    store.put_json(tender_id, "qa_log.json", log)
    return result
