"""End-to-end tests for the change impact service."""

from __future__ import annotations

import pytest

from app import db as dbmod
from app import service
from app.graph import GraphView
from app.seed import ASSESS_AT, SEVER_AT, V0, seed_demo


@pytest.fixture()
def conn():
    c = dbmod.connect(":memory:")
    dbmod.init_db(c)
    yield c
    c.close()


@pytest.fixture()
def seeded(conn):
    seed_demo(conn)
    return conn


def _new_change(conn, **over):
    body = {
        "subject_node": "MAT-PEP",
        "title": "大豆蛋白胨来源切换 S-A → S-B",
        "description": "原供应商停产，新来源标称等同",
        "change_type": "source_switch",
        "as_of": ASSESS_AT,
    }
    body.update(over)
    return service.create_change(conn, body)


# ----------------------------------------------------------- graph traversal

def test_cycle_detection_tarjan(conn):
    seed_demo(conn)
    # introduce a live cycle between two steps
    service.add_edge(conn, {"src": "STEP-A", "dst": "FORM-A", "edge_type": "feeds_back",
                            "attrs": {}, "valid_from": V0, "valid_until": None})
    nrows = dbmod.query(conn, "SELECT * FROM nodes WHERE valid_from<=?", (ASSESS_AT,))
    erows = dbmod.query(conn, "SELECT * FROM edges WHERE valid_from<=?", (ASSESS_AT,))
    g = GraphView.from_rows(nrows, erows, at=ASSESS_AT)
    cycles = g.tarjan_scc()
    assert any({"STEP-A", "FORM-A"} <= set(c) for c in cycles)


def test_all_simple_paths_covers_three_routes(seeded):
    detail = _new_change(seeded)
    paths = detail["snapshot"]["paths"]
    endpoints = {tuple(p["node_ids"]) for p in paths}
    # at least one path reaches each registered product
    assert any(seq[-1] == "PROD-X" for seq in endpoints)
    assert any(seq[-1] == "PROD-Y" for seq in endpoints)
    # every path starts from the changed material
    assert all(seq[0] == "MAT-PEP" for seq in endpoints)
    # unrelated product is never on any path
    assert not any("PROD-Z" in seq or "MAT-OTHER" in seq for seq in endpoints)


def test_path_explanation_carries_names_types_and_windows(seeded):
    detail = _new_change(seeded)
    p = next(p for p in detail["snapshot"]["paths"] if p["node_ids"][-1] == "PROD-X")
    assert p["node_names"][0].startswith("培养基关键组分")
    assert "product_market" == p["node_types"][-1]
    assert p["source"] == "frozen"
    assert all(w.startswith(f"[{V0}") for w in p["valid_windows"])
    assert p["boundary"]["valid_from"] == V0


def test_effective_interval_filtering(conn):
    seed_demo(conn)
    # edge that only becomes effective after assessment: must not show in paths
    conn.execute(
        "INSERT INTO edges(src,dst,edge_type,attrs,valid_from,valid_until)"
        " VALUES('MAT-PEP','PROD-Z','future_use','{}',?,NULL)",
        ("2027-01-01T00:00:00+00:00",))
    conn.commit()
    detail = _new_change(conn)
    assert not any("PROD-Z" in p["node_ids"] for p in detail["snapshot"]["paths"])


# --------------------------------------------------------------- snapshotting

def test_snapshot_frozen_and_sever_does_not_erase_basis(seeded):
    detail = _new_change(seeded)
    change_id = detail["change"]["id"]
    edge_to_form_a = dbmod.query_one(
        seeded, "SELECT id FROM edges WHERE src='SPEC-PEP' AND dst='FORM-A'")["id"]

    # after the change, route-A relationship is severed in the LIVE graph
    service.sever_edge(seeded, edge_to_form_a, SEVER_AT)

    reloaded = service.assess(seeded, change_id)
    paths = reloaded["snapshot"]["paths"]
    # frozen assessment still shows route-A to PROD-X
    assert any(p["node_ids"][-1] == "PROD-X" for p in paths)
    # snapshot tables themselves untouched
    snap_edge = dbmod.query_one(
        seeded, "SELECT valid_until FROM snapshot_edges WHERE edge_rowid=?", (edge_to_form_a,))
    assert snap_edge["valid_until"] is None


