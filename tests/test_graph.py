"""图遍历单元测试：as-of 生效区间、路径枚举、Tarjan 环检测、窗口交集。"""
from datetime import date

from app.graph import Edge, Node, TemporalGraph, effective_window


def g(as_of, nodes, edges):
    return TemporalGraph.build(as_of, nodes, edges)


N = [
    Node("m", "material", "M", "物料", valid_from=date(2026, 1, 1)),
    Node("s", "spec", "S", "规格", valid_from=date(2026, 1, 1)),
    Node("r", "recipe", "R", "配方", valid_from=date(2026, 1, 1)),
    Node("future", "route", "F", "未来路线", valid_from=date(2027, 1, 1)),
]


def test_as_of_filters_future_and_expired_nodes_edges():
    edges = [
        Edge("e1", "m", "s", valid_from=date(2026, 1, 1)),
        Edge("e2", "s", "r", valid_from=date(2026, 1, 1), valid_to=date(2026, 6, 1)),
        Edge("e3", "r", "future", valid_from=date(2027, 1, 1)),
        Edge("e4", "s", "future", valid_from=date(2027, 1, 1)),
    ]
    early = g(date(2026, 3, 1), N, edges)
    assert early.reachable("m") == {"m", "s", "r"}
    # 半开区间：valid_to == as_of 时边已失效
    after = g(date(2026, 6, 1), N, edges)
    assert after.reachable("m") == {"m", "s"}
    # 未来节点不参与图
    late = g(date(2027, 2, 1), N, edges)
    assert "future" in late.reachable("m")


def test_all_simple_paths_covers_three_routes():
    nodes = N + [
        Node("a", "route", "A", "路线1", valid_from=date(2026, 1, 1)),
        Node("b", "route", "B", "路线2", valid_from=date(2026, 1, 1)),
        Node("c", "route", "C", "路线3", valid_from=date(2026, 1, 1)),
    ]
    edges = [
        Edge("e0", "m", "s", valid_from=date(2026, 1, 1)),
        Edge("e1", "s", "a", valid_from=date(2026, 1, 1)),
        Edge("e2", "s", "b", valid_from=date(2026, 1, 1)),
        Edge("e3", "s", "c", valid_from=date(2026, 1, 1)),
    ]
    graph = g(date(2026, 3, 1), nodes, edges)
    paths = graph.all_simple_paths("m")
    ends = sorted(p[-1].dst for p in paths)
    assert ends == ["a", "b", "c"]
    assert all(len(p) == 2 for p in paths)


def test_cycle_detection_with_self_loop_and_scc():
    nodes = [
        Node("a", "step", "A", "a", valid_from=date(2026, 1, 1)),
        Node("b", "step", "B", "b", valid_from=date(2026, 1, 1)),
        Node("c", "step", "C", "c", valid_from=date(2026, 1, 1)),
        Node("x", "step", "X", "x", valid_from=date(2026, 1, 1)),
    ]
    edges = [
        Edge("1", "a", "b", valid_from=date(2026, 1, 1)),
        Edge("2", "b", "c", valid_from=date(2026, 1, 1)),
        Edge("3", "c", "a", valid_from=date(2026, 1, 1)),  # a-b-c 环
        Edge("4", "x", "x", valid_from=date(2026, 1, 1)),  # 自环
    ]
    graph = g(date(2026, 3, 1), nodes, edges)
    cycles = [set(c) for c in graph.cycles()]
    assert {"a", "b", "c"} in cycles
    assert {"x"} in cycles


def test_paths_terminate_on_cyclic_graph():
    nodes = [
        Node("a", "step", "A", "a", valid_from=date(2026, 1, 1)),
        Node("b", "step", "B", "b", valid_from=date(2026, 1, 1)),
    ]
    edges = [
        Edge("1", "a", "b", valid_from=date(2026, 1, 1)),
        Edge("2", "b", "a", valid_from=date(2026, 1, 1)),
    ]
    graph = g(date(2026, 3, 1), nodes, edges)
    paths = graph.all_simple_paths("a")
    # a->b 为唯一简单路径（b->a 回到已访问节点被剪枝）
    assert [[e.dst for e in p] for p in paths] == [["b"]]


def test_effective_window_intersection():
    nodes = {"m": Node("m", "material", "M", "m", valid_from=date(2026, 1, 1)),
             "p": Node("p", "product", "P", "p", valid_from=date(2025, 1, 1))}
    edges = [
        Edge("1", "m", "p", valid_from=date(2026, 3, 1), valid_to=date(2026, 9, 1)),
        Edge("2", "p", "x", valid_from=date(2026, 2, 1), valid_to=date(2026, 12, 1)),
    ]
    nodes["x"] = Node("x", "market", "X", "x", valid_from=date(2026, 1, 1))
    lo, hi = effective_window(edges, nodes)
    assert (lo, hi) == (date(2026, 3, 1), date(2026, 9, 1))
