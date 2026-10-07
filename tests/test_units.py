"""Fast unit tests for logic with no Azure dependency. Run: pytest -q"""
from types import SimpleNamespace

import pytest

from ingest import chunk, odata_quote, slugify
from ratelimit import RateLimiter, client_ip, visitor_id

# ── Rate limiting ─────────────────────────────────────────────────────────────

def test_rate_limiter_allows_then_blocks_with_wait_time():
    rl = RateLimiter([(3, 60)])
    assert [rl.check("a") for _ in range(3)] == [None, None, None]
    wait = rl.check("a")
    assert wait is not None and 1 <= wait <= 61


def test_rate_limiter_visitors_are_independent():
    rl = RateLimiter([(1, 60)])
    assert rl.check("a") is None
    assert rl.check("a") is not None
    assert rl.check("b") is None


def test_rate_limiter_applies_every_window(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr("ratelimit.time.time", lambda: clock["t"])
    rl = RateLimiter([(2, 10), (3, 3600)])
    assert rl.check("a") is None and rl.check("a") is None
    assert rl.check("a") is not None          # short window full
    clock["t"] += 11                           # short window slides
    assert rl.check("a") is None               # 3rd request in the hour
    assert rl.check("a") is not None          # long window now full


def test_blocked_requests_are_not_counted():
    rl = RateLimiter([(1, 60)])
    rl.check("a")
    for _ in range(5):
        rl.check("a")
    assert len(rl.hits["a"]) == 1


# ── Client identity ───────────────────────────────────────────────────────────

def _request(xff=None, host="10.0.0.9"):
    headers = {"x-forwarded-for": xff} if xff else {}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))


def test_client_ip_uses_last_forwarded_entry_to_resist_spoofing():
    assert client_ip(_request("6.6.6.6, 1.2.3.4")) == "1.2.3.4"


def test_client_ip_falls_back_to_socket_address():
    assert client_ip(_request()) == "10.0.0.9"


def test_visitor_id_is_a_short_one_way_hash():
    vid = visitor_id("1.2.3.4")
    assert len(vid) == 12 and "1.2.3.4" not in vid
    assert vid == visitor_id("1.2.3.4") != visitor_id("1.2.3.5")


# ── Chunking helpers ──────────────────────────────────────────────────────────

def test_chunk_overlap():
    text = "x" * 2500
    parts = chunk(text, size=1000, overlap=200)
    assert [len(p) for p in parts] == [1000, 1000, 900, 100]


def test_slugify_makes_safe_ids():
    assert slugify("My Guide (v2).PDF") == "my-guide-v2"
    assert slugify("???.pdf") == "doc"


def test_odata_quote_escapes_single_quotes():
    # prevents breaking out of a filter like: source eq 'O'Brien'
    assert odata_quote("O'Brien") == "'O''Brien'"


# ── Table splitting (Document Intelligence) ───────────────────────────────────

def test_small_table_stays_whole():
    from di_ingest import _split_table
    md = "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>"
    assert _split_table(md) == [md]


def test_big_table_splits_by_rows_and_repeats_header():
    from di_ingest import TABLE_CHUNK_CHARS, _split_table
    header = "<tr><th>Icon</th><th>Meaning</th></tr>"
    rows = "".join(f"<tr><td>i{n}</td><td>{'m' * 200}</td></tr>" for n in range(40))
    parts = _split_table(f"<table>{header}{rows}</table>")
    assert len(parts) > 1
    assert all(header in p for p in parts)
    assert all(len(p) <= TABLE_CHUNK_CHARS + len(header) + 40 for p in parts)
    assert sum(p.count("<td>i") for p in parts) == 40      # no row lost or duplicated


# ── Eval citation parser ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("(page 32)", {32}),
    ("(iPhone User Guide, pages 132 and 135)", {132, 135}),
    ("References: pages 49, 50, 139, 128", {49, 50, 128, 139}),
    ("no citation here", set()),
])
def test_cited_pages(text, expected):
    from run_evals import cited_pages
    assert cited_pages(text) == expected
