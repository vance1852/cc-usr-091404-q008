"""引擎层测试：快照冻结与不可变性、范围扩展、评审门、紧急替代、批次封锁。"""
from __future__ import annotations

from datetime import date

import pytest

from app import database as db
from app import engine
from seed import seed


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    seed(c)
    yield c
    c.close()


def _submit_all_materials(c, change_id):
    for m in engine.get_change(c, change_id)["all_materials"]:
        engine.add_evidence(c, change_id, {"requirement_key": m["requirement_key"],
                                           "title": m["title"]})


def _approve_reviews(c, change_id, roles=("technical", "regulatory", "quality")):
    for role in roles:
        engine.add_opinion(c, change_id, {"role": role, "decision": "approved",
                                          "comment": "同意", "reviewer": role})


def _node_ids(payload, seq):
    run = next(r for r in payload["impact_runs"] if r["seq"] == seq)
    return {n["node_id"] for p in run["paths"] for n in p["nodes"]}


# ---- 快照与生效区间 -------------------------------------------------------

def test_initial_snapshot_excludes_not_yet_effective_route_and_market(conn):
    change = engine.create_change(conn, {
        "title": "酵母提取物换厂", "material_node": "mat_new",
        "change_type": "normal", "as_of": date(2026, 9, 20)})
    cid = change["change_id"]
    reached = _node_ids(change, 1)
    # 9-20 只有两条路线，第三条与欧盟市场 10-01 才生效
    assert {"route_1", "route_2"} <= reached
    assert "route_3" not in reached and "recipe_c" not in reached and "mkt_eu" not in reached
    # 路径解释、生效边界
    p = change["impact_runs"][0]["paths"][0]
    assert any("material" in seg for seg in p["explanation"])
    assert p["effective_from"] <= "2026-09-20"
    assert p["effective_to"] is None


def test_unbinding_does_not_erase_original_snapshot(conn):
    change = engine.create_change(conn, {
        "title": "酵母提取物换厂", "material_node": "mat_new",
        "change_type": "normal", "as_of": date(2026, 9, 20)})
    cid = change["change_id"]
    assert "prod_y" in _node_ids(change, 1)

    # 9-25 解绑 spec_v2 -> recipe_b
    edge = conn.execute(
        "SELECT id FROM edges WHERE from_node='spec_v2' AND to_node='recipe_b'").fetchone()
    engine.retire_edge(conn, edge["id"], date(2026, 9, 25))

    expanded = engine.expand_scope(conn, cid, date(2026, 10, 5), "10月复评")
    # 初始快照原样保留
    assert "prod_y" in _node_ids(expanded, 1)
    assert "prod_y" in _node_ids(expanded, 1)
    # 新评估中 recipe_b 子树消失
    assert "prod_y" not in _node_ids(expanded, 2)
    # 但原评估要求的材料仍保留（不得抹去原评估依据）
    keys = {m["requirement_key"] for m in expanded["all_materials"]}
    assert "product_commitment:prod_y" in keys
    assert "open_batch_disposition:batch_b2603" in keys


def test_expansion_adds_october_scope_and_never_shrinks(conn):
    change = engine.create_change(conn, {
        "title": "酵母提取物换厂", "material_node": "mat_new",
        "change_type": "normal", "as_of": date(2026, 9, 20)})
    cid = change["change_id"]
    expanded = engine.expand_scope(conn, cid, date(2026, 10, 5), "新证据：欧盟注册进展")
    assert expanded["newly_added_reviews"] == []  # 三角色在初始已分派
    assert "route_3" in _node_ids(expanded, 2)
    assert "mkt_eu" in _node_ids(expanded, 2)
    keys = {m["requirement_key"] for m in expanded["missing_materials"]}
    assert "market_commitment:mkt_eu" in keys
    # 共两次评估，初始快照 kind 不变
    kinds = [(r["seq"], r["kind"]) for r in expanded["impact_runs"]]
    assert kinds == [(1, "initial"), (2, "expansion")]


# ---- 风险与评审门 ---------------------------------------------------------

