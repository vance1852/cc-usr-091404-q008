# 原辅料变更影响评估服务

供应商质量工程师（SQE）接到培养基关键组分停产通知后，需要回答：标称等同的新来源会
冲击哪些**许可证件、分析方法、验证研究与产品市场承诺**？哪些在验批仍被禁止使用新来源？

本服务以**带生效区间的依赖图**描述供应商物料 → 规格版本 → 配方 → 工艺步骤 → 分析方法 /
验证研究 / 产品市场，提供：

- 发起变更时**冻结不可变影响快照**；之后解绑关系只关闭生效区间，不删除行、不抹评估依据；
- 新证据可**追加扩展范围**（标记为 `evidence`，与冻结依据区分），但不能删除原需求；
- 依赖环检测（迭代 Tarjan SCC）、全部受影响简单路径枚举（迭代 DFS）及路径生效边界
  （半开区间交集 `[valid_from, valid_until)`）；
- 风险规则分派 **技术 / 法规 / 质量** 强制评审；仅当强制评审完成、矛盾意见有裁决、
  材料齐全、图中无环时方可批准切换；
- 紧急替代强制登记**限用批次清单**与**到期/退出条件**，到期后自动恢复禁用；
- 一次查询返回：路径解释、遗漏材料、意见版本、生效边界、仍被禁止使用新来源的批次。

## 技术栈

FastAPI + SQLite（标准库 `sqlite3`，无第三方图数据库/图库）+ Pydantic v2，图遍历为
从零实现（`app/graph.py`）。

## 运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python -m app.init_db            # 写入演示场景到 change.db
uvicorn app.main:app --reload    # http://127.0.0.1:8000  (Swagger: /docs)
```

## 数据模型

| 节点类型 | 含义 |
|---|---|
| `supplier_material` | 供应商物料（本次变更对象） |
| `spec_version` | 规格版本（注册标准、药典版本） |
| `formula` / `process_step` | 配方与工艺步骤（`attrs.route_id` 标识工艺路线） |
| `analytical_method` | 分析方法（共用方法会被多条路线引用） |
| `validation_study` | 验证研究（`attrs.status=open` 表示尚未结束） |
| `product_market` | 产品市场（注册承诺、销售市场） |

节点与边均带 `[valid_from, valid_until)` 半开区间。边类型如 `governed_by_spec`、
`used_in_formula`、`has_step`、`measured_by`、`validated_by`、`yields_product`。

## 关键接口

| 方法 & 路径 | 说明 |
|---|---|
| `POST /nodes` / `POST /edges` | 登记带生效区间的节点/依赖边 |
| `POST /edges/{id}/sever?valid_until=` | 解绑：仅关闭区间，历史保留 |
| `GET /graph/cycles?at=` | 指定时点的 live 图依赖环检测 |
| `POST /changes` | 发起变更：冻结快照、跑风险规则、分派评审，返回完整评估 |
| `GET /changes/{id}` | 查询：路径解释、遗漏材料、评审版本、生效边界、禁用批次 |
| `POST /changes/{id}/evidence` | 追加证据：`added_edge`/`added_node`（扩展范围）、`document`（补齐材料）、`note` |
| `POST /changes/{id}/reviews` | 提交评审意见（同 discipline 再次提交生成新版本） |
| `POST /changes/{id}/resolve-conflict` | 记录矛盾裁决（`upheld_change` / `rejected_change`） |
| `POST /changes/{id}/approve` | 门控批准；不满足条件时返回 `block_reasons` |

### 批准门控（全部满足才可切换）

1. 每个强制 discipline 最新一版意见为 `approved`/`conditional`（无 `objected`）；
2. 存在反对意见时必须有裁决记录，且裁决为 `upheld_change`；
3. 风险规则推导出的必需材料（规格等同性、方法适用性、验证影响、申报/市场承诺确认、
   在验批处置等）全部由 `document` 证据关闭；
4. 评估视图中无依赖环；
5. 紧急替代另需限用批次清单 + 到期条件（二者既是变更登记字段，也要求 QA 偏差体系文件）。

### 批次禁用判定（`snapshot.blocked_batches[*]`）

- 批准前：在验批（`in_validation`）、已放行批（`released`）、隔离批均禁用新来源；
  已完成批与计划沿用原来源的批不参与；
- 常规切换批准后：相关候选批放开；
- 紧急替代批准后：**仅限用清单内批次**可用；清单外禁用；到期条件过后，连清单内批次
  也重新禁用（`reasons` 给出人类可读原因）。

## 演示场景（`app/seed.py`）

停产的培养基关键组分「大豆蛋白胨」经规格 v3 进入 A/B/C 三条工艺路线，共用一个
无菌检查方法，A/B 两个验证研究尚未结束，冲击两个注册产品（X 出口 CN/EU、Y 内销）；
另有氯化钠 → 产品 Z 的无关子图用于验证影响范围不扩散。

```bash
pytest -q          # 26 个测试：图遍历、时态过滤、快照不可变、证据扩展、
                   # 评审版本/矛盾裁决、紧急限用与到期、批次禁用
```

## 模块

```
app/db.py       SQLite schema（双时态行 + 冻结快照表 + 追加证据/意见版本）
app/graph.py    GraphView：时态视图、Tarjan SCC、全部简单路径、区间交集
app/rules.py    R1–R6 风险规则、材料目录、路径解释组装
app/service.py  变更编排、快照冻结、证据扩展、评审门控、批次判定
app/main.py     FastAPI 路由
app/seed.py     演示数据
tests/          服务层与 HTTP API 测试
```
