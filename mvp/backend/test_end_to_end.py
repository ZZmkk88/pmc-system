# -*- coding: utf-8 -*-
"""端到端自动验证：销售单100个 → MRP → 采购 → 到货入库 → 生产 → 成品入库100个"""
import os, sys, io
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
# 用全新库验证，避免污染已有库
import app as app_module
# 备份原 DB 路径，改用临时库
import tempfile
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()
app_module.DB_PATH = tmp.name
# 重新初始化
app_module.init_db()

from fastapi.testclient import TestClient
client = TestClient(app_module.app)

def check(cond, msg):
    print(("✅ " if cond else "❌ ") + msg)
    assert cond, msg

# 1. 零件
r = client.get("/api/parts")
check(r.status_code==200 and len(r.json())==6, f"零件 6 条，实际 {len(r.json()) if r.status_code==200 else r.text}")
# 2. BOM
r = client.get("/api/bom/FG-001")
check(len(r.json())==5, f"BOM 5 条，实际 {len(r.json())}")
# 3. BOM爆炸 100
r = client.post("/api/bom/explode", json={"parent_code":"FG-001","qty":100})
check(all(x["qty"]==100 for x in r.json()), f"BOM爆炸 100 每个都是 100：{r.json()}")
# 4. 销售单
r = client.post("/api/sales", json={"order_no":"SO-001","customer":"演示客户","product_code":"FG-001","qty":100})
check(r.status_code==200, f"创建销售单 SO-001：{r.text}")
# 5. MRP
r = client.post("/api/mrp", json={"sales_order_no":"SO-001"})
print("MRP:", r.json())
mrp = {x["part_code"]: x for x in r.json()["results"]}
# 期初：外壳20,灯珠30,电池10,开关50,电路板15，安全各20
# 净 = 100 - 库存 + 20
expected = {"RM-001":100,"RM-002":90,"RM-003":110,"RM-004":70,"RM-005":105}
for code, exp in expected.items():
    check(abs(mrp[code]["net"]-exp)<1e-6, f"MRP {code} 净需求 {mrp[code]['net']} == {exp}")
# 6. 从MRP生成采购单
r = client.post("/api/purchase/from-mrp", json={"sales_order_no":"SO-001"})
print("采购单:", r.json())
check(r.status_code==200, f"生成采购单：{r.text}")
po_no = r.json()["order_no"]
# 7. 到货入库：按净需求全部入库
for code, qty in expected.items():
    r = client.post("/api/purchase/receive", json={"order_no":po_no,"part_code":code,"qty":qty})
    check(r.status_code==200, f"到货 {code} {qty}：{r.text}")
# 检查采购单状态
r = client.get("/api/purchase")
po = [x for x in r.json() if x["order_no"]==po_no][0]
check(po["status"]=="全部到货", f"采购单全部到货：{po['status']}")
# 8. 生产单
r = client.post("/api/production", json={"order_no":"MO-001","product_code":"FG-001","qty":100,"sales_order_no":"SO-001"})
check(r.status_code==200, f"创建生产单：{r.text}")
# 9. 领料
r = client.post("/api/production/consume", json={"order_no":"MO-001"})
check(r.status_code==200, f"领料：{r.text}")
# 领料后原材料库存应为：期初+采购-领料 = 安全库存（20）
r = client.get("/api/stock")
stock = {x["part_code"]: x["qty"] for x in r.json()}
for code in expected:
    check(abs(stock[code]-20)<1e-6, f"领料后 {code} 库存 {stock[code]} == 20（安全库存）")
# 10. 完工入库
r = client.post("/api/production/complete", json={"order_no":"MO-001"})
check(r.status_code==200, f"完工：{r.text}")
r = client.get("/api/stock")
stock = {x["part_code"]: x["qty"] for x in r.json()}
check(abs(stock["FG-001"]-100)<1e-6, f"成品库存 {stock['FG-001']} == 100")
# 11. Excel 导出
for kind in ["parts","stock","purchase"]:
    r = client.get(f"/api/excel/export/{kind}")
    check(r.status_code==200 and len(r.content)>0, f"Excel 导出 {kind} 有内容 {len(r.content)} 字节")
    check(r.content[:2]==b"PK", f"Excel {kind} 是 xlsx（PK 开头）")
# 12. Excel 模板
for kind in ["parts","stock","purchase"]:
    r = client.get(f"/api/excel/template/{kind}")
    check(r.status_code==200, f"Excel 模板 {kind}")
# 13. Excel 导入：用别名表头（物料编码/材料名称）验证灵活映射
from openpyxl import Workbook
wb = Workbook(); ws = wb.active
ws.append(["物料编码","材料名称","规格","单位","类别","安全库存","未税单价","税率"])
ws.append(["RM-999","测试零件-别名","测试规格","个","原材料",5,9.9,0.13])
buf = io.BytesIO(); wb.save(buf); buf.seek(0)
r = client.post("/api/excel/import/parts", files={"file":("test.xlsx", buf.read(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
print("导入别名:", r.json())
check(r.status_code==200 and r.json()["imported"]==1, f"Excel 导入别名映射成功：{r.json()}")
r = client.get("/api/parts")
check(any(x["code"]=="RM-999" for x in r.json()), "导入的 RM-999 已在零件列表")
# 14. Excel 导入库存（用 料号 别名）
wb2 = Workbook(); ws2 = wb2.active
ws2.append(["料号","仓库","库位","批次号","数量"])
ws2.append(["RM-999","主仓","A区-01","BATCH-TEST",42])
buf2 = io.BytesIO(); wb2.save(buf2); buf2.seek(0)
r = client.post("/api/excel/import/stock", files={"file":("stock.xlsx", buf2.read(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
print("导入库存:", r.json())
check(r.status_code==200 and r.json()["imported"]==1, f"Excel 导入库存：{r.json()}")
# 15. 看板
r = client.get("/api/dashboard")
check(r.status_code==200, f"看板：{r.text[:200]}")

print("\n全部端到端验证通过 ✅")
os.unlink(tmp.name)
