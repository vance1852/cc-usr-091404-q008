"""Demo / fixture data: a discontinued key culture-medium component whose
new nominal-equivalent source feeds three process routes, shared analytical
methods, open validation studies and registered products.
"""

from __future__ import annotations

import sqlite3

from . import service

V0 = "2026-01-01T00:00:00+00:00"
ASSESS_AT = "2026-09-20T09:00:00+00:00"
SEVER_AT = "2026-09-25T00:00:00+00:00"


def _node(conn, nid, ntype, name, **attrs):
    service.upsert_node(conn, {
        "id": nid, "node_type": ntype, "name": name, "attrs": attrs,
        "valid_from": V0, "valid_until": None,
    })


def _edge(conn, src, dst, etype, **attrs):
    return service.add_edge(conn, {
        "src": src, "dst": dst, "edge_type": etype, "attrs": attrs,
        "valid_from": V0, "valid_until": None,
    })


def seed_demo(conn: sqlite3.Connection) -> dict:
    # the changed raw material and its spec version
    _node(conn, "MAT-PEP", "supplier_material", "培养基关键组分-大豆蛋白胨",
          role="key_component", old_supplier="S-A（停产）", new_supplier="S-B")
    _node(conn, "SPEC-PEP", "spec_version", "蛋白胨质量标准 v3（标称等同）",
          standard="CP/EP", registered=True)
    _edge(conn, "MAT-PEP", "SPEC-PEP", "governed_by_spec")

    # three process routes -------------------------------------------------
    for route, prod in (("route-A", "PROD-X"), ("route-B", "PROD-Y"), ("route-C", "PROD-Y")):
        _node(conn, f"FORM-{route[-1]}", "formula", f"配方 {route[-1]}（{route}）",
              route_id=route)
        _edge(conn, "SPEC-PEP", f"FORM-{route[-1]}", "used_in_formula", route_id=route)
        _node(conn, f"STEP-{route[-1]}", "process_step", f"配料/灭菌步骤（{route}）",
              route_id=route)
        _edge(conn, f"FORM-{route[-1]}", f"STEP-{route[-1]}", "has_step", route_id=route)

    # shared + dedicated analytical methods
    _node(conn, "METHOD-SHARED", "analytical_method", "无菌检查/生长Promotion方法（共用）")
    _node(conn, "METHOD-B", "analytical_method", "路线B专属残留检测方法")
    _edge(conn, "STEP-A", "METHOD-SHARED", "measured_by")
    _edge(conn, "STEP-C", "METHOD-SHARED", "measured_by")
    _edge(conn, "STEP-B", "METHOD-B", "measured_by")

    # validation studies: A/B still open, C closed
    _node(conn, "VAL-A", "validation_study", "工艺验证 PPQ-A（未结束）", status="open")
    _node(conn, "VAL-B", "validation_study", "培养基模拟灌装验证-B（未结束）", status="open")
    _node(conn, "VAL-C", "validation_study", "路线C清洁验证（已关闭）", status="closed")
    _edge(conn, "STEP-A", "VAL-A", "validated_by")
    _edge(conn, "STEP-B", "VAL-B", "validated_by")
    _edge(conn, "STEP-C", "VAL-C", "validated_by")

    # registered products / market commitments
    _node(conn, "PROD-X", "product_market", "注射液 X（注册产品）", registered=True,
          markets=["CN", "EU"])
    _node(conn, "PROD-Y", "product_market", "冻干制剂 Y（注册产品）", registered=True,
          markets=["CN"])
    _edge(conn, "STEP-A", "PROD-X", "yields_product")
    _edge(conn, "STEP-B", "PROD-Y", "yields_product")
    _edge(conn, "STEP-C", "PROD-Y", "yields_product")

    # unrelated material -> unrelated product (must NOT be impacted)
    _node(conn, "MAT-OTHER", "supplier_material", "非相关辅料-氯化钠")
    _node(conn, "SPEC-OTHER", "spec_version", "氯化钠标准 v1")
    _node(conn, "FORM-Z", "formula", "配方 Z", route_id="route-Z")
    _node(conn, "PROD-Z", "product_market", "未受影响产品 Z", registered=False)
    _edge(conn, "MAT-OTHER", "SPEC-OTHER", "governed_by_spec")
    _edge(conn, "SPEC-OTHER", "FORM-Z", "used_in_formula")
    _edge(conn, "FORM-Z", "PROD-Z", "yields_product")

    # production batches ----------------------------------------------------
    batches = [
        ("B-VAL-A1", "PROD-X", "route-A", "in_validation", "new_source"),
        ("B-VAL-B1", "PROD-Y", "route-B", "in_validation", "new_source"),
        ("B-REL-Y2", "PROD-Y", "route-C", "released", "new_source"),
        ("B-OLD-X9", "PROD-X", "route-A", "released", "old_source"),
        ("B-DONE-0", "PROD-Y", "route-B", "completed", "new_source"),
    ]
    for bid, pid, route, status, use in batches:
        service.add_batch(conn, {
            "id": bid, "product_id": pid, "route_id": route, "status": status,
            "planned_use": use, "blocked_until_change_approved": True,
        })

    return {"assess_at": ASSESS_AT, "sever_at": SEVER_AT}
