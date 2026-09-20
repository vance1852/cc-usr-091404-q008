"""带生效区间的依赖图：构造、环检测(Tarjan SCC)、全部受影响路径枚举。

区间语义：半开区间 [valid_from, valid_to)；valid_to 为 NULL 表示至今有效。
边与节点都只在其区间内参与 as-of 时刻的图。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

INF = date(9999, 12, 31)


@dataclass
class Node:
    id: str
    type: str
    code: str
    name: str
    attrs: dict = field(default_factory=dict)
    valid_from: date | None = None
    valid_to: date | None = None


@dataclass
class Edge:
    id: str
    src: str
    dst: str
    label: str = ""
    valid_from: date | None = None
    valid_to: date | None = None


@dataclass
class TemporalGraph:
    as_of: date
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    out: dict[str, list[Edge]] = field(default_factory=dict)

    def _active(self, valid_from: date | None, valid_to: date | None) -> bool:
        if valid_from is not None and valid_from > self.as_of:
            return False
        if valid_to is not None and valid_to <= self.as_of:
            return False
        return True

    @classmethod
    def build(cls, as_of: date, nodes: list[Node], edges: list[Edge]) -> "TemporalGraph":
        g = cls(as_of=as_of, out={})
        for n in nodes:
            if g._active(n.valid_from, n.valid_to):
                g.nodes[n.id] = n
                g.out[n.id] = []
        for e in edges:
            if e.src not in g.nodes or e.dst not in g.nodes:
                continue
            if g._active(e.valid_from, e.valid_to):
                g.edges.append(e)
                g.out[e.src].append(e)
        return g

    # ---- 受影响节点（可达集合，用于快照与遗漏检查） ----
    def reachable(self, root: str) -> set[str]:
        seen: set[str] = set()
        stack = [root]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(e.dst for e in self.out.get(cur, ()))
        return seen

    # ---- 全部简单路径（DFS；环存在时不会重复访问节点，保证终止） ----
    def all_simple_paths(self, root: str) -> list[list[Edge]]:
        paths: list[list[Edge]] = []
        if root not in self.nodes:
            return paths

        def dfs(cur: str, chain: list[Edge], on_path: set[str]) -> None:
            nexts = self.out.get(cur, ())
            moves = [e for e in nexts if e.dst not in on_path]
            if not moves:
                if chain:
                    paths.append(list(chain))
                return
            for e in moves:
                chain.append(e)
                on_path.add(e.dst)
                dfs(e.dst, chain, on_path)
                on_path.remove(e.dst)
                chain.pop()

        dfs(root, [], {root})
        return paths

    # ---- 环检测：Tarjan 强连通分量，size>1 或自环即为环 ----
    def cycles(self, root: str | None = None) -> list[list[str]]:
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        counter = [0]
        sccs: list[list[str]] = []

        ids = sorted(self.nodes) if root is None else self._dfs_order(root)

        def strong(v: str) -> None:
            index[v] = low[v] = counter[0]
            counter[0] += 1
            stack.append(v)
            on_stack.add(v)
            for e in self.out.get(v, ()):
                w = e.dst
                if w not in index:
                    strong(w)
                    low[v] = min(low[v], low[w])
                elif w in on_stack:
                    low[v] = min(low[v], index[w])
            if low[v] == index[v]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.remove(w)
                    comp.append(w)
                    if w == v:
                        break
                sccs.append(comp)

        for v in ids:
            if v not in index:
                strong(v)

        cycles: list[list[str]] = []
        for comp in sccs:
            if len(comp) > 1:
                cycles.append(sorted(comp))
            else:
                v = comp[0]
                if any(e.dst == v for e in self.out.get(v, ())):
                    cycles.append([v])
        return cycles

    def _dfs_order(self, root: str) -> list[str]:
        order: list[str] = []
        seen = {root}
        stack = [root]
        while stack:
            cur = stack.pop()
            order.append(cur)
            for e in self.out.get(cur, ()):
                if e.dst not in seen:
                    seen.add(e.dst)
                    stack.append(e.dst)
        return order


def effective_window(edges: list[Edge], nodes: dict[str, Node]) -> tuple[date | None, date | None]:
    """路径（边+途经节点）生效区间的交集：所有关系同时有效的窗口。"""
    lo: date | None = None
    hi: date | None = None
    intervals: list[tuple[date | None, date | None]] = [(e.valid_from, e.valid_to) for e in edges]
    node_ids: list[str] = []
    if edges:
        node_ids.append(edges[0].src)
        node_ids.extend(e.dst for e in edges)
    for nid in node_ids:
        n = nodes.get(nid)
        if n is not None:
            intervals.append((n.valid_from, n.valid_to))
    for vf, vt in intervals:
        if vf is not None and (lo is None or vf > lo):
            lo = vf
        if vt is not None and (hi is None or vt < hi):
            hi = vt
    return lo, hi
