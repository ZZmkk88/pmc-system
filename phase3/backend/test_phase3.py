# -*- coding: utf-8 -*-
"""PMC 第三阶段端到端自动验证（FastAPI TestClient，不依赖外部服务，全新临时库）。
覆盖：简单登录（Token/改密码/退出）+ 第一/二阶段回归（MRP/生产/审批流程）
+ 智能补货（公式复核）+ 供应商自动推荐（含紧急需求日）+ 助手查询
+ 模拟表单导入（已填好文件 4 页）+ 完整流程 + 前端响应式标记。
注意：第三阶段起接口必须带 Bearer Token，X-User 直传已废弃（无 Token 返回 401）。
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

def login(user, pw=None):
    r = client.post("/api/auth/login", json={"username": user, "password": pw or user})
    assert r.status_code == 200, f"登录 {user} 失败：{r.text}"
    return r.json()["token"]

def H(token):
    return {"Authorization": f"Bearer {token}"}

ADMIN = login("admin")
WORKER = login("worker")

def get(path, token=ADMIN, **kw):
    return client.get(path, headers=H(token), **kw)

def post(path, token=ADMIN, **kw):
    return client.post(path, token and H(token) or {}, **kw) if False else client.post(path, headers=H(token), **kw)

def put(path, token=ADMIN, **kw):
    return client.put(path, headers=H(token), **kw)

FORMS = os.path.join(os.path.dirname(__file__), "..", "模拟表单", "模拟数据-已填好.xlsx")

# ===== 1. 登录鉴权 =====
r = client.get("/api/parts")
check(r.status_code == 401, f"无 Token 访问接口 → 401：{r.status_code}")
r = client.get("/api/parts", headers={"X-User": "admin"})
check(r.status_code == 401, f"只传 X-User（已废弃）→ 401：{r.status_code}")
r = client.post("/api/auth/login", json={"username": "admin", "password": "错密码"})
check(r.status_code == 401, "密码错误 → 401")
r = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
check(r.status_code == 200 and r.json()["must_change_password"] is True, f"admin/admin 登录成功且提示首次改密码：{r.text[:200]}")
r = get("/api/auth/me")
check(r.status_code == 200 and r.json()["username"] == "admin", f"查当前登录人：{r.text[:200]}")
# 改密码流程（用 buyer 做，避免动 admin）
tok_buyer = login("buyer")
r = client.post("/api/auth/change-password", headers=H(tok_buyer),
                json={"old_password": "buyer", "new_password": "123"})
check(r.status_code == 400, "新密码 <6 位 → 400")
r = client.post("/api/auth/change-password", headers=H(tok_buyer),
                json={"old_password": "错旧密码", "new_password": "buyer123"})
check(r.status_code == 401, "旧密码错误 → 401")
r = client.post("/api/auth/change-password", headers=H(tok_buyer),
                json={"old_password": "buyer", "new_password": "buyer123"})
check(r.status_code == 200, f"改密码成功：{r.text[:200]}")
r = get("/api/parts", token=tok_buyer)
check(r.status_code == 401, "改密码后旧 Token 作废 → 401")
tok_buyer2 = login("buyer", "buyer123")
check(get("/api/purchase", token=tok_buyer2).status_code == 200, "用新密码登录成功（buyer 有采购查看权）")
check(get("/api/parts", token=tok_buyer2).status_code == 403, "buyer 没有零件查看权 → 403（RBAC 仍在生效，不是鉴权失效）")
# 注：改密码要求新密码 ≥6 位，buyer 默认密码 5 位无法通过接口“恢复”；临时测试库用完即弃，无需恢复。
# 退出登录
tok_tmp = login("auditor")
r = client.post("/api/auth/logout", headers=H(tok_tmp))
check(r.status_code == 200, "退出登录成功")
check(get("/api/parts", token=tok_tmp).status_code == 401, "退出后 Token 作废 → 401")

# ===== 2. 第一/二阶段回归：SO-001 → MRP → 推荐供应商分组采购 → 到货 → 生产 =====
r = get("/api/parts")
check(len(r.json()) == 8, f"种子零件 8 条：{len(r.json())}")
r = post("/api/sales", json={"order_no": "SO-001", "customer": "演示客户", "product_code": "FG-001", "qty": 100, "due_date": "2026-12-31"})
check(r.status_code == 200, f"创建销售单 SO-001：{r.text}")
r = post("/api/mrp", json={"sales_order_no": "SO-001"})
mrp = {x["part_code"]: x for x in r.json()["results"]}
for code, exp in {"RM-001": 100, "RM-002": 90, "RM-003": 110, "RM-004": 70, "RM-005": 105}.items():
    check(abs(mrp[code]["net"] - exp) < 1e-6, f"MRP {code} 净需求 {mrp[code]['net']} == {exp}")
r = post("/api/purchase/from-mrp", json={"sales_order_no": "SO-001"})
check(r.status_code == 200, f"从 MRP 生成采购单（推荐供应商分组）：{r.text}")
pos = r.json()["purchase_orders"]
check(len(pos) == 2, f"推荐分组生成 2 张采购单（SUP-001 组 + SUP-002 组）：{pos}")
po_map = {}
for no in pos:
    po = [x for x in get("/api/purchase").json() if x["order_no"] == no][0]
    po_map[po["supplier_code"]] = po
check(abs(po_map["SUP-001"]["total_amount"] - 1058.81) < 0.01, f"SUP-001 组含税 1058.81（90×0.8+110×5+105×3 再×1.13）：{po_map['SUP-001']['total_amount']}")
check(abs(po_map["SUP-002"]["total_amount"] - 257.64) < 0.01, f"SUP-002 组含税 257.64（100×2+70×0.4 再×1.13）：{po_map['SUP-002']['total_amount']}")
check(abs(sum(p["total_amount"] for p in po_map.values()) - 1316.45) < 0.02, "两张单合计 1316.45，比旧默认供应商分组（1380.86）便宜，推荐生效")
for no in pos:
    for it in [x for x in get("/api/purchase").json() if x["order_no"] == no][0]["items"]:
        r = post("/api/purchase/receive", json={"order_no": no, "part_code": it["part_code"], "qty": it["qty"]})
        check(r.status_code == 200, f"到货 {no} {it['part_code']} {it['qty']}：{r.text}")
    po = [x for x in get("/api/purchase").json() if x["order_no"] == no][0]
    check(po["status"] == "全部到货", f"采购单 {no} 全部到货")
r = post("/api/production", token=WORKER, json={"order_no": "MO-001", "product_code": "FG-001", "qty": 100, "sales_order_no": "SO-001", "planned_start": "2026-11-01", "planned_end": "2026-11-10"})
check(r.status_code == 200, f"创建生产单 MO-001：{r.text}")
r = post("/api/production/consume", token=WORKER, json={"order_no": "MO-001"})
check(r.status_code == 200, f"领料 MO-001：{r.text}")
r = post("/api/production/complete", token=WORKER, json={"order_no": "MO-001"})
check(r.status_code == 200, f"完工 MO-001：{r.text}")
stock = {x["part_code"]: x["qty"] for x in get("/api/stock").json()}
check(stock.get("FG-001") == 100, f"完工后 FG-001 成品 100：{stock.get('FG-001')}")

# ===== 3. 智能补货：公式复核（领料 100 个/种 → 日均 100/30） =====
r = get("/api/replenishment")
check(r.status_code == 200, "补货建议接口 200")
items = {x["part_code"]: x for x in r.json()["items"]}
for code in ["RM-001", "RM-002", "RM-003", "RM-004", "RM-005"]:
    it = items[code]
    check(abs(it["daily_avg_usage"] - 100 / 30) < 0.01, f"{code} 日均用量 {it['daily_avg_usage']} ≈ 3.33")
    check(abs(it["reorder_point"] - (100 / 30 * 7 + 20)) < 0.05, f"{code} 再订货点 {it['reorder_point']} ≈ 43.33")
    check(abs(it["target_stock"] - (40 + 100 / 30 * 7)) < 0.05, f"{code} 目标库存 {it['target_stock']} ≈ 63.33")
    check(it["available"] == 20, f"{code} 领料后可用量 20：{it['available']}")
    check(it["suggested_qty"] == 44, f"{code} 建议补货 ceil(63.33-20)=44：{it['suggested_qty']}")
    check(it["need_reorder"] is True, f"{code} 需要补货")
    check("再订货点" in it["formula"] or "日均用量" in it["formula"], f"{code} 返回公式说明")
check(items["FG-001"]["need_reorder"] is False and items["FG-001"]["suggested_qty"] == 0, "FG-001 成品库存足，不用补货")
r = get("/api/replenishment", params={"only_needed": "true"})
check(all(x["need_reorder"] for x in r.json()["items"]), "only_needed 只返回需要补货的")
# 助手查补货（生成采购单之前：应有 5 种需要补货）
r = client.post("/api/assistant/query", headers=H(ADMIN), json={"text": "哪些料需要补货"})
check(r.json()["intent"] == "查补货" and len(r.json()["data"].get("items", [])) >= 5, f"助手查补货（有需求时）：{r.text[:300]}")
# 看板含补货块（在生成采购单之前查：生成后在途量计入可用量，需求会暂时消失，是正确行为）
r = get("/api/dashboard")
check("replenishment" in r.json() and r.json()["replenishment"]["count"] >= 5, f"看板含智能补货块：{list(r.json().keys())}")
# 补货一键生成采购单：按推荐供应商分组 → 2 张
r = post("/api/replenishment/generate-purchase-orders", json={})
check(r.status_code == 200 and len(r.json()["purchase_orders"]) == 2, f"补货生成 2 张采购单：{r.text[:300]}")
repl_pos = r.json()["purchase_orders"]
totals = sorted([x["total_amount"] for x in get("/api/purchase").json() if x["order_no"] in repl_pos])
check(abs(totals[0] - 119.33) < 0.02 and abs(totals[1] - 437.54) < 0.02, f"补货采购单金额 SUP-002 组 119.33 / SUP-001 组 437.54：{totals}")
# 把补货采购单收完货，避免在途量干扰后面模拟流程的 MRP 预期数
for no in repl_pos:
    for it in [x for x in get("/api/purchase").json() if x["order_no"] == no][0]["items"]:
        r = post("/api/purchase/receive", json={"order_no": no, "part_code": it["part_code"], "qty": it["qty"]})
        check(r.status_code == 200, f"补货采购单到货 {no} {it['part_code']}：{r.text[:150]}")
# 生成采购单后，在途补货量计入可用量，补货需求暂时消失（正确行为）
r = get("/api/dashboard")
check(r.json()["replenishment"]["count"] == 0, f"补货生成采购单后，看板补货需求归零（在途已覆盖）：{r.json()['replenishment']['count']}")

# ===== 4. 供应商自动推荐 =====
r = get("/api/suppliers/compare/RM-001")
check(r.json()["cheapest_supplier"] == "SUP-002" and r.json()["recommended_supplier"] == "SUP-002",
      f"比价 RM-001：最便宜=推荐=SUP-002：{r.json()['cheapest_supplier']}/{r.json()['recommended_supplier']}")
r = client.post("/api/suppliers/recommend", headers=H(ADMIN),
                json={"part_code": "RM-003", "qty": 100, "need_date": "2026-10-20", "prefer": "balanced"})
check(r.json()["recommended_supplier"] == "SUP-002", f"紧急需求（明天要货）：最便宜的 SUP-001（7天）赶不上，推荐能按时到的 SUP-002：{r.json()}")
r = client.post("/api/suppliers/recommend", headers=H(ADMIN),
                json={"part_code": "RM-003", "qty": 100, "prefer": "cheapest"})
check(r.json()["recommended_supplier"] == "SUP-001", "只看最便宜 → SUP-001")
r = client.post("/api/suppliers/recommend", headers=H(ADMIN),
                json={"part_code": "RM-003", "qty": 100, "prefer": "fastest"})
check(r.json()["recommended_supplier"] == "SUP-002", "只看最快 → SUP-002（3天）")
# 拉黑后推荐跳过
r = post("/api/suppliers/SUP-002/blacklist")
check(r.status_code == 200, f"拉黑 SUP-002：{r.text}")
r = get("/api/suppliers/compare/RM-001")
check(r.json()["recommended_supplier"] == "SUP-001", "拉黑后推荐跳过 SUP-002，改推 SUP-001")
r = post("/api/suppliers/SUP-002/approve")
check(r.status_code == 200, "恢复 SUP-002 为合格")

# ===== 5. 助手查询 =====
r = client.post("/api/assistant/query", headers=H(ADMIN), json={"text": "查外壳还剩多少"})
check(r.status_code == 200 and r.json()["intent"] == "查库存" and "外壳" in r.json()["answer"] and "可用量" in r.json()["answer"],
      f"助手查库存：{r.text[:300]}")
r = client.post("/api/assistant/query", headers=H(ADMIN), json={"text": "哪些料需要补货"})
check(r.json()["intent"] == "查补货" and r.json()["data"].get("items") == [], f"助手查补货（补货单已生成、在途已覆盖时应报无需求）：{r.text[:300]}")
r = client.post("/api/assistant/query", headers=H(ADMIN), json={"text": "外壳找谁买最便宜"})
check(r.json()["intent"] == "查比价" and ("SUP-002" in r.json()["answer"] or "深圳电子供应商B" in r.json()["answer"]),
      f"助手查比价：{r.text[:300]}")
po_no = pos[0]
r = client.post("/api/assistant/query", headers=H(ADMIN), json={"text": f"采购单 {po_no} 到哪了"})
check(r.json()["intent"] == "查采购单" and po_no in r.json()["answer"], f"助手查采购单：{r.text[:300]}")
r = client.post("/api/assistant/query", headers=H(ADMIN), json={"text": "查火星石头还剩多少"})
check("没找到" in r.json()["answer"], f"助手查不到 → 给提示不瞎编：{r.text[:200]}")
r = client.post("/api/assistant/query", headers=H(ADMIN), json={"text": ""})
check("请问" in r.json()["answer"] or "比如" in r.json()["answer"], "助手空问题 → 给示例")

# ===== 6. 模拟表单导入（已填好文件 4 页） =====
data = open(FORMS, "rb").read()
check(os.path.exists(FORMS), f"模拟表单存在：{FORMS}")
for kind, sheet, want in [("parts", "零件", 10), ("stock", "库存期初", 10), ("bom", "BOM", 12), ("purchase", "采购", 1)]:
    r = client.post(f"/api/excel/import/{kind}?sheet={sheet}", headers=H(ADMIN),
                    files={"file": ("模拟数据-已填好.xlsx", io.BytesIO(data), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    check(r.status_code == 200 and r.json().get("imported") == want, f"导入 {kind}/{sheet}：导入 {r.json().get('imported')} 条，期望 {want}，{r.text[:200]}")
r = client.post("/api/excel/import/parts?sheet=不存在的页", headers=H(ADMIN),
                files={"file": ("x.xlsx", io.BytesIO(data), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
check(r.status_code == 400, "导入不存在的工作表 → 400")
r = get("/api/parts")
check(len(r.json()) == 10, f"导入后零件 10 条（含台灯 FG-002 + 灯罩/灯杆）：{len(r.json())}")
names = {x["code"]: x["name"] for x in r.json()}
check(names.get("FG-002") == "台灯" and names.get("RM-006") == "灯罩", f"新零件台灯/灯罩已导入：{names}")
r = get("/api/stock/available/RM-001")
check(r.json()["onhand"] == 50, f"导入库存期初后外壳现有 50：{r.json()}")
r = get("/api/bom/FG-002")
check(len(r.json()) == 6, f"导入 BOM 后台灯配方 6 条（种子 4 条 + 新增灯罩/灯杆 2 条）：{len(r.json())}")
r = get("/api/stock/available/RM-006")
check(r.json()["in_transit"] == 10, f"导入采购模拟表后灯罩在途 10：{r.json()}")
# 模拟表单完整流程：SO-DEMO-001 小夜灯×100 → MRP → 生成 → 到货 → 生产 → 成品
r = post("/api/sales", json={"order_no": "SO-DEMO-001", "customer": "模拟客户", "product_code": "FG-001", "qty": 100, "due_date": "2026-12-31"})
check(r.status_code == 200, f"模拟销售单：{r.text}")
r = post("/api/mrp", json={"sales_order_no": "SO-DEMO-001"})
mrp2 = {x["part_code"]: x for x in r.json()["results"]}
for code, exp in {"RM-001": 70, "RM-002": 80, "RM-003": 90, "RM-004": 60, "RM-005": 95}.items():
    check(abs(mrp2[code]["net"] - exp) < 1e-6, f"模拟 MRP {code} 净需求 {mrp2[code]['net']} == {exp}")
r = post("/api/mrp/generate", json={"sales_order_no": "SO-DEMO-001"})
check(r.status_code == 200 and len(r.json()["purchase_orders"]) == 2, f"模拟 MRP 生成 2 张采购单：{r.text[:300]}")
demo_pos = r.json()["purchase_orders"]
demo_map = {}
for no in demo_pos:
    po = [x for x in get("/api/purchase").json() if x["order_no"] == no][0]
    demo_map[po["supplier_code"]] = po
check(abs(demo_map["SUP-001"]["total_amount"] - 902.87) < 0.01, f"模拟 SUP-001 组 902.87：{demo_map['SUP-001']['total_amount']}")
check(abs(demo_map["SUP-002"]["total_amount"] - 185.32) < 0.01, f"模拟 SUP-002 组 185.32：{demo_map['SUP-002']['total_amount']}")
for no in demo_pos:
    for it in [x for x in get("/api/purchase").json() if x["order_no"] == no][0]["items"]:
        r = post("/api/purchase/receive", json={"order_no": no, "part_code": it["part_code"], "qty": it["qty"]})
        check(r.status_code == 200, f"模拟到货 {no} {it['part_code']}：{r.text[:150]}")
r = post("/api/production", token=WORKER, json={"order_no": "MO-DEMO-001", "product_code": "FG-001", "qty": 100, "sales_order_no": "SO-DEMO-001"})
check(r.status_code == 200, f"模拟生产单：{r.text}")
r = post("/api/production/consume", token=WORKER, json={"order_no": "MO-DEMO-001"})
check(r.status_code == 200, f"模拟领料：{r.text[:200]}")
r = post("/api/production/complete", token=WORKER, json={"order_no": "MO-DEMO-001"})
check(r.status_code == 200, f"模拟完工：{r.text}")
r = get("/api/stock/available/FG-001")
check(r.json()["onhand"] == 100, f"模拟流程完工后小夜灯成品 100：{r.json()}")
# 台灯 MRP（可选演示）：库存够，只需生产半成品
r = post("/api/sales", json={"order_no": "SO-DEMO-002", "customer": "模拟客户", "product_code": "FG-002", "qty": 10, "due_date": "2026-12-31"})
check(r.status_code == 200, "模拟销售单 台灯×10")
r = post("/api/mrp", json={"sales_order_no": "SO-DEMO-002"})
mrp3 = {x["part_code"]: x for x in r.json()["results"]}
check(abs(mrp3["SUB-001"]["net"] - 10) < 1e-6 and mrp3["SUB-001"]["need_type"] == "生产", f"台灯 MRP：半成品需生产 10：{mrp3.get('SUB-001')}")
check(abs(mrp3["RM-006"]["net"] - 0) < 1e-6, f"台灯 MRP：灯罩库存+在途够用，净需求 0：{mrp3.get('RM-006')}")

# ===== 7. 前端：响应式 + 登录页 =====
r = client.get("/")
check(r.status_code == 200 and "viewport" in r.text, "前端含 viewport")
check("@media (max-width:640px)" in r.text, "前端含手机 media query")
check("登录" in r.text and "助手查询" in r.text and "智能补货" in r.text, "前端含登录/助手/补货区")
r = client.get("/health")
check(r.status_code == 200 and r.json()["ok"] is True, "健康检查 200")

print(f"\n全部第三阶段验证通过 ✅（共 {PASSED[0]} 项 ✅）")