def test_risk_rules_assign_three_mandatory_roles_and_high_risk(conn):
    change = engine.create_change(conn, {
        "title": "换厂", "material_node": "mat_new",
        "change_type": "normal", "as_of": date(2026, 9, 20)})
    roles = {a["role"] for a in change["review_assignments"] if a["mandatory"]}
    assert roles == {"technical", "regulatory", "quality"}
    assert change["risk_level"] == "high"  # 存在未结束验证 VAL-PV-26-09


def test_approval_blocked_until_all_reviews_materials_and_conflicts_resolved(conn):
    cid = engine.create_change(conn, {
        "title": "换厂", "material_node": "mat_new",
        "change_type": "normal", "as_of": date(2026, 9, 20)})["change_id"]

    with pytest.raises(engine.EngineError):
        engine.approve_change(conn, cid, date(2026, 9, 21), None)

    # 法规先反对
    obj = engine.add_opinion(conn, cid, {"role": "regulatory", "decision": "objected",
                                         "comment": "申报路径未明", "reviewer": "RA"})
    _submit_all_materials(conn, cid)
    engine.add_opinion(conn, cid, {"role": "technical", "decision": "approved"})
    engine.add_opinion(conn, cid, {"role": "quality", "decision": "approved"})
    view = engine.get_change(conn, cid)
    assert not view["approval_gate"]["approvable"]
    assert any("反对" in b for b in view["approval_gate"]["blockers"])

    # 反对意见被新的批准版本覆盖，但历史矛盾仍需显式解决
    engine.add_opinion(conn, cid, {"role": "regulatory", "decision": "approved",
                                   "comment": "CBE-30 备案确认"})
    view = engine.get_change(conn, cid)
    assert any("矛盾尚未解决" in b for b in view["approval_gate"]["blockers"])
    with pytest.raises(engine.EngineError):
        engine.approve_change(conn, cid, date(2026, 9, 21), None)

    engine.resolve_objection(conn, cid, {"opinion_id": obj["opinion_id"],
                                         "resolution": "采用 CBE-30，30 日备案",
                                         "reviewer": "QA-head"})
    approved = engine.approve_change(conn, cid, date(2026, 9, 21), None)
    assert approved["status"] == "approved"
    assert approved["approved_window"]["effective_from"] == "2026-09-21"
    # 意见版本完整保留
    reg = [o for o in approved["opinions"] if o["role"] == "regulatory"]
    assert [o["version"] for o in reg] == [1, 2]
    assert reg[0]["resolved"]["resolution"].startswith("采用 CBE-30")
    assert reg[1]["is_latest"] and reg[1]["decision"] == "approved"


def test_reachable_cycle_blocks_approval_but_initial_snapshot_clean(conn):
    cid = engine.create_change(conn, {
        "title": "换厂", "material_node": "mat_new",
        "change_type": "normal", "as_of": date(2026, 9, 20)})["change_id"]
    # 制造一个可从物料到达的环：step_a -> route_1（与 route_1->step_a 成环）
    engine.add_edge(conn, {"from_node": "step_a", "to_node": "route_1",
                           "label": "错误回链", "valid_from": date(2026, 9, 22)})
    expanded = engine.expand_scope(conn, cid, date(2026, 9, 23), "主数据异常")
    assert expanded["impact_runs"][0]["cycles"] == []
    latest_cycles = [c for c in expanded["impact_runs"][1]["cycles"] if c["reachable"]]
    assert latest_cycles and {"route_1", "step_a"} <= set(latest_cycles[0]["nodes"])
    assert any("可达环" in b for b in expanded["approval_gate"]["blockers"])
    _submit_all_materials(conn, cid)
    _approve_reviews(conn, cid)
    with pytest.raises(engine.EngineError):
        engine.approve_change(conn, cid, date(2026, 9, 24), None)


# ---- 批次封锁与紧急替代 ----------------------------------------------------

def test_open_validation_and_production_batches_blocked_before_approval(conn):
    cid = engine.create_change(conn, {
        "title": "换厂", "material_node": "mat_new",
        "change_type": "normal", "as_of": date(2026, 9, 20)})["change_id"]
    view = engine.get_change(conn, cid, on=date(2026, 9, 21))
    blocked = {b["batch_node"] for b in view["blocked_batches"]}
    assert blocked == {"B2601", "B2602", "B2603"}


