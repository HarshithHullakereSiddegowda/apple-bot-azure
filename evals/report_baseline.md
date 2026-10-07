# Eval report

25 questions (23 in scope, 2 out of scope), top-5 retrieval, chat model `chat`.

## Exam 1: Retrieval

| Mode | hit@5 | MRR | direct | paraphrase | synonym | two_part |
| --- | --- | --- | --- | --- | --- | --- |
| keyword | 70% | 0.53 | 80% | 44% | 100% | 100% |
| vector | 100% | 0.95 | 100% | 100% | 100% | 100% |
| hybrid | 96% | 0.85 | 100% | 89% | 100% | 100% |
| hybrid_rerank | 96% | 0.86 | 100% | 89% | 100% | 100% |

Misses (no expected page in top 5):

- **keyword**: restart, voiceover, location-privacy, frozen, wipe-before-selling, magnify-screen, share-internet
- **vector**: none
- **hybrid**: wipe-before-selling
- **hybrid_rerank**: wipe-before-selling

## Exam 2: Answers (hybrid_rerank)

| Metric | Result |
| --- | --- |
| Groundedness, mean | 0.99 |
| Grounded answers (score >= 0.7) | 23/23 |
| Completeness, mean | 1.00 |
| Fully complete answers (every part answered) | 23/23 |
| Cites an expected page | 21/23 |
| False refusals (in-scope answered 'couldn't find') | 0/23 |
| Correct refusals (out-of-scope) | 2/2 |
| Latency p50 / p95 (retrieve + generate) | 2486 ms / 4166 ms |

Lowest groundedness:

- **wipe-before-selling** (0.75): 2. Navigate to General > Reset.
- **bluetooth-pairing** (1.00): -
- **airprint** (1.00): -

Completeness by question style:

| Style | Mean completeness | Fully complete |
| --- | --- | --- |
| direct | 1.00 | 10/10 |
| paraphrase | 1.00 | 9/9 |
| synonym | 1.00 | 1/1 |
| two_part | 1.00 | 3/3 |

Lowest completeness:

- **bluetooth-pairing** (1.00) missing: -
- **airprint** (1.00) missing: -
- **restart** (1.00) missing: -

Tokens for exam 2 (answers + judge): 66,364
