"""Time-aware dependency graph.

A :class:`GraphView` is a set of nodes/edges that were effective at a given
instant ``t`` (ISO-8601 strings compared lexicographically, which is valid for
normalized ISO-8601).  It can be built from either the live ``nodes`` /
``edges`` tables or from a frozen snapshot, optionally augmented with
evidence-added edges ("新证据扩展范围").

Implemented from scratch (no third-party graph library):

* Tarjan's strongly-connected-components algorithm for cycle detection;
* enumeration of *all* simple downstream paths via iterative DFS;
* effective boundary of a path = intersection of the half-open validity
  windows of the edges that compose it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable


def effective(window: tuple[str, str | None], t: str) -> bool:
    start, end = window
    return start <= t and (end is None or t < end)


def intersect_windows(windows: Iterable[tuple[str, str | None]]) -> tuple[str, str | None]:
    """Intersection of half-open ``[from, until)`` windows.

    ``None`` upper bound means +infinity.  An empty intersection yields
    ``("", "")`` so callers can surface a broken boundary instead of silently
    dropping the path.
    """
    lo = ""
    hi: str | None = None
    seen = False
    for start, end in windows:
        seen = True
        if start > lo:
            lo = start
        if hi is None or (end is not None and end < hi):
            hi = end
        if hi is not None and lo >= hi:
            return ("", "")
    if not seen:
        return ("", None)
    return (lo, hi)


@dataclass
class Node:
    id: str
    node_type: str
    name: str
    attrs: dict
    valid_from: str
    valid_until: str | None


@dataclass
class Edge:
    src: str
    dst: str
    edge_type: str
    attrs: dict
    valid_from: str
    valid_until: str | None
    source: str = "frozen"          # frozen | evidence
    seq: int | None = None


def _row_get(r, *names):
    d = dict(r)
    for n in names:
        if n in d:
            return d[n]
    raise KeyError(names)


@dataclass
class GraphView:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    at: str = ""

    def __post_init__(self) -> None:
        self._rebuild_adj()

    def _rebuild_adj(self) -> None:
        adj: dict[str, list[Edge]] = {nid: [] for nid in self.nodes}
        for e in self.edges:
            adj.setdefault(e.src, []).append(e)
        self._adj = adj

    # ------------------------------------------------------------------ build

    @classmethod
    def from_rows(cls, node_rows: Iterable, edge_rows: Iterable, at: str) -> "GraphView":
        """Build the view of every row effective at instant ``at``."""
        nodes: dict[str, Node] = {}
        for r in node_rows:
            nodes[_row_get(r, "node_id", "id")] = Node(
                id=_row_get(r, "node_id", "id"),
                node_type=r["node_type"],
                name=r["name"],
                attrs=json.loads(r["attrs"]),
                valid_from=r["valid_from"],
                valid_until=r["valid_until"],
            )
        edges: list[Edge] = []
        for r in edge_rows:
            e = Edge(
                src=r["src"], dst=r["dst"], edge_type=r["edge_type"],
                attrs=json.loads(r["attrs"]),
                valid_from=r["valid_from"], valid_until=r["valid_until"],
                source=dict(r).get("source", "frozen"),
            )
            if e.src in nodes and e.dst in nodes:
                edges.append(e)
        return cls(nodes=nodes, edges=edges, at=at)

    def add_evidence_edge(self, e: Edge) -> None:
        """Append a scope-extending edge (new evidence after freeze).

        Such edges are tagged ``source='evidence'`` so the path explanation can
        distinguish the frozen basis from later additions.
        """
        for end in (e.src, e.dst):
            if end not in self.nodes:
                raise KeyError(f"evidence edge references unknown node {end!r}")
        self.edges.append(e)
        self._adj.setdefault(e.src, []).append(e)

    # ---------------------------------------------------------------- queries

    def downstream(self, root: str) -> dict[str, list[Edge]]:
        """Reachable nodes mapped to the edges on one path reaching them."""
        if root not in self.nodes:
            return {}
        reached: dict[str, list[Edge]] = {root: []}
        stack = [root]
        while stack:
            cur = stack.pop()
            for e in self._adj.get(cur, []):
                if e.dst not in reached:
                    reached[e.dst] = reached[cur] + [e]
                    stack.append(e.dst)
        return reached

    def all_simple_paths(self, root: str, max_paths: int = 1000) -> tuple[list[list[Edge]], bool]:
        """All simple (vertex-simple) downstream paths starting at ``root``.

        Returns ``(paths, truncated)``.  Every prefix is recorded, so callers
        receive paths ending at each depth, not only leaves.  Iterative DFS
        avoids recursion limits on long manufacturing routes.
        """
        if root not in self.nodes:
            return [], False
        paths: list[list[Edge]] = []
        path_edges: list[Edge] = []
        on_path = {root}
        # Each frame is (node, index of next outgoing edge to try).
        stack: list[tuple[str, int]] = [(root, 0)]
        while stack:
            cur, idx = stack[-1]
            out = self._adj.get(cur, [])
            if idx >= len(out):
                stack.pop()
                if stack:  # not the root frame: detach the edge that led here
                    e = path_edges.pop()
                    on_path.discard(e.dst)
                continue
            e = out[idx]
            stack[-1] = (cur, idx + 1)
            if e.dst in on_path:
                continue  # would close a cycle; cyclic walks are not simple paths
            path_edges.append(e)
            on_path.add(e.dst)
            paths.append(list(path_edges))
            if len(paths) >= max_paths:
                return paths, True
            stack.append((e.dst, 0))
        return paths, False

    # --------------------------------------------------------------- cycles

    def tarjan_scc(self) -> list[list[str]]:
        """Strongly connected components of size > 1, plus self-loops."""
        indices: dict[str, int] = {}
        lowlink: dict[str, int] = {}
        on_stack: set[str] = set()
        stack: list[str] = []
        counter = 0
        cycles: list[list[str]] = []

        def run(start: str) -> None:
            nonlocal counter
            work: list[tuple[str, int]] = [(start, 0)]
            while work:
                v, pi = work[-1]
                if pi == 0:
                    indices[v] = lowlink[v] = counter
                    counter += 1
                    stack.append(v)
                    on_stack.add(v)
                neighbours = [e.dst for e in self._adj.get(v, [])]
                if pi < len(neighbours):
                    w = neighbours[pi]
                    work[-1] = (v, pi + 1)
                    if w not in indices:
                        work.append((w, 0))
                    elif w in on_stack:
                        lowlink[v] = min(lowlink[v], indices[w])
                else:
                    if lowlink[v] == indices[v]:
                        comp: list[str] = []
                        while True:
                            w = stack.pop()
                            on_stack.discard(w)
                            comp.append(w)
                            if w == v:
                                break
                        if len(comp) > 1 or any(e.dst == v for e in self._adj.get(v, [])):
                            cycles.append(sorted(comp))
                    work.pop()
                    if work:
                        lowlink[work[-1][0]] = min(lowlink[work[-1][0]], lowlink[v])

        for n in self.nodes:
            if n not in indices:
                run(n)
        return cycles
