# 原辅料变更影响评估服务

培养基（或任何关键原辅料）供应商停产、切换新来源时，质量工程师面对的核心问题是：
**这一个物料节点的变更，会沿着哪些仍在生效的依赖关系，波及哪些配方、工艺路线、
分析方法、在验研究、许可文件、产品承诺与市场？哪些评审与证据齐备前，哪些批次不能使用新来源？**

本服务把供应商物料、规格版本、生产配方、工艺路线/步骤、分析方法、验证研究、
生产批次、许可文件、产品与市场建成一张**带生效区间的依赖图**，并在变更发起时
**冻结影响快照**，之后允许新证据扩展范围，但永不因关系后来解绑而抹去原评估依据。

## 模型与语义

- **节点类型**：`material / spec / recipe / route / step / method / validation / batch / license / product / market`
- **边**：`from → to` 表示 to 依赖 from（变更沿边正向传播），如
  `material→spec→recipe→route→{step→method, validation, batch}→product→{license, market}`
- **生效区间**：节点与边均带半开区间 `[valid_from, valid_to)`（`valid_to` 为空表示至今）。
  as-of 查询只看到该时刻有效的图：未来 10 月投产的第三条路线在 9 月的评估中不可见。
- **影响快照不可变**：每次评估（`impact_runs`，seq=1 初始，其后为 expansion）把当时
  可达节点、边、全部路径与环检测结果复制进 `snapshot_*` 表。之后即便边被
  `POST /edges/{id}/retire` 解绑（valid_to 截断到过去），初始快照与由其派生的
  材料要求、评审意见都原样保留。
- **扩展只增不减**：必备材料清单取历次快照范围的并集；扩展可追加强制评审角色。

## 风险规则与批准门

- 影响技术节点（spec/recipe/route/step/method/validation）→ 强制**技术评审**
- 影响法规节点（license/product/market）→ 强制**法规评审**
- 任何变更 → 强制**质量评审**
- 风险等级：存在未结束验证研究或紧急替代 → high；触及 ≥2 条路线或验证 → medium；否则 low
- 每类受影响节点派生**必备材料**（供应商审计、对比 CoA、规格等同性、方法桥接验证、
  在验研究评估、未结批处置、许可申报、产品/市场承诺），未交齐即为遗漏。
- 意见是**只追加的版本链**：同一角色可提交多版（反对→改判批准）。任何历史
  objected 版本都必须有显式 resolution；批准要求：
  1. 每个强制角色最新意见均为 approved；
  2. 所有历史反对意见的矛盾已解决；
  3. 最新范围中无可达依赖环（Tarjan SCC 检测）；
  4. 必备材料全部提交。
- **紧急替代**额外要求记录限用批次清单、到期日与使用条件；到期后或不在清单内的
  批次重新被封锁。
- 常规切换批准后，未结批次仍封锁，须逐批 `release` 放行。

查询 `GET /changes/{id}?on=YYYY-MM-DD` 一次返回：路径解释、每条路径的生效边界
（路径上节点/边区间的交集及该日是否有效）、遗漏材料、意见版本与解决记录、
评审门状态，以及该日**仍被禁止使用新来源的批次及原因**。

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python seed.py                      # 培养基组分场景（22 节点/21 边）
CHANGE_DB=$PWD/changeimpact.db .venv/bin/uvicorn app.main:app --reload
# 交互文档 http://localhost:8000/docs
.venv/bin/python smoke.py                     # 端到端业务闭环演示（需先起服务）
.venv/bin/python -m pytest -q                 # 20 个测试
```

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/nodes` `/edges` | 维护主数据（带生效区间） |
| POST | `/edges/{edge_id}/retire?valid_to=` | 解绑依赖（历史快照不受影响） |
| GET | `/graph/cycles?as_of=` | as-of 全图依赖环检查 |
| POST | `/changes` | 发起变更并冻结初始影响快照 |
| POST | `/changes/{id}/expand` | 新证据到达，追加扩展评估（只增范围） |
| GET | `/changes/{id}?on=` | 完整影响视图（路径/材料/意见/边界/封锁批次） |
| POST | `/changes/{id}/opinions` | 提交评审意见（自动版本递增） |
| POST | `/changes/{id}/resolutions` | 记录反对意见的矛盾解决 |
| POST | `/changes/{id}/evidence` | 提交必备材料 |
| POST | `/changes/{id}/emergency-restriction` | 紧急替代：限用批次/到期/条件 |
| POST | `/changes/{id}/batches/{batch}/release` | 批准后逐批放行 |
| POST | `/changes/{id}/approve` | 批准门校验通过后切换，可给生效窗口 |

## 代码结构

```
app/database.py  SQLite 表结构（时态节点/边、不可变快照、意见版本、放行记录）
app/graph.py     as-of 时态图、DFS 全部简单路径、Tarjan SCC 环检测、路径生效窗口
app/engine.py    风险规则、快照冻结、范围扩展、评审门、紧急替代、批次封锁
app/models.py    Pydantic 模型
app/main.py      FastAPI 路由
seed.py          培养基三路线 + 在验批次 + 许可/多市场场景
tests/           图算法、引擎规则、HTTP 端到端共 20 个测试
```

## 种子场景时间线

- 2026-09-20：旧厂酵母提取物停产、新厂 B 规格"看似等同"；路线一/二、国内市场在效，
  验证 VAL-PV-26-09（挂验证批 B2601/B2602）与生产批 B2603 未结束。
- 2026-10-01：路线三（滴眼液）与欧盟市场生效——9 月发起时不可见，10 月
  `expand` 后才进入影响范围与法规评审视野。
