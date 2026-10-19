# -*- coding: utf-8 -*-
"""PMC 第一阶段端到端自动验证（FastAPI TestClient，不依赖外部服务）。

覆盖：MVP 回归（SO-001 100 个小夜灯）+ 第一阶段新增：
多层 BOM 爆炸 / MRP 手动调整 / BOM 导入 / 供应商比价 / 库存调拨-盘点-冻结-可用量 /
采购申请审批 / 采购单合并 / 拖期标红 / 生产完整流程（含计划日期）/ Excel 导入导出 / 看板四块。
"""
import os, sys, io, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import app as app_module
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()
app_module.DB_PATH = tmp.name
app_module.init_db()

from fastapi.testclient import TestClient
client = TestClient(app_module.app)

def check(cond, msg):
    print(("✅ " if cond else "❌ ") + msg)
    assert cond, msg

# ===== 0. 种子数据 =====
r = client.get("/api/parts")
check(r.status_code == 200 and len(r.json()) == 8, f"零件 8 条（含 FG-002/SUB-001），实际 {len(r.json()) if r.status_code==200 else r.text}")

# ===== 1. MVP 回归：SO-001 100 个小夜灯 =====
r = client.get("/api/bom/FG-001")
check(len(r.json()) == 5, f"BOM FG-001 V1 默认 5 条，实际 {len(r.json())}")
r = client.post("/api/bom/explode", json={"parent_code": "FG-001", "qty": 100, "leaf_only": True})
check(all(x["qty"] == 100 for x in r.json()), f"BOM 爆炸 FG-001 100 每个都是 100：{r.json()}")
r = client.post("/api/sales", json={"order_no": "SO-001", "customer": "演示客户", "product_code": "FG-001", "qty": 100, "due_date": "2026-12-31"})
check(r.status_code == 200, f"创建销售单 SO-001：{r.text}")
r = client.post("/api/mrp", json={"sales_order_no": "SO-001"})
mrp = {x["part_code"]: x for x in r.json()["results"]}
expected = {"RM-001": 100, "RM-002": 90, "RM-003": 110, "RM-004": 70, "RM-005": 105}
for code, exp in expected.items():
    check(abs(mrp[code]["net"] - exp) < 1e-6, f"MRP {code} 净需求 {mrp[code]['net']} == {exp}")
check(mrp["RM-001"]["latest_order_date"] == "2026-12-24", f"MRP 最晚下单日 = 交期 2026-12-31 - 提前期 7 天 = 2026-12-24，实际 {mrp['RM-001']['latest_order_date']}")
r = client.post("/api/purchase/from-mrp", json={"sales_order_no": "SO-001"})
check(r.status_code == 200, f"从 MRP 生成采购单：{r.text}")
po_no = r.json()["order_no"]
check(abs(r.json()["total_amount_with_tax"] - 1380.86) < 0.01, f"采购单含税总额 1380.86，实际 {r.json()['total_amount_with_tax']}")
for code, qty in expected.items():
    r = client.post("/api/purchase/receive", json={"order_no": po_no, "part_code": code, "qty": qty})
    check(r.status_code == 200, f"到货 {code} {qty}：{r.text}")
r = client.get("/api/purchase")
po = [x for x in r.json() if x["order_no"] == po_no][0]
check(po["status"] == "全部到货", f"采购单全部到货：{po['status']}")
# 对账字段
r = client.put(f"/api/purchase/{po_no}/reconcile", json={"invoice_no": "INV-001", "invoice_amount": 1380.86, "reconciled": True})
check(r.status_code == 200 and r.json()["reconciled"] == 1, f"采购对账：{r.text}")
r = client.post("/api/production", json={"order_no": "MO-001", "product_code": "FG-001", "qty": 100, "sales_order_no": "SO-001", "planned_start": "2026-11-01", "planned_end": "2026-11-10"})
check(r.status_code == 200, f"创建生产单 MO-001（带计划日期）：{r.text}")
r = client.post("/api/production/consume", json={"order_no": "MO-001"})
check(r.status_code == 200, f"领料：{r.text}")
r = client.get("/api/stock")
stock = {x["part_code"]: x["qty"] for x in r.json()}
for code in expected:
    check(abs(stock[code] - 20) < 1e-6, f"领料后 {code} 库存 {stock[code]} == 20（安全库存）")
