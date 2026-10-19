# -*- coding: utf-8 -*-
"""PMC 第二阶段端到端自动验证（FastAPI TestClient，不依赖外部服务，全新临时库）。
覆盖：MVP/第一阶段回归 + 逐级领料 + 按批次冻结 + 数据库序列单号 + 多公司/多仓库/多币种
+ 金额分级审批 + 财务对账（三方核对/应付/生产成本）+ RBAC 权限 + 报表/Excel/审计日志。
用户通过请求头 X-User 传递（演示用身份，非正式登录）。
"""
import os, sys, io, re, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import app as app_module
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()
app_module.DB_PATH = tmp.name
app_module.init_db()

from fastapi.testclient import TestClient
client = TestClient(app_module.app)
PASSED = [0]

def check(cond, msg):
    print(("✅ " if cond else "❌ ") + msg)
    assert cond, msg
    PASSED[0] += 1

def H(user):
    return {"X-User": user}

def get(path, user="admin", **kw):
    return client.get(path, headers=H(user), **kw)

def post(path, user="admin", **kw):
    return client.post(path, headers=H(user), **kw)

def put(path, user="admin", **kw):
    return client.put(path, headers=H(user), **kw)

# ===== 0. 种子：公司/仓库/币种/汇率/用户/角色/审批规则 =====
r = get("/api/parts")
check(r.status_code == 200 and len(r.json()) == 8, f"零件 8 条，实际 {len(r.json()) if r.status_code==200 else r.text}")
r = get("/api/companies")
check(len(r.json()) == 2 and {c["code"] for c in r.json()} == {"COMP-001", "COMP-002"}, f"公司 2 家：{r.json()}")
r = get("/api/warehouses")
check(len(r.json()) == 4, f"仓库 4 个：{[w['code'] for w in r.json()]}")
r = get("/api/currencies", user="finance")
check(len(r.json()) == 4, f"币种 4 个：{r.json()}")
r = get("/api/exchange-rates", user="finance")
check(any(x["from_currency"] == "USD" and x["to_currency"] == "CNY" and x["rate"] == 7.2 for x in r.json()), f"USD→CNY 汇率 7.2：{r.json()}")
r = get("/api/users")
check(len(r.json()) == 8, f"用户 8 个：{[u['username'] for u in r.json()]}")
r = get("/api/roles")
check(len(r.json()) == 8, f"角色 8 个：{r.json()}")
r = get("/api/approvals/rules", params={"doc_type": "采购申请"})
check(len(r.json()) == 3, f"采购申请默认审批规则 3 条（经理/经理+总监）：{r.json()}")

# ===== 1. MVP 回归：SO-001 100 个小夜灯 =====
r = post("/api/sales", json={"order_no": "SO-001", "customer": "演示客户", "product_code": "FG-001", "qty": 100, "due_date": "2026-12-31"})
check(r.status_code == 200, f"创建销售单 SO-001：{r.text}")
r = post("/api/mrp", json={"sales_order_no": "SO-001"})
mrp = {x["part_code"]: x for x in r.json()["results"]}
expected = {"RM-001": 100, "RM-002": 90, "RM-003": 110, "RM-004": 70, "RM-005": 105}
for code, exp in expected.items():
    check(abs(mrp[code]["net"] - exp) < 1e-6, f"MRP {code} 净需求 {mrp[code]['net']} == {exp}")
r = post("/api/purchase/from-mrp", json={"sales_order_no": "SO-001"})
check(r.status_code == 200, f"从 MRP 生成采购单：{r.text}")
po_no = r.json()["order_no"]
check(re.match(r"^PO-\d{4}-\d{4}$", po_no) is not None, f"采购单号为数据库序列格式 PO-YYYY-NNNN：{po_no}")
check(abs(r.json()["total_amount_with_tax"] - 1380.86) < 0.01, f"采购单含税总额 1380.86，实际 {r.json()['total_amount_with_tax']}")
for code, qty in expected.items():
    r = post("/api/purchase/receive", json={"order_no": po_no, "part_code": code, "qty": qty})
    check(r.status_code == 200, f"到货 {code} {qty}：{r.text}")
r = get("/api/purchase")
po = [x for x in r.json() if x["order_no"] == po_no][0]
check(po["status"] == "全部到货", f"采购单全部到货：{po['status']}")
r = put(f"/api/purchase/{po_no}/reconcile", user="finance", json={"invoice_no": "INV-001", "invoice_amount": 1380.86, "reconciled": True})
check(r.status_code == 200 and r.json()["reconciled"] == 1, f"采购对账（财务角色）：{r.text}")
r = post("/api/production", user="worker", json={"order_no": "MO-001", "product_code": "FG-001", "qty": 100, "sales_order_no": "SO-001", "planned_start": "2026-11-01", "planned_end": "2026-11-10"})
check(r.status_code == 200, f"创建生产单 MO-001：{r.text}")
r = post("/api/production/consume", user="worker", json={"order_no": "MO-001"})
check(r.status_code == 200, f"领料 MO-001（单层 BOM，直接子件=原材料）：{r.text}")
r = get("/api/stock")
stock = {x["part_code"]: x["qty"] for x in r.json()}
for code in expected:
    check(abs(stock[code] - 20) < 1e-6, f"领料后 {code} 库存 {stock[code]} == 20")
