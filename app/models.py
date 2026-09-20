"""Pydantic request/response models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

NodeType = Literal[
    "supplier_material",
    "spec_version",
    "formula",
    "process_step",
    "analytical_method",
    "validation_study",
    "product_market",
]


class NodeIn(BaseModel):
    id: str
    node_type: NodeType
    name: str
    attrs: dict[str, Any] = Field(default_factory=dict)
    valid_from: str
    valid_until: str | None = None


class EdgeIn(BaseModel):
    src: str
    dst: str
    edge_type: str = "depends_on"
    attrs: dict[str, Any] = Field(default_factory=dict)
    valid_from: str
    valid_until: str | None = None


class BatchIn(BaseModel):
    id: str
    product_id: str
    route_id: str
    status: Literal["in_validation", "released", "completed", "quarantined"]
    planned_use: Literal["new_source", "old_source"] = "new_source"
    blocked_until_change_approved: bool = True


class ChangeIn(BaseModel):
    subject_node: str
    title: str
    description: str = ""
    change_type: Literal["source_switch", "emergency_substitution"] = "source_switch"
    as_of: str | None = None          # defaults to now (freeze time)
    effective_from: str | None = None
    limited_batches: list[str] = Field(default_factory=list)
    expiry_rule: str = ""


class EvidenceIn(BaseModel):
    kind: Literal["added_edge", "added_node", "document", "note"]
    payload: dict[str, Any]
    added_by: str = "system"


class ReviewIn(BaseModel):
    discipline: Literal["technical", "regulatory", "quality"]
    reviewer: str
    decision: Literal["approved", "conditional", "objected"]
    comment: str = ""


class ResolveConflictIn(BaseModel):
    resolution: Literal["upheld_change", "rejected_change"]
    note: str = ""
    by: str = "quality_lead"


class PathExplain(BaseModel):
    node_ids: list[str]
    node_types: list[str]
    node_names: list[str]
    edge_types: list[str]
    source: Literal["frozen", "evidence"]
    valid_windows: list[str]


class ImpactSnapshotOut(BaseModel):
    change_id: str
    subject_node: str
    as_of: str
    frozen_at: str
    affected: dict[str, list[str]]
    paths: list[PathExplain]
    cycles: list[list[str]]
    risk_level: str
    triggered_rules: list[str]
    required_materials: list["RequiredMaterial"]
    missing_materials: list["RequiredMaterial"]
    effective_boundary: dict[str, Any]
    blocked_batches: list[dict[str, Any]]


class RequiredMaterial(BaseModel):
    key: str
    discipline: Literal["technical", "regulatory", "quality"]
    item: str
    required: bool
    provided: bool
    source: str = ""
    evidence_seq: int | None = None


class ReviewState(BaseModel):
    discipline: str
    mandatory: bool
    latest_decision: str | None
    latest_version: int | None
    history: list[dict[str, Any]]


class ChangeDetailOut(BaseModel):
    change: dict[str, Any]
    snapshot: ImpactSnapshotOut
    reviews: list[ReviewState]
    gating: dict[str, Any]
    evidence: list[dict[str, Any]]
    approvals: list[dict[str, Any]]
    can_approve: bool
    block_reasons: list[str]


ImpactSnapshotOut.model_rebuild()


class ApproveOut(BaseModel):
    change_id: str
    approved: bool
    approved_at: str | None = None
    block_reasons: list[str] = Field(default_factory=list)
