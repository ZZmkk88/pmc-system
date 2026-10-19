"""修复回归测试（针对审核报告里的 P0/P1 问题逐条验证）。

运行：cd phase3 && ..\\.venv\\Scripts\\python.exe backend\\test_fixes.py
用临时数据库，不碰真实 pmc_phase3.db。
"""
import os, sys, tempfile, threading, io
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backend.app as app_module
from fastapi.testclient import TestClient

PASS = 0
FAIL = []

def check(cond, msg):
    global PASS
    if cond:
        PASS += 1
        print("[OK] " + msg)
    else:
        FAIL.append(msg)
        print("[FAIL] " + msg)

def login(c, user):
    r = c.post("/api/auth/login", json={"username": user, "password": user})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    return {"Authorization": "Bearer " + tok}

def make_xlsx(headers, rows):
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = "零件"
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return buf.getvalue()

def main():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    app_module.DB_PATH = tmp.name
    app_module.init_db()
    c = TestClient(app_module.app)
    admin = login(c, "admin")

    # ---------- P0-2：一个坏请求不再卡死写库 ----------
    r = c.post("/api/stock/in", headers=admin, json={"part_code": "RM-001", "qty": "abc"})
    check(r.status_code == 422, "P0-2 数量非数字 -> 422（不再 500），实际 %s %s" % (r.status_code, r.text[:120]))
    r = c.post("/api/purchase", headers=admin, json={"supplier_code": "SUP-001", "items": [{"part_code": "RM-001", "qty": "x"}]})
    check(r.status_code == 422, "P0-2 采购明细数量非数字 -> 422，实际 %s" % r.status_code)
    r = c.post("/api/stock/in", headers=admin, json={"part_code": "RM-001", "qty": 5, "warehouse": "主仓"})
    check(r.status_code == 200, "P0-2 坏请求之后写操作仍正常（连接不再泄漏卡库），实际 %s %s" % (r.status_code, r.text[:120]))

    # ---------- P0-4：越权读/跨公司看板/审计日志 ----------
    r = c.post("/api/companies", headers=admin, json={"code": "COMP-T2", "name": "测试二公司"})
    r = c.post("/api/parts", headers=admin, json={"code": "RM-T2", "name": "二公司零件", "safety_stock": 1000, "company_code": "COMP-T2"})
    check(r.status_code == 200, "P0-4 准备数据：COMP-T2 零件创建成功")
    c.post("/api/stock/in", headers=admin, json={"part_code": "RM-T2", "qty": 5, "warehouse": "主仓", "company_code": "COMP-T2"})
    keeper = login(c, "keeper")
    r = c.get("/api/stock/available/RM-T2", headers=keeper)
    check(r.status_code == 403, "P0-4 仓库员读别公司零件可用量 -> 403（原来 200），实际 %s" % r.status_code)
    r = c.get("/api/dashboard", headers=keeper)
    body = r.text
    check(r.status_code == 200 and "RM-T2" not in body, "P0-4 仓库员看板不含别公司零件（原来会跨公司扫板），实际 %s，含RM-T2=%s" % (r.status_code, "RM-T2" in body))
    r = c.get("/api/dashboard", headers=admin)
    check(r.status_code == 200 and "RM-T2" in r.text, "P0-4 管理员看板仍能看到全部公司")
    worker = login(c, "worker")
    check(c.get("/api/audit-log", headers=worker).status_code == 403, "P0-4 生产员看审计日志 -> 403（原来 200）")
    auditor = login(c, "auditor")
    check(c.get("/api/audit-log", headers=auditor).status_code == 200, "P0-4 审计员看审计日志 -> 200")
    check(c.post("/api/mrp", headers=worker, json={"sales_order_no": "SO-001"}).status_code == 403,
          "P0-4 生产员跑 MRP -> 403（需要采购/生产计划权限）")

    # ---------- P0-3：MRP 口径含已分配/冻结 ----------
    admin_so = c.post("/api/sales", headers=admin, json={"order_no": "SO-T3", "qty": 100, "due_date": "2026-12-31"})
    check(admin_so.status_code == 200, "P0-3 准备销售单 SO-T3")
    mo = c.post("/api/production", headers=admin, json={"product_code": "FG-001", "qty": 1})
    check(mo.status_code == 200, "P0-3 准备生产单（占用 RM-001..005）")
    r = c.post("/api/mrp", headers=admin, json={"sales_order_no": "SO-T3"})
    check(r.status_code == 200, "P0-3 MRP 运行成功")
    rows = {x["part_code"]: x for x in r.json().get("results", [])}
    if "RM-001" in rows:
        row = rows["RM-001"]
        check("available" in row, "P0-3 MRP 结果新增 available（可用量）字段")
        check(abs(row["net"] - max(0, row["gross"] - row["available"] + row["safety"])) < 1e-6,
              "P0-3 净需求 = 毛需求 - 可用量 + 安全库存（含已分配/冻结）")
    else:
        check(False, "P0-3 MRP 结果里应有 RM-001")

    # ---------- P1-1：并发取号不重复 ----------
    conn = app_module.get_conn()
    nos = []
    lock = threading.Lock()
    def worker_no():
        c2 = app_module.get_conn()
        try:
            n = app_module.next_doc_no(c2, "PO")
            with lock:
                nos.append(n)
        finally:
            c2.close()
    threads = [threading.Thread(target=worker_no) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    conn.close()
    check(len(set(nos)) == 8, "P1-1 8 线程并发取号不重复，实际 %d 个唯一号：%s" % (len(set(nos)), sorted(nos)))

    # ---------- P1-3：Excel 只导入出现的列，不把没导入的列冲成 0 ----------
    before = c.get("/api/parts/RM-001").json() if False else None
    conn = app_module.get_conn()
    row = conn.execute("SELECT price, tax_rate, lead_time_days FROM parts WHERE code='RM-001'").fetchone()
    price0, tax0, lead0 = float(row["price"]), float(row["tax_rate"]), int(row["lead_time_days"])
    conn.close()
    xlsx = make_xlsx(["零件编码", "名称"], [["RM-001", "外壳-改名"]])
    r = c.post("/api/excel/import/parts", headers=admin,
               files={"file": ("p.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    check(r.status_code == 200, "P1-3 只含两列的 Excel 导入成功，实际 %s %s" % (r.status_code, r.text[:120]))
    conn = app_module.get_conn()
    row = conn.execute("SELECT price, tax_rate, lead_time_days FROM parts WHERE code='RM-001'").fetchone()
    conn.close()
    check(abs(float(row["price"]) - price0) < 1e-9 and abs(float(row["tax_rate"]) - tax0) < 1e-9 and int(row["lead_time_days"]) == lead0,
          "P1-3 未出现在 Excel 的单价/税率/提前期保持原值（原来会冲成 0）")

    # ---------- P1-6：生产完工状态与入库同一事务 ----------
    mo_no = mo.json()["order_no"]
    r = c.post("/api/production/consume", headers=worker, json={"order_no": mo_no})
    check(r.status_code == 200, "P1-6 领料成功，实际 %s %s" % (r.status_code, r.text[:160]))
    r = c.post("/api/production/complete", headers=worker, json={"order_no": mo_no})
    check(r.status_code == 200, "P1-6 完工成功")
    conn = app_module.get_conn()
    st = conn.execute("SELECT status FROM production_orders WHERE order_no=?", (mo_no,)).fetchone()["status"]
    finished = float(conn.execute("SELECT COALESCE(SUM(qty),0) FROM stock WHERE part_code='FG-001'").fetchone()[0])
    conn.close()
    check(st == "已完工" and finished > 0, "P1-6 完工状态与成品入库同时生效（状态=%s，FG-001 库存=%.1f）" % (st, finished))

    # ---------- 模板回导（原审核报告的 500 场景） ----------
    tpl = c.get("/api/excel/template/parts", headers=admin)
    r = c.post("/api/excel/import/parts", headers=admin,
               files={"file": ("t.xlsx", tpl.content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    check(r.status_code == 200, "模板原样回导 200（不再 database is locked），实际 %s" % r.status_code)

    # ---------- P2-4 / P1-9：前端做单页（静态页自检） ----------
    page = c.get("/").text
    tabs = ["看板", "零件", "库存", "采购", "生产", "销售", "MRP 运算", "智能补货", "报表", "审计日志", "导入导出", "助手查询"]
    check(all(t in page for t in tabs), "P2-4 前端含 12 个页签（做单闭环）")
    check("html:true" in page and "function table(" in page, "P2-4 前端表格渲染（操作按钮列不再被转义）")
    check("首次登录，请先改密码" in page, "P1-9 前端强制首改密卡片存在")
    r = c.get("/api/reports/stock_balance.xlsx", headers=admin)
    check(r.status_code == 200 and r.content[:2] == b"PK", "P2-4 报表 xlsx 导出返回真实 Excel（PK 头）")

    print("\n%d 项通过，%d 项失败" % (PASS, len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)

if __name__ == "__main__":
    main()
