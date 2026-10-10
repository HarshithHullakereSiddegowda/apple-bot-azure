"""Deterministic scoring rules for the Tender Copilot (no LLM, no Azure)."""
import pytest

from tender.tools import _final_verdict, _match_line, _quoted_in, _unproven_certifications, service_lines

PROFILE = """# Test Co
## Accreditations
- ISO 9001: not stated in this profile.
- DISP accredited.
## Service lines
1. **Publications development, including S1000D.** Technical writing.
2. **Training, learning and development (Academy).** Courses.
## Experience
Something.
"""


def test_service_lines_parsed_from_profile():
    assert service_lines(PROFILE) == ["Publications development, including S1000D",
                                      "Training, learning and development (Academy)"]
    assert service_lines("# no lines here") == []


def test_service_line_matching_is_tolerant_but_strict():
    lines = service_lines(PROFILE)
    assert _match_line("training, learning and development (academy)", lines) == lines[1]
    assert _match_line("none", lines) is None
    assert _match_line("Program evaluation", lines) is None


@pytest.mark.parametrize("kwargs,expected", [
    (dict(model_verdict="strong", score=80, blockers=[], short_notice=True, in_service_line=True), "strong"),
    (dict(model_verdict="poor", score=10, blockers=[], short_notice=True), "poor"),
    (dict(model_verdict="moderate", score=60, blockers=[], in_service_line=False), "poor"),
    (dict(model_verdict="insufficient_information", score=0, blockers=[], in_service_line=False),
     "insufficient_information"),   # the visible mistake is the cheaper one
    (dict(model_verdict="moderate", score=60, blockers=[], in_service_line=False, short_notice=True), "poor"),
    (dict(model_verdict="strong", score=85, blockers=[], in_service_line=True, short_notice=False), "strong"),
    (dict(model_verdict="moderate", score=65, blockers=[], in_service_line=True, scale_fit="prime_scale"), "poor"),
    (dict(model_verdict="confirm_before_bidding", score=0, blockers=["ISO 27001"]), "poor"),
    (dict(model_verdict="strong", score=90, blockers=["insurance"], in_service_line=True), "confirm_before_bidding"),
    (dict(model_verdict="confirm_before_bidding", score=88, blockers=[], in_service_line=True), "strong"),
    (dict(model_verdict="strong", score=62, blockers=[], in_service_line=True), "moderate"),
    (dict(model_verdict="insufficient_information", score=0, blockers=[]), "insufficient_information"),
])
def test_verdict_precedence(kwargs, expected):
    verdict, rules = _final_verdict(**kwargs)
    assert verdict == expected and rules


def test_quote_spanning_table_cells_is_found():
    row = "<tr><td>Closing time</td><td>20 November 2026, 12:00 noon AEDT</td></tr><td>Settings &gt; General</td>"
    assert _quoted_in("Closing time 20 November 2026, 12:00 noon AEDT", row)
    assert _quoted_in("Settings > General", row)


def test_quote_grounding_drops_invented_conditions():
    tender = "Tenderers must hold current ISO 9001 certification. Personnel need Baseline clearance."
    assert _quoted_in("Tenderers must hold current ISO 9001", tender)
    assert not _quoted_in("Tenderer must provide an ABN", tender)
    assert not _quoted_in("ISO", tender)            # too short to count as proof


def test_certification_guard():
    assert _unproven_certifications("Must hold ISO 9001.", PROFILE) == ["ISO 9001"]
    assert _unproven_certifications("Supplier must be DISP accredited.", PROFILE) == []


# ── extract_requirements: deterministic checks ────────────────────────────────

CRITERIA_TABLE = ("<table><tr><th>#</th><th>Criterion</th><th>Weighting</th></tr>"
                  "<tr><td>1</td><td>Capability</td><td>60%</td></tr>"
                  "<tr><td>2</td><td>Personnel</td><td>30%</td></tr>"
                  "<tr><td>3</td><td>Value for money</td><td>10%</td></tr></table>")
CHUNKS = [{"content_type": "text", "content": "Tenderer must hold ISO 9001 certification from an accredited body."},
          {"content_type": "table", "content": CRITERIA_TABLE}]
TEXT = "\n".join(c["content"] for c in CHUNKS)


def _req(**overrides):
    base = {"conditions": [], "evaluation_criteria": [], "response_parts": [], "key_dates": [],
            "pricing_items": [], "scope_items": [], "closing_time": "20 November 2026, 12:00"}
    base.update(overrides)
    return base


def _crit(name, weight):
    return {"number": None, "name": name, "weight_percent": weight, "tender_quote": f"<td>{name}</td>", "page": 1}


def test_table_row_count():
    from tender.tools import _table_rows_with_header
    assert _table_rows_with_header(CHUNKS, "weight") == 3
    assert _table_rows_with_header(CHUNKS, "price") is None


def test_check_requirements_catches_every_problem():
    from tender.tools import check_requirements
    req = _req(
        conditions=[{"id": "C1", "text": "ISO 9001", "page": 1, "tender_quote": "must hold ISO 9001 certification"},
                    {"id": "C9", "text": "invented", "page": 1, "tender_quote": "must provide an ABN to the agency"}],
        evaluation_criteria=[{**_crit("Capability", 60), "tender_quote": "Capability</td><td>60%"},
                             {**_crit("Personnel", 30), "tender_quote": "Personnel</td><td>30%"}],
        closing_time=None)
    cleaned, warnings = check_requirements(req, CHUNKS, TEXT)
    assert [c["id"] for c in cleaned["conditions"]] == ["C1"]                      # invented condition dropped
    assert any("dropped" in w for w in warnings)
    assert any("sum to 90%" in w for w in warnings)                               # weights don't add up
    assert any("3 rows but 2 were extracted" in w for w in warnings)              # a criterion was missed
    assert any("no closing time" in w for w in warnings)


def test_check_requirements_clean_form_has_no_warnings():
    from tender.tools import check_requirements
    req = _req(evaluation_criteria=[{**_crit(n, w), "tender_quote": f"{n}</td><td>{w}%"}
                                    for n, w in (("Capability", 60), ("Personnel", 30), ("Value for money", 10))])
    _, warnings = check_requirements(req, CHUNKS, TEXT)
    assert warnings == []


# ── ask_tender: citation checks ───────────────────────────────────────────────

def test_citations_to_unseen_pages_are_flagged():
    from tender.tools import check_citations
    assert check_citations("Part B is 15 pages (T-001, page 2).", {1, 2}) == []
    assert any("not retrieved" in w for w in check_citations("Closes 20 Nov (T-001, page 7).", {1, 2}))


def test_uncited_answer_is_flagged_but_not_found_is_fine():
    from tender.tools import NOT_FOUND, check_citations
    assert any("no citation" in w for w in check_citations("ISO 9001 is required.", {1}))
    assert check_citations(NOT_FOUND, {1}) == []
