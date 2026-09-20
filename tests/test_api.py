"""API-level tests using FastAPI TestClient with an in-memory database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.main import app, reset_conn_for_tests
from app.seed import ASSESS_AT, seed_demo


@pytest.fixture()
def client():
    reset_conn_for_tests(":memory:")
    c = TestClient(app)
    from app import main
    seed_demo(main.get_conn())
    yield c


def _create_change(c, **over):
    body = {"subject_node": "MAT-PEP", "title": "来源切换", "change_type": "source_switch",
            "as_of": ASSESS_AT}
    body.update(over)
    r = c.post("/changes", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_health_and_cycles_endpoint(client):
    assert client.get("/health").json() == {"status": "ok"}
    r = client.get("/graph/cycles", params={"at": ASSESS_AT})
    assert r.status_code == 200
    assert r.json()["cycles"] == []


def test_change_query_explains_everything(client):
    d = _create_change(client)
    r = client.get(f"/changes/{d['change']['id']}")
    assert r.status_code == 200
    body = r.json()
    # path explanations
    assert body["snapshot"]["paths"], "应有受影响路径"
    # missing materials
    assert body["snapshot"]["missing_materials"]
    # review versions section present
    assert {x["discipline"] for x in body["reviews"]} == {"technical", "regulatory", "quality"}
    # effective boundary
    assert body["snapshot"]["effective_boundary"]["snapshot_as_of"] == ASSESS_AT
    # blocked batches before approval
    blocked = {b["batch_id"] for b in body["snapshot"]["blocked_batches"]
               if b["new_source_forbidden"]}
    assert blocked == {"B-VAL-A1", "B-VAL-B1", "B-REL-Y2"}
    assert body["can_approve"] is False


def test_document_evidence_clears_material_then_approval(client):
    d = _create_change(client)
    cid = d["change"]["id"]
    for m in d["snapshot"]["required_materials"]:
        r = client.post(f"/changes/{cid}/evidence", json={
            "kind": "document",
            "payload": {"material_key": m["key"], "name": m["key"]}})
        assert r.status_code == 200
    for disc, who in (("technical", "t"), ("regulatory", "r"), ("quality", "q")):
        r = client.post(f"/changes/{cid}/reviews", json={
            "discipline": disc, "reviewer": who, "decision": "approved"})
        assert r.status_code == 200
    r = client.post(f"/changes/{cid}/approve", json={"approved_by": "qa_head"})
    assert r.status_code == 200
    assert r.json()["approved"] is True
    body = client.get(f"/changes/{cid}").json()
    assert all(not b["new_source_forbidden"] for b in body["snapshot"]["blocked_batches"])


def test_sever_after_freeze_preserves_assessment(client):
    d = _create_change(client)
    cid = d["change"]["id"]
    # find the live edge rowid via registry tables through the app conn
    from app import main
    rowid = dbmod.query_one(
        main.get_conn(),
        "SELECT id FROM edges WHERE src='SPEC-PEP' AND dst='FORM-A'")["id"]
    r = client.post(f"/edges/{rowid}/sever", params={"valid_until": "2026-09-25T00:00:00+00:00"})
    assert r.status_code == 200
    body = client.get(f"/changes/{cid}").json()
    assert any(p["node_ids"][-1] == "PROD-X" for p in body["snapshot"]["paths"])


def test_evidence_extension_endpoint(client):
    d = _create_change(client)
    cid = d["change"]["id"]
    r = client.post(f"/changes/{cid}/evidence", json={
        "kind": "added_edge",
        "payload": {"src": "MAT-PEP", "dst": "FORM-Z", "edge_type": "used_in_formula",
                    "valid_from": "2027-01-01T00:00:00+00:00"}})
    assert r.status_code == 200
    products = r.json()["snapshot"]["affected"].get("product_market", [])
    assert "PROD-Z" in products


def test_emergency_requires_restrictions(client):
    d = _create_change(client, change_type="emergency_substitution")
    body = client.get(f"/changes/{d['change']['id']}").json()
    reasons = "\n".join(body["block_reasons"])
    assert "限用批次清单" in reasons
    assert "到期/退出条件" in reasons


def test_404_for_unknown_change(client):
    assert client.get("/changes/CHG-9999").status_code == 404
