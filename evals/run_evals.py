"""Two exams over evals/golden.json.

Exam 1 - Retrieval (all 4 modes, in-scope questions only):
    hit@5  did any expected page appear in the top 5?
    MRR    1 / rank of the first correct page (1.0 = always first), averaged

Exam 2 - Answers (hybrid_rerank, the production mode, all questions):
    groundedness    share of the answer's claims supported by the retrieved excerpts (LLM judge)
    completeness    share of the question's parts the answer addresses (LLM judge, sees no excerpts)
    citation        answer cites at least one expected page
    false refusal   in-scope question wrongly answered "I couldn't find that"
    refusal         out-of-scope question correctly refused

Usage (from the repo root):
    python evals/run_evals.py            # writes evals/report.md and evals/results.json
"""
import json
import re
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import generate  # noqa: E402
from config import CHAT_DEPLOYMENT, aoai  # noqa: E402
from ingest import DEFAULT_SOURCE  # noqa: E402
from retrieval import MODES, retrieve  # noqa: E402

# The golden set's expected pages refer to this document. Pin retrieval to it so pages from
# uploaded documents can never count as hits.
EVAL_SOURCE = DEFAULT_SOURCE

K = 5
REFUSAL = "couldn't find that"
GROUNDED_PASS = 0.7

JUDGE_PROMPT = """You are a strict grader. Split the ANSWER into its individual factual claims
(each step or statement of fact). For each claim, decide whether the EXCERPTS directly support it.
Page citations like "(page 32)" are not claims. Ignore them.

Reply with JSON only:
{"total_claims": <int>, "supported_claims": <int>, "unsupported": ["<claim>", ...]}"""

COMPLETENESS_PROMPT = """You are a strict grader. List every distinct part the QUESTION asks for
(e.g. "how to back up with iCloud" and "whether iTunes can also be used" are two parts).
For each part, decide whether the ANSWER actually addresses it. Do not judge whether the answer
is true; only whether each part of the question gets an answer.

Reply with JSON only:
{"parts_asked": ["<part>", ...], "parts_answered": <int>, "missing": ["<part>", ...]}"""


def cited_pages(answer: str) -> set[int]:
    """Every page number the answer cites: 'page 32', 'pages 132 and 135', 'pages 49, 50, 139'."""
    pages = set()
    for run in re.findall(r"pages?\s+((?:\d+(?:\s*(?:,|and|&|-)\s*)?)+)", answer, flags=re.IGNORECASE):
        pages.update(int(n) for n in re.findall(r"\d+", run))
    return pages


def judge(answer: str, hits: list[dict]) -> dict:
    excerpts = "\n\n".join(f"[page {h['page']}]\n{h['content']}" for h in hits)
    resp = aoai.chat.completions.create(
        model=CHAT_DEPLOYMENT,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": f"EXCERPTS:\n{excerpts}\n\nANSWER:\n{answer}"},
        ],
    )
    verdict = json.loads(resp.choices[0].message.content)
    total = max(int(verdict.get("total_claims", 0)), 1)
    verdict["score"] = min(int(verdict.get("supported_claims", 0)) / total, 1.0)
    verdict["tokens"] = resp.usage.total_tokens
    return verdict


def judge_completeness(question: str, answer: str) -> dict:
    """Question <-> answer leg of the RAG triad. Sees no excerpts, on purpose."""
    resp = aoai.chat.completions.create(
        model=CHAT_DEPLOYMENT,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": COMPLETENESS_PROMPT},
            {"role": "user", "content": f"QUESTION:\n{question}\n\nANSWER:\n{answer}"},
        ],
    )
    verdict = json.loads(resp.choices[0].message.content)
    asked = max(len(verdict.get("parts_asked", [])), 1)
    verdict["score"] = min(int(verdict.get("parts_answered", 0)) / asked, 1.0)
    verdict["tokens"] = resp.usage.total_tokens
    return verdict


# ── Exam 1: retrieval ─────────────────────────────────────────────────────────

def exam_retrieval(cases: list[dict]) -> dict:
    in_scope = [c for c in cases if c["expected_pages"]]
    out = {}
    for mode in MODES:
        rows = []
        for c in in_scope:
            pages = [h["page"] for h in retrieve(c["query"], mode, K, source=EVAL_SOURCE)]
            rank = next((i + 1 for i, p in enumerate(pages) if p in c["expected_pages"]), None)
            rows.append({"id": c["id"], "style": c["style"], "pages": pages, "rank": rank})
        by_style = defaultdict(list)
        for r in rows:
            by_style[r["style"]].append(r["rank"] is not None)
        out[mode] = {
            "hit_at_5": sum(r["rank"] is not None for r in rows) / len(rows),
            "mrr": statistics.mean(1 / r["rank"] if r["rank"] else 0 for r in rows),
            "by_style": {s: sum(v) / len(v) for s, v in by_style.items()},
            "misses": [r["id"] for r in rows if r["rank"] is None],
            "rows": rows,
        }
    return out


# ── Exam 2: answers ───────────────────────────────────────────────────────────

