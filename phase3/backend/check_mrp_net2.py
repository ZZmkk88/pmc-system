"""复测 P0-3 精确场景（复刻审核报告 SO-DEMO-002 的数字）：
  现有 120、已分配 100、可用 20、毛需求 100、安全 20
  修复后 MRP 应返回 net = 100 - 20 + 20 = 100（老口径用现有量会得 0）。

运行：F:\pmc-system-main\phase3\.venv\Scripts\python.exe check_mrp_net2.py
"""
import os, sys, tempfile

_here = os.path.dirname(os.path.abspath(__file__))
# 工作区根目录（旁边有 fix\phase3）或项目 backend 目录（上一级就是 phase3）都能跑
_base = os.path.join(_here, "fix", "phase3") if os.path.isdir(os.path.join(_here, "fix", "phase3")) else os.path.dirname(_here)
sys.path.insert(0, _base)
import backend.app as app_module
from fastapi.testclient import TestClient

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
app_module.DB_PATH = tmp.name
app_module.init_db()
c = TestClient(app_module.app)
tok = c.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
H = {"Authorization": "Bearer " + tok}

print("初始 RM-001:", c.get("/api/stock/available/RM-001", headers=H).json())

# 补到现有 120
print("入库 100:", c.post("/api/stock/in", headers=H, json={"part_code": "RM-001", "qty": 100, "warehouse": "主仓"}).status_code)
# 建生产单占 100
print("生产单 FG-001 x100:", c.post("/api/production", headers=H, json={"product_code": "FG-001", "qty": 100}).text[:160])
print("占料后 RM-001:", c.get("/api/stock/available/RM-001", headers=H).json())

# 销售单 qty 100 -> RM-001 毛需求 100
print("销售单 SO-CHECK x100:", c.post("/api/sales", headers=H, json={"order_no": "SO-CHECK", "qty": 100, "due_date": "2026-12-31"}).status_code)
r = c.post("/api/mrp", headers=H, json={"sales_order_no": "SO-CHECK"})
print("MRP:", r.status_code)
for row in r.json().get("results", []):
    if row.get("part_code") == "RM-001":
        g, a, s, n = float(row["gross"]), float(row["available"]), float(row["safety"]), float(row["net"])
        oh = float(row.get("onhand", 0))
        print("MRP RM-001:", {k: row[k] for k in ("gross", "onhand", "available", "safety", "net")})
        print("新公式: %.2f - %.2f + %.2f = %.2f ；实际 net = %.2f ；%s" % (g, a, s, g - a + s, n, "一致" if abs(n - (g - a + s)) < 1e-6 else "不一致"))
        print("老公式(现有量): %.2f - %.2f + %.2f = %.2f" % (g, oh, s, g - oh + s))
        break
