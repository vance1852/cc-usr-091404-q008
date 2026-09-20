"""Domain service: change orchestration, immutable snapshots, evidence
extension, review gating and batch-blocking decisions.

Immutability guarantees:

* ``create_change`` copies the rows effective at ``as_of`` into
  ``snapshot_nodes`` / ``snapshot_edges``.  Severing a live edge afterwards
  only closes the live interval; the frozen rows stay.
* Required materials derived at freeze time stay rows forever.  New evidence
  can *add* requirements (``origin='evidence'``) and satisfy existing ones,
  never delete the original basis.
* Reviews and conflict resolutions are append-only/versioned.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from . import db as dbmod
from .graph import Edge, GraphView, intersect_windows
from .rules import QUALITY, evaluate, path_explains

DISCIPLINES = ("technical", "regulatory", "quality")
BLOCKING_BATCH_STATUSES = ("in_validation", "released", "quarantined")


class ServiceError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


# --------------------------------------------------------------------- helpers

def _loads(s: str, default):
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ------------------------------------------------------------- registry (graph)

def upsert_node(conn: sqlite3.Connection, data: dict) -> dict:
    existing = dbmod.query_one(conn, "SELECT id FROM nodes WHERE id=?", (data["id"],))
    if existing:
        with dbmod.tx(conn):
            conn.execute(
                "UPDATE nodes SET node_type=?, name=?, attrs=?, valid_from=?, valid_until=? WHERE id=?",
                (data["node_type"], data["name"], json.dumps(data["attrs"], ensure_ascii=False),
                 data["valid_from"], data["valid_until"], data["id"]),
            )
    else:
        with dbmod.tx(conn):
            conn.execute(
                "INSERT INTO nodes(id, node_type, name, attrs, valid_from, valid_until) "
                "VALUES(?,?,?,?,?,?)",
                (data["id"], data["node_type"], data["name"],
                 json.dumps(data["attrs"], ensure_ascii=False),
                 data["valid_from"], data["valid_until"]),
            )
    return data


def add_edge(conn: sqlite3.Connection, data: dict) -> dict:
    for end in ("src", "dst"):
        if not dbmod.query_one(conn, "SELECT 1 FROM nodes WHERE id=?", (data[end],)):
            raise ServiceError(404, f"节点不存在: {data[end]}")
    with dbmod.tx(conn):
        cur = conn.execute(
            "INSERT INTO edges(src, dst, edge_type, attrs, valid_from, valid_until) "
            "VALUES(?,?,?,?,?,?)",
            (data["src"], data["dst"], data["edge_type"],
             json.dumps(data["attrs"], ensure_ascii=False),
             data["valid_from"], data["valid_until"]),
        )
        data["rowid"] = cur.lastrowid
    return data


def sever_edge(conn: sqlite3.Connection, rowid: int, valid_until: str) -> dict:
    """Sever a relationship: close its interval.  The row is kept forever."""
    row = dbmod.query_one(conn, "SELECT * FROM edges WHERE id=?", (rowid,))
    if row is None:
        raise ServiceError(404, f"边不存在: {rowid}")
    if valid_until <= row["valid_from"]:
        raise ServiceError(422, "valid_until 必须晚于 valid_from（半开区间）")
    with dbmod.tx(conn):
        conn.execute("UPDATE edges SET valid_until=? WHERE id=?", (valid_until, rowid))
    return {"rowid": rowid, "valid_until": valid_until, "severed": True}


def add_batch(conn: sqlite3.Connection, data: dict) -> dict:
    with dbmod.tx(conn):
        conn.execute(
            "INSERT OR REPLACE INTO production_batches"
            "(id, product_id, route_id, status, planned_use, blocked_until_change_approved, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (data["id"], data["product_id"], data["route_id"], data["status"],
             data["planned_use"], int(data.get("blocked_until_change_approved", True)),
             dbmod.utcnow_iso()),
        )
    return data


# ----------------------------------------------------------------- change flow

def create_change(conn: sqlite3.Connection, data: dict) -> dict:
    as_of = data.get("as_of") or dbmod.utcnow_iso()
    subject = data["subject_node"]
    node = dbmod.query_one(conn, "SELECT * FROM nodes WHERE id=?", (subject,))
    if node is None:
        raise ServiceError(404, f"变更对象节点不存在: {subject}")
    if not (node["valid_from"] <= as_of and (node["valid_until"] is None or as_of < node["valid_until"])):
        raise ServiceError(422, f"变更对象在评估时点 {as_of} 未生效")

    change_id = _new_change_id(conn)
    now = dbmod.utcnow_iso()
    with dbmod.tx(conn):
        conn.execute(
            "INSERT INTO changes(id, subject_node, title, description, change_type, status,"
            " as_of, effective_from, limited_batches, expiry_rule, created_at, version)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
            (change_id, subject, data["title"], data.get("description", ""),
             data["change_type"], "open", as_of, data.get("effective_from"),
             json.dumps(data.get("limited_batches", [])), data.get("expiry_rule", ""), now),
        )
        # ---- freeze the snapshot effective at as_of -----------------------
        nrows = conn.execute(
            "SELECT * FROM nodes WHERE valid_from<=? AND (valid_until IS NULL OR ?<valid_until)",
            (as_of, as_of),
        ).fetchall()
        erows = conn.execute(
            "SELECT * FROM edges WHERE valid_from<=? AND (valid_until IS NULL OR ?<valid_until)",
            (as_of, as_of),
        ).fetchall()
        conn.execute(
            "INSERT INTO snapshots(change_id, as_of, subject_node, frozen_at, note) VALUES(?,?,?,?,?)",
            (change_id, as_of, subject, now, "发起变更时冻结的影响快照"),
        )
        conn.executemany(
            "INSERT INTO snapshot_nodes(change_id, node_id, node_type, name, attrs, valid_from, valid_until)"
            " VALUES(?,?,?,?,?,?,?)",
            [(change_id, r["id"], r["node_type"], r["name"], r["attrs"], r["valid_from"], r["valid_until"])
             for r in nrows],
        )
        conn.executemany(
            "INSERT INTO snapshot_edges(change_id, edge_rowid, src, dst, edge_type, attrs, valid_from, valid_until)"
            " VALUES(?,?,?,?,?,?,?,?)",
            [(change_id, r["id"], r["src"], r["dst"], r["edge_type"], r["attrs"],
              r["valid_from"], r["valid_until"]) for r in erows],
        )

    detail = assess(conn, change_id)
    # persist the frozen-basis materials & review assignments
    _persist_materials(conn, change_id, detail["snapshot"]["required_materials"], origin="frozen")
    _reconcile_assignments(conn, change_id, detail["gating"]["mandatory_disciplines"])
    return detail


def add_evidence(conn: sqlite3.Connection, change_id: str, data: dict) -> dict:
    change = _get_change(conn, change_id)
    if change["status"] in ("approved", "rejected"):
        raise ServiceError(409, f"变更已 {change['status']}，不能再追加证据")
    payload = data["payload"]
    kind = data["kind"]

    if kind == "added_edge":
        for k in ("src", "dst", "valid_from"):
            if k not in payload:
                raise ServiceError(422, f"added_edge 证据缺少字段 {k}")
        known = {r["node_id"] for r in dbmod.query(
            conn, "SELECT node_id FROM snapshot_nodes WHERE change_id=?", (change_id,))}
        for r in dbmod.query(
            conn,
            "SELECT payload FROM evidence WHERE change_id=? AND kind='added_node'", (change_id,)):
            known.add(_loads(r["payload"], {}).get("id"))
        for end in ("src", "dst"):
            if payload[end] not in known:
                raise ServiceError(422, f"证据边引用的节点 {payload[end]} 不在快照或已声明节点中")
    elif kind == "added_node":
        for k in ("id", "node_type", "name", "valid_from"):
            if k not in payload:
                raise ServiceError(422, f"added_node 证据缺少字段 {k}")
    elif kind == "document":
        if "material_key" not in payload:
            raise ServiceError(422, "document 证据需要 material_key")

    seq = dbmod.query_one(
        conn, "SELECT COALESCE(MAX(seq),0)+1 AS s FROM evidence WHERE change_id=?", (change_id,))["s"]
    with dbmod.tx(conn):
        conn.execute(
            "INSERT INTO evidence(change_id, seq, kind, payload, added_by, added_at) VALUES(?,?,?,?,?,?)",
            (change_id, seq, kind, json.dumps(payload, ensure_ascii=False),
             data.get("added_by", "system"), dbmod.utcnow_iso()),
        )

    detail = assess(conn, change_id)
    # new evidence may EXTEND scope: insert newly discovered requirements,
    # never delete requirements from the frozen basis
    if kind in ("added_edge", "added_node"):
        _persist_materials(conn, change_id, detail["snapshot"]["required_materials"], origin="evidence")
        _reconcile_assignments(conn, change_id, detail["gating"]["mandatory_disciplines"])
    if kind == "document":
        key = payload["material_key"]
        with dbmod.tx(conn):
            conn.execute(
                "UPDATE change_materials SET provided=1, evidence_seq=? WHERE change_id=? AND key=?",
                (seq, change_id, key),
            )
    return assess(conn, change_id)


def add_review(conn: sqlite3.Connection, change_id: str, data: dict) -> dict:
    _get_change(conn, change_id)
    version = dbmod.query_one(
        conn,
        "SELECT COALESCE(MAX(version),0)+1 AS v FROM reviews WHERE change_id=? AND discipline=?",
        (change_id, data["discipline"]),
    )["v"]
    with dbmod.tx(conn):
        conn.execute(
            "INSERT INTO reviews(change_id, discipline, reviewer, decision, comment, version, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (change_id, data["discipline"], data["reviewer"], data["decision"],
             data.get("comment", ""), version, dbmod.utcnow_iso()),
        )
    return assess(conn, change_id)


def resolve_conflict(conn: sqlite3.Connection, change_id: str, data: dict) -> dict:
    _get_change(conn, change_id)
    with dbmod.tx(conn):
        conn.execute(
            "INSERT INTO conflict_resolutions(change_id, resolution, note, by, created_at)"
            " VALUES(?,?,?,?,?)",
            (change_id, data["resolution"], data.get("note", ""),
             data.get("by", "quality_lead"), dbmod.utcnow_iso()),
        )
    return assess(conn, change_id)


def approve_change(conn: sqlite3.Connection, change_id: str, approved_by: str, note: str = "") -> dict:
    detail = assess(conn, change_id)
    if detail["block_reasons"]:
        return {"change_id": change_id, "approved": False,
                "approved_at": None, "block_reasons": detail["block_reasons"]}
    with dbmod.tx(conn):
        conn.execute("UPDATE changes SET status='approved' WHERE id=?", (change_id,))
        conn.execute(
            "INSERT OR REPLACE INTO approvals(change_id, approved_by, approved_at, note) VALUES(?,?,?,?)",
            (change_id, approved_by, dbmod.utcnow_iso(), note),
        )
    return {"change_id": change_id, "approved": True,
            "approved_at": dbmod.utcnow_iso(), "block_reasons": []}


def reject_change(conn, change_id, by, note=""):
    _get_change(conn, change_id)
    with dbmod.tx(conn):
        conn.execute("UPDATE changes SET status='rejected' WHERE id=?", (change_id,))
    return {"change_id": change_id, "status": "rejected", "by": by, "note": note}


# ----------------------------------------------------------------- assessment

def _load_assessment_graph(conn: sqlite3.Connection, change: sqlite3.Row) -> GraphView:
    as_of = change["as_of"]
    nrows = dbmod.query(conn, "SELECT * FROM snapshot_nodes WHERE change_id=?", (change["id"],))
    erows = dbmod.query(conn, "SELECT * FROM snapshot_edges WHERE change_id=?", (change["id"],))
    g = GraphView.from_rows(nrows, erows, at=as_of)

    # overlay append-only evidence (scope extension)
    for ev in dbmod.query(conn, "SELECT * FROM evidence WHERE change_id=? ORDER BY seq", (change["id"],)):
        p = _loads(ev["payload"], {})
        if ev["kind"] == "added_node" and p.get("id") not in g.nodes:
            from .graph import Node
            g.nodes[p["id"]] = Node(
                id=p["id"], node_type=p["node_type"], name=p["name"],
                attrs=p.get("attrs", {}), valid_from=p["valid_from"],
                valid_until=p.get("valid_until"))
            g._adj.setdefault(p["id"], [])
        elif ev["kind"] == "added_edge":
            g.add_evidence_edge(Edge(
                src=p["src"], dst=p["dst"], edge_type=p.get("edge_type", "depends_on"),
                attrs=p.get("attrs", {}), valid_from=p.get("valid_from", as_of),
                valid_until=p.get("valid_until"), source="evidence", seq=ev["seq"]))
    g._rebuild_adj()
    return g


def assess(conn: sqlite3.Connection, change_id: str) -> dict:
    change = _get_change(conn, change_id)
    g = _load_assessment_graph(conn, change)
    if change["subject_node"] not in g.nodes:
        raise ServiceError(422, "快照中缺少变更对象节点（数据异常）")

    paths, affected, truncated, cycles = path_explains(g, change["subject_node"])
    product_ids = affected.get("product_market", [])

    # live batches riding the affected products
    if product_ids:
        batch_rows = dbmod.query(
            conn,
            f"SELECT * FROM production_batches WHERE product_id IN ({','.join('?' * len(product_ids))})",
            tuple(product_ids),
        )
    else:
        batch_rows = []
    affected_batches = [dict(b) for b in batch_rows]
    for b in affected_batches:
        b["blocked_until_change_approved"] = bool(b["blocked_until_change_approved"])
    # batches that could actually consume the new source
    switch_candidate_batches = [
        b for b in affected_batches
        if b["status"] != "completed" and b.get("planned_use") != "old_source"
    ]

    rule = evaluate(g, change["subject_node"], {"change_type": change["change_type"]},
                    switch_candidate_batches)

    # merge persisted material state (provided flags survive)
    persisted = {r["key"]: dict(r) for r in dbmod.query(
        conn, "SELECT * FROM change_materials WHERE change_id=?", (change_id,))}
    materials_out = []
    for key, m in rule.materials.items():
        p = persisted.get(key)
        m.provided = bool(p["provided"]) if p else False
        m.evidence_seq = p["evidence_seq"] if p else None
        d = m.as_dict()
        d["origin"] = p["origin"] if p else "pending"
        materials_out.append(d)
    # requirements that vanished from the live re-evaluation are KEPT (unbind-proof)
    for key, p in persisted.items():
        if key not in rule.materials:
            materials_out.append({
                "key": p["key"], "discipline": p["discipline"], "item": p["item"],
                "required": bool(p["required"]), "provided": bool(p["provided"]),
                "source": p["source"], "evidence_seq": p["evidence_seq"],
                "origin": p["origin"], "retained_after_unbind": True,
            })
    materials_out.sort(key=lambda x: (x["discipline"], x["key"]))
    missing = [m for m in materials_out if m["required"] and not m["provided"]]

    # reviews
    reviews = _review_states(conn, change_id)
    latest = {r["discipline"]: r["latest_decision"] for r in reviews
              if r["latest_decision"] is not None}
    mandatory = sorted(rule.mandatory)
    resolution_rows = dbmod.query(
        conn, "SELECT * FROM conflict_resolutions WHERE change_id=? ORDER BY id", (change_id,))
    latest_resolution = dict(resolution_rows[-1]) if resolution_rows else None
    objecting = sorted({r["discipline"] for r in reviews if r["latest_decision"] == "objected"})

    block_reasons: list[str] = []
    for d in mandatory:
        if latest.get(d) is None:
            block_reasons.append(f"缺少强制评审: {d}")
        elif latest[d] == "objected":
            block_reasons.append(f"强制评审 {d} 最新意见为反对")
    for d in objecting:
        if d not in mandatory:
            block_reasons.append(f"评审 {d} 提出反对意见")
    if objecting:
        if latest_resolution is None:
            block_reasons.append("存在矛盾/反对意见，尚未记录裁决")
        elif latest_resolution["resolution"] == "rejected_change":
            block_reasons.append("矛盾裁决为否决变更")
        # upheld_change -> contradiction resolved, recorded
    for m in missing:
        block_reasons.append(f"遗漏材料: {m['item']} ({m['key']})")
    if cycles:
        block_reasons.append(f"依赖图存在环: {cycles}")
    if change["change_type"] == "emergency_substitution":
        if not _loads(change["limited_batches"], []):
            block_reasons.append("紧急替代必须登记限用批次清单")
        if not change["expiry_rule"]:
            block_reasons.append("紧急替代必须记录到期/退出条件")
    if change["status"] == "rejected":
        block_reasons.append("变更已被拒绝")

    approved = change["status"] == "approved"
    blocked_batches = _blocked_batches(change, affected_batches, approved)

    subject_node = g.nodes[change["subject_node"]]
    effective_boundary = {
        "subject_window": f"[{subject_node.valid_from}, {subject_node.valid_until or '∞'})",
        "change_effective_from": change["effective_from"],
        "snapshot_as_of": change["as_of"],
        "emergency": {
            "limited_batches": _loads(change["limited_batches"], []),
            "expiry_rule": change["expiry_rule"],
            "expired": _emergency_expired(change["expiry_rule"]),
        } if change["change_type"] == "emergency_substitution" else None,
        "downstream_windows": sorted({
            w for p in paths for w in p["valid_windows"]
        }),
    }

    gating = {
        "mandatory_disciplines": mandatory,
        "reviews_complete": all(latest.get(d) in ("approved", "conditional") for d in mandatory),
        "objecting_disciplines": objecting,
        "conflict_resolution": latest_resolution,
        "missing_material_count": len(missing),
        "cycles": cycles,
    }

    return {
        "change": dict(change),
        "snapshot": {
            "change_id": change["id"],
            "subject_node": change["subject_node"],
            "as_of": change["as_of"],
            "frozen_at": dbmod.query_one(
                conn, "SELECT frozen_at FROM snapshots WHERE change_id=?", (change_id,))["frozen_at"],
            "affected": affected,
            "paths": paths,
            "cycles": cycles,
            "paths_truncated": truncated,
            "risk_level": rule.risk_level,
            "triggered_rules": rule.triggered_rules,
            "required_materials": materials_out,
            "missing_materials": missing,
            "effective_boundary": effective_boundary,
            "blocked_batches": blocked_batches,
        },
        "reviews": reviews,
        "gating": gating,
        "evidence": [dict(e) for e in dbmod.query(
            conn, "SELECT * FROM evidence WHERE change_id=? ORDER BY seq", (change_id,))],
        "approvals": [dict(a) for a in dbmod.query(
            conn, "SELECT * FROM approvals WHERE change_id=?", (change_id,))],
        "can_approve": not block_reasons,
        "block_reasons": block_reasons,
    }


def _blocked_batches(change: sqlite3.Row, affected_batches: list[dict], approved: bool) -> list[dict]:
    """Decide which batches are STILL forbidden from using the new source."""
    out = []
    limited = set(_loads(change["limited_batches"], []))
    emergency = change["change_type"] == "emergency_substitution"
    expired = _emergency_expired(change["expiry_rule"])
    for b in affected_batches:
        # Completed history and batches planned to keep the old source are not
        # candidates for the new source at all; explicit opt-outs are honoured.
        if b["status"] == "completed":
            continue
        if b.get("planned_use") == "old_source":
            continue
        if not b.get("blocked_until_change_approved", True):
            continue
        reasons = []
        allowed = False
        if not approved:
            if b["status"] in BLOCKING_BATCH_STATUSES:
                reasons.append("变更尚未批准")
            if b["status"] == "in_validation":
                reasons.append("验证批未结束")
        elif emergency:
            if expired:
                reasons.append("紧急替代已到期，须恢复经批准来源")
            elif b["id"] in limited:
                allowed = True
            else:
                reasons.append("不在紧急限用批次清单内")
        else:
            allowed = True  # approved routine switch
        out.append({
            "batch_id": b["id"], "product_id": b["product_id"], "route_id": b["route_id"],
            "status": b["status"], "new_source_forbidden": not allowed,
            "allowed": allowed, "reasons": reasons,
        })
    return sorted(out, key=lambda x: x["batch_id"])


def _emergency_expired(expiry_rule: str) -> bool:
    dt = _parse_iso(expiry_rule or "")
    if dt is None:
        return False
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.utcnow()
    return now > dt


# ----------------------------------------------------------------- small utils

def _get_change(conn, change_id) -> sqlite3.Row:
    row = dbmod.query_one(conn, "SELECT * FROM changes WHERE id=?", (change_id,))
    if row is None:
        raise ServiceError(404, f"变更不存在: {change_id}")
    return row


def _new_change_id(conn) -> str:
    n = dbmod.query_one(conn, "SELECT COUNT(*) AS c FROM changes")["c"]
    return f"CHG-{n + 1:04d}"


def _persist_materials(conn, change_id, materials, origin: str) -> None:
    with dbmod.tx(conn):
        for m in materials:
            conn.execute(
                "INSERT INTO change_materials(change_id, key, discipline, item, required, provided, source, origin)"
                " VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(change_id, key) DO UPDATE SET "
                "item=excluded.item, discipline=excluded.discipline, source=excluded.source",
                (change_id, m["key"], m["discipline"], m["item"],
                 int(m["required"]), int(m["provided"]), m.get("source", ""), origin),
            )


def _reconcile_assignments(conn, change_id, mandatory: list[str]) -> None:
    with dbmod.tx(conn):
        for d in DISCIPLINES:
            conn.execute(
                "INSERT INTO review_assignments(change_id, discipline, mandatory, reason, assigned_at)"
                " VALUES(?,?,?,?,?) ON CONFLICT(change_id, discipline) DO UPDATE SET mandatory=excluded.mandatory",
                (change_id, d, int(d in mandatory),
                 "风险规则触发的强制评审" if d in mandatory else "可选知会", dbmod.utcnow_iso()),
            )


def _review_states(conn, change_id) -> list[dict]:
    states = []
    for d in DISCIPLINES:
        rows = dbmod.query(
            conn, "SELECT * FROM reviews WHERE change_id=? AND discipline=? ORDER BY version",
            (change_id, d))
        assignment = dbmod.query_one(
            conn, "SELECT mandatory FROM review_assignments WHERE change_id=? AND discipline=?",
            (change_id, d))
        states.append({
            "discipline": d,
            "mandatory": bool(assignment["mandatory"]) if assignment else False,
            "latest_decision": rows[-1]["decision"] if rows else None,
            "latest_version": rows[-1]["version"] if rows else None,
            "history": [{"version": r["version"], "reviewer": r["reviewer"],
                         "decision": r["decision"], "comment": r["comment"],
                         "created_at": r["created_at"]} for r in rows],
        })
    return states


def list_changes(conn) -> list[dict]:
    return [dict(r) for r in dbmod.query(conn, "SELECT * FROM changes ORDER BY id")]
