"""FastAPI 端到端测试。"""
from __future__ import annotations

import os
from datetime import date

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("CHANGE_DB", str(db_file))
    # 延迟导入，使环境变量生效
    from app import database as db
    from app.main import app

    conn = db.connect(str(db_file))
    db.init_db(conn)
    from seed import seed
    seed(conn)
    conn.close()

    with TestClient(app) as c:
        yield c


def _create_change(client):
    r = client.post("/changes", json={
        "title": "酵母提取物换厂", "material_node": "mat_new",
        "change_type": "normal", "as_of": "2026-09-20"})
    assert r.status_code == 201, r.text
    return r.json()["change_id"]


def test_change_query_shows_paths_materials_opinions_boundaries(client):
    cid = _create_change(client)
    r = client.get(f"/changes/{cid}", params={"on": "2026-09-21"})
    view = r.json()
    assert view["frozen_as_of"] == "2026-09-20"
    # 路径解释可读
    paths = view["impact_runs"][0]["paths"]
    assert any("material:MAT-MEDIA-NEW" in seg for p in paths for seg in p["explanation"])
    # 生效边界：spec_v2->recipe_a 边 2026-09-01 起，路径 active
    assert all(p["effective_from"] for p in paths)
    # 遗漏材料覆盖供应商资质、方法、许可与市场
    missing = {m["requirement_key"].split(":", 1)[0] for m in view["missing_materials"]}
    assert {"supplier_qualification", "method_verification", "regulatory_filing",
            "market_commitment", "validation_assessment"} <= missing
    # 三个强制角色
    assert {a["role"] for a in view["review_assignments"]} == {
        "technical", "regulatory", "quality"}
    # 未批准：三批仍被禁止使用新来源
    assert {b["batch_node"] for b in view["blocked_batches"]} == {"B2601", "B2602", "B2603"}


def test_full_approval_flow_over_http(client):
    cid = _create_change(client)
    view = client.get(f"/changes/{cid}").json()
    for m in view["all_materials"]:
        r = client.post(f"/changes/{cid}/evidence", json={
            "requirement_key": m["requirement_key"], "title": m["title"]})
        assert r.status_code == 201

    obj = client.post(f"/changes/{cid}/opinions", json={
        "role": "quality", "decision": "objected", "comment": "缺偏差预案"}).json()
    for role in ("technical", "regulatory"):
        assert client.post(f"/changes/{cid}/opinions", json={
            "role": role, "decision": "approved"}).status_code == 201

    # 批准被门拦住
    r = client.post(f"/changes/{cid}/approve", json={"effective_from": "2026-09-21"})
    assert r.status_code == 400 and "反对" in r.json()["detail"]

    # 质量先重新批准，再解决历史矛盾
    client.post(f"/changes/{cid}/opinions", json={
        "role": "quality", "decision": "approved", "comment": "偏差预案已补"})
    r = client.post(f"/changes/{cid}/resolutions", json={
        "opinion_id": obj["opinion_id"], "resolution": "补充偏差 SOP 并培训"})
    assert r.status_code == 201

    r = client.post(f"/changes/{cid}/approve", json={"effective_from": "2026-09-21"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"


def test_retire_then_expand_preserves_history_over_http(client):
    cid = _create_change(client)
    assert client.get("/graph/cycles", params={"as_of": "2026-09-20"}).json()["cycles"] == []

    from app import database as db
    conn = db.connect(os.environ["CHANGE_DB"])
    row = conn.execute(
        "SELECT id FROM edges WHERE from_node='spec_v2' AND to_node='recipe_b'").fetchone()
    conn.close()
    r = client.post(f"/edges/{row['id']}/retire", params={"valid_to": "2026-09-25"})
    assert r.status_code == 200

    r = client.post(f"/changes/{cid}/expand", json={"as_of": "2026-10-05", "note": "解绑后复评"})
    view = r.json()
    seq1 = next(x for x in view["impact_runs"] if x["seq"] == 1)
    seq2 = next(x for x in view["impact_runs"] if x["seq"] == 2)
    seq1_nodes = {n["node_id"] for p in seq1["paths"] for n in p["nodes"]}
    seq2_nodes = {n["node_id"] for p in seq2["paths"] for n in p["nodes"]}
    assert "prod_y" in seq1_nodes and "prod_y" not in seq2_nodes
    # 10 月生效的路线/市场进入新范围
    assert {"route_3", "mkt_eu"} <= seq2_nodes


def test_graph_endpoints_reject_bad_windows(client):
    r = client.post("/nodes", json={
        "type": "material", "code": "X", "name": "X",
        "valid_from": "2026-01-01", "valid_to": "2025-01-01"})
    assert r.status_code == 422
    r = client.post("/edges", json={
        "from_node": "mat_new", "to_node": "nope", "label": "x",
        "valid_from": "2026-01-01"})
    assert r.status_code == 404