r = post("/api/production/complete", user="worker", json={"order_no": "MO-001"})
check(r.status_code == 200, f"完工 MO-001：{r.text}")
r = get("/api/stock")
check(abs({x["part_code"]: x["qty"] for x in r.json()}.get("FG-001", 0) - 100) < 1e-6, "成品库存 FG-001 == 100")

# ===== 2. 逐级领料（核心修复）：成品先耗半成品库存，半成品不足先生产半成品，不直接扣底层原材料 =====
r = post("/api/sales", json={"order_no": "SO-002", "customer": "客户B", "product_code": "FG-002", "qty": 10, "due_date": "2026-12-20"})
check(r.status_code == 200, f"创建销售单 SO-002（FG-002）：{r.text}")
r = post("/api/sales/SO-002/create-production", json={"order_no": "MO-002", "planned_start": "2026-11-05", "planned_end": "2026-11-15"})
check(r.status_code == 200 and r.json()["order_no"] == "MO-002", f"销售单触发生产单 MO-002：{r.text}")
def onhand(code):
    rows = [x for x in get("/api/stock").json() if x["part_code"] == code]
    return sum(x["qty"] for x in rows)
rm005_before, rm002_before = onhand("RM-005"), onhand("RM-002")
check(rm005_before == 20 and rm002_before == 20, f"领料前 RM-005/RM-002 各 20：{rm005_before}/{rm002_before}")
r = post("/api/production/consume", user="worker", json={"order_no": "MO-002"})
check(r.status_code == 400 and "半成品" in r.text and "ensure-subassemblies" in r.text,
      f"半成品不足时领料应失败且提示先生产半成品：{r.text}")
check(onhand("RM-005") == rm005_before and onhand("RM-002") == rm002_before,
      "领料失败时底层原材料 RM-005/RM-002 一点没扣（不直接扣底层原材料）")
r = post("/api/production/MO-002/ensure-subassemblies", user="worker")
check(r.status_code == 200 and len(r.json()["sub_production_orders"]) == 1, f"一键生成半成品生产单：{r.text}")
sub_mo = r.json()["sub_production_orders"][0]
r = get("/api/production")
sub = [x for x in r.json() if x["order_no"] == sub_mo][0]
check(sub["product_code"] == "SUB-001" and sub["qty"] == 10 and sub["parent_order_no"] == "MO-002",
      f"半成品生产单：造 SUB-001 x10，父单 MO-002：{sub}")
r = post("/api/production/consume", user="worker", json={"order_no": sub_mo})
check(r.status_code == 200, f"半成品生产单领料（扣 RM-005/RM-002 各 10）：{r.text}")
check(onhand("RM-005") == 10 and onhand("RM-002") == 10, "半成品领料后 RM-005/RM-002 各 10")
check(onhand("SUB-001") == 0, "半成品完工前 SUB-001 库存仍 0")
r = post("/api/production/complete", user="worker", json={"order_no": sub_mo})
check(r.status_code == 200, f"半成品完工：{r.text}")
check(onhand("SUB-001") == 10, "半成品完工后 SUB-001 库存 10")
r = post("/api/production/consume", user="worker", json={"order_no": "MO-002"})
check(r.status_code == 200, f"半成品就绪后 MO-002 领料成功：{r.text}")
cons = [x for x in r.json()["consumed"] if x["part_code"] in ("SUB-001", "RM-005", "RM-002")]
check(any(x["part_code"] == "SUB-001" and x["qty"] == 10 for x in cons), f"MO-002 领料扣了半成品 SUB-001 10：{cons}")
check(not any(x["part_code"] in ("RM-005", "RM-002") for x in cons), f"MO-002 领料没再扣底层 RM-005/RM-002：{cons}")
check(onhand("SUB-001") == 0, "MO-002 领料后 SUB-001 库存 0（被成品消耗，无虚增）")
check(onhand("RM-005") == 10 and onhand("RM-002") == 10, "MO-002 领料后 RM-005/RM-002 仍 10（只被半成品生产扣过一次）")
r = post("/api/production/complete", user="worker", json={"order_no": "MO-002"})
check(r.status_code == 200, f"MO-002 完工：{r.text}")
check(onhand("FG-002") == 10, "FG-002 成品库存 10")
# 重复领料拒绝
r = post("/api/production/consume", user="worker", json={"order_no": "MO-002"})
check(r.status_code == 400, "已领料/已完工的生产单重复领料应拒绝")

