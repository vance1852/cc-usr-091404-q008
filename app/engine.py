"""变更影响评估引擎：风险规则、快照冻结、范围扩展、评审门、紧急替代、批次封锁。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date
from typing import Any

from . import database as db
from .graph import Edge, Node, TemporalGraph, effective_window

# ---- 风险规则 -------------------------------------------------------------

# 受影响节点类型 -> (证据键, 材料名称, 责任角色)
NODE_REQUIREMENTS: dict[str, list[tuple[str, str, str]]] = {
    "material": [
        ("supplier_qualification", "新供应商资质审计报告", "quality"),
        ("comparative_coa", "新旧来源关键质量属性对比 CoA", "technical"),
    ],
    "spec": [
        ("spec_equivalence", "规格等同性评估（限度逐项对比）", "technical"),
    ],
    "method": [
        ("method_verification", "分析方法适用性/桥接验证方案与报告", "technical"),
    ],
    "validation": [
        ("validation_assessment", "在验研究影响评估与补充验证方案", "technical"),
    ],
    "batch": [
        ("open_batch_disposition", "未结验证/生产批使用新来源的处置决定", "quality"),
    ],
    "license": [
        ("regulatory_filing", "许可文件变更申报/备案评估", "regulatory"),
    ],
    "product": [
        ("product_commitment", "产品质量承诺与稳定性影响评估", "quality"),
    ],
    "market": [
        ("market_commitment", "市场供货承诺与标签/说明书核查", "regulatory"),
    ],
}

TECHNICAL_TYPES = {"spec", "recipe", "route", "step", "method", "validation"}
REGULATORY_TYPES = {"license", "product", "market"}
ROLE_NAMES = {"technical": "技术评审", "regulatory": "法规评审", "quality": "质量评审"}

TERMINAL_STATUSES = {"approved", "rejected", "withdrawn"}


class EngineError(Exception):
    """规则校验失败（HTTP 400/409）。"""


# ---- 图装载 ---------------------------------------------------------------

def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def load_live_graph(conn: sqlite3.Connection, as_of: date) -> TemporalGraph:
    nodes = [
        Node(
            id=r["id"], type=r["type"], code=r["code"], name=r["name"],
            attrs=json.loads(r["attrs"]),
            valid_from=_parse_date(r["valid_from"]), valid_to=_parse_date(r["valid_to"]),
        )
        for r in conn.execute("SELECT * FROM nodes")
    ]
    edges = [
        Edge(
            id=r["id"], src=r["from_node"], dst=r["to_node"], label=r["label"],
            valid_from=_parse_date(r["valid_from"]), valid_to=_parse_date(r["valid_to"]),
        )
        for r in conn.execute("SELECT * FROM edges")
    ]
    return TemporalGraph.build(as_of, nodes, edges)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ---- 图维护 ---------------------------------------------------------------

def upsert_node(conn: sqlite3.Connection, payload: dict[str, Any]) -> str:
    node_id = payload.get("id") or _new_id(payload["type"])
    conn.execute(
        """INSERT INTO nodes (id, type, code, name, attrs, valid_from, valid_to, created_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             type=excluded.type, code=excluded.code, name=excluded.name,
             attrs=excluded.attrs, valid_from=excluded.valid_from, valid_to=excluded.valid_to""",
        (node_id, payload["type"], payload["code"], payload["name"],
         json.dumps(payload.get("attrs", {}), ensure_ascii=False),
         payload["valid_from"].isoformat(),
         payload["valid_to"].isoformat() if payload.get("valid_to") else None,
         db.utc_now()),
    )
    return node_id


def add_edge(conn: sqlite3.Connection, payload: dict[str, Any]) -> str:
    edge_id = _new_id("edge")
    conn.execute(
        """INSERT INTO edges (id, from_node, to_node, label, attrs, valid_from, valid_to, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (edge_id, payload["from_node"], payload["to_node"], payload.get("label", ""),
         json.dumps(payload.get("attrs", {}), ensure_ascii=False),
         payload["valid_from"].isoformat(),
         payload["valid_to"].isoformat() if payload.get("valid_to") else None,
         db.utc_now()),
    )
    return edge_id


def retire_edge(conn: sqlite3.Connection, edge_id: str, valid_to: date) -> None:
    cur = conn.execute(
        "UPDATE edges SET valid_to=? WHERE id=? AND (valid_to IS NULL OR valid_to > ?)",
        (valid_to.isoformat(), edge_id, valid_to.isoformat()),
    )
    if cur.rowcount == 0:
        raise EngineError(f"边 {edge_id} 不存在或已失效，无法解绑")


# ---- 风险评估 -------------------------------------------------------------

def _risk_and_roles(graph: TemporalGraph, reached: set[str], change_type: str):
    types = {graph.nodes[n].type for n in reached}
    open_validations = [
        n for n in reached
        if graph.nodes[n].type == "validation" and graph.nodes[n].attrs.get("status") == "open"
    ]
    routes = {n for n in reached if graph.nodes[n].type == "route"}
    if change_type == "emergency" or open_validations:
        risk = "high"
    elif len(routes) >= 2 or "validation" in types:
        risk = "medium"
    else:
        risk = "low"
    roles = ["quality"]
    if types & TECHNICAL_TYPES:
        roles.append("technical")
    if types & REGULATORY_TYPES:
        roles.append("regulatory")
    rationale = {
        "quality": "变更控制强制质量评审",
        "technical": f"影响技术节点：{sorted(types & TECHNICAL_TYPES)}",
        "regulatory": f"影响法规节点：{sorted(types & REGULATORY_TYPES)}",
    }
    return risk, roles, rationale, open_validations


def _requirements(graph: TemporalGraph, reached: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for nid in sorted(reached):
        node = graph.nodes[nid]
        for base, title, role in NODE_REQUIREMENTS.get(node.type, []):
            out.append({
                "key": f"{base}:{nid}",
                "title": f"{title}（{node.code} {node.name}）",
                "responsible": role,
                "node_id": nid,
            })
    return out


def _freeze_snapshot(conn, change_id: str, seq: int, kind: str, as_of: date,
                     graph: TemporalGraph, root: str, note: str) -> int:
    cur = conn.execute(
        """INSERT INTO impact_runs (change_id, seq, kind, as_of, note, created_at)
           VALUES (?,?,?,?,?,?)""",
        (change_id, seq, kind, as_of.isoformat(), note, db.utc_now()),
    )
    run_id = cur.lastrowid
    reached = graph.reachable(root)
    active_edges = [e for e in graph.edges if e.src in reached and e.dst in reached]
    conn.executemany(
        """INSERT INTO snapshot_nodes (run_id, node_id, type, code, name, attrs, valid_from, valid_to)
           VALUES (?,?,?,?,?,?,?,?)""",
        [(run_id, nid, graph.nodes[nid].type, graph.nodes[nid].code, graph.nodes[nid].name,
          json.dumps(graph.nodes[nid].attrs, ensure_ascii=False),
          graph.nodes[nid].valid_from.isoformat(),
          graph.nodes[nid].valid_to.isoformat() if graph.nodes[nid].valid_to else None)
         for nid in sorted(reached)],
    )
    conn.executemany(
        """INSERT INTO snapshot_edges (run_id, edge_id, src, dst, label, valid_from, valid_to)
           VALUES (?,?,?,?,?,?,?)""",
        [(run_id, e.id, e.src, e.dst, e.label,
          e.valid_from.isoformat() if e.valid_from else None,
          e.valid_to.isoformat() if e.valid_to else None) for e in active_edges],
    )
    paths = graph.all_simple_paths(root)
    for idx, chain in enumerate(paths):
        node_chain = [{
            "node_id": root, "type": graph.nodes[root].type,
            "code": graph.nodes[root].code, "name": graph.nodes[root].name,
        }]
        edge_chain = []
        for e in chain:
            node_chain.append({
                "node_id": e.dst, "type": graph.nodes[e.dst].type,
                "code": graph.nodes[e.dst].code, "name": graph.nodes[e.dst].name,
            })
            edge_chain.append({"edge_id": e.id, "src": e.src, "dst": e.dst, "label": e.label})
        conn.execute(
            "INSERT INTO impact_paths (run_id, path_idx, nodes, edges) VALUES (?,?,?,?)",
            (run_id, idx, json.dumps(node_chain, ensure_ascii=False),
             json.dumps(edge_chain, ensure_ascii=False)),
        )
    cycles = graph.cycles(root)
    conn.executemany(
        "INSERT INTO cycle_findings (run_id, cycle, reachable) VALUES (?,?,?)",
        [(run_id, json.dumps(c, ensure_ascii=False), 1 if any(n in reached for n in c) else 0)
         for c in cycles],
    )
    return run_id


# ---- 变更生命周期 ---------------------------------------------------------

def create_change(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    as_of: date = payload["as_of"]
    root = payload["material_node"]
    graph = load_live_graph(conn, as_of)
    if root not in graph.nodes:
        raise EngineError(f"物料节点 {root} 在 {as_of} 不存在或未生效")

    change_id = _new_id("chg")
    risk, roles, rationale, _ = _risk_and_roles(graph, graph.reachable(root), payload["change_type"])
    conn.execute(
        """INSERT INTO changes (id, title, change_type, material_node, status, risk_level,
                                as_of, created_at)
           VALUES (?,?,?,?, 'open', ?, ?, ?)""",
        (change_id, payload["title"], payload["change_type"], root, risk,
         as_of.isoformat(), db.utc_now()),
    )
    _freeze_snapshot(conn, change_id, 1, "initial", as_of, graph, root, "发起变更时冻结的影响快照")
    for role in roles:
        conn.execute(
            """INSERT INTO review_assignments (change_id, role, mandatory, rationale, added_seq)
               VALUES (?,?,1,?,1)""",
            (change_id, role, rationale[role]),
        )
    conn.commit()
    return get_change(conn, change_id, on=as_of)


def expand_scope(conn: sqlite3.Connection, change_id: str, as_of: date, note: str) -> dict[str, Any]:
    change = _get_change_row(conn, change_id)
    if change["status"] in TERMINAL_STATUSES:
        raise EngineError(f"变更已处于终态 {change['status']}，不能扩展范围")
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq),0)+1 AS s FROM impact_runs WHERE change_id=?", (change_id,)
    ).fetchone()["s"]
    root = change["material_node"]
    graph = load_live_graph(conn, as_of)
    _freeze_snapshot(conn, change_id, seq, "expansion", as_of, graph, root, note)

    reached = graph.reachable(root)
    risk, roles, rationale, _ = _risk_and_roles(graph, reached, change["change_type"])
    added_roles: list[str] = []
    for role in roles:
        cur = conn.execute(
            "INSERT OR IGNORE INTO review_assignments (change_id, role, mandatory, rationale, added_seq)"
            " VALUES (?,? ,1,?,?)",
            (change_id, role, rationale[role], seq),
        )
        if cur.rowcount:
            added_roles.append(role)
    conn.execute("UPDATE changes SET risk_level=? WHERE id=? AND risk_level='low'", (risk, change_id))
    conn.commit()
    result = get_change(conn, change_id, on=as_of)
    result["newly_added_reviews"] = added_roles
    return result


