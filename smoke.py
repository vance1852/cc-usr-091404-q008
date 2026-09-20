"""端到端冒烟脚本：演示完整业务闭环。使用后可删。"""
import json
import urllib.request

B = "http://localhost:8765"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(B + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


# 1. 发起变更（9-20 快照）
cid = call("POST", "/changes", {
    "title": "酵母提取物换厂B-冒烟", "material_node": "mat_new",
    "change_type": "normal", "as_of": "2026-09-20"})["change_id"]
v = call("GET", f"/changes/{cid}?on=2026-09-21")
r1 = v["impact_runs"][0]
print("1) 发起：路径", len(r1["paths"]), "条；强制评审",
      sorted(a["role"] for a in v["review_assignments"]), "；风险", v["risk_level"])
print("   封锁批次", sorted(b["batch_node"] for b in v["blocked_batches"]))
print("   示例路径:", " -> ".join(s.split("(")[0].split(":")[1] for s in r1["paths"][3]["explanation"]))

# 2. 10-05 新证据扩展：第三条路线/欧盟市场进入范围
v = call("POST", f"/changes/{cid}/expand", {"as_of": "2026-10-05", "note": "欧盟注册推进"})
r2 = v["impact_runs"][1]
r2_nodes = {n["node_id"] for p in r2["paths"] for n in p["nodes"]}
print("2) 扩展：新增 route_3/mkt_eu =", {"route_3", "mkt_eu"} <= r2_nodes,
      "；欧盟材料缺口出现 =",
      any(m["requirement_key"] == "market_commitment:mkt_eu" for m in v["missing_materials"]))

# 3. 提交全部材料 + 评审（法规先反对后改批准，并解决矛盾）
for m in call("GET", f"/changes/{cid}")["all_materials"]:
    call("POST", f"/changes/{cid}/evidence",
         {"requirement_key": m["requirement_key"], "title": m["title"]})
obj = call("POST", f"/changes/{cid}/opinions",
           {"role": "regulatory", "decision": "objected", "comment": "申报路径未明"})
for role in ("technical", "quality"):
    call("POST", f"/changes/{cid}/opinions", {"role": role, "decision": "approved"})
try:
    call("POST", f"/changes/{cid}/approve", {"effective_from": "2026-10-10"})
    print("3) 错误：不应批准")
except urllib.error.HTTPError as e:
    print("3) 有反对意见时拒绝批准 ✓（", json.load(e)["detail"][:40], "）")
call("POST", f"/changes/{cid}/opinions",
     {"role": "regulatory", "decision": "approved", "comment": "CBE-30 确认"})
try:
    call("POST", f"/changes/{cid}/approve", {"effective_from": "2026-10-10"})
except urllib.error.HTTPError as e:
    print("   矛盾未解决时仍拒绝 ✓（", json.load(e)["detail"][:40], "）")
call("POST", f"/changes/{cid}/resolutions",
     {"opinion_id": obj["opinion_id"], "resolution": "CBE-30 备案 30 日", "reviewer": "RA-head"})
v = call("POST", f"/changes/{cid}/approve", {"effective_from": "2026-10-10"})
print("   全部满足后状态 =", v["status"], "生效窗口 =", v["approved_window"])

# 4. 批准后批次仍封锁，逐批放行
v = call("GET", f"/changes/{cid}?on=2026-10-11")
print("4) 批准后仍封锁", sorted(b["batch_node"] for b in v["blocked_batches"]))
call("POST", f"/changes/{cid}/batches/B2601/release", {"reason": "桥检合格", "reviewer": "QA"})
v = call("GET", f"/changes/{cid}?on=2026-10-11")
print("   放行 B2601 后剩余", sorted(b["batch_node"] for b in v["blocked_batches"]))

# 5. 历史快照未被后来变化抹去
v = call("GET", f"/changes/{cid}?on=2026-10-11")
seq1 = {n["node_id"] for p in v["impact_runs"][0]["paths"] for n in p["nodes"]}
print("5) 初始快照仍含全部 9 月范围（lic_cde/val_001）=",
      {"lic_cde", "val_001", "prod_y"} <= seq1)
print("   意见版本数 =", {o["role"]: o["version"] for o in v["opinions"]
                        if o["role"] == "regulatory"})
print("SMOKE OK")