# ===== 3. 按批次冻结 =====
r = post("/api/parts", json={"code": "RM-B1", "name": "批次测试零件", "category": "原材料", "safety_stock": 0, "price": 1.0})
check(r.status_code == 200, f"新建零件 RM-B1：{r.text}")
r = post("/api/stock/in", user="keeper", json={"part_code": "RM-B1", "qty": 100, "batch": "BATCH-A", "warehouse": "主仓"})
check(r.status_code == 200, f"入库 BATCH-A 100：{r.text}")
r = post("/api/stock/in", user="keeper", json={"part_code": "RM-B1", "qty": 50, "batch": "BATCH-B", "warehouse": "主仓"})
check(r.status_code == 200, f"入库 BATCH-B 50：{r.text}")
r = post("/api/stock/freeze", user="keeper", json={"part_code": "RM-B1", "qty": 30, "batch": "BATCH-A", "warehouse": "主仓", "reason": "质检待确认"})
check(r.status_code == 200, f"按批次冻结 BATCH-A 30：{r.text}")
r = get("/api/stock/available/batch", params={"part_code": "RM-B1", "warehouse": "主仓", "batch": "BATCH-A"})
check(r.json()["available"] == 70 and r.json()["frozen"] == 30, f"BATCH-A 可用 70（100-30）：{r.json()}")
r = get("/api/stock/available/batch", params={"part_code": "RM-B1", "warehouse": "主仓", "batch": "BATCH-B"})
check(r.json()["available"] == 50, f"BATCH-B 不受影响，可用 50：{r.json()}")
r = post("/api/stock/freeze", user="keeper", json={"part_code": "RM-B1", "qty": 71, "batch": "BATCH-A", "warehouse": "主仓"})
check(r.status_code == 400, f"超批次可用量冻结应拒绝：{r.text}")
r = post("/api/stock/out", user="keeper", json={"part_code": "RM-B1", "qty": 71, "batch": "BATCH-A", "warehouse": "主仓"})
check(r.status_code == 400, f"超批次可用量出库应拒绝（现有 100 但冻了 30）：{r.text}")
r = post("/api/stock/out", user="keeper", json={"part_code": "RM-B1", "qty": 70, "batch": "BATCH-A", "warehouse": "主仓"})
check(r.status_code == 200, f"按批次可用量出库 70：{r.text}")
r = post("/api/stock/unfreeze", user="keeper", json={"part_code": "RM-B1", "qty": 10, "batch": "BATCH-A", "warehouse": "主仓"})
check(r.status_code == 200, f"按批次解冻 BATCH-A 10：{r.text}")
r = get("/api/stock/available/batch", params={"part_code": "RM-B1", "warehouse": "主仓", "batch": "BATCH-A"})
check(r.json()["available"] == 10 and r.json()["frozen"] == 20, f"解冻后 BATCH-A 可用 10（现有 30 - 冻结 20）：{r.json()}")
r = get("/api/stock/frozen", params={"part_code": "RM-B1", "batch": "BATCH-A"})
check(len(r.json()) == 1 and r.json()[0]["qty"] == 20, f"冻结明细按批次可查：{r.json()}")
# 质检不合格按批次冻结
r = post("/api/purchase", json={"order_no": "PO-QC", "supplier_code": "SUP-001", "due_date": "2026-12-31",
      "items": [{"part_code": "RM-B1", "qty": 5, "price": 1.0}]})
check(r.status_code == 200, f"新建采购单 PO-QC：{r.text}")
before_qc = onhand("RM-B1")
r = post("/api/purchase/receive", json={"order_no": "PO-QC", "part_code": "RM-B1", "qty": 5, "qc_result": "不合格", "batch": "BATCH-QC1"})
check(r.status_code == 200 and "冻结" in r.text, f"质检不合格按批次冻结：{r.text}")
check(onhand("RM-B1") == before_qc, "质检不合格不进库存")
r = get("/api/stock/frozen", params={"part_code": "RM-B1", "batch": "BATCH-QC1"})
check(len(r.json()) == 1 and r.json()[0]["qty"] == 5, f"质检冻结按批次可查：{r.json()}")
post("/api/stock/unfreeze", user="keeper", json={"part_code": "RM-B1", "qty": 5, "batch": "BATCH-QC1", "warehouse": "主仓"})

# ===== 4. 数据库序列单号 =====
po_nos = []
for i in range(3):
    r = post("/api/purchase", json={"supplier_code": "SUP-001", "due_date": "2026-12-31",
          "items": [{"part_code": "RM-001", "qty": 1, "price": 2.5}]})
    check(r.status_code == 200, f"自动单号采购单 {i+1}：{r.text}")
    po_nos.append(r.json()["order_no"])
check(len(set(po_nos)) == 3 and all(re.match(r"^PO-\d{4}-\d{4}$", n) for n in po_nos), f"3 张采购单号互不重复且格式正确：{po_nos}")
nums = [int(n.rsplit("-", 1)[1]) for n in po_nos]
check(nums == sorted(nums) and len(set(nums)) == 3, f"序列号递增不重复：{nums}")
mo_nos = []
for i in range(2):
    r = post("/api/production", json={"product_code": "FG-001", "qty": 1})
    check(r.status_code == 200, f"自动单号生产单 {i+1}：{r.text}")
    mo_nos.append(r.json()["order_no"])
