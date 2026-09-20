"""SQLite 存储层。

节点与依赖边都带生效区间(valid_from / valid_to, 半开区间 [valid_from, valid_to))；
影响评估的每次运行(impact_runs)把当时的节点、边、路径、环原样复制进快照表，
此后即使边被解绑(valid_to 置为过去)也不会改变历史评估依据。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 依赖图节点：供应商物料/规格版本/配方/工艺路线/工艺步骤/分析方法/
-- 验证研究/生产批次/许可文件/产品/市场
CREATE TABLE IF NOT EXISTS nodes (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    code       TEXT NOT NULL,
    name       TEXT NOT NULL,
    attrs      TEXT NOT NULL DEFAULT '{}',
    valid_from TEXT NOT NULL,
    valid_to   TEXT,
    created_at TEXT NOT NULL
);

-- 依赖边：from_node 的变更影响沿边传播到 to_node（to_node 依赖 from_node）
CREATE TABLE IF NOT EXISTS edges (
    id         TEXT PRIMARY KEY,
    from_node  TEXT NOT NULL REFERENCES nodes(id),
    to_node    TEXT NOT NULL REFERENCES nodes(id),
    label      TEXT NOT NULL DEFAULT '',
    attrs      TEXT NOT NULL DEFAULT '{}',
    valid_from TEXT NOT NULL,
    valid_to   TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_node);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_node);

-- 变更单
CREATE TABLE IF NOT EXISTS changes (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    change_type         TEXT NOT NULL CHECK (change_type IN ('normal','emergency')),
    material_node       TEXT NOT NULL REFERENCES nodes(id),
    status              TEXT NOT NULL DEFAULT 'open',
    risk_level          TEXT NOT NULL DEFAULT 'low',
    as_of               TEXT NOT NULL,           -- 发起时冻结日期
    effective_from      TEXT,                    -- 批准切换的生效起点
    effective_to        TEXT,                    -- 生效终点(可空)
    emergency_restrictions TEXT NOT NULL DEFAULT '{}',
    decided_at          TEXT,
    created_at          TEXT NOT NULL
);

-- 影响评估运行：seq=1 为发起快照，其后为“新证据扩展范围”
CREATE TABLE IF NOT EXISTS impact_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id  TEXT NOT NULL REFERENCES changes(id),
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('initial','expansion')),
    as_of      TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (change_id, seq)
);

CREATE TABLE IF NOT EXISTS snapshot_nodes (
    run_id     INTEGER NOT NULL REFERENCES impact_runs(id),
    node_id    TEXT NOT NULL,
    type       TEXT NOT NULL,
    code       TEXT NOT NULL,
    name       TEXT NOT NULL,
    attrs      TEXT NOT NULL DEFAULT '{}',
    valid_from TEXT NOT NULL,
    valid_to   TEXT,
    PRIMARY KEY (run_id, node_id)
);

CREATE TABLE IF NOT EXISTS snapshot_edges (
    run_id     INTEGER NOT NULL REFERENCES impact_runs(id),
    edge_id    TEXT NOT NULL,
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    valid_from TEXT NOT NULL,
    valid_to   TEXT,
    PRIMARY KEY (run_id, edge_id)
);

CREATE TABLE IF NOT EXISTS impact_paths (
    run_id     INTEGER NOT NULL REFERENCES impact_runs(id),
    path_idx   INTEGER NOT NULL,
    nodes      TEXT NOT NULL,   -- [{node_id,type,code,name}]
    edges      TEXT NOT NULL,   -- [{edge_id,src,dst,label,valid_from,valid_to}]
    PRIMARY KEY (run_id, path_idx)
);

CREATE TABLE IF NOT EXISTS cycle_findings (
    run_id     INTEGER NOT NULL REFERENCES impact_runs(id),
    cycle      TEXT NOT NULL,   -- 环上的节点 id 列表
    reachable  INTEGER NOT NULL -- 是否可从变更物料到达
);

-- 评审分派（扩展范围可能追加新的强制角色）
CREATE TABLE IF NOT EXISTS review_assignments (
    change_id   TEXT NOT NULL REFERENCES changes(id),
    role        TEXT NOT NULL,
    mandatory   INTEGER NOT NULL,
    rationale   TEXT NOT NULL DEFAULT '',
    added_seq   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (change_id, role)
);

-- 意见版本：每次提交追加一行，version 单调递增；最新版本为当前意见
CREATE TABLE IF NOT EXISTS review_opinions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id  TEXT NOT NULL REFERENCES changes(id),
    role       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    decision   TEXT NOT NULL CHECK (decision IN ('approved','objected')),
    comment    TEXT NOT NULL DEFAULT '',
    reviewer   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (change_id, role, version)
);

-- 矛盾意见的解决记录（针对某个 objected 历史版本）
CREATE TABLE IF NOT EXISTS resolutions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id  TEXT NOT NULL REFERENCES changes(id),
    opinion_id INTEGER NOT NULL REFERENCES review_opinions(id),
    resolution TEXT NOT NULL,
    reviewer   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 已提交证据材料
CREATE TABLE IF NOT EXISTS evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id       TEXT NOT NULL REFERENCES changes(id),
    requirement_key TEXT NOT NULL,
    title           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (change_id, requirement_key)
);

-- 批准后逐批放行（未放行批次仍禁止使用新来源）
CREATE TABLE IF NOT EXISTS batch_releases (
    change_id  TEXT NOT NULL REFERENCES changes(id),
    batch_node TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    reviewer   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (change_id, batch_node)
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db_path() -> Path:
    return Path(os.environ.get("CHANGE_DB", str(Path.cwd() / "changeimpact.db")))


def connect(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or get_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
