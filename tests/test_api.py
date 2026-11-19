"""API contract: card fields, ordering, exclusions and error responses."""

import json

import pytest
from fastapi.testclient import TestClient

from app.store import apply_message


def body(reg, nature, ts="2026-08-14T09:00:00+08:00", kind="company-upserted", **extra) -> bytes:
    data = {"companyName": f"{reg} Sdn. Bhd.", "natureOfBusiness": nature, "industry": "X", "state": "Johor"}
    return json.dumps({"type": kind, "registrationNumber": reg, "updatedAt": ts, "data": data | extra}).encode()


@pytest.fixture
def client(conn, embedder, database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    seed = [
        ("SYN-000001", "Bakery and cafe in Shah Alam"),
        ("SYN-000002", "Bakery and cafe in Johor Bahru"),
        ("SYN-000003", "Cafe and bakery chain"),
        ("SYN-000004", "Oil palm plantation"),
        ("SYN-000005", "Bakery and cafe in Ipoh"),
    ]
    for reg, nature in seed:
        apply_message(conn, embedder, reg, body(reg, nature))
    struck = {
        "type": "company-struck-off",
        "registrationNumber": "SYN-000005",
        "updatedAt": "2026-08-15T09:00:00Z",
    }
    apply_message(conn, embedder, "struck", json.dumps(struck).encode())
    from app.api import app

    with TestClient(app) as test_client:
        yield test_client


def test_similar_returns_cards_most_similar_first(client):
    response = client.get("/companies/SYN-000001/similar", params={"limit": 3})
    assert response.status_code == 200
    payload = response.json()
    assert payload["company"]["registration_number"] == "SYN-000001"
    similar = payload["similar"]
    assert [c["registration_number"] for c in similar][:1] == ["SYN-000002"]
    assert set(similar[0]) == {"registration_number", "company_name", "industry", "state", "similarity"}
    scores = [c["similarity"] for c in similar]
    assert scores == sorted(scores, reverse=True)


def test_excludes_itself_and_struck_off_companies(client):
    regs = [
        c["registration_number"] for c in client.get("/companies/SYN-000001/similar?limit=20").json()["similar"]
    ]
    assert "SYN-000001" not in regs
    assert "SYN-000005" not in regs
    assert len(regs) == 3


def test_struck_off_company_page_still_works(client):
    response = client.get("/companies/SYN-000005/similar")
    assert response.status_code == 200
    assert response.json()["company"]["status"] == "struck_off"


def test_lenient_registration_number_in_path(client):
    assert client.get("/companies/syn-000001/similar").status_code == 200


def test_errors(client):
    assert client.get("/companies/SYN-999999/similar").json()["error"]["code"] == "company_not_found"
    assert client.get("/companies/SYN-999999/similar").status_code == 404
    assert client.get("/companies/not-a-number/similar").status_code == 400
    assert client.get("/companies/SYN-000001/similar?limit=0").status_code == 422
    assert client.get("/companies/SYN-000001/similar?limit=500").status_code == 422


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