def test_after_approval_batches_stay_blocked_until_released(conn):
    cid = engine.create_change(conn, {
        "title": "换厂", "material_node": "mat_new",
        "change_type": "normal", "as_of": date(2026, 9, 20)})["change_id"]
    _submit_all_materials(conn, cid)
    _approve_reviews(conn, cid)
    engine.approve_change(conn, cid, date(2026, 9, 21), None)

    view = engine.get_change(conn, cid, on=date(2026, 9, 22))
    assert {b["batch_node"] for b in view["blocked_batches"]} == {"B2601", "B2602", "B2603"}
    engine.release_batch(conn, cid, "B2601", reason="首批桥接检验合格", reviewer="QA")
    view = engine.get_change(conn, cid, on=date(2026, 9, 22))
    assert {b["batch_node"] for b in view["blocked_batches"]} == {"B2602", "B2603"}


def test_emergency_substitution_requires_restriction_and_enforces_batch_limit(conn):
    cid = engine.create_change(conn, {
        "title": "断料紧急替代", "material_node": "mat_new",
        "change_type": "emergency", "as_of": date(2026, 9, 20)})["change_id"]
    _submit_all_materials(conn, cid)
    _approve_reviews(conn, cid)
    with pytest.raises(engine.EngineError):  # 缺少限用批次/到期条件
        engine.approve_change(conn, cid, date(2026, 9, 21), None)

    engine.set_emergency_restriction(conn, cid, {
        "allowed_batches": ["B2601"], "expires_on": date(2026, 12, 1),
        "conditions": "仅限 B2601，须加检生长促进试验并留样观察 90 天"})
    approved = engine.approve_change(conn, cid, date(2026, 9, 21),
                                     date(2026, 12, 1))
    assert approved["risk_level"] == "high"

    view = engine.get_change(conn, cid, on=date(2026, 9, 22))
    blocked = {b["batch_node"]: b["reason"] for b in view["blocked_batches"]}
    assert "B2601" not in blocked
    assert "不在紧急替代限用批次清单内" in blocked["B2602"]

    expired = engine.get_change(conn, cid, on=date(2026, 12, 1))
    assert all("到期" in b["reason"] for b in expired["blocked_batches"])
    assert {"B2601", "B2602", "B2603"} <= {b["batch_node"] for b in expired["blocked_batches"]}


def test_expansion_can_add_new_mandatory_role(conn):
    # 先对一个不触及法规节点的物料发起变更：只有 quality+technical
    isolated = {"id": "mat_iso", "type": "material", "code": "MAT-ISO",
                "name": "内部试验物料", "attrs": {},
                "valid_from": date(2026, 9, 1), "valid_to": None}
    engine.upsert_node(conn, isolated)
    engine.add_edge(conn, {"from_node": "mat_iso", "to_node": "method_ph",
                           "label": "仅方法关联", "valid_from": date(2026, 9, 1)})
    cid = engine.create_change(conn, {
        "title": "内部物料", "material_node": "mat_iso",
        "change_type": "normal", "as_of": date(2026, 9, 20)})["change_id"]
    roles = {a["role"] for a in conn.execute(
        "SELECT role FROM review_assignments WHERE change_id=?", (cid,)).fetchall()}
    assert roles == {"technical", "quality"}

    # 新证据表明该物料随后进入许可产品
    engine.add_edge(conn, {"from_node": "mat_iso", "to_node": "spec_v2",
                           "label": "补录：进入培养基规格", "valid_from": date(2026, 9, 25)})
    expanded = engine.expand_scope(conn, cid, date(2026, 9, 26), "新证据：进入许可链")
    assert "regulatory" in expanded["newly_added_reviews"]
    # 新强制角色未评审前不能批准
    assert any("法规评审" in b for b in expanded["approval_gate"]["blockers"])


def test_terminal_change_cannot_expand(conn):
    cid = engine.create_change(conn, {
        "title": "换厂", "material_node": "mat_new",
        "change_type": "normal", "as_of": date(2026, 9, 20)})["change_id"]
    _submit_all_materials(conn, cid)
    _approve_reviews(conn, cid)
    engine.approve_change(conn, cid, date(2026, 9, 21), None)
    with pytest.raises(engine.EngineError):
        engine.expand_scope(conn, cid, date(2026, 10, 1), "事后补充")