def test_new_evidence_extends_scope(seeded):
    detail = _new_change(seeded)
    change_id = detail["change"]["id"]
    products_before = set(detail["snapshot"]["affected"].get("product_market", []))
    assert "PROD-Z" not in products_before

    # new evidence: the component will also enter formula Z from next year
    out = service.add_evidence(seeded, change_id, {
        "kind": "added_edge",
        "payload": {"src": "MAT-PEP", "dst": "FORM-Z", "edge_type": "used_in_formula",
                    "valid_from": "2027-01-01T00:00:00+00:00"},
        "added_by": "sqe",
    })
    extended = {p for p in out["snapshot"]["affected"].get("product_market", [])}
    assert "PROD-Z" in extended
    ev_paths = [p for p in out["snapshot"]["paths"] if "PROD-Z" in p["node_ids"]]
    assert ev_paths and ev_paths[0]["source"] == "evidence"
    # frozen paths remain attributed to frozen
    frozen = [p for p in out["snapshot"]["paths"] if p["source"] == "frozen"]
    assert frozen


def test_evidence_node_must_reference_known_nodes(seeded):
    detail = _new_change(seeded)
    with pytest.raises(service.ServiceError) as ei:
        service.add_evidence(seeded, detail["change"]["id"], {
            "kind": "added_edge",
            "payload": {"src": "MAT-PEP", "dst": "GHOST", "valid_from": ASSESS_AT}})
    assert ei.value.status == 422


# ----------------------------------------------------------- risk & gating

def test_risk_dispatch_and_mandatory_disciplines(seeded):
    detail = _new_change(seeded)
    assert detail["snapshot"]["risk_level"] == "high"   # multi-route + open validation
    mandatory = detail["gating"]["mandatory_disciplines"]
    assert set(mandatory) == {"technical", "regulatory", "quality"}
    rules = detail["snapshot"]["triggered_rules"]
    assert {"R1_SPEC_EQUIVALENCE", "R2_METHOD_SUITABILITY", "R3_MULTI_ROUTE",
            "R4_OPEN_VALIDATION_BATCH", "R5_REGISTERED_SPEC"} <= set(rules)


def test_missing_materials_block_approval(seeded):
    detail = _new_change(seeded)
    keys = {m["key"] for m in detail["snapshot"]["required_materials"]}
    assert "spec_equivalence:SPEC-PEP" in keys
    assert "method_suitability:METHOD-SHARED" in keys
    assert "filing_assessment" in keys
    assert "open_batch_disposition" in keys
    assert detail["can_approve"] is False
    assert any("遗漏材料" in r for r in detail["block_reasons"])
    assert any("缺少强制评审" in r for r in detail["block_reasons"])


def _provide_all_materials(conn, change_id, detail):
    for m in detail["snapshot"]["required_materials"]:
        service.add_evidence(conn, change_id, {
            "kind": "document",
            "payload": {"material_key": m["key"], "name": f"{m['key']} 证据文件"},
            "added_by": m["discipline"],
        })


def _approve_all_disciplines(conn, change_id):
    for d, who in (("technical", "tech_lead"),
                   ("regulatory", "ra_lead"),
                   ("quality", "qa_lead")):
        service.add_review(conn, change_id, {"discipline": d, "reviewer": who,
                                             "decision": "approved", "comment": "同意"})


def test_full_approval_path(seeded):
    detail = _new_change(seeded)
    cid = detail["change"]["id"]
    _provide_all_materials(seeded, cid, detail)
    _approve_all_disciplines(seeded, cid)
    detail = service.assess(seeded, cid)
    assert detail["can_approve"] is True
    res = service.approve_change(seeded, cid, "qa_head")
    assert res["approved"] is True
    detail2 = service.assess(seeded, cid)
    assert all(not b["new_source_forbidden"] for b in detail2["snapshot"]["blocked_batches"])