check(len(set(mo_nos)) == 2 and all(re.match(r"^MO-\d{4}-\d{4}$", n) for n in mo_nos), f"生产单号序列格式：{mo_nos}")
pr_nos = []
for i in range(2):
    r = post("/api/purchase/requisitions", json={"applicant": "测试", "items": [{"part_code": "RM-001", "qty": 1, "price": 2.5, "supplier_code": "SUP-001"}]})
    check(r.status_code == 200, f"自动单号采购申请 {i+1}：{r.text}")
    pr_nos.append(r.json()["req_no"])
check(len(set(pr_nos)) == 2 and all(re.match(r"^PR-\d{4}-\d{4}$", n) for n in pr_nos), f"采购申请单号序列格式：{pr_nos}")

# ===== 5. 多公司 / 多仓库 / 多币种 =====
r = post("/api/parts", json={"code": "RM-SH", "name": "上海测试零件", "category": "原材料", "company_code": "COMP-002", "price": 3.0})
check(r.status_code == 200, f"新建分公司零件 RM-SH：{r.text}")
r = post("/api/stock/in", user="keeper", json={"part_code": "RM-SH", "qty": 50, "batch": "BATCH-SH", "warehouse": "上海仓"})
check(r.status_code == 403, f"仓库员 keeper 无上海仓权限，入库应拒绝（按仓库隔离）：{r.text}")
r = post("/api/stock/in", json={"part_code": "RM-SH", "qty": 50, "batch": "BATCH-SH", "warehouse": "上海仓"})
check(r.status_code == 200, f"管理员入库上海仓 50：{r.text}")
r = get("/api/stock", params={"warehouse": "上海仓"})
check(any(x["part_code"] == "RM-SH" and x["qty"] == 50 for x in r.json()), f"按仓库过滤库存：上海仓有 RM-SH：{r.json()}")
r = get("/api/stock", params={"warehouse": "主仓"})
check(not any(x["part_code"] == "RM-SH" for x in r.json()), "按仓库过滤：主仓没有 RM-SH（仓库隔离）")
r = get("/api/stock", user="keeper", params={"warehouse": "上海仓"})
check(r.status_code == 403, f"keeper 看上海仓库存应拒绝：{r.text}")
r = get("/api/stock", user="keeper", params={"warehouse": "主仓"})
check(r.status_code == 200, "keeper 看主仓库存允许")
# 跨公司调拨拒绝；同公司跨仓库调拨允许
r = post("/api/stock/transfer", json={"part_code": "RM-001", "qty": 1, "from_warehouse": "主仓", "to_warehouse": "上海仓", "from_location": "A区-01", "to_location": "A区-01"})
check(r.status_code == 400 and "跨公司" in r.text, f"跨公司调拨应拒绝：{r.text}")
rm001_main_before = sum(x["qty"] for x in get("/api/stock", params={"warehouse": "主仓"}).json() if x["part_code"] == "RM-001")
r = post("/api/stock/transfer", json={"part_code": "RM-001", "qty": 5, "from_warehouse": "主仓", "to_warehouse": "分仓", "from_location": "A区-01", "to_location": "A区-01"})
check(r.status_code == 200, f"同公司跨仓库调拨 主仓→分仓 5：{r.text}")
check(sum(x["qty"] for x in get("/api/stock", params={"warehouse": "分仓"}).json() if x["part_code"] == "RM-001") == 5, "分仓收到 RM-001 5")
check(sum(x["qty"] for x in get("/api/stock", params={"warehouse": "主仓"}).json() if x["part_code"] == "RM-001") == rm001_main_before - 5, "主仓 RM-001 减少 5")
# 公司隔离：采购单按公司过滤
r = post("/api/purchase", json={"supplier_code": "SUP-001", "due_date": "2026-12-31", "company_code": "COMP-002", "warehouse": "上海仓",
      "items": [{"part_code": "RM-SH", "qty": 2, "price": 3.0}]})
check(r.status_code == 200, f"新建分公司采购单：{r.text}")
po_sh = r.json()["order_no"]
r = get("/api/purchase", params={"company_code": "COMP-002"})
check(any(x["order_no"] == po_sh for x in r.json()), "按公司过滤：COMP-002 能看到分公司采购单")
r = get("/api/purchase", params={"company_code": "COMP-001"})
check(not any(x["order_no"] == po_sh for x in r.json()), "按公司过滤：COMP-001 看不到分公司采购单（公司隔离）")
# 跨公司合并拒绝
r = post("/api/purchase", json={"supplier_code": "SUP-001", "due_date": "2026-12-31", "company_code": "COMP-001",
      "items": [{"part_code": "RM-001", "qty": 1, "price": 2.5}]})