r = client.post("/api/production/complete", json={"order_no": "MO-001"})
check(r.status_code == 200, f"完工：{r.text}")
r = client.get("/api/stock")
stock = {x["part_code"]: x["qty"] for x in r.json()}
check(abs(stock["FG-001"] - 100) < 1e-6, f"成品库存 {stock['FG-001']} == 100")

# ===== 2. 多层 BOM 爆炸（FG-002 → SUB-001 → RM-005/RM-002） =====
r = client.post("/api/bom/explode", json={"parent_code": "FG-002", "qty": 10})
print("多层爆炸 FG-002 x10:", r.json())
by_code = {}
for x in r.json():
    by_code.setdefault(x["part_code"], 0)
    by_code[x["part_code"]] += x["qty"]
check(abs(by_code.get("SUB-001", 0) - 10) < 1e-6, "多层：SUB-001 10")
check(abs(by_code.get("RM-005", 0) - 10) < 1e-6, "多层：RM-005 10（经半成品展开）")
check(abs(by_code.get("RM-002", 0) - 10) < 1e-6, "多层：RM-002 10（经半成品展开）")
check(abs(by_code.get("RM-001", 0) - 10) < 1e-6, "多层：RM-001 10")
r = client.post("/api/bom/explode", json={"parent_code": "FG-002", "qty": 10, "leaf_only": True})
leaf = {x["part_code"]: x["qty"] for x in r.json()}
check("SUB-001" not in leaf, "叶子爆炸不含半成品 SUB-001")
# BOM 循环检测：尝试让 RM-005 的 BOM 包含 FG-002（会形成 FG-002→SUB-001→RM-005→FG-002 循环）
r = client.post("/api/bom", json={"parent_code": "RM-005", "child_code": "FG-002", "qty": 1, "version": "V9"})
check(r.status_code == 400 and "循环" in r.text, f"BOM 循环检测应拒绝：{r.text}")
# BOM 对比 V1 vs V2：LED 灯珠 1 → 2
r = client.post("/api/bom/compare", json={"parent_code": "FG-001", "version_a": "V1", "version_b": "V2"})
print("BOM 对比:", r.json())
check(len(r.json()["changed"]) == 1 and r.json()["changed"][0]["child_code"] == "RM-002"
      and r.json()["changed"][0]["qty_after"] == 2, f"BOM 对比 V1→V2 只有 RM-002 用量 1→2：{r.json()}")
# BOM 版本列表
r = client.get("/api/bom/versions/FG-001")
check(len(r.json()) == 2, f"FG-001 有 2 个 BOM 版本：{r.json()}")
# 指定 V2 爆炸：100 个小夜灯需要 200 个灯珠
r = client.post("/api/bom/explode", json={"parent_code": "FG-001", "qty": 100, "version": "V2", "leaf_only": True})
leaf2 = {x["part_code"]: x["qty"] for x in r.json()}
check(abs(leaf2["RM-002"] - 200) < 1e-6, f"V2 爆炸 RM-002 应为 200，实际 {leaf2.get('RM-002')}")