def test_objection_and_conflict_resolution(seeded):
    detail = _new_change(seeded)
    cid = detail["change"]["id"]
    _provide_all_materials(seeded, cid, detail)
    _approve_all_disciplines(seeded, cid)
    # regulatory objects in a new version
    service.add_review(seeded, cid, {"discipline": "regulatory", "reviewer": "ra_lead",
                                    "decision": "objected", "comment": "EU 变更分类未确认"})
    detail = service.assess(seeded, cid)
    assert not detail["can_approve"]
    assert any("矛盾" in r or "反对" in r for r in detail["block_reasons"])

    # rejecting the objection's claim blocks; upholding the change resolves it
    service.resolve_conflict(seeded, cid, {"resolution": "upheld_change",
                                           "note": "按 Type IA 备案处理", "by": "qa_head"})
    detail = service.assess(seeded, cid)
    # objection still exists but the recorded resolution discharges the conflict gate
    assert not any("矛盾" in r for r in detail["block_reasons"])
    assert detail["gating"]["conflict_resolution"]["resolution"] == "upheld_change"
    # the mandatory reviewer is still objecting, however: its review must flip
    assert any("regulatory 最新意见为反对" in r for r in detail["block_reasons"])
    service.add_review(seeded, cid, {"discipline": "regulatory", "reviewer": "ra_lead",
                                    "decision": "approved", "comment": "备案路径已确认"})
    assert service.assess(seeded, cid)["can_approve"] is True


def test_review_versions_are_append_only(seeded):
    detail = _new_change(seeded)
    cid = detail["change"]["id"]
    service.add_review(seeded, cid, {"discipline": "technical", "reviewer": "a",
                                    "decision": "objected", "comment": "v1"})
    service.add_review(seeded, cid, {"discipline": "technical", "reviewer": "a",
                                    "decision": "approved", "comment": "v2"})
    tech = next(r for r in service.assess(seeded, cid)["reviews"] if r["discipline"] == "technical")
    assert tech["latest_version"] == 2
    assert tech["latest_decision"] == "approved"
    assert [h["version"] for h in tech["history"]] == [1, 2]
    assert tech["history"][0]["decision"] == "objected"  # prior opinion preserved


def test_material_added_by_evidence_survives_later_unbind(seeded):
    detail = _new_change(seeded)
    cid = detail["change"]["id"]
    # evidence extends scope to PROD-Z -> new market commitment material appears
    service.add_evidence(seeded, cid, {
        "kind": "added_edge",
        "payload": {"src": "MAT-PEP", "dst": "FORM-Z", "edge_type": "used_in_formula",
                    "valid_from": "2027-01-01T00:00:00+00:00"}})
    detail = service.assess(seeded, cid)
    assert any(m["key"] == "market_commitment:PROD-Z" for m in detail["snapshot"]["required_materials"])
    # NOTE: edge lives only in evidence (append-only).  Even a retraction note cannot
    # delete a derived requirement; demonstrate retention directly:
    service.add_evidence(seeded, cid, {"kind": "note",
                                       "payload": {"text": "后续邮件声称该扩展取消"}})
    keys = {m["key"] for m in service.assess(seeded, cid)["snapshot"]["required_materials"]}
    assert "market_commitment:PROD-Z" in keys


# --------------------------------------------------------- blocked batches

def test_blocked_batches_before_approval(seeded):
    detail = _new_change(seeded)
    blocked = {b["batch_id"]: b for b in detail["snapshot"]["blocked_batches"]}
    # in-validation and released batches blocked; completed and old_source excluded
    assert set(blocked) == {"B-VAL-A1", "B-VAL-B1", "B-REL-Y2"}
    assert all(b["new_source_forbidden"] for b in blocked.values())
    reasons = blocked["B-VAL-A1"]["reasons"]
    assert "验证批未结束" in reasons