po_c1 = r.json()["order_no"]
r = post("/api/purchase/merge", json={"order_nos": [po_c1, po_sh]})
check(r.status_code == 400 and "公司" in r.text, f"跨公司采购单合并应拒绝：{r.text}")
# 多币种：USD 按种子汇率 7.2 折算
r = post("/api/purchase", json={"supplier_code": "SUP-001", "due_date": "2026-12-31", "currency": "USD",
      "items": [{"part_code": "RM-001", "qty": 10, "price": 10.0}]})
check(r.status_code == 200, f"新建 USD 采购单：{r.text}")
check(abs(r.json()["total_amount"] - 113.0) < 0.01 and abs(r.json()["total_amount_base"] - 813.6) < 0.01,
      f"USD 采购单：含税 113 美元，本位币 813.6：{r.json()}")
# EUR 指定汇率 8.0
r = post("/api/purchase", json={"supplier_code": "SUP-001", "due_date": "2026-12-31", "currency": "EUR", "exchange_rate": 8.0,
      "items": [{"part_code": "RM-001", "qty": 1, "price": 100.0}]})
check(r.status_code == 200 and abs(r.json()["total_amount_base"] - 904.0) < 0.01, f"EUR 指定汇率 8.0：本位币 904：{r.json()}")
# JPY 无汇率拒绝；维护汇率后可用
r = post("/api/purchase", json={"supplier_code": "SUP-001", "due_date": "2026-12-31", "currency": "JPY",
      "items": [{"part_code": "RM-001", "qty": 1, "price": 1000.0}]})
check(r.status_code == 400 and "汇率" in r.text, f"无汇率的外币单应拒绝：{r.text}")
r = post("/api/exchange-rates", user="finance", json={"from_currency": "JPY", "to_currency": "CNY", "rate": 0.05, "effective_date": "2026-01-01"})
check(r.status_code == 200, f"维护 JPY 汇率：{r.text}")
r = post("/api/purchase", json={"supplier_code": "SUP-001", "due_date": "2026-12-31", "currency": "JPY",
      "items": [{"part_code": "RM-001", "qty": 1, "price": 1000.0}]})
check(r.status_code == 200 and abs(r.json()["total_amount_base"] - 56.5) < 0.01, f"JPY 汇率 0.05：本位币 56.5：{r.json()}")
# 按仓库查可用量：分仓 RM-001 可用 5
r = get("/api/stock/available", params={"warehouse": "分仓"})
row = [x for x in r.json() if x["part_code"] == "RM-001"][0]
check(abs(row["available"] - 5) < 1e-6, f"分仓 RM-001 可用 5：{row}")

# ===== 6. 金额分级审批 =====
# <=10000 自动通过
r = post("/api/purchase/requisitions", json={"req_no": "PR-SMALL", "applicant": "采购员",
      "items": [{"part_code": "RM-001", "qty": 10, "price": 2.5, "supplier_code": "SUP-001"},
                {"part_code": "RM-002", "qty": 20, "price": 0.8, "supplier_code": "SUP-001"}]})
check(r.status_code == 200, f"新建小额申请 PR-SMALL：{r.text}")
r = post("/api/purchase/requisitions/PR-SMALL/submit")
check(r.status_code == 200 and r.json()["status"] == "已批准" and r.json()["approval_steps"] == 0,
      f"<=10000 自动批准：{r.text}")
r = get("/api/approvals", params={"doc_no": "PR-SMALL"})
check(any(x["status"] == "已批准" and "自动" in (x["comment"] or "") for x in r.json()), f"自动通过有审批记录：{r.json()}")
# 未提交不能批
r = post("/api/purchase/requisitions", json={"req_no": "PR-NOSUB", "applicant": "采购员",
      "items": [{"part_code": "RM-001", "qty": 5000, "price": 2.5, "supplier_code": "SUP-001"}]})
r = post("/api/purchase/requisitions/PR-NOSUB/approve", user="manager")
check(r.status_code == 400, f"未提交的申请不能批准：{r.text}")
# >10000 经理批：采购员（无审批权）批不了，经理能批
r = post("/api/purchase/requisitions/PR-NOSUB/submit")
check(r.status_code == 200 and r.json()["status"] == "待审批" and r.json()["approval_steps"] == 1, f">10000 提交后待审批，1 级：{r.text}")
r = post("/api/purchase/requisitions/PR-NOSUB/approve", user="buyer")
check(r.status_code == 403, f"采购员无权批经理级步骤：{r.text}")
r = post("/api/purchase/requisitions/PR-NOSUB/approve", user="manager", json={"comment": "同意"})
check(r.status_code == 200 and r.json()["status"] == "已批准", f"经理批准：{r.text}")
r = get("/api/approvals", params={"doc_no": "PR-NOSUB"})
check(any(x["action_by"] == "manager" and x["status"] == "已批准" for x in r.json()), f"审批记录有经理批准：{r.json()}")
# >100000 经理+总监两级
r = post("/api/purchase/requisitions", json={"req_no": "PR-BIG", "applicant": "采购员",
      "items": [{"part_code": "RM-001", "qty": 50000, "price": 2.5, "supplier_code": "SUP-001"}]})