def add_opinion(conn: sqlite3.Connection, change_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    change = _get_change_row(conn, change_id)
    if change["status"] != "open":
        raise EngineError("变更不在开放状态，不能提交意见")
    role = payload["role"]
    assigned = conn.execute(
        "SELECT 1 FROM review_assignments WHERE change_id=? AND role=?", (change_id, role)
    ).fetchone()
    if not assigned:
        raise EngineError(f"{ROLE_NAMES[role]}不在本次变更的评审分派中")
    version = conn.execute(
        "SELECT COALESCE(MAX(version),0)+1 AS v FROM review_opinions WHERE change_id=? AND role=?",
        (change_id, role),
    ).fetchone()["v"]
    cur = conn.execute(
        """INSERT INTO review_opinions (change_id, role, version, decision, comment, reviewer, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (change_id, role, version, payload["decision"], payload.get("comment", ""),
         payload.get("reviewer", ""), db.utc_now()),
    )
    conn.commit()
    return {"opinion_id": cur.lastrowid, "change_id": change_id, "role": role, "version": version}


def resolve_objection(conn: sqlite3.Connection, change_id: str, payload: dict[str, Any]) -> None:
    _get_change_row(conn, change_id)
    opinion = conn.execute(
        "SELECT * FROM review_opinions WHERE id=? AND change_id=?",
        (payload["opinion_id"], change_id),
    ).fetchone()
    if opinion is None:
        raise EngineError("意见不存在")
    if opinion["decision"] != "objected":
        raise EngineError("只有反对意见需要解决")
    dup = conn.execute(
        "SELECT 1 FROM resolutions WHERE opinion_id=?", (payload["opinion_id"],)
    ).fetchone()
    if dup:
        raise EngineError("该反对意见已有解决记录")
    conn.execute(
        """INSERT INTO resolutions (change_id, opinion_id, resolution, reviewer, created_at)
           VALUES (?,?,?,?,?)""",
        (change_id, payload["opinion_id"], payload["resolution"],
         payload.get("reviewer", ""), db.utc_now()),
    )
    conn.commit()


def add_evidence(conn: sqlite3.Connection, change_id: str, payload: dict[str, Any]) -> None:
    _get_change_row(conn, change_id)
    conn.execute(
        "INSERT OR IGNORE INTO evidence (change_id, requirement_key, title, created_at)"
        " VALUES (?,?,?,?)",
        (change_id, payload["requirement_key"], payload["title"], db.utc_now()),
    )
    conn.commit()


def set_emergency_restriction(conn: sqlite3.Connection, change_id: str, payload: dict[str, Any]) -> None:
    change = _get_change_row(conn, change_id)
    if change["change_type"] != "emergency":
        raise EngineError("仅紧急替代可设置限用批次与到期条件")
    conn.execute(
        "UPDATE changes SET emergency_restrictions=? WHERE id=?",
        (json.dumps({
            "allowed_batches": payload["allowed_batches"],
            "expires_on": payload["expires_on"].isoformat(),
            "conditions": payload.get("conditions", ""),
        }, ensure_ascii=False), change_id),
    )
    conn.commit()


def release_batch(conn: sqlite3.Connection, change_id: str, batch_node: str,
                  reason: str, reviewer: str) -> None:
    _get_change_row(conn, change_id)
    conn.execute(
        "INSERT OR IGNORE INTO batch_releases (change_id, batch_node, reason, reviewer, created_at)"
        " VALUES (?,?,?,?,?)",
        (change_id, batch_node, reason, reviewer, db.utc_now()),
    )
    conn.commit()


def _open_blocked_batches(conn, change_id: str) -> list[dict[str, Any]]:
    """最新一次评估快照中状态为 open 的验证/生产批节点。"""
    run = conn.execute(
        "SELECT id FROM impact_runs WHERE change_id=? ORDER BY seq DESC LIMIT 1", (change_id,)
    ).fetchone()
    rows = conn.execute(
        "SELECT node_id, type, code, name, attrs FROM snapshot_nodes WHERE run_id=? AND type IN ('batch','validation')",
        (run["id"],),
    ).fetchall()
    out = []
    for r in rows:
        attrs = json.loads(r["attrs"])
        batches = attrs.get("open_batches")
        if r["node_id"].startswith("validation") or r["type"] == "validation":
            if attrs.get("status") != "open" or not batches:
                continue
            for b in batches:
                out.append({"batch_node": b if isinstance(b, str) else b["id"],
                            "via_node": r["node_id"], "code": r["code"], "name": r["name"]})
        else:  # batch 节点：以批次号(code)作为封锁清单标识
            if attrs.get("status") == "open":
                out.append({"batch_node": r["code"], "via_node": r["node_id"],
                            "code": r["code"], "name": r["name"]})
    return out


def approve_change(conn, change_id: str, effective_from: date | None,
                   effective_to: date | None) -> dict[str, Any]:
    payload = get_change(conn, change_id)
    gate = payload["approval_gate"]
    if not gate["approvable"]:
        raise EngineError("批准条件未满足：" + "；".join(gate["blockers"]))
    eff_from = effective_from or date.today()
    conn.execute(
        "UPDATE changes SET status='approved', effective_from=?, effective_to=?, decided_at=? WHERE id=?",
        (eff_from.isoformat(), effective_to.isoformat() if effective_to else None,
         db.utc_now(), change_id),
    )
    conn.commit()
    return get_change(conn, change_id, on=eff_from)


# ---- 查询 -----------------------------------------------------------------

def _get_change_row(conn, change_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
    if row is None:
        raise EngineError(f"变更单 {change_id} 不存在")
    return row


def _missing_materials(conn, change_id: str) -> list[dict[str, Any]]:
    """合并历次评估范围得到的必备材料，对照已提交证据；扩展只会增补，不会移除。"""
    required: dict[str, dict[str, Any]] = {}
    runs = conn.execute(
        "SELECT id FROM impact_runs WHERE change_id=? ORDER BY seq", (change_id,)
    ).fetchall()
    type_map = NODE_REQUIREMENTS
    for run in runs:
        for r in conn.execute("SELECT * FROM snapshot_nodes WHERE run_id=?", (run["id"],)):
            attrs = json.loads(r["attrs"])
            node_type = r["type"]
            templates = list(type_map.get(node_type, []))
            if node_type == "validation" and (attrs.get("status") != "open" or not attrs.get("open_batches")):
                templates = []
            if node_type == "batch" and attrs.get("status") != "open":
                templates = []
            for base, title, role in templates:
                key = f"{base}:{r['node_id']}"
                required[key] = {
                    "requirement_key": key,
                    "title": f"{title}（{r['code']} {r['name']}）",
                    "responsible": role,
                    "node_id": r["node_id"],
                }
    submitted = {
        r["requirement_key"] for r in conn.execute(
            "SELECT requirement_key FROM evidence WHERE change_id=?", (change_id,))
    }
    out = []
    for key, item in sorted(required.items()):
        item["submitted"] = key in submitted
        out.append(item)
    return out


def _path_view(conn, run_id: int, on: date) -> list[dict[str, Any]]:
    nodes: dict[str, Node] = {}
    for r in conn.execute("SELECT * FROM snapshot_nodes WHERE run_id=?", (run_id,)):
        nodes[r["node_id"]] = Node(
            id=r["node_id"], type=r["type"], code=r["code"], name=r["name"],
            attrs=json.loads(r["attrs"]),
            valid_from=_parse_date(r["valid_from"]), valid_to=_parse_date(r["valid_to"]),
        )
    edges: dict[str, Edge] = {}
    for r in conn.execute("SELECT * FROM snapshot_edges WHERE run_id=?", (run_id,)):
        edges[r["edge_id"]] = Edge(
            id=r["edge_id"], src=r["src"], dst=r["dst"], label=r["label"],
            valid_from=_parse_date(r["valid_from"]), valid_to=_parse_date(r["valid_to"]),
        )
    paths = []
    for r in conn.execute(
        "SELECT path_idx, nodes, edges FROM impact_paths WHERE run_id=? ORDER BY path_idx", (run_id,)
    ):
        chain_nodes = json.loads(r["nodes"])
        chain_edges = json.loads(r["edges"])
        chain = [edges[e["edge_id"]] for e in chain_edges]
        wf, wt = effective_window(chain, nodes)
        explanation = []
        for i, n in enumerate(chain_nodes):
            seg = f"{n['type']}:{n['code']}({n['name']})"
            if i < len(chain_edges):
                lab = chain_edges[i]["label"] or "依赖"
                seg += f"  --[{lab}]-->"
            explanation.append(seg)
        paths.append({
            "path_idx": r["path_idx"],
            "explanation": explanation,
            "nodes": chain_nodes,
            "edges": chain_edges,
            "effective_from": wf.isoformat() if wf else None,
            "effective_to": wt.isoformat() if wt else None,
            "active_on": (wf is None or wf <= on) and (wt is None or wt > on),
        })
    return paths


def get_change(conn, change_id: str, on: date | None = None) -> dict[str, Any]:
    change = _get_change_row(conn, change_id)
    on = on or date.today()

    runs = []
    for r in conn.execute(
        "SELECT * FROM impact_runs WHERE change_id=? ORDER BY seq", (change_id,)
    ):
        cycles = [
            {"nodes": json.loads(c["cycle"]), "reachable": bool(c["reachable"])}
            for c in conn.execute(
                "SELECT cycle, reachable FROM cycle_findings WHERE run_id=? ORDER BY rowid", (r["id"],))
        ]
        runs.append({
            "seq": r["seq"], "kind": r["kind"], "as_of": r["as_of"], "note": r["note"],
            "created_at": r["created_at"], "paths": _path_view(conn, r["id"], on),
            "cycles": cycles,
        })

    assignments = [
        {"role": r["role"], "role_name": ROLE_NAMES[r["role"]], "mandatory": bool(r["mandatory"]),
         "rationale": r["rationale"], "added_seq": r["added_seq"]}
        for r in conn.execute(
            "SELECT * FROM review_assignments WHERE change_id=? ORDER BY role", (change_id,))
    ]

    opinions = []
    for r in conn.execute(
        "SELECT * FROM review_opinions WHERE change_id=? ORDER BY role, version", (change_id,)
    ):
        resolved = conn.execute(
            "SELECT resolution, reviewer, created_at FROM resolutions WHERE opinion_id=?", (r["id"],)
        ).fetchone()
        opinions.append({
            "opinion_id": r["id"], "role": r["role"], "role_name": ROLE_NAMES[r["role"]],
            "version": r["version"], "decision": r["decision"], "comment": r["comment"],
            "reviewer": r["reviewer"], "created_at": r["created_at"],
            "resolved": None if resolved is None else {
                "resolution": resolved["resolution"], "reviewer": resolved["reviewer"],
                "created_at": resolved["created_at"],
            },
        })
    for o in opinions:
        role_rows = [x for x in opinions if x["role"] == o["role"]]
        o["is_latest"] = o["version"] == max(x["version"] for x in role_rows)

    raw_restriction = json.loads(change["emergency_restrictions"]) if change["emergency_restrictions"] else None
    restriction = raw_restriction or None
    blocked = _blocked_batches(conn, change, on, restriction)

    payload = {
        "change_id": change["id"],
        "title": change["title"],
        "change_type": change["change_type"],
        "status": change["status"],
        "risk_level": change["risk_level"],
        "material_node": change["material_node"],
        "frozen_as_of": change["as_of"],
        "approved_window": {
            "effective_from": change["effective_from"],
            "effective_to": change["effective_to"],
        },
        "emergency_restriction": restriction,
        "impact_runs": runs,
        "review_assignments": assignments,
        "opinions": opinions,
        "missing_materials": [m for m in _missing_materials(conn, change_id) if not m["submitted"]],
        "all_materials": _missing_materials(conn, change_id),
        "blocked_batches_on": on.isoformat(),
        "blocked_batches": blocked,
    }
    payload["approval_gate"] = _gate_from_payload(change, payload)
    return payload


def _gate_from_payload(change, payload: dict[str, Any]) -> dict[str, Any]:
    blockers: list[str] = []
    status: dict[str, str] = {}
    latest_by_role: dict[str, dict] = {}
    for o in payload["opinions"]:
        latest_by_role[o["role"]] = o
    for a in payload["review_assignments"]:
        role = a["role"]
        o = latest_by_role.get(role)
        if o is None:
            blockers.append(f"{a['role_name']}尚未提交意见")
            status[role] = "missing"
        elif o["decision"] != "approved":
            blockers.append(f"{a['role_name']}最新意见(v{o['version']})为反对")
            status[role] = f"objected_v{o['version']}"
        else:
            status[role] = f"approved_v{o['version']}"
    for o in payload["opinions"]:
        if o["decision"] == "objected" and o["resolved"] is None:
            blockers.append(f"{o['role_name']}反对意见 v{o['version']} 的矛盾尚未解决")
    for run in payload["impact_runs"][-1:]:
        for c in run["cycles"]:
            if c["reachable"]:
                blockers.append(f"依赖图存在可达环：{c['nodes']}，须先修正主数据")
    if payload["missing_materials"]:
        blockers.append(f"缺少 {len(payload['missing_materials'])} 项必备材料（见 missing_materials）")
    restriction = payload["emergency_restriction"]
    if change["change_type"] == "emergency":
        if (not restriction or not restriction.get("allowed_batches")
                or not restriction.get("expires_on") or not restriction.get("conditions")):
            blockers.append("紧急替代必须记录限用批次、到期日期与使用条件")
    return {
        "approvable": change["status"] == "open" and not blockers,
        "opinion_status": status,
        "blockers": blockers,
    }


def _blocked_batches(conn, change, on: date, restriction: dict | None) -> list[dict[str, Any]]:
    candidates = _open_blocked_batches(conn, change["id"])
    released = {
        r["batch_node"] for r in conn.execute(
            "SELECT batch_node FROM batch_releases WHERE change_id=?", (change["id"],))
    }
    blocked = []
    if change["status"] != "approved":
        for b in candidates:
            blocked.append({**b, "reason": "变更尚未批准切换"})
        return blocked
    if change["change_type"] == "emergency":
        allowed = set((restriction or {}).get("allowed_batches", []))
        expires = _parse_date((restriction or {}).get("expires_on"))
        expired = expires is not None and on >= expires
        for b in candidates:
            if expired:
                blocked.append({**b, "reason": f"紧急替代已于 {expires} 到期"})
            elif b["batch_node"] not in allowed:
                blocked.append({**b, "reason": "不在紧急替代限用批次清单内"})
        return blocked
    for b in candidates:
        if b["batch_node"] not in released:
            blocked.append({**b, "reason": "切换已批准但该批次尚未逐批放行"})
    return blocked