# ===== 3. 多层 BOM 的 MRP + 手动调整 + 按比价生成 =====
r = client.post("/api/sales", json={"order_no": "SO-002", "customer": "客户B", "product_code": "FG-002", "qty": 10, "due_date": "2026-12-20"})
check(r.status_code == 200, f"创建销售单 SO-002（FG-002）：{r.text}")
r = client.post("/api/mrp", json={"sales_order_no": "SO-002"})
print("MRP SO-002:", r.json())
mrp2 = {x["part_code"]: x for x in r.json()["results"]}
check(mrp2["SUB-001"]["need_type"] == "生产", f"半成品 SUB-001 需求类型=生产：{mrp2['SUB-001']}")
check(mrp2["SUB-001"]["level"] == 1, "SUB-001 层级 1")
check(mrp2["RM-005"]["level"] == 2, "RM-005 层级 2（经半成品）")
# 手动调整：把 RM-001 净需求改成 5
r = client.put("/api/mrp/adjust", json={"sales_order_no": "SO-002", "part_code": "RM-001", "adjusted_net": 5})
check(r.status_code == 200 and r.json()["adjusted_net"] == 5, f"MRP 手动调整：{r.text}")
r = client.get("/api/mrp/SO-002")
adj = [x for x in r.json() if x["part_code"] == "RM-001"][0]
check(adj["adjusted_net"] == 5, "调整后数量已保存")
# 按比价生成：RM-001 最便宜是 SUP-002（2.0），RM-003 最便宜是 SUP-001（5.0），应分属两张采购单
r = client.post("/api/mrp/generate", json={"sales_order_no": "SO-002"})
print("MRP 生成:", r.json())
check(r.status_code == 200 and len(r.json()["purchase_orders"]) >= 2, f"MRP 生成应按最便宜供应商分组出多张采购单：{r.text}")
check(len(r.json()["production_orders"]) >= 1, f"MRP 生成应含半成品生产单：{r.text}")
# 检查生成的采购单里 RM-001 在 SUP-002 的单上
r = client.get("/api/purchase")
pos = r.json()
rm001_pos = [p for p in pos if any(i["part_code"] == "RM-001" and i["qty"] == 5 for i in p["items"]) and p["supplier_code"] == "SUP-002"]
check(len(rm001_pos) >= 1, "调整后 RM-001 数量 5 已生成到 SUP-002（最便宜）的采购单上")