r = post("/api/purchase/requisitions/PR-BIG/submit")
check(r.status_code == 200 and r.json()["approval_steps"] == 2, f">100000 需 2 级审批：{r.text}")
r = post("/api/purchase/requisitions/PR-BIG/approve", user="manager", json={"comment": "经理同意"})
check(r.status_code == 200 and r.json()["status"] == "待审批" and r.json()["remaining_steps"] == 1, f"经理批完还剩总监一级：{r.text}")
r = post("/api/purchase/requisitions/PR-BIG/approve", user="director", json={"comment": "总监同意"})
check(r.status_code == 200 and r.json()["status"] == "已批准", f"总监批完已批准：{r.text}")
# 拒绝：整单拒绝，转单失败
r = post("/api/purchase/requisitions", json={"req_no": "PR-REJ", "applicant": "采购员",
      "items": [{"part_code": "RM-001", "qty": 5000, "price": 2.5, "supplier_code": "SUP-001"}]})
post("/api/purchase/requisitions/PR-REJ/submit")
r = post("/api/purchase/requisitions/PR-REJ/reject", user="manager", json={"comment": "预算不足"})
check(r.status_code == 200 and r.json()["status"] == "已拒绝", f"经理拒绝：{r.text}")
r = post("/api/purchase/requisitions/PR-REJ/convert")
check(r.status_code == 400, f"已拒绝的申请不能转采购单：{r.text}")
# 采购单审批：未批准不能到货
r = post("/api/purchase", json={"order_no": "PO-APPR", "supplier_code": "SUP-001", "due_date": "2026-12-31",
      "items": [{"part_code": "RM-001", "qty": 5000, "price": 2.5}]})
check(r.status_code == 200, f"新建待审批采购单 PO-APPR：{r.text}")
r = post("/api/purchase/PO-APPR/submit-approval")
check(r.status_code == 200 and r.json()["status"] == "待审批", f"采购单提交审批：{r.text}")
r = post("/api/purchase/receive", json={"order_no": "PO-APPR", "part_code": "RM-001", "qty": 1})
check(r.status_code == 400 and "审批" in r.text, f"待审批的采购单不能到货：{r.text}")
r = post("/api/purchase/PO-APPR/approve", user="manager")
check(r.status_code == 200 and r.json()["status"] == "已批准", f"采购单经理批准：{r.text}")
r = post("/api/purchase/receive", json={"order_no": "PO-APPR", "part_code": "RM-001", "qty": 10})
check(r.status_code == 200, f"批准后可到货：{r.text}")
# 自定义审批人：指定 buyer 批小额采购单；经理批不了，buyer 能批
r = post("/api/approvals/rules", user="admin", json={"doc_type": "采购单", "name": "测试自定义：指定采购员批",
      "min_amount": 0, "max_amount": 99999999, "approver_user": "buyer", "step_order": 1, "company_code": ""})
check(r.status_code == 200, f"创建自定义审批规则：{r.text}")
r = post("/api/purchase", json={"order_no": "PO-CUSTOM", "supplier_code": "SUP-001", "due_date": "2026-12-31",
      "items": [{"part_code": "RM-001", "qty": 10, "price": 10.0}]})
r = post("/api/purchase/PO-CUSTOM/submit-approval")
check(r.status_code == 200 and r.json()["status"] == "待审批", f"自定义规则下提交审批：{r.text}")
r = post("/api/purchase/PO-CUSTOM/approve", user="manager")
check(r.status_code == 403, f"指定审批人是 buyer，经理不能批：{r.text}")
r = post("/api/purchase/PO-CUSTOM/approve", user="buyer", json={"comment": "指定人批准"})
check(r.status_code == 200 and r.json()["status"] == "已批准", f"指定审批人 buyer 批准：{r.text}")
# 清理自定义规则，避免影响后续
r = get("/api/approvals/rules", params={"doc_type": "采购单"})
custom = [x for x in r.json() if x["is_custom"] == 1]
for x in custom:
    rr = client.delete(f"/api/approvals/rules/{x['id']}", headers=H("admin"))
    check(rr.status_code == 200, f"删除自定义审批规则 {x['id']}：{rr.text}")
# 待审批查询
r = get("/api/approvals/pending", user="manager")
check(r.status_code == 200, f"我的待审批查询：{r.text[:200]}")

# ===== 7. 财务对账 =====
r = post("/api/purchase", json={"supplier_code": "SUP-001", "due_date": "2026-12-31",
      "items": [{"part_code": "RM-001", "qty": 10, "price": 2.5}]})
po_fin = r.json()["order_no"]
r = post("/api/purchase/receive", json={"order_no": po_fin, "part_code": "RM-001", "qty": 10})
check(r.status_code == 200, f"财务测试采购单到货：{r.text}")
r = post("/api/finance/invoices", user="finance", json={"invoice_no": "INV-F1", "po_no": po_fin, "total_amount": 28.25})
check(r.status_code == 200 and r.json()["status"] == "待对账" and abs(r.json()["diff_amount"]) < 0.01,
      f"发票三方核对一致：{r.text}")
