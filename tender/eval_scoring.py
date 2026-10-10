"""Evaluate score_fit on both labelled sets and report triage quality.

    python -m tender.eval_scoring

Sets:
  fictional  tender/data/notices.json       9 designed cases with known verdicts (regression tests)
  real       tender/data/real_notices.json  21 AusTender notices with reference labels

Triage = what the bid manager sees: shortlist (strong / moderate / confirm_before_bidding), reject (poor),
need_info (insufficient_information). Missing a good tender is the costliest error, so recall on shortlist matters most.
"""
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from tender.tools import SCORE_PROMPT_VERSION, score_fit

DATA = Path(__file__).parent / "data"
TRIAGE = {"strong": "shortlist", "moderate": "shortlist", "confirm_before_bidding": "shortlist",
          "poor": "reject", "insufficient_information": "need_info"}


def evaluate(path: Path) -> dict:
    cases = json.loads(path.read_text())
    rows, tokens = [], 0
    for c in cases:
        r = score_fit(c["tender_id"])
        tokens += r.get("tokens", 0)
        expected_triage = c.get("expected_triage") or TRIAGE[c["expected_verdict"]]
        rows.append({"tender_id": c["tender_id"], "title": c["title"][:60],
                     "expected": expected_triage, "got": TRIAGE[r["verdict"]],
                     "expected_verdict": c["expected_verdict"], "verdict": r["verdict"], "score": r["score"],
                     "service_line": r.get("service_line_matched"), "scale_fit": r.get("scale_fit"),
                     "rules_fired": r.get("rules_fired"), "confidence": c.get("label_confidence")})
    good = [x for x in rows if x["expected"] == "shortlist"]
    return {
        "cases": len(rows),
        "triage_agreement": sum(x["expected"] == x["got"] for x in rows),
        "verdict_agreement": sum(x["expected_verdict"] == x["verdict"] for x in rows),
        "shortlist_recall": f"{sum(x['got'] == 'shortlist' for x in good)}/{len(good)}",
        "false_shortlists": sum(x["got"] == "shortlist" and x["expected"] != "shortlist" for x in rows),
        "confusion": dict(Counter(f"{x['expected']}→{x['got']}" for x in rows)),
        "tokens": tokens,
        "rows": rows,
    }


def main() -> None:
    report = {"prompt_version": SCORE_PROMPT_VERSION,
              "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sets": {}}
    for name, file in (("fictional", "notices.json"), ("real", "real_notices.json")):
        r = evaluate(DATA / file)
        report["sets"][name] = r
        print(f"\n=== {name} ({r['cases']} cases) — {SCORE_PROMPT_VERSION} ===")
        print(f"triage agreement {r['triage_agreement']}/{r['cases']}"
              f" · exact verdict {r['verdict_agreement']}/{r['cases']}"
              f" · good tenders found {r['shortlist_recall']} · false shortlists {r['false_shortlists']}")
        for x in r["rows"]:
            if x["expected"] != x["got"]:
                print(f"  ✗ {x['tender_id']:24} expected {x['expected']:9} got {x['got']:9} "
                      f"({x['verdict']} {x['score']}, line={x['service_line'] or 'none'}, scale={x['scale_fit']}, "
                      f"rules={x['rules_fired']}, label confidence={x['confidence']})")
    out = DATA / f"eval_scoring_{SCORE_PROMPT_VERSION}.json"
    out.write_text(json.dumps(report, indent=2))
    tokens = sum(s["tokens"] for s in report["sets"].values())
    print(f"\nsaved {out.name} · cost ≈ ${tokens * 0.0000006:.3f}")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(".env")
    main()