def test_emergency_substitution_controls(conn):
    seed_demo(conn)
    detail = service.create_change(conn, {
        "subject_node": "MAT-PEP",
        "title": "紧急替代",
        "change_type": "emergency_substitution",
        "as_of": ASSESS_AT,
        "limited_batches": [],
        "expiry_rule": "",
    })
    assert "R6_EMERGENCY_CONTROLS" in detail["snapshot"]["triggered_rules"]
    assert any("限用批次清单" in r for r in detail["block_reasons"])
    assert any("到期/退出条件" in r for r in detail["block_reasons"])


def test_emergency_only_limited_batches_allowed_and_expiry(seeded):
    detail = service.create_change(seeded, {
        "subject_node": "MAT-PEP",
        "title": "紧急替代",
        "change_type": "emergency_substitution",
        "as_of": ASSESS_AT,
        "limited_batches": ["B-VAL-A1"],
        "expiry_rule": "2026-12-31T00:00:00+00:00",
    })
    cid = detail["change"]["id"]
    _provide_all_materials(seeded, cid, detail)
    _approve_all_disciplines(seeded, cid)
    res = service.approve_change(seeded, cid, "qa_head")
    assert res["approved"]
    detail = service.assess(seeded, cid)
    by_id = {b["batch_id"]: b for b in detail["snapshot"]["blocked_batches"]}
    assert by_id["B-VAL-A1"]["allowed"] is True
    assert by_id["B-VAL-B1"]["new_source_forbidden"] is True
    assert "不在紧急限用批次清单内" in by_id["B-VAL-B1"]["reasons"]

    # once the expiry condition passes, even the listed batch is forbidden again
    seeded.execute("UPDATE changes SET expiry_rule='2020-01-01T00:00:00+00:00' WHERE id=?", (cid,))
    seeded.commit()
    detail = service.assess(seeded, cid)
    by_id = {b["batch_id"]: b for b in detail["snapshot"]["blocked_batches"]}
    assert by_id["B-VAL-A1"]["new_source_forbidden"] is True
    assert any("到期" in r for r in by_id["B-VAL-A1"]["reasons"])
    # boundary shows the emergency window
    assert detail["snapshot"]["effective_boundary"]["emergency"]["limited_batches"] == ["B-VAL-A1"]


# ---------------------------------------------------------- effective window

def test_edge_severed_before_freeze_is_absent_from_snapshot(conn):
    seed_demo(conn)
    # sever route-A usage BEFORE the change: the snapshot must not contain it
    eid = dbmod.query_one(
        conn, "SELECT id FROM edges WHERE src='SPEC-PEP' AND dst='FORM-A'")["id"]
    service.sever_edge(conn, eid, "2026-09-01T00:00:00+00:00")
    detail = _new_change(conn)
    assert all("PROD-X" not in p["node_ids"] for p in detail["snapshot"]["paths"])

    # a NEW change initiated later still gets its own fresh frozen snapshot
    detail2 = _new_change(conn)
    assert detail2["change"]["id"] != detail["change"]["id"]
    assert all("PROD-X" not in p["node_ids"] for p in detail2["snapshot"]["paths"])


def test_snapshot_cycle_blocks_approval(seeded):
    detail = _new_change(seeded)
    cid = detail["change"]["id"]
    # add a cyclic edge via evidence (enters the frozen+evidence view)
    service.add_evidence(seeded, cid, {
        "kind": "added_edge",
        "payload": {"src": "STEP-A", "dst": "FORM-A", "edge_type": "feeds_back",
                    "valid_from": ASSESS_AT}})
    _provide_all_materials(seeded, cid, service.assess(seeded, cid))
    _approve_all_disciplines(seeded, cid)
    detail = service.assess(seeded, cid)
    assert not detail["can_approve"]
    assert any("依赖图存在环" in r for r in detail["block_reasons"])
    assert detail["gating"]["cycles"]


def test_change_requires_subject_effective_at_as_of(conn):
    seed_demo(conn)
    with pytest.raises(service.ServiceError):
        service.create_change(conn, {
            "subject_node": "MAT-PEP", "title": "x", "change_type": "source_switch",
            "as_of": "2025-01-01T00:00:00+00:00"})