r = post("/api/finance/invoices/INV-F1/reconcile", user="finance")
check(r.status_code == 200 and r.json()["status"] == "已对账", f"发票对账通过：{r.text}")
r = post("/api/finance/invoices/INV-F1/pay", user="finance", json={"amount": 10, "method": "银行转账"})
check(r.status_code == 200 and r.json()["status"] == "部分付款" and abs(r.json()["paid_amount_base"] - 10) < 0.01, f"部分付款 10：{r.text}")
check(re.match(r"^PAY-\d{4}-\d{4}$", r.json()["payment_no"]) is not None, f"付款单号序列格式：{r.json()['payment_no']}")
r = post("/api/finance/invoices/INV-F1/pay", user="finance", json={"amount": 18.25})
check(r.status_code == 200 and r.json()["status"] == "已付款", f"付清 18.25：{r.text}")
r = post("/api/finance/invoices/INV-F1/pay", user="finance", json={"amount": 1})
check(r.status_code == 400, f"已付清的发票不能再付：{r.text}")
# 差异发票：三方核对不通过
r = post("/api/purchase", json={"supplier_code": "SUP-001", "due_date": "2026-12-31",
      "items": [{"part_code": "RM-001", "qty": 10, "price": 2.5}]})
po_fin2 = r.json()["order_no"]
post("/api/purchase/receive", json={"order_no": po_fin2, "part_code": "RM-001", "qty": 10})
r = post("/api/finance/invoices", user="finance", json={"invoice_no": "INV-F2", "po_no": po_fin2, "total_amount": 100})
check(r.status_code == 200 and r.json()["status"] == "差异待处理" and abs(r.json()["diff_amount"] - 71.75) < 0.01,
      f"发票金额 100 vs 到货 28.25，差额 71.75，差异待处理：{r.text}")
r = post("/api/finance/invoices/INV-F2/reconcile", user="finance")
check(r.status_code == 400 and "不通过" in r.text, f"差异发票对账应拒绝：{r.text}")
# 重复发票号拒绝
r = post("/api/finance/invoices", user="finance", json={"invoice_no": "INV-F1", "po_no": po_fin2, "total_amount": 28.25})
check(r.status_code == 400 and "已存在" in r.text, f"重复发票号应拒绝（防重复付款）：{r.text}")
# 未对账不能付款
r = post("/api/finance/invoices/INV-F2/pay", user="finance", json={"amount": 10})
check(r.status_code == 400 and "对账" in r.text, f"未对账的发票不能付款：{r.text}")
# 应付报表
r = get("/api/finance/payables", user="finance")
rows = {x["supplier_code"]: x for x in r.json()["rows"]}
check("SUP-001" in rows and rows["SUP-001"]["unpaid_amount_base"] >= 100 and rows["SUP-001"]["unreconciled_amount"] >= 100,
      f"应付报表 SUP-001 未付/未对账 >= 100（含差异发票）：{rows.get('SUP-001')}")
# 生产成本：MO-002 直接材料 = SUB-001 10x0 + RM-001 10x2.5 + RM-003 10x5 + RM-004 10x0.5 = 80
r = get("/api/finance/production-cost/MO-002", user="finance")
check(r.status_code == 200 and abs(r.json()["material_cost"] - 80) < 0.01, f"MO-002 生产成本 80：{r.json()}")
check(any(i["part_code"] == "SUB-001" for i in r.json()["items"]), "生产成本含半成品领料")
r = get("/api/finance/production-cost", user="finance")
check(any(x["order_no"] == "MO-002" for x in r.json()), "生产成本列表含 MO-002")

# ===== 8. RBAC 权限细化 =====
r = post("/api/parts", user="auditor", json={"code": "RM-NO", "name": "无权新建"})
check(r.status_code == 403, f"审计员（只读）新建零件应拒绝：{r.text}")
r = get("/api/parts", user="auditor")
check(r.status_code == 200, "审计员查看零件允许")
r = post("/api/parts", user="keeper", json={"code": "RM-NO2", "name": "无权新建"})
check(r.status_code == 403, f"仓库员新建零件应拒绝：{r.text}")
r = get("/api/parts", user="ghost")
check(r.status_code == 403, f"不存在的用户应拒绝：{r.text}")
r = get("/api/permissions/check", user="buyer", params={"module": "采购", "action": "approve"})
check(r.json()["allowed"] is False, f"采购员审批权限检查为否：{r.json()}")
r = get("/api/permissions/check", user="manager", params={"module": "采购", "action": "approve"})
check(r.json()["allowed"] is True, f"经理审批权限检查为是：{r.json()}")
r = get("/api/permissions", params={"role": "采购经理"})
check(any(x["module"] == "采购" and x["can_approve"] == 1 for x in r.json()), f"采购经理有采购审批权：{r.json()}")
# 新建用户并分配仓库范围
r = post("/api/users", json={"username": "tester", "name": "测试员", "roles": ["仓库员"], "warehouses": ["分仓"], "companies": ["COMP-001"]})
check(r.status_code == 200, f"新建用户 tester（仓库员，仅分仓）：{r.text}")
r = get("/api/stock", user="tester", params={"warehouse": "主仓"})
check(r.status_code == 403, f"tester 看主仓应拒绝：{r.text}")
r = get("/api/stock", user="tester", params={"warehouse": "分仓"})
check(r.status_code == 200, "tester 看分仓允许")
r = get("/api/users")
check(any(u["username"] == "tester" and u["warehouses"] == ["分仓"] for u in r.json()), "用户 tester 仓库范围已保存")

