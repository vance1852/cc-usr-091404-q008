"""FastAPI application for raw-material change impact assessment."""

from __future__ import annotations

import os
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from . import db as dbmod
from . import service
from .models import (
    BatchIn,
    ChangeIn,
    EdgeIn,
    EvidenceIn,
    NodeIn,
    ResolveConflictIn,
    ReviewIn,
)

DB_PATH = os.environ.get("CHANGE_DB", str(Path(__file__).resolve().parent.parent / "change.db"))
_conn = None


def get_conn():
    global _conn
    if _conn is None:
        _conn = dbmod.connect(DB_PATH)
        dbmod.init_db(_conn)
    return _conn


def reset_conn_for_tests(path: str = ":memory:") -> None:
    global _conn, DB_PATH
    if _conn is not None:
        _conn.close()
    DB_PATH = path
    _conn = dbmod.connect(path)
    dbmod.init_db(_conn)


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_conn()
    yield


app = FastAPI(
    title="原辅料变更影响评估服务",
    version="1.0.0",
    description="带生效区间的供应商物料依赖图、不可变影响快照、风险规则分派技术/法规/质量评审门控。",
    lifespan=lifespan,
)


def raise_service_error(e: service.ServiceError) -> None:
    raise HTTPException(status_code=e.status, detail=e.detail)


# ------------------------------------------------------------------ registry

@app.post("/nodes", status_code=201, tags=["registry"])
def create_node(body: NodeIn, conn=Depends(get_conn)):
    try:
        return service.upsert_node(conn, body.model_dump())
    except service.ServiceError as e:
        raise_service_error(e)


@app.post("/edges", status_code=201, tags=["registry"])
def create_edge(body: EdgeIn, conn=Depends(get_conn)):
    try:
        return service.add_edge(conn, body.model_dump())
    except service.ServiceError as e:
        raise_service_error(e)


@app.post("/edges/{rowid}/sever", tags=["registry"])
def sever_edge(rowid: int, valid_until: str, conn=Depends(get_conn)):
    """解绑关系：只关闭生效区间，历史快照与评估依据保留。"""
    try:
        return service.sever_edge(conn, rowid, valid_until)
    except service.ServiceError as e:
        raise_service_error(e)


@app.post("/batches", status_code=201, tags=["registry"])
def create_batch(body: BatchIn, conn=Depends(get_conn)):
    return service.add_batch(conn, body.model_dump())


@app.get("/graph/cycles", tags=["registry"])
def graph_cycles(at: str, conn=Depends(get_conn)):
    """检测 live 图在某时点的依赖环。"""
    from .graph import GraphView
    nrows = dbmod.query(
        conn, "SELECT * FROM nodes WHERE valid_from<=? AND (valid_until IS NULL OR ?<valid_until)",
        (at, at))
    erows = dbmod.query(
        conn, "SELECT * FROM edges WHERE valid_from<=? AND (valid_until IS NULL OR ?<valid_until)",
        (at, at))
    g = GraphView.from_rows(nrows, erows, at=at)
    return {"at": at, "cycles": g.tarjan_scc(), "node_count": len(g.nodes), "edge_count": len(g.edges)}


# ------------------------------------------------------------------- changes

@app.post("/changes", status_code=201, tags=["changes"])
def create_change(body: ChangeIn, conn=Depends(get_conn)):
    try:
        return service.create_change(conn, body.model_dump())
    except service.ServiceError as e:
        raise_service_error(e)


@app.get("/changes", tags=["changes"])
def list_changes(conn=Depends(get_conn)):
    return service.list_changes(conn)


@app.get("/changes/{change_id}", tags=["changes"])
def get_change(change_id: str, conn=Depends(get_conn)):
    """一次查询看到：路径解释、遗漏材料、意见版本、生效边界、仍禁用新来源的批次。"""
    try:
        return service.assess(conn, change_id)
    except service.ServiceError as e:
        raise_service_error(e)


@app.post("/changes/{change_id}/evidence", tags=["changes"])
def add_evidence(change_id: str, body: EvidenceIn, conn=Depends(get_conn)):
    try:
        return service.add_evidence(conn, change_id, body.model_dump())
    except service.ServiceError as e:
        raise_service_error(e)


@app.post("/changes/{change_id}/reviews", tags=["changes"])
def add_review(change_id: str, body: ReviewIn, conn=Depends(get_conn)):
    try:
        return service.add_review(conn, change_id, body.model_dump())
    except service.ServiceError as e:
        raise_service_error(e)


@app.post("/changes/{change_id}/resolve-conflict", tags=["changes"])
def resolve_conflict(change_id: str, body: ResolveConflictIn, conn=Depends(get_conn)):
    try:
        return service.resolve_conflict(conn, change_id, body.model_dump())
    except service.ServiceError as e:
        raise_service_error(e)


class ApproveIn(BaseModel):
    approved_by: str
    note: str = ""


@app.post("/changes/{change_id}/approve", tags=["changes"])
def approve(change_id: str, body: ApproveIn, conn=Depends(get_conn)):
    try:
        return service.approve_change(conn, change_id, body.approved_by, body.note)
    except service.ServiceError as e:
        raise_service_error(e)


class RejectIn(BaseModel):
    by: str
    note: str = ""


@app.post("/changes/{change_id}/reject", tags=["changes"])
def reject(change_id: str, body: RejectIn, conn=Depends(get_conn)):
    return service.reject_change(conn, change_id, body.by, body.note)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
