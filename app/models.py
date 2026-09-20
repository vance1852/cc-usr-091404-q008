"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

NodeType = Literal[
    "material", "spec", "recipe", "route", "step", "method",
    "validation", "batch", "license", "product", "market",
]


class NodeIn(BaseModel):
    id: str | None = None
    type: NodeType
    code: str
    name: str
    attrs: dict[str, Any] = Field(default_factory=dict)
    valid_from: date
    valid_to: date | None = None


class EdgeIn(BaseModel):
    from_node: str
    to_node: str
    label: str = ""
    attrs: dict[str, Any] = Field(default_factory=dict)
    valid_from: date
    valid_to: date | None = None


class ChangeIn(BaseModel):
    title: str
    material_node: str
    change_type: Literal["normal", "emergency"] = "normal"
    as_of: date


class ExpandIn(BaseModel):
    as_of: date
    note: str = ""


class OpinionIn(BaseModel):
    role: Literal["technical", "regulatory", "quality"]
    decision: Literal["approved", "objected"]
    comment: str = ""
    reviewer: str = ""


class ResolutionIn(BaseModel):
    opinion_id: int
    resolution: str
    reviewer: str = ""


class EvidenceIn(BaseModel):
    requirement_key: str
    title: str


class RestrictionIn(BaseModel):
    allowed_batches: list[str] = Field(default_factory=list)
    expires_on: date
    conditions: str = ""


class BatchReleaseIn(BaseModel):
    reason: str = ""
    reviewer: str = ""


class ApproveIn(BaseModel):
    effective_from: date | None = None
    effective_to: date | None = None
