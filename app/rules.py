"""Risk rules for raw-material change impact assessment.

Rules inspect the *downstream closure* of the changed supplier material and
decide:

1. overall risk level (low / medium / high);
2. which review disciplines are mandatory (technical, regulatory, quality);
3. the catalogue of required deliverables ("materials") -- licence/filing
   documents, method suitability evidence, registered-spec / market-commitment
   confirmations, validation impact assessments and emergency-restriction
   records.

A material is *provided* only by an append-only evidence document carrying the
same ``material_key``; later severing of graph edges never removes a provided
material nor the requirement that was derived at freeze time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .graph import Edge, GraphView, Node

TECHNICAL = "technical"
REGULATORY = "regulatory"
QUALITY = "quality"


@dataclass
class RequiredMaterial:
    key: str
    discipline: str
    item: str
    required: bool = True
    provided: bool = False
    source: str = ""                 # node id that triggered the requirement
    evidence_seq: int | None = None

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "discipline": self.discipline,
            "item": self.item,
            "required": self.required,
            "provided": self.provided,
            "source": self.source,
            "evidence_seq": self.evidence_seq,
        }


@dataclass
class RuleResult:
    risk_level: str
    triggered_rules: list[str] = field(default_factory=list)
    mandatory: set[str] = field(default_factory=set)
    materials: dict[str, RequiredMaterial] = field(default_factory=dict)

    def require(self, key: str, discipline: str, item: str, source: str = "") -> None:
        if key not in self.materials:
            self.materials[key] = RequiredMaterial(key=key, discipline=discipline,
                                                   item=item, source=source)

    def mark_provided(self, key: str, seq: int) -> bool:
        m = self.materials.get(key)
        if m is None:
            return False
        m.provided = True
        m.evidence_seq = seq
        return True


def _route_key(n: Node, e_attrs: dict | None = None) -> str:
    return str(n.attrs.get("route_id") or n.id)


def evaluate(graph: GraphView, subject: str, change_attrs: dict,
             affected_batches: list[dict]) -> RuleResult:
    """Apply the risk rules.

    ``change_attrs`` carries at least ``change_type``.
    ``affected_batches`` are live batches whose product is downstream.
    """
    res = RuleResult(risk_level="low")
    reachable = graph.downstream(subject)
    reached_nodes = [graph.nodes[nid] for nid in reachable if nid != subject and nid in graph.nodes]

    by_type: dict[str, list[Node]] = {}
    for n in reached_nodes:
        by_type.setdefault(n.node_type, []).append(n)

    spec_nodes = by_type.get("spec_version", [])
    method_nodes = by_type.get("analytical_method", [])
    study_nodes = by_type.get("validation_study", [])
    product_nodes = by_type.get("product_market", [])
    step_nodes = by_type.get("process_step", [])
    formula_nodes = by_type.get("formula", [])

    # ---- R1: spec equivalence (every spec version reached) ---------------
    if spec_nodes:
        res.triggered_rules.append("R1_SPEC_EQUIVALENCE")
        res.mandatory.add(TECHNICAL)
        for s in spec_nodes:
            res.require(
                f"spec_equivalence:{s.id}", TECHNICAL,
                f"规格等同性确认（{s.name}）—— 全检对比/药典符合性", source=s.id,
            )

    # ---- R2: analytical methods need suitability verification ------------
    if method_nodes:
        res.triggered_rules.append("R2_METHOD_SUITABILITY")
        res.mandatory.add(TECHNICAL)
        for m in method_nodes:
            res.require(
                f"method_suitability:{m.id}", TECHNICAL,
                f"分析方法适用性/再验证（{m.name}）", source=m.id,
            )

    # ---- R3: process routes (feasibility; count distinct routes) ---------
    routes = {_route_key(n) for n in formula_nodes + step_nodes}
    if routes:
        res.triggered_rules.append("R3_PROCESS_FEASIBILITY")
        res.mandatory.add(TECHNICAL)
        res.require("process_feasibility", TECHNICAL,
                    f"工艺可行性评估（涉及 {len(routes)} 条工艺路线）")
        if len(routes) >= 2:
            res.triggered_rules.append("R3_MULTI_ROUTE")

    # ---- R4: validation studies (open ones are the sharp edge) -----------
    open_studies = [v for v in study_nodes if str(v.attrs.get("status", "open")) != "closed"]
    if study_nodes:
        res.triggered_rules.append("R4_VALIDATION_IMPACT")
        res.mandatory.add(QUALITY)
        for v in study_nodes:
            res.require(
                f"validation_impact:{v.id}", QUALITY,
                f"验证影响评估/方案修订（{v.name}）"
                + ["", "—— 验证尚未结束"][v in open_studies],
                source=v.id,
            )

    in_validation_batches = [b for b in affected_batches if b["status"] == "in_validation"]
    if in_validation_batches:
        res.triggered_rules.append("R4_OPEN_VALIDATION_BATCH")
        res.mandatory.add(QUALITY)
        res.require(
            "open_batch_disposition", QUALITY,
            f"在验批处置意见（{len(in_validation_batches)} 批尚未结束验证）",
        )

    # ---- R5: market / regulatory filings and product commitments ----------
    if product_nodes:
        res.triggered_rules.append("R5_REGULATORY_FILING")
        res.mandatory.add(REGULATORY)
        res.require("filing_assessment", REGULATORY,
                    "许可/备案变更分类评估（监管申报、许可证件清单）")
        for p in product_nodes:
            res.require(
                f"market_commitment:{p.id}", REGULATORY,
                f"产品市场承诺/注册标准符合性确认（{p.name}）", source=p.id,
            )
        registered = [p for p in product_nodes if p.attrs.get("registered") is not False]
        if registered:
            res.triggered_rules.append("R5_REGISTERED_SPEC")

    # ---- R6: emergency substitution restrictions -------------------------
    if change_attrs.get("change_type") == "emergency_substitution":
        res.triggered_rules.append("R6_EMERGENCY_CONTROLS")
        res.mandatory.add(QUALITY)
        res.mandatory.add(TECHNICAL)
        res.require("deviation_record", QUALITY, "紧急替代偏差/临时变更记录")
        res.require("restricted_batch_list", QUALITY, "限用批次清单（仅限批准批次使用新来源）")
        res.require("expiry_condition", QUALITY, "到期/退出条件（到期后恢复经批准来源）")

    # ---- risk level -------------------------------------------------------
    level = "low"
    if len(routes) >= 2 or len(product_nodes) >= 2 or in_validation_batches:
        level = "high"
    elif method_nodes or study_nodes or product_nodes or change_attrs.get("change_type") == "emergency_substitution":
        level = "medium"
    if change_attrs.get("change_type") == "emergency_substitution":
        level = "high"
    res.risk_level = level
    return res


def path_explains(graph: GraphView, subject: str, max_paths: int = 500):
    """Return (path dicts, affected-by-type, truncated, cycles)."""
    paths, truncated = graph.all_simple_paths(subject, max_paths=max_paths)
    cycles = graph.tarjan_scc()
    out_paths = []
    affected: dict[str, list[str]] = {}
    for edges in paths:
        node_ids = [subject] + [e.dst for e in edges]
        windows = [(e.valid_from, e.valid_until) for e in edges]
        from .graph import intersect_windows
        lo, hi = intersect_windows(windows)
        sources = sorted({e.source for e in edges})
        for nid in node_ids[1:]:
            n = graph.nodes.get(nid)
            if n:
                affected.setdefault(n.node_type, [])
                if nid not in affected[n.node_type]:
                    affected[n.node_type].append(nid)
        out_paths.append({
            "node_ids": node_ids,
            "node_types": [graph.nodes[n].node_type for n in node_ids],
            "node_names": [graph.nodes[n].name for n in node_ids],
            "edge_types": [e.edge_type for e in edges],
            "source": "evidence" if "evidence" in sources else "frozen",
            "valid_windows": [f"[{e.valid_from}, {e.valid_until or '∞'})" for e in edges],
            "boundary": {"valid_from": lo, "valid_until": hi},
            "edge_sources": sources,
        })
    return out_paths, affected, truncated, cycles
