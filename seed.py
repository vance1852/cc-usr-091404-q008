"""培养基关键组分供应商停产场景的种子数据。

运行：.venv/bin/python seed.py
时间线：2026-09-20 发起变更时仅两条工艺路线/国内市场在效；
2026-10-01 起第三条路线与欧盟市场生效，可用于演示范围扩展。
"""
from __future__ import annotations

from datetime import date

from app import database as db
from app import engine

D = date  # 2026


def node(id, type, code, name, vf, vt=None, **attrs):
    return {"id": id, "type": type, "code": code, "name": name,
            "attrs": attrs, "valid_from": vf, "valid_to": vt}


def edge(src, dst, label, vf, vt=None):
    return {"from_node": src, "to_node": dst, "label": label,
            "valid_from": vf, "valid_to": vt}


NODES = [
    # 供应商物料：旧来源停产，新来源 9 月到货
    node("mat_old", "material", "MAT-MEDIA-OLD", "酵母提取物（旧厂 A，停产）",
         D(2020, 1, 1), D(2026, 10, 1), supplier="厂A", status="discontinued"),
    node("mat_new", "material", "MAT-MEDIA-NEW", "酵母提取物（新厂 B，替代来源）",
         D(2026, 9, 1), supplier="厂B", coa="equivalent-on-paper"),
    # 规格版本
    node("spec_v1", "spec", "SPEC-MEDIA-v1", "培养基原料规格 v1",
         D(2020, 1, 1), D(2026, 8, 31), version="v1"),
    node("spec_v2", "spec", "SPEC-MEDIA-v2", "培养基原料规格 v2（新来源）",
         D(2026, 9, 1), version="v2"),
    # 三个配方 -> 三条工艺路线
    node("recipe_a", "recipe", "REC-A", "小容量注射剂培养基配方", D(2021, 3, 1)),
    node("recipe_b", "recipe", "REC-B", "冻干粉针培养基配方", D(2021, 3, 1)),
    node("recipe_c", "recipe", "REC-C", "滴眼液培养基配方", D(2022, 6, 1)),
    node("route_1", "route", "RT-1", "配制-灌装工艺路线（一）", D(2021, 3, 1)),
    node("route_2", "route", "RT-2", "冻干工艺路线（二）", D(2021, 3, 1)),
    node("route_3", "route", "RT-3", "无菌灌装工艺路线（三，10月投产）", D(2026, 10, 1)),
    node("step_a", "step", "ST-A1", "培养基配制步骤", D(2021, 3, 1)),
    node("step_b", "step", "ST-B1", "冻干前灌装步骤", D(2021, 3, 1)),
    # 分析方法
    node("method_ph", "method", "M-PH", "pH/渗透压检测方法", D(2020, 6, 1)),
    node("method_assay", "method", "M-ASSAY", "生长促进试验方法", D(2020, 6, 1)),
    node("method_sterility", "method", "M-STER", "无菌检查方法", D(2020, 6, 1)),
    # 尚未结束的验证研究，挂着两个验证批
    node("val_001", "validation", "VAL-PV-26-09", "培养基模拟灌装工艺验证（进行中）",
         D(2026, 7, 1), status="open", protocol="PV-26-09",
         open_batches=["B2601", "B2602"]),
    # 一个在产批次
    node("batch_b2603", "batch", "B2603", "生产批 B2603（未结批）",
         D(2026, 9, 10), status="open"),
    # 许可文件
    node("lic_cde", "license", "LIC-CDE-DM", "药品生产许可/原辅包登记备案",
         D(2020, 1, 1), filing_type="CBE-30"),
    # 产品与市场承诺
    node("prod_x", "product", "PROD-X", "注射剂产品 X", D(2021, 3, 1)),
    node("prod_y", "product", "PROD-Y", "冻干粉针产品 Y", D(2021, 3, 1)),
    node("mkt_cn", "market", "MKT-CN", "中国市场供货承诺", D(2021, 3, 1)),
    node("mkt_eu", "market", "MKT-EU", "欧盟市场供货承诺（10月生效）", D(2026, 10, 1)),
]

EDGES = [
    edge("mat_old", "spec_v1", "按旧规格采购", D(2020, 1, 1), D(2026, 8, 31)),
    edge("mat_new", "spec_v2", "按新规格采购", D(2026, 9, 1)),
    edge("spec_v2", "recipe_a", "原料进入配方", D(2026, 9, 1)),
    edge("spec_v2", "recipe_b", "原料进入配方", D(2026, 9, 1)),
    edge("spec_v2", "recipe_c", "原料进入配方", D(2026, 10, 1)),
    edge("recipe_a", "route_1", "按路线生产", D(2021, 3, 1)),
    edge("recipe_b", "route_2", "按路线生产", D(2021, 3, 1)),
    edge("recipe_c", "route_3", "按路线生产", D(2022, 6, 1)),
    edge("route_1", "step_a", "包含步骤", D(2021, 3, 1)),
    edge("route_2", "step_b", "包含步骤", D(2021, 3, 1)),
    edge("step_a", "method_ph", "执行方法", D(2021, 3, 1)),
    edge("step_a", "method_assay", "执行方法", D(2021, 3, 1)),
    edge("step_b", "method_sterility", "执行方法", D(2021, 3, 1)),
    edge("route_1", "val_001", "在验研究覆盖该路线", D(2026, 7, 1)),
    edge("route_2", "batch_b2603", "在产批次走该路线", D(2026, 9, 10)),
    edge("recipe_a", "prod_x", "配方用于产品", D(2021, 3, 1)),
    edge("recipe_b", "prod_y", "配方用于产品", D(2021, 3, 1)),
    edge("prod_x", "lic_cde", "许可登记原料来源", D(2021, 3, 1)),
    edge("prod_x", "mkt_cn", "供货承诺", D(2021, 3, 1)),
    edge("prod_y", "mkt_cn", "供货承诺", D(2021, 3, 1)),
    edge("prod_x", "mkt_eu", "出口承诺", D(2026, 10, 1)),
]


def seed(conn) -> None:
    for n in NODES:
        engine.upsert_node(conn, n)
    for e in EDGES:
        engine.add_edge(conn, e)
    conn.commit()


if __name__ == "__main__":
    conn = db.connect()
    db.init_db(conn)
    seed(conn)
    print(f"seeded {len(NODES)} nodes, {len(EDGES)} edges into {db.get_db_path()}")
    conn.close()
