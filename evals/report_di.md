# Eval report

34 questions (32 in scope, 2 out of scope), top-5 retrieval, chat model `chat`.

## Exam 1: Retrieval

| Mode | hit@5 | MRR | direct | figure | paraphrase | synonym | table | two_part |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| keyword | 75% | 0.56 | 80% | 100% | 44% | 100% | 80% | 100% |
| vector | 100% | 0.92 | 100% | 100% | 100% | 100% | 100% | 100% |
| hybrid | 97% | 0.82 | 100% | 100% | 89% | 100% | 100% | 100% |
| hybrid_rerank | 97% | 0.95 | 100% | 100% | 89% | 100% | 100% | 100% |

Misses (no expected page in top 5):

- **keyword**: restart, ringtone, location-privacy, bigger-text, frozen, wipe-before-selling, share-internet, gprs-icon
- **vector**: none
- **hybrid**: wipe-before-selling
- **hybrid_rerank**: wipe-before-selling

## Exam 2: Answers (hybrid_rerank)

| Metric | Result |
| --- | --- |
| Groundedness, mean | 0.99 |
| Grounded answers (score >= 0.7) | 32/32 |
| Completeness, mean | 1.00 |
| Fully complete answers (every part answered) | 32/32 |
| Cites an expected page | 32/32 |
| False refusals (in-scope answered 'couldn't find') | 0/32 |
| Correct refusals (out-of-scope) | 2/2 |
| Latency p50 / p95 (retrieve + generate) | 2424 ms / 3494 ms |

Lowest groundedness:

- **airprint** (0.75): Tap the share or action icon (this may appear as a square with an arrow or a similar symbol).; Select the AirPrint-enabled printer.
- **wipe-before-selling** (0.91): To erase all content and settings, see the Reset section on page 138 (not included in the excerpts).
- **bluetooth-pairing** (1.00): -

Completeness by question style:

| Style | Mean completeness | Fully complete |
| --- | --- | --- |
| direct | 1.00 | 10/10 |
| figure | 1.00 | 4/4 |
| paraphrase | 1.00 | 9/9 |
| synonym | 1.00 | 1/1 |
| table | 1.00 | 5/5 |
| two_part | 1.00 | 3/3 |

Lowest completeness:

- **bluetooth-pairing** (1.00) missing: -
- **airprint** (1.00) missing: -
- **restart** (1.00) missing: -

Tokens for exam 2 (answers + judge): 91,770
