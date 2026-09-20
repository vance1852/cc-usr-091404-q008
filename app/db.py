"""SQLite persistence for the raw-material change impact service.

The schema is *bitemporal-lite*:

* Every node and edge carries a validity half-open interval
  ``[valid_from, valid_until)`` expressed as ISO-8601 strings.  A relationship
  being severed only sets ``valid_until`` -- the row is never deleted, so any
  later historical assessment keeps its basis ("解绑不抹证据").
* Change snapshots copy the node/edge rows that were effective at freeze time
  into immutable ``snapshot_nodes`` / ``snapshot_edges`` tables.  Live
  re-binding afterwards can never mutate a frozen snapshot.
* Reviews and evidence are append-only versioned rows.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS nodes (
    id           TEXT PRIMARY KEY,
    node_type    TEXT NOT NULL,           -- supplier_material|spec_version|formula|
                                          -- process_step|analytical_method|
                                          -- validation_study|product_market
    name         TEXT NOT NULL,
    attrs        TEXT NOT NULL DEFAULT '{}',  -- JSON bag
    valid_from   TEXT NOT NULL,
    valid_until  TEXT
);

CREATE TABLE IF NOT EXISTS edges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    src          TEXT NOT NULL REFERENCES nodes(id),
    dst          TEXT NOT NULL REFERENCES nodes(id),
    edge_type    TEXT NOT NULL,
    attrs        TEXT NOT NULL DEFAULT '{}',
    valid_from   TEXT NOT NULL,
    valid_until  TEXT,
    UNIQUE(src, dst, edge_type, valid_from)
);

CREATE TABLE IF NOT EXISTS changes (
    id              TEXT PRIMARY KEY,
    subject_node    TEXT NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    change_type     TEXT NOT NULL,          -- source_switch|emergency_substitution
    status          TEXT NOT NULL,          -- open|approved|rejected|withdrawn
    as_of           TEXT NOT NULL,          -- freeze / assessment time
    effective_from  TEXT,                   -- boundary for the new source
    limited_batches TEXT NOT NULL DEFAULT '[]',  -- JSON: emergency-only batch scope
    expiry_rule     TEXT NOT NULL DEFAULT '',    -- emergency expiry condition
    created_at      TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS snapshots (
    change_id   TEXT PRIMARY KEY REFERENCES changes(id),
    as_of       TEXT NOT NULL,
    subject_node TEXT NOT NULL,
    frozen_at   TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS snapshot_nodes (
    change_id  TEXT NOT NULL,
    node_id    TEXT NOT NULL,
    node_type  TEXT NOT NULL,
    name       TEXT NOT NULL,
    attrs      TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    PRIMARY KEY (change_id, node_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS snapshot_edges (
    change_id  TEXT NOT NULL,
    edge_rowid INTEGER NOT NULL,
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    edge_type  TEXT NOT NULL,
    attrs      TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    PRIMARY KEY (change_id, edge_rowid)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id   TEXT NOT NULL REFERENCES changes(id),
    seq         INTEGER NOT NULL,           -- append order; extends scope
    kind        TEXT NOT NULL,              -- added_edge|added_node|document|note
    payload     TEXT NOT NULL,              -- JSON
    added_by    TEXT NOT NULL DEFAULT 'system',
    added_at    TEXT NOT NULL,
    UNIQUE(change_id, seq)
);

CREATE TABLE IF NOT EXISTS review_assignments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id   TEXT NOT NULL REFERENCES changes(id),
    discipline  TEXT NOT NULL,              -- technical|regulatory|quality
    mandatory   INTEGER NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    assigned_at TEXT NOT NULL,
    UNIQUE(change_id, discipline)
);

CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id   TEXT NOT NULL,
    discipline  TEXT NOT NULL,
    reviewer    TEXT NOT NULL,
    decision    TEXT NOT NULL,              -- approved|conditional|objected
    comment     TEXT NOT NULL DEFAULT '',
    version     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(change_id, discipline, version)
);

CREATE TABLE IF NOT EXISTS approvals (
    change_id   TEXT PRIMARY KEY REFERENCES changes(id),
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS change_materials (
    change_id   TEXT NOT NULL REFERENCES changes(id),
    key         TEXT NOT NULL,
    discipline  TEXT NOT NULL,
    item        TEXT NOT NULL,
    required    INTEGER NOT NULL DEFAULT 1,
    provided    INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT '',
    evidence_seq INTEGER,
    origin      TEXT NOT NULL DEFAULT 'frozen', -- frozen|evidence
    PRIMARY KEY (change_id, key)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS conflict_resolutions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id   TEXT NOT NULL REFERENCES changes(id),
    resolution  TEXT NOT NULL,              -- upheld_change|rejected_change
    note        TEXT NOT NULL DEFAULT '',
    by          TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS production_batches (
    id           TEXT PRIMARY KEY,
    product_id   TEXT NOT NULL,             -- node id (product_market)
    route_id     TEXT NOT NULL,             -- process route identifier
    status       TEXT NOT NULL,             -- in_validation|released|completed|quarantined
    planned_use  TEXT NOT NULL DEFAULT 'new_source'
                              CHECK (planned_use IN ('new_source','old_source')),
    blocked_until_change_approved INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_reviews_change ON reviews(change_id);
CREATE INDEX IF NOT EXISTS idx_batches_product ON production_batches(product_id);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def query(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params).fetchall())


def query_one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()
