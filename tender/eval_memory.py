"""Choose memory.MIN_SCORE from data: real embeddings over labelled (question, memory) pairs.

For every question we label which memories SHOULD be recalled. All other pairs should not. We embed everything with the
real model, score every pair, and report: the score ranges of matching vs non-matching pairs, precision / recall / F1
at each candidate threshold, and which questions would be missed. Read-only: nothing is saved to memory.

    python -m tender.eval_memory
"""
from config import embed
from tender.memory import MIN_SCORE, _cosine

MEMORIES = {
    "min_value": "Horsell does not bid on contracts below $200k",
    "canberra": "Horsell avoids work that needs staff based in Canberra",
    "iso27001": "Horsell has no ISO 27001 certification yet; expected mid-2027",
    "t003": "T-003 (Logistics support, score 82 strong): AI recommended bid; Jane Smith decided no_bid: "
            "too far from Canberra for our team",
    "prefer_defence": "Prefer Defence logistics and maritime support work over general IT services",
}

# question → memories that should be recalled (agent-style short queries, as seen in the first live trace)
LABELS = {
    "cyber security tender, $150k": {"min_value", "iso27001"},
    "should we bid on a $90,000 contract": {"min_value"},
    "small value contract, $50k": {"min_value"},
    "tender requiring personnel on site in Canberra": {"canberra", "t003"},
    "travel to the ACT required weekly": {"canberra", "t003"},
    "ISO 27001 certified supplier required": {"iso27001"},
    "information security management system certification": {"iso27001"},
    "past decisions on logistics tenders": {"t003", "prefer_defence"},
    "Navy ship maintenance logistics, Sydney, $1.2M": {"prefer_defence"},
    "IT helpdesk services for a council": {"prefer_defence"},
    "office cleaning services, Brisbane, $400k": set(),             # nothing relevant: everything should stay out
}

# the same need, asked briefly vs descriptively (does a better recall query help?)
QUERY_STYLES = [("cyber security tender, $150k",
                 "contract value $150k: any minimum contract value rule? cyber security services")]


def main() -> None:
    names, questions = list(MEMORIES), list(LABELS)
    vectors = embed([MEMORIES[n] for n in names] + questions + [q for pair in QUERY_STYLES for q in pair])
    mem_vec = dict(zip(names, vectors[:len(names)], strict=True))
    q_vec = dict(zip(questions + [q for pair in QUERY_STYLES for q in pair], vectors[len(names):], strict=True))

    pairs = [(q, n, _cosine(q_vec[q], mem_vec[n]), n in LABELS[q]) for q in questions for n in names]
    pos = sorted(s for *_, s, ok in pairs if ok)
    neg = sorted(s for *_, s, ok in pairs if not ok)
    print(f"{len(pairs)} pairs: {len(pos)} should match, {len(neg)} should not")
    print(f"  should match     : min {pos[0]:.3f}  median {pos[len(pos) // 2]:.3f}  max {pos[-1]:.3f}")
    print(f"  should NOT match : min {neg[0]:.3f}  median {neg[len(neg) // 2]:.3f}  max {neg[-1]:.3f}")

    print("\nthreshold  precision  recall  F1    (current = %.2f)" % MIN_SCORE)
    best = None
    for t in [x / 100 for x in range(15, 56, 5)]:
        tp = sum(s >= t for s in pos)
        fp = sum(s >= t for s in neg)
        p, r = (tp / (tp + fp) if tp + fp else 1.0), tp / len(pos)
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        best = max(best or (f1, t), (f1, t))
        print(f"  {t:.2f}      {p:5.2f}      {r:5.2f}   {f1:.2f}")
    print(f"best F1 {best[0]:.2f} at threshold {best[1]:.2f}")

    print(f"\nmatching pairs a threshold of {MIN_SCORE:.2f} would MISS:")
    for q, n, s, ok in sorted(pairs, key=lambda p: p[2]):
        if ok and s < MIN_SCORE:
            print(f"  {s:.3f}  {q!r} → {n}")

    print("\nquery style (same need, same memory 'min_value'):")
    for short, rich in QUERY_STYLES:
        print(f"  short {_cosine(q_vec[short], mem_vec['min_value']):.3f}  {short!r}")
        print(f"  rich  {_cosine(q_vec[rich], mem_vec['min_value']):.3f}  {rich!r}")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(".env")
    main()
