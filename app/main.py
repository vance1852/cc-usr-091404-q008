"""FastAPI 入口：主数据维护、变更生命周期、评审、批次封锁查询。"""
from __future__ import annotations

from datetime import date
from typing import Any

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import database as db
from . import engine
from .models import (
    ApproveIn, BatchReleaseIn, ChangeIn, EdgeIn, EvidenceIn, ExpandIn,
    NodeIn, OpinionIn, ResolutionIn, RestrictionIn,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    conn.close()
    yield


app = FastAPI(
    title="原辅料变更影响评估服务",
    description="带生效区间的供应商物料依赖图、影响快照、风险评审门与紧急替代管理",
    version="1.0.0",
    lifespan=lifespan,
)


def get_conn():
    conn = db.connect()
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(engine.EngineError)
def engine_error_handler(_request, exc: engine.EngineError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ---- 主数据 ---------------------------------------------------------------

@app.post("/nodes", status_code=201, tags=["graph"])
def create_node(body: NodeIn, conn=Depends(get_conn)) -> dict[str, Any]:
    if body.valid_to is not None and body.valid_to <= body.valid_from:
        raise HTTPException(422, "valid_to 必须晚于 valid_from")
    node_id = engine.upsert_node(conn, body.model_dump())
    conn.commit()
    return {"node_id": node_id}


@app.get("/nodes", tags=["graph"])
def list_nodes(as_of: date | None = Query(None), conn=Depends(get_conn)):
    rows = conn.execute("SELECT * FROM nodes ORDER BY type, code").fetchall()
    out = []
    for r in rows:
        if as_of is not None:
            if r["valid_from"] > as_of.isoformat():
                continue
            if r["valid_to"] is not None and r["valid_to"] <= as_of.isoformat():
                continue
        out.append({"id": r["id"], "type": r["type"], "code": r["code"], "name": r["name"],
                    "valid_from": r["valid_from"], "valid_to": r["valid_to"]})
    return out


@app.post("/edges", status_code=201, tags=["graph"])
def create_edge(body: EdgeIn, conn=Depends(get_conn)):
    if body.valid_to is not None and body.valid_to <= body.valid_from:
        raise HTTPException(422, "valid_to 必须晚于 valid_from")
    for nid in (body.from_node, body.to_node):
        if not conn.execute("SELECT 1 FROM nodes WHERE id=?", (nid,)).fetchone():
            raise HTTPException(404, f"节点 {nid} 不存在")
    edge_id = engine.add_edge(conn, body.model_dump())
    conn.commit()
    return {"edge_id": edge_id}


@app.post("/edges/{edge_id}/retire", tags=["graph"])
def retire_edge(edge_id: str, valid_to: date = Query(..., description="解绑生效日期"),
                conn=Depends(get_conn)):
    """解绑依赖关系：把边的 valid_to 截断到该日期。历史快照不受影响。"""
    try:
        engine.retire_edge(conn, edge_id, valid_to)
    except engine.EngineError as e:
        raise HTTPException(400, str(e))
    conn.commit()
    return {"edge_id": edge_id, "valid_to": valid_to.isoformat()}


@app.get("/graph/cycles", tags=["graph"])
def graph_cycles(as_of: date, conn=Depends(get_conn)):
    """查看 as-of 时刻全图依赖环（用于维护主数据）。"""
    graph = engine.load_live_graph(conn, as_of)
    return {"as_of": as_of.isoformat(), "cycles": graph.cycles()}


# ---- 变更 -----------------------------------------------------------------

@app.post("/changes", status_code=201, tags=["changes"])
def create_change(body: ChangeIn, conn=Depends(get_conn)):
    return engine.create_change(conn, body.model_dump())


@app.get("/changes", tags=["changes"])
def list_changes(conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT id, title, change_type, status, risk_level, material_node, as_of FROM changes ORDER BY rowid"
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/changes/{change_id}", tags=["changes"])
def get_change(change_id: str, on: date = Query(default_factory=date.today),
               conn=Depends(get_conn)):
    """一次变更的完整视图：路径解释、生效边界、材料缺口、意见版本、封锁批次。"""
    return engine.get_change(conn, change_id, on=on)


@app.post("/changes/{change_id}/expand", tags=["changes"])
def expand_scope(change_id: str, body: ExpandIn, conn=Depends(get_conn)):
    """新证据到达时追加评估：扩展影响范围，但永不缩减或改写初始快照。"""
    return engine.expand_scope(conn, change_id, body.as_of, body.note)


@app.post("/changes/{change_id}/opinions", status_code=201, tags=["reviews"])
def add_opinion(change_id: str, body: OpinionIn, conn=Depends(get_conn)):
    return engine.add_opinion(conn, change_id, body.model_dump())


@app.post("/changes/{change_id}/resolutions", status_code=201, tags=["reviews"])
def resolve_objection(change_id: str, body: ResolutionIn, conn=Depends(get_conn)):
    engine.resolve_objection(conn, change_id, body.model_dump())
    return {"change_id": change_id, "opinion_id": body.opinion_id, "resolved": True}


@app.post("/changes/{change_id}/evidence", status_code=201, tags=["reviews"])
def add_evidence(change_id: str, body: EvidenceIn, conn=Depends(get_conn)):
    engine.add_evidence(conn, change_id, body.model_dump())
    return {"change_id": change_id, "requirement_key": body.requirement_key}


@app.post("/changes/{change_id}/emergency-restriction", tags=["changes"])
def set_restriction(change_id: str, body: RestrictionIn, conn=Depends(get_conn)):
    engine.set_emergency_restriction(conn, change_id, body.model_dump())
    return {"change_id": change_id, "restriction_set": True}


@app.post("/changes/{change_id}/batches/{batch_node}/release", tags=["changes"])
def release_batch(change_id: str, batch_node: str, body: BatchReleaseIn = BatchReleaseIn(),
                  conn=Depends(get_conn)):
    engine.release_batch(conn, change_id, batch_node, body.reason, body.reviewer)
    return {"change_id": change_id, "batch_node": batch_node, "released": True}


@app.post("/changes/{change_id}/approve", tags=["changes"])
def approve_change(change_id: str, body: ApproveIn = ApproveIn(), conn=Depends(get_conn)):
    """强制评审全部批准、历史矛盾已解决且无可达环/缺材料时方可切换。"""
    return engine.approve_change(conn, change_id, body.effective_from, body.effective_to)
