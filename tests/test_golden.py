"""Offline checks on the eval golden set: a broken answer key would make every eval run meaningless."""
import json
from pathlib import Path

CASES = json.loads((Path(__file__).resolve().parents[1] / "evals" / "golden.json").read_text())
STYLES = {"direct", "paraphrase", "two_part", "synonym", "table", "figure", "out_of_scope"}
MANUAL_PAGES = 156


def test_ids_are_unique():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))


def test_every_case_is_well_formed():
    for c in CASES:
        assert c["style"] in STYLES, c["id"]
        assert len(c["query"]) >= 10, c["id"]
        assert all(isinstance(p, int) and 1 <= p <= MANUAL_PAGES for p in c["expected_pages"]), c["id"]


def test_out_of_scope_cases_expect_refusal_and_no_pages():
    for c in CASES:
        if c["style"] == "out_of_scope":
            assert c.get("expect_refusal") is True and c["expected_pages"] == [], c["id"]
        else:
            assert c["expected_pages"], c["id"]


def test_coverage_of_question_styles():
    styles = {c["style"] for c in CASES}
    assert {"paraphrase", "table", "figure", "out_of_scope"} <= styles
    assert len(CASES) >= 30
