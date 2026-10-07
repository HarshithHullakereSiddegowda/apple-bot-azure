"""API boundary tests with FastAPI's TestClient. No Azure calls are made."""
from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def test_health_is_open():
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_chat_page_is_served():
    r = client.get("/")
    assert r.status_code == 200 and "<title>Apple Support Assistant" in r.text


def test_upload_requires_api_key():
    r = client.post("/upload", files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 401


def test_upload_rejects_wrong_key():
    r = client.post("/upload", headers={"X-API-Key": "wrong"},
                    files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 401


def test_upload_rejects_non_pdf_even_with_key():
    r = client.post("/upload", headers={"X-API-Key": "unit-test-app-key"},
                    files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 415


def test_ask_validates_input_before_any_model_call():
    assert client.post("/ask", json={"question": "a"}).status_code == 422          # too short
    assert client.post("/ask", json={"question": "hello", "k": 50}).status_code == 422  # k > 10