# ===== 4. BOM Excel 导入 =====
from openpyxl import Workbook
wb = Workbook(); ws = wb.active
ws.append(["父件编码", "子件编码", "用量", "损耗率", "版本号", "生效日期"])
ws.append(["FG-001", "RM-999", 1, 0, "V1", "2026-01-01"])  # RM-999 还不存在，先建零件
r = client.post("/api/parts", json={"code": "RM-999", "name": "测试零件", "category": "原材料", "safety_stock": 0, "price": 1.0})
check(r.status_code == 200, "新建零件 RM-999")
buf = io.BytesIO(); wb.save(buf); buf.seek(0)
r = client.post("/api/excel/import/bom", files={"file": ("bom.xlsx", buf.read(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
print("BOM 导入:", r.json())
check(r.status_code == 200 and r.json()["imported"] == 1, f"BOM Excel 导入：{r.json()}")
r = client.get("/api/bom/FG-001")
check(any(x["child_code"] == "RM-999" for x in r.json()), "BOM 导入的 RM-999 已在 FG-001 BOM 中")
# BOM 导入校验：用量为 0 应报错并提示行号
wb = Workbook(); ws = wb.active
ws.append(["父件编码", "子件编码", "用量", "版本号"])
ws.append(["FG-001", "RM-001", 0, "V1"])
buf = io.BytesIO(); wb.save(buf); buf.seek(0)
r = client.post("/api/excel/import/bom", files={"file": ("bad.xlsx", buf.read(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
check("第 2 行" in str(r.json()["errors"]), f"BOM 导入校验应提示行号：{r.json()}")

# ===== 5. 供应商：CRUD + 比价 + 准时率 + 黑名单 =====
r = client.get("/api/suppliers/compare/RM-001")
print("比价 RM-001:", r.json())
check(r.json()["cheapest_supplier"] == "SUP-002", "RM-001 最便宜 SUP-002（2.0 < 2.5）")
r = client.get("/api/suppliers/compare/RM-003")
check(r.json()["cheapest_supplier"] == "SUP-001", "RM-003 最便宜 SUP-001（5.0 < 6.0）")
check(r.json()["fastest_supplier"] == "SUP-002", "RM-003 交期最短 SUP-002（3 天 < 7 天）")
# 供应商 CRUD
r = client.post("/api/suppliers", json={"code": "SUP-003", "name": "测试供应商C", "contact": "王经理"})
check(r.status_code == 200, f"新建供应商 SUP-003：{r.text}")
r = client.put("/api/suppliers/SUP-003", json={"phone": "13900000000"})
check(r.status_code == 200, "修改供应商")
r = client.post("/api/supplier-parts", json={"supplier_code": "SUP-003", "part_code": "RM-001", "price": 1.5, "lead_time_days": 4})
check(r.status_code == 200, "新增供应商价格")
r = client.get("/api/suppliers/compare/RM-001")
check(r.json()["cheapest_supplier"] == "SUP-003", "SUP-003 报价 1.5 成为最便宜")
r = client.post("/api/suppliers/SUP-003/blacklist")
check(r.status_code == 200 and r.json()["status"] == "黑名单", "拉黑 SUP-003")
r = client.get("/api/suppliers/compare/RM-001")
check(r.json()["cheapest_supplier"] == "SUP-002", "黑名单供应商不参与比价，最便宜回到 SUP-002")
check(any(o["skipped_blacklist"] for o in r.json()["options"]), "比价结果标注黑名单跳过")
r = client.post("/api/suppliers/SUP-003/approve")
check(r.json()["status"] == "合格", "恢复合格供应商")
r = client.delete("/api/suppliers/SUP-003")
check(r.status_code == 200, "删除无采购单的供应商")
# 准时率：SO-001 的采购单按期到货（due=今天，received=今天），应为 100%
r = client.get("/api/suppliers/SUP-001/stats")
print("供应商准时率:", r.json())
check(r.json()["on_time_rate_pct"] == 100.0, f"SUP-001 准时率 100%：{r.json()}")

# ===== 6. 库存：调拨 / 盘点 / 冻结 / 可用量 =====
# 调拨：RM-004 从 A区-01 调 10 个到 B区-01
r = client.post("/api/stock/transfer", json={"part_code": "RM-004", "qty": 10, "from_location": "A区-01", "to_location": "B区-01"})
check(r.status_code == 200, f"调拨：{r.text}")
r = client.get("/api/stock")
rows = [x for x in r.json() if x["part_code"] == "RM-004"]
check(any(x["location"] == "B区-01" and x["qty"] == 10 for x in rows), "调拨后 B区-01 有 10 个 RM-004")
check(sum(x["qty"] for x in rows) == 20, "调拨不改变总数（仍 20）")
# 盘点：把 RM-004 B区-01 实盘改成 12（盘盈 2）
r = client.post("/api/stock/count", json={"part_code": "RM-004", "location": "B区-01", "counted_qty": 12})
check(r.status_code == 200 and r.json()["diff"] == 2, f"盘点差异 +2：{r.text}")
r = client.get("/api/stock")
check(sum(x["qty"] for x in r.json() if x["part_code"] == "RM-004") == 22, "盘点后 RM-004 总数 22")
# 冻结 / 解冻：冻结 RM-004 5 个，可用量减少 5；解冻后恢复
r = client.get("/api/stock/available/RM-004")
avail_before = r.json()["available"]
r = client.post("/api/stock/freeze", json={"part_code": "RM-004", "qty": 5, "reason": "测试冻结"})
check(r.status_code == 200, f"冻结：{r.text}")
r = client.get("/api/stock/available/RM-004")
check(abs(r.json()["available"] - (avail_before - 5)) < 1e-6 and r.json()["frozen"] == 5, "冻结后可用量 -5")
r = client.post("/api/stock/unfreeze", json={"part_code": "RM-004", "qty": 5})
check(r.status_code == 200, "解冻")
r = client.get("/api/stock/available/RM-004")
check(abs(r.json()["available"] - avail_before) < 1e-6, "解冻后可用量恢复")
# 可用量公式：现有 + 在途 - 已分配 - 冻结（RM-001：现有 20 + 在途 0 - 占用 - 冻结 0）
r = client.get("/api/stock/available/RM-001")
a = r.json()
check(abs(a["available"] - (a["onhand"] + a["in_transit"] - a["allocated"] - a["frozen"])) < 1e-6, f"可用量公式成立：{a}")
# 高于最高库存预警：把 RM-004 现有库存抬到超过最高库存 200？改用小值：设 max_stock=10
r = client.put("/api/parts/RM-004", json={"max_stock": 10})
check(r.status_code == 200, "设置 RM-004 最高库存 10")
r = client.get("/api/stock/available/RM-004")
check(r.json()["status"] == "黄", f"高于最高库存应为黄：{r.json()}")
r = client.put("/api/parts/RM-004", json={"max_stock": 200})
# 批次追溯：RM-001 应有期初 + 采购入库流水
r = client.get("/api/stock/trace/RM-001")
check(len(r.json()["ledger"]) >= 2 and any(x["type"] == "采购入库" for x in r.json()["ledger"]), f"批次追溯有采购入库流水：{r.json()['ledger'][:2]}")
# 入库/出库通用接口
r = client.post("/api/stock/in", json={"part_code": "RM-999", "qty": 50, "type": "其他入库", "ref_no": "TEST-IN"})
check(r.status_code == 200 and r.json()["onhand"] == 50, "通用入库")
r = client.post("/api/stock/out", json={"part_code": "RM-999", "qty": 20, "type": "其他出库", "ref_no": "TEST-OUT"})
check(r.status_code == 200 and r.json()["onhand"] == 30, "通用出库")
r = client.post("/api/stock/out", json={"part_code": "RM-999", "qty": 9999})
check(r.status_code == 400, "超库存出库应拒绝")

# ===== 7. 采购申请审批 + 合并 + 拖期 =====
r = client.post("/api/purchase/requisitions", json={"req_no": "PR-001", "applicant": "采购员", "reason": "测试申请",
      "items": [{"part_code": "RM-001", "qty": 10, "price": 2.5, "supplier_code": "SUP-001"},
                {"part_code": "RM-002", "qty": 20, "price": 0.8, "supplier_code": "SUP-001"}]})
check(r.status_code == 200, f"新建采购申请：{r.text}")
r = client.post("/api/purchase/requisitions/PR-001/approve")
check(r.status_code == 400, "未提交的申请不能直接批准")
r = client.post("/api/purchase/requisitions/PR-001/submit")
check(r.json()["status"] == "已提交", "提交申请")
r = client.post("/api/purchase/requisitions/PR-001/approve")
check(r.json()["status"] == "已批准", "一级审批批准")
r = client.post("/api/purchase/requisitions/PR-001/convert")
check(r.status_code == 200 and len(r.json()["purchase_orders"]) == 1, f"申请转采购单（同供应商合并 1 张）：{r.text}")
pr_po = r.json()["purchase_orders"][0]
# 采购单合并：建两张同供应商采购单，合并成一张
r = client.post("/api/purchase", json={"order_no": "PO-M1", "supplier_code": "SUP-001", "due_date": "2026-12-31",
      "items": [{"part_code": "RM-001", "qty": 5, "price": 2.5}]})
check(r.status_code == 200, "手动新建采购单 PO-M1")
r = client.post("/api/purchase", json={"order_no": "PO-M2", "supplier_code": "SUP-001", "due_date": "2026-12-31",
      "items": [{"part_code": "RM-001", "qty": 7, "price": 2.5}, {"part_code": "RM-002", "qty": 3, "price": 0.8}]})
check(r.status_code == 200, "手动新建采购单 PO-M2")
r = client.post("/api/purchase/merge", json={"order_nos": ["PO-M1", "PO-M2"]})
print("合并:", r.json())
check(r.status_code == 200, f"合并采购单：{r.text}")
merged_no = r.json()["order_no"]
r = client.get("/api/purchase")
merged = [x for x in r.json() if x["order_no"] == merged_no][0]
mi = {i["part_code"]: i["qty"] for i in merged["items"]}
check(mi.get("RM-001") == 12 and mi.get("RM-002") == 3, f"合并后同零件数量相加：{mi}")
old = [x for x in r.json() if x["order_no"] in ("PO-M1", "PO-M2")]
check(all(x["status"] == "已合并" for x in old), "被合并的原单状态=已合并")
# 拖期标红：建一张交期为过去的采购单
r = client.post("/api/purchase", json={"order_no": "PO-OVERDUE", "supplier_code": "SUP-001", "due_date": "2026-01-01",
      "items": [{"part_code": "RM-001", "qty": 1, "price": 2.5}]})
check(r.status_code == 200, "新建拖期采购单")
r = client.get("/api/purchase")
od = [x for x in r.json() if x["order_no"] == "PO-OVERDUE"][0]
check(od["is_overdue"] is True and od["overdue_days"] > 0, f"拖期标红：{od}")
# 质检不合格：到货进冻结，不进库存
before = client.get("/api/stock/available/RM-001").json()["onhand"]
r = client.post("/api/purchase/receive", json={"order_no": "PO-OVERDUE", "part_code": "RM-001", "qty": 1, "qc_result": "不合格"})
check(r.status_code == 200 and "冻结" in r.text, f"质检不合格：{r.text}")
after = client.get("/api/stock/available/RM-001").json()
check(abs(after["onhand"] - before) < 1e-6 and after["frozen"] == 1, "质检不合格不进库存、进冻结")
client.post("/api/stock/unfreeze", json={"part_code": "RM-001", "qty": 1})

# ===== 8. 生产完整流程（FG-002 多层 BOM） =====
r = client.post("/api/sales/SO-002/create-production", json={"order_no": "MO-002", "planned_start": "2026-11-05", "planned_end": "2026-11-15"})
check(r.status_code == 200 and r.json()["order_no"] == "MO-002", f"销售单触发生产单：{r.text}")
r = client.get("/api/production")
mo2 = [x for x in r.json() if x["order_no"] == "MO-002"][0]
check(mo2["planned_start"] == "2026-11-05" and mo2["planned_end"] == "2026-11-15", "生产单计划日期已保存")
r = client.post("/api/production/MO-002/plan", json={"planned_start": "2026-11-06", "planned_end": "2026-11-16"})
check(r.json()["planned_start"] == "2026-11-06", "修改生产计划日期")
r = client.post("/api/production/MO-002/plan", json={"planned_start": "2026-11-20", "planned_end": "2026-11-10"})
check(r.status_code == 400, "计划结束早于开始应拒绝")
# 领料前先把 MRP 生成的采购单全部到货（保证库存够）
r = client.get("/api/purchase")
for po in r.json():
    if po["status"] in ("新建", "已下单", "部分到货") and po["order_no"] not in ("PO-OVERDUE",):
        for it in po["items"]:
            remain = it["qty"] - it["received_qty"]
            if remain > 0:
                rr = client.post("/api/purchase/receive", json={"order_no": po["order_no"], "part_code": it["part_code"], "qty": remain})
                assert rr.status_code == 200, f"到货 {po['order_no']} {it['part_code']}: {rr.text}"
# MO-002 领料（多层 BOM 叶子：RM-001 10、RM-003 10、RM-004 10、RM-005 10、RM-002 10）
# 注意：MRP 生成的半成品生产单也会占用库存；先完成那些半成品生产单的领料/完工，避免占用冲突
r = client.get("/api/production")
for mo in r.json():
    if mo["order_no"] != "MO-002" and mo["status"] == "新建" and mo["product_code"] == "SUB-001":
        rr = client.post("/api/production/consume", json={"order_no": mo["order_no"]})
        assert rr.status_code == 200, f"半成品领料 {mo['order_no']}: {rr.text}"
        rr = client.post("/api/production/complete", json={"order_no": mo["order_no"]})
        assert rr.status_code == 200, f"半成品完工 {mo['order_no']}: {rr.text}"
r = client.post("/api/production/consume", json={"order_no": "MO-002"})
check(r.status_code == 200, f"MO-002 领料（多层 BOM）：{r.text}")
r = client.post("/api/production/complete", json={"order_no": "MO-002"})
check(r.status_code == 200, f"MO-002 完工：{r.text}")
r = client.get("/api/stock")
check(abs({x["part_code"]: x["qty"] for x in r.json()}.get("FG-002", 0) - 10) < 1e-6, "FG-002 成品库存 10")

# ===== 9. Excel 导入导出（全部种类） =====
for kind in ["parts", "stock", "purchase", "bom", "supplier", "sales", "production", "mrp"]:
    r = client.get(f"/api/excel/export/{kind}")
    check(r.status_code == 200 and r.content[:2] == b"PK", f"Excel 导出 {kind} 是 xlsx（PK 开头），{len(r.content)} 字节")
for kind in ["parts", "stock", "purchase", "bom", "supplier", "sales", "production"]:
    r = client.get(f"/api/excel/template/{kind}")
    check(r.status_code == 200, f"Excel 模板 {kind}")
# 导入：供应商（别名表头）、销售、生产
wb = Workbook(); ws = wb.active
ws.append(["供应商编码", "供应商名称", "联系人", "电话"])
ws.append(["SUP-901", "导入供应商", "赵经理", "13900000001"])
buf = io.BytesIO(); wb.save(buf); buf.seek(0)
r = client.post("/api/excel/import/supplier", files={"file": ("s.xlsx", buf.read(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
check(r.status_code == 200 and r.json()["imported"] == 1, f"Excel 导入供应商：{r.json()}")
wb = Workbook(); ws = wb.active
ws.append(["销售单号", "客户", "成品编码", "数量", "交期"])
ws.append(["SO-901", "导入客户", "FG-001", 5, "2026-12-01"])
buf = io.BytesIO(); wb.save(buf); buf.seek(0)
r = client.post("/api/excel/import/sales", files={"file": ("s.xlsx", buf.read(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
check(r.status_code == 200 and r.json()["imported"] == 1, f"Excel 导入销售：{r.json()}")
wb = Workbook(); ws = wb.active
ws.append(["生产单号", "成品编码", "数量", "计划开始", "计划结束"])
ws.append(["MO-901", "FG-001", 5, "2026-11-01", "2026-11-05"])
buf = io.BytesIO(); wb.save(buf); buf.seek(0)
r = client.post("/api/excel/import/production", files={"file": ("p.xlsx", buf.read(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
check(r.status_code == 200 and r.json()["imported"] == 1, f"Excel 导入生产：{r.json()}")
# 导入灵活映射：物料编码/材料名称 仍能映射到零件编码/名称
wb = Workbook(); ws = wb.active
ws.append(["物料编码", "材料名称", "规格", "单位", "类别", "安全库存", "未税单价", "税率"])
ws.append(["RM-888", "别名测试零件", "规格", "个", "原材料", 5, 9.9, 0.13])
buf = io.BytesIO(); wb.save(buf); buf.seek(0)
r = client.post("/api/excel/import/parts", files={"file": ("p.xlsx", buf.read(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
check(r.status_code == 200 and r.json()["imported"] == 1, f"Excel 导入别名映射：{r.json()}")
# MRP 导出带销售单号参数
r = client.get("/api/excel/export/mrp", params={"sales_order_no": "SO-001"})
check(r.status_code == 200 and r.content[:2] == b"PK", "MRP 导出（按销售单）")

# ===== 10. 看板四块 =====
# 拖期看板验证：新建一张交期为过去的采购单，应出现在拖期采购与供应商排行中
r = client.post("/api/purchase", json={"order_no": "PO-OVERDUE2", "supplier_code": "SUP-002", "due_date": "2026-01-01",
      "items": [{"part_code": "RM-001", "qty": 1, "price": 2.0}]})
check(r.status_code == 200, "新建第二张拖期采购单 PO-OVERDUE2")
r = client.get("/api/dashboard")
d = r.json()
print("看板:", {k: (v["status"], v["count"]) for k, v in d.items() if isinstance(v, dict) and "status" in v})
for key in ["shortage", "overdue_purchase", "supplier_ranking", "production_shortage"]:
    check(key in d and "status" in d[key] and d[key]["status"] in ("红", "黄", "绿"), f"看板 {key} 有红黄绿状态：{d[key]['status']}")
check(d["overdue_purchase"]["status"] == "红" and any(x["order_no"] == "PO-OVERDUE2" for x in d["overdue_purchase"]["items"]),
      "看板拖期采购为红且含 PO-OVERDUE2")
check(any(x["supplier_code"] == "SUP-002" and x["overdue_count"] == 1 for x in d["supplier_ranking"]["items"]),
      "供应商拖期排行含 SUP-002（1 张拖期单）")

print("\n全部第一阶段验证通过 ✅")
import os as _os
_os.unlink(tmp.name)