# ===== 9. 报表 + Excel + 审计日志 =====
r = get("/api/reports/stock_balance", params={"warehouse": "主仓"})
check(r.status_code == 200 and any(x["零件编码"] == "RM-001" for x in r.json()["rows"]), f"库存结存报表：{str(r.json())[:300]}")
check("库存金额" in str(r.json()["headers"]), "库存结存报表含库存金额列")
r = get("/api/reports/purchase_summary")
check(r.status_code == 200 and any(x["供应商编码"] == "SUP-001" and x["采购单数"] >= 1 for x in r.json()["rows"]), f"采购汇总报表：{str(r.json())[:300]}")
r = get("/api/reports/production_summary")
check(r.status_code == 200 and any(x["成品编码"] == "FG-002" and x["已完工单数"] >= 1 for x in r.json()["rows"]), f"生产汇总报表：{str(r.json())[:300]}")
r = get("/api/reports/payable")
check(r.status_code == 200 and any(x["供应商编码"] == "SUP-001" for x in r.json()["rows"]), "应付报表（报表接口）含 SUP-001")
r = get("/api/reports/stock_balance", user="auditor")
check(r.status_code == 200, "审计员可看报表（只读）")
for kind in ["stock_balance", "purchase_summary", "production_summary", "payable", "audit_log"]:
    r = get(f"/api/excel/export/{kind}")
    check(r.status_code == 200 and r.content[:2] == b"PK", f"Excel 导出报表 {kind} 是 xlsx：{len(r.content)} 字节")
r = get("/api/excel/export/purchase")
check(r.status_code == 200 and r.content[:2] == b"PK", "Excel 导出采购单（含公司/仓库/币种列）")
# Excel 导入：零件带公司编码
from openpyxl import Workbook
wb = Workbook(); ws = wb.active
ws.append(["物料编码", "材料名称", "规格", "单位", "类别", "安全库存", "未税单价", "税率", "公司编码"])
ws.append(["RM-IMP2", "导入测试零件", "规格", "个", "原材料", 5, 9.9, 0.13, "COMP-002"])
buf = io.BytesIO(); wb.save(buf); buf.seek(0)
r = client.post("/api/excel/import/parts", headers=H("admin"),
                files={"file": ("p.xlsx", buf.read(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
check(r.status_code == 200 and r.json()["imported"] == 1, f"Excel 导入零件（带公司编码）：{r.json()}")
r = get("/api/parts", params={"company_code": "COMP-002"})
check(any(x["code"] == "RM-IMP2" for x in r.json()), "导入的 RM-IMP2 归属 COMP-002")
# 审计日志
r = post("/api/parts", json={"code": "RM-AUD", "name": "审计测试零件", "category": "原材料", "price": 1.0})
check(r.status_code == 200, "新建审计测试零件")
r = get("/api/audit-log/零件/RM-AUD")
check(any(x["action"] == "创建" and x["username"] == "admin" for x in r.json()), f"审计日志有零件创建记录：{r.json()}")
r = get("/api/audit-log", params={"entity_type": "采购单", "entity_id": po_fin})
check(len(r.json()) >= 1 and any(x["action"] in ("到货入库", "对账", "创建") for x in r.json()), f"审计日志按单据可查：{r.json()}")
r = get("/api/audit-log", params={"user": "manager"})
check(any(x["action"] in ("审批通过", "审批拒绝", "提交审批") for x in r.json()), f"审计日志按用户可查经理审批：{str(r.json())[:300]}")
r = client.delete("/api/audit-log", headers=H("admin"))
check(r.status_code in (404, 405), f"审计日志没有删除接口（只能追加）：{r.status_code}")

# ===== 10. 看板 + 健康检查 =====
r = get("/api/dashboard")
d = r.json()
for key in ["shortage", "overdue_purchase", "supplier_ranking", "production_shortage"]:
    check(key in d and d[key]["status"] in ("红", "黄", "绿"), f"看板 {key} 有红黄绿状态")
r = get("/health")
check(r.status_code == 200 and r.json()["ok"] is True, f"健康检查：{r.text}")

print(f"\n全部第二阶段验证通过 ✅（共 {PASSED[0]} 项 ✅）")
import os as _os
_os.unlink(tmp.name)