def exam_answers(cases: list[dict], mode: str = "hybrid_rerank") -> list[dict]:
    rows = []
    for c in cases:
        t0 = time.perf_counter()
        hits = retrieve(c["query"], mode, K, source=EVAL_SOURCE)
        resp = generate(c["query"], hits)
        answer = resp.choices[0].message.content
        latency_ms = int((time.perf_counter() - t0) * 1000)
        refused = REFUSAL in answer.lower()
        row = {
            "id": c["id"], "style": c["style"], "answer": answer, "refused": refused,
            "latency_ms": latency_ms,
            "tokens": resp.usage.prompt_tokens + resp.usage.completion_tokens,
        }
        if c.get("expect_refusal"):
            row["correct_refusal"] = refused
        elif not refused:
            v = judge(answer, hits)
            comp = judge_completeness(c["query"], answer)
            row.update({
                "groundedness": v["score"],
                "unsupported": v.get("unsupported", []),
                "completeness": comp["score"],
                "parts_asked": comp.get("parts_asked", []),
                "missing_parts": comp.get("missing", []),
                "cites_expected_page": bool(cited_pages(answer) & set(c["expected_pages"])),
                "judge_tokens": v["tokens"] + comp["tokens"],
            })
        rows.append(row)
        print(f"  answered {len(rows)}/{len(cases)}", end="\r", file=sys.stderr)
    print(file=sys.stderr)
    return rows


# ── Report ────────────────────────────────────────────────────────────────────

def pct(x: float) -> str:
    return f"{x:.0%}"


def report(cases: list[dict], ret: dict, ans: list[dict]) -> str:
    in_scope = [c for c in cases if c["expected_pages"]]
    styles = sorted({c["style"] for c in in_scope})
    lines = [
        "# Eval report",
        "",
        f"{len(cases)} questions ({len(in_scope)} in scope, {len(cases) - len(in_scope)} out of scope), "
        f"top-{K} retrieval, chat model `{CHAT_DEPLOYMENT}`.",
        "",
        "## Exam 1: Retrieval",
        "",
        "| Mode | hit@5 | MRR | " + " | ".join(styles) + " |",
        "| --- | --- | --- | " + " | ".join("---" for _ in styles) + " |",
    ]
    for mode in MODES:
        r = ret[mode]
        lines.append(f"| {mode} | {pct(r['hit_at_5'])} | {r['mrr']:.2f} | "
                     + " | ".join(pct(r["by_style"].get(s, 0)) for s in styles) + " |")
    lines += ["", "Misses (no expected page in top 5):", ""]
    for mode in MODES:
        lines.append(f"- **{mode}**: {', '.join(ret[mode]['misses']) or 'none'}")

    answered = [a for a in ans if "groundedness" in a]
    in_scope_ans = [a for a in ans if a["style"] != "out_of_scope"]
    oos = [a for a in ans if "correct_refusal" in a]
    g = [a["groundedness"] for a in answered]
    cs = [a["completeness"] for a in answered]
    lat = sorted(a["latency_ms"] for a in ans)
    p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))]
    lines += [
        "",
        "## Exam 2: Answers (hybrid_rerank)",
        "",
        "| Metric | Result |",
        "| --- | --- |",
        f"| Groundedness, mean | {statistics.mean(g):.2f} |" if g else "| Groundedness, mean | n/a |",
        f"| Grounded answers (score >= {GROUNDED_PASS}) | {sum(s >= GROUNDED_PASS for s in g)}/{len(g)} |",
        f"| Completeness, mean | {statistics.mean(cs):.2f} |" if cs else "| Completeness, mean | n/a |",
        f"| Fully complete answers (every part answered) | {sum(s >= 1.0 for s in cs)}/{len(cs)} |",
        f"| Cites an expected page | {sum(a['cites_expected_page'] for a in answered)}/{len(answered)} |",
        f"| False refusals (in-scope answered 'couldn't find') | "
        f"{sum(a['refused'] for a in in_scope_ans)}/{len(in_scope_ans)} |",
        f"| Correct refusals (out-of-scope) | {sum(a['correct_refusal'] for a in oos)}/{len(oos)} |",
        f"| Latency p50 / p95 (retrieve + generate) | {lat[len(lat) // 2]} ms / {p95} ms |",
        "",
        "Lowest groundedness:",
        "",
    ]
    for a in sorted(answered, key=lambda a: a["groundedness"])[:3]:
        lines.append(f"- **{a['id']}** ({a['groundedness']:.2f}): {'; '.join(a['unsupported'][:2]) or '-'}")

    by_style = defaultdict(list)
    for a in answered:
        by_style[a["style"]].append(a["completeness"])
    lines += ["", "Completeness by question style:", "",
              "| Style | Mean completeness | Fully complete |", "| --- | --- | --- |"]
    for s in sorted(by_style):
        v = by_style[s]
        lines.append(f"| {s} | {statistics.mean(v):.2f} | {sum(x >= 1.0 for x in v)}/{len(v)} |")
    lines += ["", "Lowest completeness:", ""]
    for a in sorted(answered, key=lambda a: a["completeness"])[:3]:
        lines.append(f"- **{a['id']}** ({a['completeness']:.2f}) missing: "
                     f"{'; '.join(a['missing_parts'][:2]) or '-'}")
    tokens = sum(a["tokens"] + a.get("judge_tokens", 0) for a in ans)
    lines += ["", f"Tokens for exam 2 (answers + judge): {tokens:,}"]
    return "\n".join(lines) + "\n"


def main() -> None:
    cases = json.loads((ROOT / "evals" / "golden.json").read_text())
    print("Exam 1: retrieval across 4 modes...", file=sys.stderr)
    ret = exam_retrieval(cases)
    print("Exam 2: answers + judge...", file=sys.stderr)
    ans = exam_answers(cases)

    md = report(cases, ret, ans)
    (ROOT / "evals" / "report.md").write_text(md)
    (ROOT / "evals" / "results.json").write_text(
        json.dumps({"retrieval": ret, "answers": ans}, indent=2))
    print(md)


if __name__ == "__main__":
    main()
