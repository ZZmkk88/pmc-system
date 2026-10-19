# -*- coding: utf-8 -*-
"""
PMC 系统 第一阶段（完整版）- 后端
技术：Python + FastAPI + SQLite（大白话：轻量单文件数据库，第一阶段先用它，后期再切 PostgreSQL）
由 MVP（~/workspace/pmc/mvp）复制扩展而来，MVP 已验证的端到端流程保持兼容。

参考出处（先查再做，严禁猜测，只学思路、自己重写，未复制 GPL 代码）：
- MRP 三步：BOM 爆炸 → 对库存净算 → 按提前期偏移（大白话：算出最晚哪天必须下单）
  来源：https://dev.to/seven_billion_ffd761d9574/mrp-is-correct-and-still-wrong-a-worked-example-d0l
  来源：https://www.brahmin-solutions.com/blog/how-to-do-mrp
  来源：https://www.inventorypath.com/material-requirements-planning-objectives/
  来源：https://docs.namasoft.com/modules/manufacturing/material-requirements-planning.html
- 净需求公式：净需求 = 毛需求 - 可用库存 - 在途 + 安全库存（MVP 已验证，见 MVP 验证报告）
- 可用量：可用量 = 现有 + 在途 - 已分配 - 冻结（大白话：手头有的 + 正在路上的 - 已被生产单占的 - 被质检/盘点冻住的）
  思路来源：ERPNext 预计库存（Projected Quantity）= 现有 - 预留 + 在途，见 https://erpnext.com/erp-guide/inventory-management
  思路来源：库存状态分层（在手/已分配/质检冻结不能混用），见 https://xorosoft.com/erp-inventory-cutover/
  思路来源：库存流水（Stock Ledger）+ 汇总分开，见 https://erpnext.com/erp-guide/inventory-management
- 多层 BOM + 版本管理 + 循环检测：通用 BOM 做法（BOM 版本/生效日期、递归展开、路径判重），见 MVP 验证报告与方案第 4.2.1 节
- 供应商比价 / 准时率：采购时按价格、交期排序，准时率 = 按期到货单数 / 总到货单数（通用采购做法）
- 单据流：采购申请 → 审批（一级）→ 采购单 → 到货 → 质检可选 → 入库 → 对账（方案第 4.2.4 节）
注意：本代码为独立重写，未复制 ERPNext（GPLv3）/ InvenTree 源码原文。
"""
import os
import sqlite3
import io
from datetime import date, datetime, timedelta
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 大白话：数据库文件路径，第一阶段用 SQLite 单文件，放在 backend 目录下
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "pmc_phase1.db")
TODAY = date.today().isoformat()

app = FastAPI(title="PMC 系统 第一阶段", description="生产物料控制（大白话：管缺啥料、买多少、什么时候买）第一阶段完整版")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ---------- 数据库 ----------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def parse_date(s):
    """把 'YYYY-MM-DD' 解析成 date，解析失败返回 None（大白话：日期写错了就当没写）"""
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None

def init_db():
    """建表 + 种子数据（第一次运行时把表建好，并放进小夜灯例子数据）"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, spec TEXT DEFAULT '', unit TEXT DEFAULT '个',
            category TEXT DEFAULT '原材料', is_purchase INTEGER DEFAULT 1,
            safety_stock REAL DEFAULT 0, min_stock REAL DEFAULT 0,
            max_stock REAL DEFAULT 0,
            lead_time_days INTEGER DEFAULT 7, price REAL DEFAULT 0,
            tax_rate REAL DEFAULT 0.13, supplier_code TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        )
    """)
    # BOM 版本表：一个成品可以有多个版本（大白话：配方改了就升一个版本，老版本留着对比）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bom_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_code TEXT NOT NULL, version TEXT NOT NULL,
            effective_date TEXT DEFAULT '', status TEXT DEFAULT '生效',
            remark TEXT DEFAULT '',
            UNIQUE(parent_code, version)
        )
    """)
    # BOM 明细：版本号 + 生效日期，同一父件同一版本下子件唯一
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bom_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_code TEXT NOT NULL, child_code TEXT NOT NULL,
            qty REAL NOT NULL, loss_rate REAL DEFAULT 0,
            version TEXT DEFAULT 'V1', effective_date TEXT DEFAULT '',
            remark TEXT DEFAULT '',
            UNIQUE(parent_code, child_code, version)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            part_code TEXT NOT NULL, warehouse TEXT DEFAULT '主仓',
            location TEXT DEFAULT '', batch TEXT DEFAULT '', qty REAL DEFAULT 0,
            UNIQUE(part_code, warehouse, location, batch)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            part_code TEXT NOT NULL, warehouse TEXT DEFAULT '主仓',
            batch TEXT DEFAULT '', change_qty REAL NOT NULL,
            type TEXT NOT NULL, ref_no TEXT DEFAULT '',
            remark TEXT DEFAULT '', created_at TEXT DEFAULT ''
        )
    """)
    # 冻结表：被质检/盘点等原因冻住的量（大白话：货在仓库，但暂时不能用）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS frozen_stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            part_code TEXT NOT NULL, qty REAL NOT NULL,
            reason TEXT DEFAULT '', ref_no TEXT DEFAULT '', created_at TEXT DEFAULT ''
        )
    """)
    # 已分配表：生产单占用的量（大白话：已经分给某个生产单了，别人不能再算进去）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref_no TEXT NOT NULL, part_code TEXT NOT NULL,
            qty REAL NOT NULL, created_at TEXT DEFAULT ''
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            contact TEXT DEFAULT '', phone TEXT DEFAULT '',
            address TEXT DEFAULT '', payment TEXT DEFAULT '',
            lead_time_days INTEGER DEFAULT 7, rating TEXT DEFAULT '合格',
            status TEXT DEFAULT '合格'
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS supplier_parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_code TEXT NOT NULL, part_code TEXT NOT NULL,
            price REAL DEFAULT 0, tax_rate REAL DEFAULT 0.13,
            lead_time_days INTEGER DEFAULT 7, min_order_qty REAL DEFAULT 0,
            UNIQUE(supplier_code, part_code)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sales_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT UNIQUE NOT NULL, customer TEXT DEFAULT '',
            product_code TEXT NOT NULL, qty REAL NOT NULL,
            due_date TEXT DEFAULT '', status TEXT DEFAULT '新建', created_at TEXT DEFAULT ''
        )
    """)
    # 采购申请表（一级审批：新建 → 已提交 → 已批准/已拒绝 → 已转采购单）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS purchase_requisitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            req_no TEXT UNIQUE NOT NULL, applicant TEXT DEFAULT '',
            reason TEXT DEFAULT '', status TEXT DEFAULT '新建', created_at TEXT DEFAULT ''
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS purchase_requisition_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            req_no TEXT NOT NULL, part_code TEXT NOT NULL,
            qty REAL NOT NULL, price REAL DEFAULT 0, supplier_code TEXT DEFAULT ''
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS purchase_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT UNIQUE NOT NULL, supplier_code TEXT NOT NULL,
            status TEXT DEFAULT '新建', due_date TEXT DEFAULT '',
            total_amount REAL DEFAULT 0, created_at TEXT DEFAULT '',
            received_date TEXT DEFAULT '', invoice_no TEXT DEFAULT '',
            invoice_amount REAL DEFAULT 0, reconciled INTEGER DEFAULT 0,
            remark TEXT DEFAULT ''
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS purchase_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT NOT NULL, part_code TEXT NOT NULL,
            qty REAL NOT NULL, price REAL DEFAULT 0,
            received_qty REAL DEFAULT 0, tax_rate REAL DEFAULT 0.13
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS production_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT UNIQUE NOT NULL, product_code TEXT NOT NULL,
            qty REAL NOT NULL, status TEXT DEFAULT '新建',
            sales_order_no TEXT DEFAULT '', created_at TEXT DEFAULT '',
            planned_start TEXT DEFAULT '', planned_end TEXT DEFAULT ''
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mrp_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sales_order_no TEXT NOT NULL, part_code TEXT NOT NULL,
            part_name TEXT DEFAULT '', gross REAL DEFAULT 0,
            onhand REAL DEFAULT 0, in_transit REAL DEFAULT 0,
            safety REAL DEFAULT 0, net REAL DEFAULT 0,
            adjusted_net REAL, level INTEGER DEFAULT 1,
            need_type TEXT DEFAULT '采购', latest_order_date TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        )
    """)
    conn.commit()

    cnt = cur.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
    if cnt == 0:
        now = now_str()
        parts = [
            ("FG-001", "小夜灯", "LED小夜灯成品", "个", "成品", 0, 0, 0, 0, 3, 0, 0.13, ""),
            ("FG-002", "小夜灯Pro", "LED小夜灯Pro成品（用半成品电路板组件）", "个", "成品", 0, 0, 0, 0, 3, 0, 0.13, ""),
            ("SUB-001", "电路板组件", "半成品：电路板+灯珠组装", "个", "半成品", 0, 0, 0, 0, 2, 0, 0.13, ""),
            ("RM-001", "外壳", "小夜灯外壳 白色", "个", "原材料", 1, 20, 10, 200, 7, 2.5, 0.13, "SUP-001"),
            ("RM-002", "LED灯珠", "LED灯珠 3W", "个", "原材料", 1, 20, 10, 200, 7, 0.8, 0.13, "SUP-001"),
            ("RM-003", "电池", "锂电池 18650", "个", "原材料", 1, 20, 10, 200, 7, 5.0, 0.13, "SUP-001"),
            ("RM-004", "开关", "轻触开关", "个", "原材料", 1, 20, 10, 200, 7, 0.5, 0.13, "SUP-001"),
            ("RM-005", "电路板", "小夜灯电路板", "个", "原材料", 1, 20, 10, 200, 7, 3.0, 0.13, "SUP-001"),
        ]
        for p in parts:
            cur.execute("INSERT INTO parts (code,name,spec,unit,category,is_purchase,safety_stock,min_stock,max_stock,lead_time_days,price,tax_rate,supplier_code,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*p, now))
        # BOM 版本
        for parent, ver, eff, st in [("FG-001","V1","2026-01-01","生效"), ("FG-001","V2","2099-01-01","未生效"),
                                     ("FG-002","V1","2026-01-01","生效"), ("SUB-001","V1","2026-01-01","生效")]:
            cur.execute("INSERT INTO bom_versions (parent_code,version,effective_date,status,remark) VALUES (?,?,?,?,?)",
                        (parent, ver, eff, st, "V2：LED灯珠用量 1→2（演示 BOM 对比）" if ver == "V2" else ""))
        # FG-001 V1：单层，和 MVP 完全一致（保证 MVP 流程不破坏）
        for child in ["RM-001","RM-002","RM-003","RM-004","RM-005"]:
            cur.execute("INSERT INTO bom_items (parent_code,child_code,qty,loss_rate,version,effective_date) VALUES (?,?,?,?,?,?)", ("FG-001", child, 1, 0, "V1", "2026-01-01"))
        # FG-001 V2：仅 LED 灯珠用量改为 2（演示对比用，生效日期在未来，默认不生效）
        for child in ["RM-001","RM-002","RM-003","RM-004","RM-005"]:
            cur.execute("INSERT INTO bom_items (parent_code,child_code,qty,loss_rate,version,effective_date) VALUES (?,?,?,?,?,?)",
                        ("FG-001", child, 2 if child == "RM-002" else 1, 0, "V2", "2099-01-01"))
        # FG-002 V1：多层 BOM（成品 → 半成品 SUB-001 → 原材料）
        for child, q in [("SUB-001",1),("RM-001",1),("RM-003",1),("RM-004",1)]:
            cur.execute("INSERT INTO bom_items (parent_code,child_code,qty,loss_rate,version,effective_date) VALUES (?,?,?,?,?,?)", ("FG-002", child, q, 0, "V1", "2026-01-01"))
        # SUB-001 V1：半成品 BOM
        for child, q in [("RM-005",1),("RM-002",1)]:
            cur.execute("INSERT INTO bom_items (parent_code,child_code,qty,loss_rate,version,effective_date) VALUES (?,?,?,?,?,?)", ("SUB-001", child, q, 0, "V1", "2026-01-01"))
        # 供应商：SUP-001 默认，SUP-002 用于比价（部分便宜、部分贵、交期不同）
        cur.execute("INSERT INTO suppliers (code,name,contact,phone,address,payment,lead_time_days,rating,status) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("SUP-001","深圳光电供应商A","张经理","13800000001","深圳市宝安区","月结30天",7,"合格","合格"))
        cur.execute("INSERT INTO suppliers (code,name,contact,phone,address,payment,lead_time_days,rating,status) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("SUP-002","深圳电子供应商B","李经理","13800000002","深圳市南山区","月结15天",5,"合格","合格"))
        for code, price in [("RM-001",2.5),("RM-002",0.8),("RM-003",5.0),("RM-004",0.5),("RM-005",3.0)]:
            cur.execute("INSERT INTO supplier_parts (supplier_code,part_code,price,tax_rate,lead_time_days,min_order_qty) VALUES (?,?,?,?,?,?)", ("SUP-001", code, price, 0.13, 7, 0))
        for code, price, lt in [("RM-001",2.0,5),("RM-002",0.9,5),("RM-003",6.0,3),("RM-004",0.4,5),("RM-005",3.5,5)]:
            cur.execute("INSERT INTO supplier_parts (supplier_code,part_code,price,tax_rate,lead_time_days,min_order_qty) VALUES (?,?,?,?,?,?)", ("SUP-002", code, price, 0.13, lt, 0))
        init_stock = [("FG-001",0),("FG-002",0),("SUB-001",0),("RM-001",20),("RM-002",30),("RM-003",10),("RM-004",50),("RM-005",15)]
        for code, qty in init_stock:
            cur.execute("INSERT INTO stock (part_code,warehouse,location,batch,qty) VALUES (?,?,?,?,?)", (code,"主仓","A区-01","BATCH-20261019",qty))
            if qty > 0:
                cur.execute("INSERT INTO stock_ledger (part_code,warehouse,batch,change_qty,type,ref_no,remark,created_at) VALUES (?,?,?,?,?,?,?,?)",
                            (code,"主仓","BATCH-20261019",qty,"期初","期初","第一阶段种子期初",now))
        conn.commit()
    conn.close()

init_db()

# ---------- 工具函数 ----------
def row_to_dict(r):
    return dict(r) if r else None

def get_onhand(part_code: str) -> float:
    conn = get_conn()
    v = conn.execute("SELECT COALESCE(SUM(qty),0) FROM stock WHERE part_code=?", (part_code,)).fetchone()[0]
    conn.close()
    return float(v or 0)

def get_in_transit(part_code: str) -> float:
    """在途：已下采购单但还没到货的（大白话：已经买了，还在路上）"""
    conn = get_conn()
    v = conn.execute("""
        SELECT COALESCE(SUM(poi.qty - poi.received_qty),0)
        FROM purchase_order_items poi JOIN purchase_orders po ON po.order_no = poi.order_no
        WHERE poi.part_code=? AND po.status IN ('新建','已下单','部分到货')
    """, (part_code,)).fetchone()[0]
    conn.close()
    return float(v or 0)

def get_allocated(part_code: str) -> float:
    """已分配：已被未领料生产单占用的量"""
    conn = get_conn()
    v = conn.execute("SELECT COALESCE(SUM(qty),0) FROM allocations WHERE part_code=?", (part_code,)).fetchone()[0]
    conn.close()
    return float(v or 0)

def get_frozen(part_code: str) -> float:
    """冻结：被质检/盘点冻住、暂时不能用的量"""
    conn = get_conn()
    v = conn.execute("SELECT COALESCE(SUM(qty),0) FROM frozen_stock WHERE part_code=?", (part_code,)).fetchone()[0]
    conn.close()
    return float(v or 0)

def get_available(part_code: str) -> float:
    """可用量 = 现有 + 在途 - 已分配 - 冻结（核心公式，第一阶段新增）"""
    return get_onhand(part_code) + get_in_transit(part_code) - get_allocated(part_code) - get_frozen(part_code)

def stock_status(available: float, safety: float, max_stock: float) -> str:
    """红黄绿：可用量<=0 红（断货），<安全库存 黄（预警），>最高库存 黄（积压），其余 绿"""
    if available <= 0:
        return "红"
    if available < safety:
        return "黄"
    if max_stock and max_stock > 0 and available > max_stock:
        return "黄"
    return "绿"

def add_ledger(part_code, qty_change, typ, ref_no, remark="", warehouse="主仓", batch="BATCH-20261019"):
    conn = get_conn()
    conn.execute("INSERT INTO stock_ledger (part_code,warehouse,batch,change_qty,type,ref_no,remark,created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (part_code, warehouse, batch, qty_change, typ, ref_no, remark, now_str()))
    conn.commit()
    conn.close()

def adjust_stock(part_code, delta, typ, ref_no, warehouse="主仓", location="A区-01", batch="BATCH-20261019"):
    """调库存（大白话：入库就加，出库就减），同时记流水"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM stock WHERE part_code=? AND warehouse=? AND location=? AND batch=?", (part_code, warehouse, location, batch)).fetchone()
    if row:
        conn.execute("UPDATE stock SET qty=qty+? WHERE id=?", (delta, row["id"]))
    else:
        conn.execute("INSERT INTO stock (part_code,warehouse,location,batch,qty) VALUES (?,?,?,?,?)", (part_code, warehouse, location, batch, delta))
    conn.commit()
    conn.close()
    add_ledger(part_code, delta, typ, ref_no, f"{typ} {delta}", warehouse, batch)

def resolve_bom_version(conn, parent_code: str, version: Optional[str] = None) -> str:
    """解析 BOM 版本：指定了就用指定的；没指定就用“生效日期 <= 今天”的最新版本；都没有就用最新版本"""
    if version:
        row = conn.execute("SELECT version FROM bom_items WHERE parent_code=? AND version=? LIMIT 1", (parent_code, version)).fetchone()
        if row:
            return version
        raise HTTPException(404, f"父件 {parent_code} 没有 BOM 版本 {version}")
    row = conn.execute("SELECT version FROM bom_items WHERE parent_code=? AND (effective_date='' OR effective_date<=?) ORDER BY effective_date DESC, version DESC LIMIT 1",
                       (parent_code, TODAY)).fetchone()
    if row:
        return row["version"]
    row = conn.execute("SELECT version FROM bom_items WHERE parent_code=? ORDER BY version DESC LIMIT 1", (parent_code,)).fetchone()
    return row["version"] if row else "V1"

def bom_explode(conn, parent: str, qty: float, version: Optional[str] = None):
    """
    多层 BOM 爆炸（大白话：成品 → 半成品 → 原材料，一层层展开成最终要买的料）
    返回：[{"part_code","qty","level","is_leaf"}]，level=1 是直接子件，level 越大越底层。
    循环检测：用“当前路径”判重（大白话：同一条线上出现两次才算循环，菱形结构不算循环）。
    """
    ver = resolve_bom_version(conn, parent, version)
    out = []
    def _walk(code, need, level, path):
        if code in path:
            raise HTTPException(400, f"BOM 循环引用：{' -> '.join(list(path) + [code])}，请检查 BOM")
        rows = conn.execute("SELECT * FROM bom_items WHERE parent_code=? AND version=? ORDER BY child_code", (code, ver if code == parent else resolve_bom_version(conn, code, None))).fetchall() if code != parent else conn.execute("SELECT * FROM bom_items WHERE parent_code=? AND version=? ORDER BY child_code", (code, ver)).fetchall()
        # 注意：子件自己的 BOM 版本用各自的生效版本（resolve），父件用指定版本
        if not rows:
            out.append({"part_code": code, "qty": need, "level": level, "is_leaf": True})
            return
        for r in rows:
            child_need = need * float(r["qty"]) * (1 + float(r["loss_rate"] or 0))
            child = r["child_code"]
            child_rows = conn.execute("SELECT 1 FROM bom_items WHERE parent_code=? LIMIT 1", (child,)).fetchone()
            if child_rows:
                out.append({"part_code": child, "qty": child_need, "level": level + 1, "is_leaf": False})
                _walk(child, child_need, level + 1, path + (code,))
            else:
                out.append({"part_code": child, "qty": child_need, "level": level + 1, "is_leaf": True})
    _walk(parent, qty, 0, ())
    # 去掉根节点本身（level 0 不会出现在 out 里，因为 _walk 只记录子件），合并同零件同层级
    merged: Dict[tuple, float] = {}
    meta: Dict[tuple, dict] = {}
    for it in out:
        key = (it["part_code"], it["level"], it["is_leaf"])
        merged[key] = merged.get(key, 0) + it["qty"]
        meta[key] = it
    return [{"part_code": k[0], "qty": v, "level": k[1], "is_leaf": k[2]} for k, v in merged.items()]

def bom_leaf_explode(conn, parent: str, qty: float, version: Optional[str] = None):
    """只取叶子零件（大白话：最终要买/要领的原材料），合并同零件数量"""
    rows = bom_explode(conn, parent, qty, version)
    merged: Dict[str, float] = {}
    for it in rows:
        if it["is_leaf"]:
            merged[it["part_code"]] = merged.get(it["part_code"], 0) + it["qty"]
    return [{"part_code": k, "qty": v} for k, v in merged.items()]

def best_supplier_for(conn, part_code: str):
    """比价：返回 (最便宜供应商行, 交期最短供应商行)，跳过黑名单供应商"""
    rows = conn.execute("""
        SELECT sp.*, s.status as supplier_status, s.name as supplier_name
        FROM supplier_parts sp LEFT JOIN suppliers s ON s.code=sp.supplier_code
        WHERE sp.part_code=? ORDER BY sp.price ASC
    """, (part_code,)).fetchall()
    usable = [r for r in rows if (r["supplier_status"] or "合格") != "黑名单"]
    if not usable:
        return None, None
    cheapest = usable[0]
    fastest = min(usable, key=lambda r: (r["lead_time_days"] if r["lead_time_days"] is not None else 999))
    return cheapest, fastest

def is_overdue(due_date: str, status: str) -> bool:
    """拖期：交期已过且还没全部到货（大白话：该到了还没到）"""
    if status in ("全部到货", "已合并", "已取消"):
        return False
    d = parse_date(due_date)
    return bool(d and d < date.today())

def overdue_days(due_date: str) -> int:
    d = parse_date(due_date)
    if not d:
        return 0
    return max(0, (date.today() - d).days)

# ---------- Pydantic 模型 ----------
class PartIn(BaseModel):
    code: str; name: str; spec: str = ""; unit: str = "个"; category: str = "原材料"
    is_purchase: int = 1; safety_stock: float = 0; min_stock: float = 0; max_stock: float = 0
    lead_time_days: int = 7; price: float = 0; tax_rate: float = 0.13; supplier_code: str = ""

class PartUpdate(BaseModel):
    name: Optional[str] = None; spec: Optional[str] = None; unit: Optional[str] = None
    category: Optional[str] = None; is_purchase: Optional[int] = None
    safety_stock: Optional[float] = None; min_stock: Optional[float] = None; max_stock: Optional[float] = None
    lead_time_days: Optional[int] = None; price: Optional[float] = None
    tax_rate: Optional[float] = None; supplier_code: Optional[str] = None

class SupplierIn(BaseModel):
    code: str; name: str; contact: str = ""; phone: str = ""; address: str = ""
    payment: str = ""; lead_time_days: int = 7; rating: str = "合格"; status: str = "合格"

class SupplierUpdate(BaseModel):
    name: Optional[str] = None; contact: Optional[str] = None; phone: Optional[str] = None
    address: Optional[str] = None; payment: Optional[str] = None
    lead_time_days: Optional[int] = None; rating: Optional[str] = None; status: Optional[str] = None

class SupplierPartIn(BaseModel):
    supplier_code: str; part_code: str; price: float = 0; tax_rate: float = 0.13
    lead_time_days: int = 7; min_order_qty: float = 0

class SalesOrderIn(BaseModel):
    order_no: str; customer: str = ""; product_code: str = "FG-001"; qty: float = 100; due_date: str = ""

class ReceiveIn(BaseModel):
    order_no: str; part_code: str; qty: float
    qc_result: str = "合格"; batch: str = ""; location: str = ""

# ---------- 零件 ----------
@app.get("/api/parts")
def list_parts():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM parts ORDER BY code").fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.get("/api/parts/{code}")
def get_part(code: str):
    conn = get_conn()
    r = conn.execute("SELECT * FROM parts WHERE code=?", (code,)).fetchone()
    conn.close()
    if not r:
        raise HTTPException(404, f"零件 {code} 不存在")
    return row_to_dict(r)

@app.post("/api/parts")
def create_part(p: PartIn):
    conn = get_conn()
    try:
        conn.execute("INSERT INTO parts (code,name,spec,unit,category,is_purchase,safety_stock,min_stock,max_stock,lead_time_days,price,tax_rate,supplier_code,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (p.code,p.name,p.spec,p.unit,p.category,p.is_purchase,p.safety_stock,p.min_stock,p.max_stock,p.lead_time_days,p.price,p.tax_rate,p.supplier_code,now_str()))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"零件编码 {p.code} 已存在")
    conn.close()
    return {"ok": True, "code": p.code}

@app.put("/api/parts/{code}")
def update_part(code: str, p: PartUpdate):
    conn = get_conn()
    r = conn.execute("SELECT id FROM parts WHERE code=?", (code,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, f"零件 {code} 不存在")
    updates = {k: v for k, v in p.dict().items() if v is not None}
    for k, v in updates.items():
        conn.execute(f"UPDATE parts SET {k}=? WHERE code=?", (v, code))
    conn.commit(); conn.close()
    return {"ok": True, "code": code}

@app.delete("/api/parts/{code}")
def delete_part(code: str):
    conn = get_conn()
    used = conn.execute("SELECT COUNT(*) FROM bom_items WHERE parent_code=? OR child_code=?", (code, code)).fetchone()[0]
    if used:
        conn.close()
        raise HTTPException(400, f"零件 {code} 已在 BOM 中使用，不能删除，请先删除相关 BOM")
    conn.execute("DELETE FROM parts WHERE code=?", (code,))
    conn.commit(); conn.close()
    return {"ok": True, "code": code}

# ---------- 供应商 ----------
@app.get("/api/suppliers")
def list_suppliers():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM suppliers ORDER BY code").fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.get("/api/suppliers/{code}")
def get_supplier(code: str):
    conn = get_conn()
    r = conn.execute("SELECT * FROM suppliers WHERE code=?", (code,)).fetchone()
    conn.close()
    if not r:
        raise HTTPException(404, f"供应商 {code} 不存在")
    return row_to_dict(r)

@app.post("/api/suppliers")
def create_supplier(s: SupplierIn):
    conn = get_conn()
    try:
        conn.execute("INSERT INTO suppliers (code,name,contact,phone,address,payment,lead_time_days,rating,status) VALUES (?,?,?,?,?,?,?,?,?)",
                     (s.code,s.name,s.contact,s.phone,s.address,s.payment,s.lead_time_days,s.rating,s.status))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"供应商编码 {s.code} 已存在")
    conn.close()
    return {"ok": True, "code": s.code}

@app.put("/api/suppliers/{code}")
def update_supplier(code: str, s: SupplierUpdate):
    conn = get_conn()
    r = conn.execute("SELECT id FROM suppliers WHERE code=?", (code,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, f"供应商 {code} 不存在")
    updates = {k: v for k, v in s.dict().items() if v is not None}
    for k, v in updates.items():
        conn.execute(f"UPDATE suppliers SET {k}=? WHERE code=?", (v, code))
    conn.commit(); conn.close()
    return {"ok": True, "code": code}

@app.delete("/api/suppliers/{code}")
def delete_supplier(code: str):
    conn = get_conn()
    used = conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE supplier_code=?", (code,)).fetchone()[0]
    if used:
        conn.close()
        raise HTTPException(400, f"供应商 {code} 已有采购单，不能删除，可改为黑名单")
    conn.execute("DELETE FROM supplier_parts WHERE supplier_code=?", (code,))
    conn.execute("DELETE FROM suppliers WHERE code=?", (code,))
    conn.commit(); conn.close()
    return {"ok": True, "code": code}

@app.post("/api/suppliers/{code}/blacklist")
def blacklist_supplier(code: str):
    """拉黑供应商（大白话：这个供应商不能再用了，比价时会自动跳过）"""
    conn = get_conn()
    r = conn.execute("SELECT id FROM suppliers WHERE code=?", (code,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, f"供应商 {code} 不存在")
    conn.execute("UPDATE suppliers SET status='黑名单' WHERE code=?", (code,))
    conn.commit(); conn.close()
    return {"ok": True, "code": code, "status": "黑名单"}

@app.post("/api/suppliers/{code}/approve")
def approve_supplier(code: str):
    """恢复为合格供应商"""
    conn = get_conn()
    r = conn.execute("SELECT id FROM suppliers WHERE code=?", (code,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, f"供应商 {code} 不存在")
    conn.execute("UPDATE suppliers SET status='合格' WHERE code=?", (code,))
    conn.commit(); conn.close()
    return {"ok": True, "code": code, "status": "合格"}

@app.get("/api/suppliers/{code}/stats")
def supplier_stats(code: str):
    """交货准时率统计（大白话：这个供应商按时送货的比例）"""
    conn = get_conn()
    s = conn.execute("SELECT * FROM suppliers WHERE code=?", (code,)).fetchone()
    if not s:
        conn.close()
        raise HTTPException(404, f"供应商 {code} 不存在")
    pos = conn.execute("SELECT * FROM purchase_orders WHERE supplier_code=? AND status IN ('全部到货','部分到货')", (code,)).fetchall()
    total = len(pos)
    on_time = 0
    delays = []
    for po in pos:
        if po["received_date"] and po["due_date"]:
            rd, dd = parse_date(po["received_date"]), parse_date(po["due_date"])
            if rd and dd:
                delays.append((rd - dd).days)
                if rd <= dd:
                    on_time += 1
    overdue_pos = conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE supplier_code=? AND status IN ('新建','已下单','部分到货')", (code,)).fetchone()[0]
    conn.close()
    rate = round(on_time / total * 100, 1) if total else None
    avg_delay = round(sum(delays) / len(delays), 1) if delays else None
    return {"supplier_code": code, "supplier_name": s["name"], "status": s["status"],
            "delivered_orders": total, "on_time_orders": on_time, "on_time_rate_pct": rate,
            "avg_delay_days": avg_delay, "pending_orders": overdue_pos}

# ---------- 供应商零件价格表 ----------
@app.get("/api/supplier-parts")
def list_supplier_parts(part_code: Optional[str] = None, supplier_code: Optional[str] = None):
    conn = get_conn()
    sql = "SELECT sp.*, s.name as supplier_name, p.name as part_name FROM supplier_parts sp LEFT JOIN suppliers s ON s.code=sp.supplier_code LEFT JOIN parts p ON p.code=sp.part_code WHERE 1=1"
    args = []
    if part_code:
        sql += " AND sp.part_code=?"; args.append(part_code)
    if supplier_code:
        sql += " AND sp.supplier_code=?"; args.append(supplier_code)
    sql += " ORDER BY sp.part_code, sp.price"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.post("/api/supplier-parts")
def create_supplier_part(sp: SupplierPartIn):
    conn = get_conn()
    try:
        conn.execute("INSERT INTO supplier_parts (supplier_code,part_code,price,tax_rate,lead_time_days,min_order_qty) VALUES (?,?,?,?,?,?)",
                     (sp.supplier_code, sp.part_code, sp.price, sp.tax_rate, sp.lead_time_days, sp.min_order_qty))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"供应商 {sp.supplier_code} 的零件 {sp.part_code} 价格已存在，请用修改")
    conn.close()
    return {"ok": True}

@app.put("/api/supplier-parts/{sp_id}")
def update_supplier_part(sp_id: int, payload: Dict[str, Any]):
    conn = get_conn()
    r = conn.execute("SELECT id FROM supplier_parts WHERE id=?", (sp_id,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, f"供应商价格 {sp_id} 不存在")
    for k in ("price", "tax_rate", "lead_time_days", "min_order_qty"):
        if k in payload and payload[k] is not None:
            conn.execute(f"UPDATE supplier_parts SET {k}=? WHERE id=?", (payload[k], sp_id))
    conn.commit(); conn.close()
    return {"ok": True, "id": sp_id}

@app.delete("/api/supplier-parts/{sp_id}")
def delete_supplier_part(sp_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM supplier_parts WHERE id=?", (sp_id,))
    conn.commit(); conn.close()
    return {"ok": True, "id": sp_id}

@app.get("/api/suppliers/compare/{part_code}")
def compare_suppliers(part_code: str):
    """自动比价（大白话：买这个零件，找谁最便宜、找谁到货最快，一眼能看）"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT sp.*, s.name as supplier_name, s.status as supplier_status
        FROM supplier_parts sp LEFT JOIN suppliers s ON s.code=sp.supplier_code
        WHERE sp.part_code=? ORDER BY sp.price ASC
    """, (part_code,)).fetchall()
    conn.close()
    if not rows:
        raise HTTPException(404, f"零件 {part_code} 没有任何供应商报价")
    usable = [r for r in rows if (r["supplier_status"] or "合格") != "黑名单"]
    cheapest_code = usable[0]["supplier_code"] if usable else None
    fastest_code = min(usable, key=lambda r: r["lead_time_days"] or 999)["supplier_code"] if usable else None
    out = []
    for r in rows:
        d = row_to_dict(r)
        d["is_cheapest"] = (r["supplier_code"] == cheapest_code)
        d["is_fastest"] = (r["supplier_code"] == fastest_code)
        d["skipped_blacklist"] = ((r["supplier_status"] or "合格") == "黑名单")
        out.append(d)
    return {"part_code": part_code, "cheapest_supplier": cheapest_code, "fastest_supplier": fastest_code, "options": out}

# ---------- BOM ----------
@app.get("/api/bom/{parent_code}")
def get_bom(parent_code: str, version: Optional[str] = None):
    conn = get_conn()
    ver = resolve_bom_version(conn, parent_code, version)
    rows = conn.execute("""
        SELECT b.*, p.name as child_name, p.unit FROM bom_items b
        LEFT JOIN parts p ON p.code=b.child_code
        WHERE b.parent_code=? AND b.version=? ORDER BY b.child_code
    """, (parent_code, ver)).fetchall()
    conn.close()
    # 兼容 MVP：直接返回明细列表（版本信息见 /api/bom/versions/{parent}）
    return [row_to_dict(r) for r in rows]

@app.get("/api/bom/versions/{parent_code}")
def list_bom_versions(parent_code: str):
    """BOM 多版本管理（大白话：这个成品有几个版本的配方，各自什么时候生效）"""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM bom_versions WHERE parent_code=? ORDER BY effective_date", (parent_code,)).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.post("/api/bom")
def create_bom_item(payload: Dict[str, Any]):
    """新增/修改一条 BOM 明细（同父件+子件+版本已存在则更新用量）"""
    parent = payload.get("parent_code", "")
    child = payload.get("child_code", "")
    qty = float(payload.get("qty", 0))
    version = payload.get("version", "V1")
    eff = payload.get("effective_date", "")
    loss = float(payload.get("loss_rate", 0) or 0)
    if not parent or not child or qty <= 0:
        raise HTTPException(400, "父件编码、子件编码必填，用量必须 > 0")
    if parent == child:
        raise HTTPException(400, f"BOM 不能自己引用自己：{parent}")
    conn = get_conn()
    if not conn.execute("SELECT id FROM parts WHERE code=?", (parent,)).fetchone():
        conn.close()
        raise HTTPException(404, f"父件 {parent} 不存在，请先建零件")
    if not conn.execute("SELECT id FROM parts WHERE code=?", (child,)).fetchone():
        conn.close()
        raise HTTPException(404, f"子件 {child} 不存在，请先建零件")
    # 循环检测：子件的 BOM 里不能反过来包含父件
    try:
        bom_leaf_explode(conn, child, 1)
    except HTTPException as e:
        conn.close()
        raise e
    leaves = []
    try:
        leaves = bom_explode(conn, child, 1)
    except HTTPException as e:
        conn.close()
        raise e
    if any(x["part_code"] == parent for x in leaves):
        conn.close()
        raise HTTPException(400, f"BOM 循环引用：{child} 的 BOM 里已包含 {parent}")
    conn.execute("INSERT INTO bom_versions (parent_code,version,effective_date,status,remark) VALUES (?,?,?,?,?) ON CONFLICT(parent_code,version) DO UPDATE SET effective_date=excluded.effective_date",
                 (parent, version, eff, "生效" if eff and eff <= TODAY else "未生效", ""))
    conn.execute("INSERT INTO bom_items (parent_code,child_code,qty,loss_rate,version,effective_date) VALUES (?,?,?,?,?,?) ON CONFLICT(parent_code,child_code,version) DO UPDATE SET qty=excluded.qty, loss_rate=excluded.loss_rate, effective_date=excluded.effective_date",
                 (parent, child, qty, loss, version, eff))
    conn.commit(); conn.close()
    return {"ok": True, "parent_code": parent, "child_code": child, "version": version}

@app.delete("/api/bom")
def delete_bom_item(parent_code: str, child_code: str, version: str = "V1"):
    conn = get_conn()
    conn.execute("DELETE FROM bom_items WHERE parent_code=? AND child_code=? AND version=?", (parent_code, child_code, version))
    conn.commit(); conn.close()
    return {"ok": True}

@app.post("/api/bom/compare")
def compare_bom(payload: Dict[str, Any]):
    """BOM 对比（大白话：两个版本的配方差在哪：新增、删除、用量变了）"""
    parent = payload.get("parent_code", "")
    v1 = payload.get("version_a", "V1")
    v2 = payload.get("version_b", "V2")
    conn = get_conn()
    def _map(v):
        rows = conn.execute("SELECT child_code, qty, loss_rate FROM bom_items WHERE parent_code=? AND version=?", (parent, v)).fetchall()
        return {r["child_code"]: {"qty": float(r["qty"]), "loss_rate": float(r["loss_rate"] or 0)} for r in rows}
    m1, m2 = _map(v1), _map(v2)
    conn.close()
    added = [{"child_code": k, "qty": v["qty"]} for k, v in m2.items() if k not in m1]
    removed = [{"child_code": k, "qty": v["qty"]} for k, v in m1.items() if k not in m2]
    changed = [{"child_code": k, "qty_before": m1[k]["qty"], "qty_after": m2[k]["qty"]} for k in m1 if k in m2 and abs(m1[k]["qty"] - m2[k]["qty"]) > 1e-9]
    return {"parent_code": parent, "version_a": v1, "version_b": v2, "added": added, "removed": removed, "changed": changed}

@app.post("/api/bom/explode")
def bom_explode_api(payload: Dict[str, Any]):
    """BOM 爆炸（多层，带层级 + 循环检测）。传 leaf_only=true 只返回叶子零件。"""
    parent = payload.get("parent_code", "FG-001")
    qty = float(payload.get("qty", 1))
    version = payload.get("version")
    conn = get_conn()
    try:
        if payload.get("leaf_only"):
            result = bom_leaf_explode(conn, parent, qty, version)
        else:
            result = bom_explode(conn, parent, qty, version)
    finally:
        conn.close()
    return result

# ---------- 库存 ----------
@app.get("/api/stock")
def list_stock():
    conn = get_conn()
    rows = conn.execute("""
        SELECT s.*, p.name as part_name, p.unit, p.safety_stock, p.max_stock
        FROM stock s LEFT JOIN parts p ON p.code=s.part_code ORDER BY s.part_code
    """).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.get("/api/stock/ledger")
def list_ledger(part_code: Optional[str] = None):
    conn = get_conn()
    if part_code:
        rows = conn.execute("SELECT * FROM stock_ledger WHERE part_code=? ORDER BY id DESC LIMIT 200", (part_code,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM stock_ledger ORDER BY id DESC LIMIT 200").fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.get("/api/stock/trace/{part_code}")
def trace_stock(part_code: str):
    """批次追溯（大白话：一个零件从哪来、到哪去：期初/采购入库/领料/盘点/调拨，全部流水）"""
    conn = get_conn()
    part = conn.execute("SELECT * FROM parts WHERE code=?", (part_code,)).fetchone()
    if not part:
        conn.close()
        raise HTTPException(404, f"零件 {part_code} 不存在")
    batches = conn.execute("SELECT * FROM stock WHERE part_code=? ORDER BY batch", (part_code,)).fetchall()
    ledger = conn.execute("SELECT * FROM stock_ledger WHERE part_code=? ORDER BY id", (part_code,)).fetchall()
    frozen = conn.execute("SELECT * FROM frozen_stock WHERE part_code=? ORDER BY id", (part_code,)).fetchall()
    conn.close()
    return {"part_code": part_code, "part_name": part["name"],
            "onhand": get_onhand(part_code), "in_transit": get_in_transit(part_code),
            "allocated": get_allocated(part_code), "frozen": get_frozen(part_code),
            "available": get_available(part_code),
            "batches": [row_to_dict(r) for r in batches],
            "frozen_detail": [row_to_dict(r) for r in frozen],
            "ledger": [row_to_dict(r) for r in ledger]}

@app.get("/api/stock/available")
def list_available():
    """可用量总览：每个零件的 现有/在途/已分配/冻结/可用量 + 红黄绿"""
    conn = get_conn()
    parts = conn.execute("SELECT * FROM parts ORDER BY code").fetchall()
    conn.close()
    out = []
    for p in parts:
        code = p["code"]
        onhand = get_onhand(code); it = get_in_transit(code)
        al = get_allocated(code); fr = get_frozen(code)
        avail = onhand + it - al - fr
        out.append({"part_code": code, "part_name": p["name"], "onhand": onhand, "in_transit": it,
                    "allocated": al, "frozen": fr, "available": avail,
                    "safety_stock": p["safety_stock"], "max_stock": p["max_stock"],
                    "status": stock_status(avail, float(p["safety_stock"] or 0), float(p["max_stock"] or 0))})
    return out

@app.get("/api/stock/available/{part_code}")
def one_available(part_code: str):
    conn = get_conn()
    p = conn.execute("SELECT * FROM parts WHERE code=?", (part_code,)).fetchone()
    conn.close()
    if not p:
        raise HTTPException(404, f"零件 {part_code} 不存在")
    onhand = get_onhand(part_code); it = get_in_transit(part_code)
    al = get_allocated(part_code); fr = get_frozen(part_code)
    avail = onhand + it - al - fr
    return {"part_code": part_code, "part_name": p["name"], "onhand": onhand, "in_transit": it,
            "allocated": al, "frozen": fr, "available": avail,
            "safety_stock": p["safety_stock"], "max_stock": p["max_stock"],
            "status": stock_status(avail, float(p["safety_stock"] or 0), float(p["max_stock"] or 0))}

@app.post("/api/stock/in")
def stock_in(payload: Dict[str, Any]):
    """入库（大白话：货进仓库，库存增加）。type 如：采购入库/其他入库/生产入库"""
    code = payload.get("part_code", ""); qty = float(payload.get("qty", 0) or 0)
    if not code or qty <= 0:
        raise HTTPException(400, "零件编码必填，数量必须 > 0")
    adjust_stock(code, qty, payload.get("type", "其他入库"), payload.get("ref_no", "手动入库"),
                 payload.get("warehouse", "主仓"), payload.get("location", "A区-01"), payload.get("batch", "BATCH-20261019"))
    return {"ok": True, "part_code": code, "qty": qty, "onhand": get_onhand(code)}

@app.post("/api/stock/out")
def stock_out(payload: Dict[str, Any]):
    """出库（大白话：货出仓库，库存减少）。出库前检查现有库存够不够。"""
    code = payload.get("part_code", ""); qty = float(payload.get("qty", 0) or 0)
    if not code or qty <= 0:
        raise HTTPException(400, "零件编码必填，数量必须 > 0")
    if get_onhand(code) + 1e-9 < qty:
        raise HTTPException(400, f"零件 {code} 现有库存不足：需要 {qty}，现有 {get_onhand(code)}")
    adjust_stock(code, -qty, payload.get("type", "其他出库"), payload.get("ref_no", "手动出库"),
                 payload.get("warehouse", "主仓"), payload.get("location", "A区-01"), payload.get("batch", "BATCH-20261019"))
    return {"ok": True, "part_code": code, "qty": qty, "onhand": get_onhand(code)}

@app.post("/api/stock/transfer")
def stock_transfer(payload: Dict[str, Any]):
    """调拨（大白话：同一仓库内从一个库位挪到另一个库位，总数不变，只换位置）"""
    code = payload.get("part_code", ""); qty = float(payload.get("qty", 0) or 0)
    from_loc = payload.get("from_location", "A区-01"); to_loc = payload.get("to_location", "B区-01")
    from_batch = payload.get("from_batch", "BATCH-20261019"); to_batch = payload.get("to_batch", from_batch)
    warehouse = payload.get("warehouse", "主仓")
    if not code or qty <= 0:
        raise HTTPException(400, "零件编码必填，数量必须 > 0")
    if from_loc == to_loc and from_batch == to_batch:
        raise HTTPException(400, "调出与调入库位/批次相同，不用调拨")
    conn = get_conn()
    row = conn.execute("SELECT * FROM stock WHERE part_code=? AND warehouse=? AND location=? AND batch=?", (code, warehouse, from_loc, from_batch)).fetchone()
    if not row or float(row["qty"]) + 1e-9 < qty:
        conn.close()
        raise HTTPException(400, f"调出库位库存不足：{from_loc}/{from_batch} 现有 {float(row['qty']) if row else 0}，要调 {qty}")
    conn.execute("UPDATE stock SET qty=qty-? WHERE id=?", (qty, row["id"]))
    dest = conn.execute("SELECT * FROM stock WHERE part_code=? AND warehouse=? AND location=? AND batch=?", (code, warehouse, to_loc, to_batch)).fetchone()
    if dest:
        conn.execute("UPDATE stock SET qty=qty+? WHERE id=?", (qty, dest["id"]))
    else:
        conn.execute("INSERT INTO stock (part_code,warehouse,location,batch,qty) VALUES (?,?,?,?,?)", (code, warehouse, to_loc, to_batch, qty))
    conn.commit(); conn.close()
    add_ledger(code, -qty, "调拨出", payload.get("ref_no", "调拨"), f"从 {from_loc}/{from_batch} 调到 {to_loc}/{to_batch}", warehouse, from_batch)
    add_ledger(code, qty, "调拨入", payload.get("ref_no", "调拨"), f"从 {from_loc}/{from_batch} 调到 {to_loc}/{to_batch}", warehouse, to_batch)
    return {"ok": True, "part_code": code, "qty": qty, "from": f"{from_loc}/{from_batch}", "to": f"{to_loc}/{to_batch}"}

@app.post("/api/stock/count")
def stock_count(payload: Dict[str, Any]):
    """盘点（大白话：实地数一数，数出来多少库存就是多少，差额记盘盈/盘亏）"""
    code = payload.get("part_code", "")
    counted = float(payload.get("counted_qty", 0) or 0)
    warehouse = payload.get("warehouse", "主仓"); location = payload.get("location", "A区-01")
    batch = payload.get("batch", "BATCH-20261019")
    if not code:
        raise HTTPException(400, "零件编码必填")
    conn = get_conn()
    row = conn.execute("SELECT * FROM stock WHERE part_code=? AND warehouse=? AND location=? AND batch=?", (code, warehouse, location, batch)).fetchone()
    old = float(row["qty"]) if row else 0
    diff = counted - old
    if row:
        conn.execute("UPDATE stock SET qty=? WHERE id=?", (counted, row["id"]))
    else:
        conn.execute("INSERT INTO stock (part_code,warehouse,location,batch,qty) VALUES (?,?,?,?,?)", (code, warehouse, location, batch, counted))
    conn.commit(); conn.close()
    if abs(diff) > 1e-9:
        add_ledger(code, diff, "盘点", payload.get("ref_no", "盘点"), f"盘点：账面 {old} → 实盘 {counted}，差异 {diff}", warehouse, batch)
    return {"ok": True, "part_code": code, "old_qty": old, "counted_qty": counted, "diff": diff}

@app.post("/api/stock/freeze")
def stock_freeze(payload: Dict[str, Any]):
    """冻结（大白话：这批货暂时不能用，比如等质检结果），可用量会减少"""
    code = payload.get("part_code", ""); qty = float(payload.get("qty", 0) or 0)
    if not code or qty <= 0:
        raise HTTPException(400, "零件编码必填，数量必须 > 0")
    if get_available(code) + 1e-9 < qty:
        raise HTTPException(400, f"零件 {code} 可用量不足，无法冻结 {qty}（可用 {get_available(code)}）")
    conn = get_conn()
    conn.execute("INSERT INTO frozen_stock (part_code,qty,reason,ref_no,created_at) VALUES (?,?,?,?,?)",
                 (code, qty, payload.get("reason", "冻结"), payload.get("ref_no", ""), now_str()))
    conn.commit(); conn.close()
    return {"ok": True, "part_code": code, "frozen_qty": qty, "available": get_available(code)}

@app.post("/api/stock/unfreeze")
def stock_unfreeze(payload: Dict[str, Any]):
    """解冻（大白话：之前冻住的货又能用了），可用量会恢复"""
    code = payload.get("part_code", ""); qty = float(payload.get("qty", 0) or 0)
    if not code or qty <= 0:
        raise HTTPException(400, "零件编码必填，数量必须 > 0")
    conn = get_conn()
    rows = conn.execute("SELECT * FROM frozen_stock WHERE part_code=? ORDER BY id", (code,)).fetchall()
    total = sum(float(r["qty"]) for r in rows)
    if total + 1e-9 < qty:
        conn.close()
        raise HTTPException(400, f"零件 {code} 冻结量只有 {total}，无法解冻 {qty}")
    remain = qty
    for r in rows:
        if remain <= 0:
            break
        take = min(float(r["qty"]), remain)
        if take >= float(r["qty"]) - 1e-9:
            conn.execute("DELETE FROM frozen_stock WHERE id=?", (r["id"],))
        else:
            conn.execute("UPDATE frozen_stock SET qty=qty-? WHERE id=?", (take, r["id"]))
        remain -= take
    conn.commit(); conn.close()
    return {"ok": True, "part_code": code, "unfrozen_qty": qty, "available": get_available(code)}

# ---------- 销售单（轻量） ----------
@app.get("/api/sales")
def list_sales():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM sales_orders ORDER BY id DESC").fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.post("/api/sales")
def create_sales(o: SalesOrderIn):
    conn = get_conn()
    try:
        conn.execute("INSERT INTO sales_orders (order_no,customer,product_code,qty,due_date,status,created_at) VALUES (?,?,?,?,?,?,?)",
                     (o.order_no,o.customer,o.product_code,o.qty,o.due_date,"新建",now_str()))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"销售单号 {o.order_no} 已存在")
    conn.close()
    return {"ok": True, "order_no": o.order_no}

@app.put("/api/sales/{order_no}")
def update_sales(order_no: str, payload: Dict[str, Any]):
    conn = get_conn()
    r = conn.execute("SELECT id FROM sales_orders WHERE order_no=?", (order_no,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, f"销售单 {order_no} 不存在")
    for k in ("customer", "product_code", "qty", "due_date", "status"):
        if k in payload and payload[k] is not None:
            conn.execute(f"UPDATE sales_orders SET {k}=? WHERE order_no=?", (payload[k], order_no))
    conn.commit(); conn.close()
    return {"ok": True, "order_no": order_no}

@app.delete("/api/sales/{order_no}")
def delete_sales(order_no: str):
    conn = get_conn()
    conn.execute("DELETE FROM sales_orders WHERE order_no=?", (order_no,))
    conn.execute("DELETE FROM mrp_results WHERE sales_order_no=?", (order_no,))
    conn.commit(); conn.close()
    return {"ok": True, "order_no": order_no}

@app.post("/api/sales/{order_no}/create-production")
def sales_create_production(order_no: str, payload: Dict[str, Any] = None):
    """销售单触发生产需求（大白话：客户下单了，就自动生成一张生产单，不做 CRM）"""
    conn = get_conn()
    so = conn.execute("SELECT * FROM sales_orders WHERE order_no=?", (order_no,)).fetchone()
    conn.close()
    if not so:
        raise HTTPException(404, f"销售单 {order_no} 不存在")
    mo_no = (payload or {}).get("order_no") or f"MO-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    return create_production({"order_no": mo_no, "product_code": so["product_code"], "qty": float(so["qty"]),
                              "sales_order_no": order_no,
                              "planned_start": (payload or {}).get("planned_start", ""),
                              "planned_end": (payload or {}).get("planned_end", "") or so["due_date"]})

# ---------- MRP ----------
@app.post("/api/mrp")
def run_mrp(payload: Dict[str, Any]):
    """
    MRP 计算（多层 BOM 版）
    步骤：1) 多层 BOM 爆炸得毛需求  2) 查可用库存/在途  3) 算净需求  4) 按提前期偏移算最晚下单日  5) 存结果
    公式：净需求 = 毛需求 - 可用库存 - 在途 + 安全库存；最晚下单日 = 需求日期 - 提前期天数
    """
    sales_no = payload.get("sales_order_no", "")
    conn = get_conn()
    so = conn.execute("SELECT * FROM sales_orders WHERE order_no=?", (sales_no,)).fetchone()
    if not so:
        conn.close()
        raise HTTPException(404, f"销售单 {sales_no} 不存在")
    product = so["product_code"]
    order_qty = float(so["qty"])
    need_date = parse_date(so["due_date"]) or date.today()
    exploded = bom_explode(conn, product, order_qty)
    conn.execute("DELETE FROM mrp_results WHERE sales_order_no=?", (sales_no,))
    results = []
    for it in exploded:
        child = it["part_code"]
        gross = float(it["qty"])
        part = conn.execute("SELECT * FROM parts WHERE code=?", (child,)).fetchone()
        safety = float(part["safety_stock"]) if part else 0
        lead = int(part["lead_time_days"]) if part and part["lead_time_days"] is not None else 7
        part_name = part["name"] if part else child
        is_purchase = int(part["is_purchase"]) if part else 1
        onhand = float(conn.execute("SELECT COALESCE(SUM(qty),0) FROM stock WHERE part_code=?", (child,)).fetchone()[0] or 0)
        in_transit = float(conn.execute("""
            SELECT COALESCE(SUM(poi.qty - poi.received_qty),0)
            FROM purchase_order_items poi JOIN purchase_orders po ON po.order_no=poi.order_no
            WHERE poi.part_code=? AND po.status IN ('新建','已下单','部分到货')
        """, (child,)).fetchone()[0] or 0)
        net = gross - onhand - in_transit + safety
        if net < 0:
            net = 0
        latest = (need_date - timedelta(days=lead)).isoformat()
        need_type = "采购" if is_purchase else "生产"
        conn.execute("INSERT INTO mrp_results (sales_order_no,part_code,part_name,gross,onhand,in_transit,safety,net,adjusted_net,level,need_type,latest_order_date,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (sales_no, child, part_name, gross, onhand, in_transit, safety, net, None, it["level"], need_type, latest, now_str()))
        results.append({"part_code": child, "part_name": part_name, "gross": gross, "onhand": onhand,
                        "in_transit": in_transit, "safety": safety, "net": net, "adjusted_net": None,
                        "level": it["level"], "need_type": need_type, "latest_order_date": latest})
    conn.commit()
    conn.close()
    return {"sales_order_no": sales_no, "results": results}

@app.get("/api/mrp/{sales_no}")
def get_mrp(sales_no: str):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM mrp_results WHERE sales_order_no=? ORDER BY level, part_code", (sales_no,)).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.put("/api/mrp/adjust")
def adjust_mrp(payload: Dict[str, Any]):
    """MRP 手动调整（大白话：系统算出来的数，人工可以改，改完再生成采购单/生产单）"""
    sales_no = payload.get("sales_order_no", "")
    part_code = payload.get("part_code", "")
    if "adjusted_net" not in payload or payload["adjusted_net"] is None:
        raise HTTPException(400, "adjusted_net 必填（调整后数量）")
    adj = float(payload["adjusted_net"])
    if adj < 0:
        raise HTTPException(400, "调整后数量不能为负")
    conn = get_conn()
    r = conn.execute("SELECT id FROM mrp_results WHERE sales_order_no=? AND part_code=?", (sales_no, part_code)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, f"销售单 {sales_no} 的 MRP 结果里没有零件 {part_code}")
    conn.execute("UPDATE mrp_results SET adjusted_net=? WHERE id=?", (adj, r["id"]))
    conn.commit(); conn.close()
    return {"ok": True, "sales_order_no": sales_no, "part_code": part_code, "adjusted_net": adj}

def _mrp_effective_qty(row) -> float:
    """调整后数量优先（大白话：人工改过的数说了算，没改过就用系统算的）"""
    if row["adjusted_net"] is not None:
        return float(row["adjusted_net"])
    return float(row["net"])

@app.post("/api/mrp/generate")
def mrp_generate(payload: Dict[str, Any]):
    """
    MRP 一键生成：采购件按最便宜供应商分组生成采购单（跳过黑名单），
    半成品/成品需求生成生产单。返回生成的单号。
    """
    sales_no = payload.get("sales_order_no", "")
    conn = get_conn()
    mrp = conn.execute("SELECT * FROM mrp_results WHERE sales_order_no=?", (sales_no,)).fetchall()
    if not mrp:
        conn.close()
        raise HTTPException(400, "MRP 结果为空，请先跑 MRP")
    # 采购部分：按最便宜供应商分组
    groups: Dict[str, List] = {}
    production_created = []
    for r in mrp:
        qty = _mrp_effective_qty(r)
        if qty <= 0:
            continue
        if r["need_type"] == "采购":
            cheapest, _ = best_supplier_for(conn, r["part_code"])
            supplier = cheapest["supplier_code"] if cheapest else "SUP-001"
            groups.setdefault(supplier, []).append((r, qty))
        else:
            # 半成品需求：生成生产单（成品本身的需求由销售单触发的生产单覆盖，这里只对半成品）
            part = conn.execute("SELECT * FROM parts WHERE code=?", (r["part_code"],)).fetchone()
            if part and part["category"] == "半成品":
                mo_no = f"MO-{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{r['part_code']}"
                conn.execute("INSERT INTO production_orders (order_no,product_code,qty,status,sales_order_no,created_at) VALUES (?,?,?,?,?,?)",
                             (mo_no, r["part_code"], qty, "新建", sales_no, now_str()))
                production_created.append(mo_no)
    po_nos = []
    for supplier, items in groups.items():
        po_no = f"PO-{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{supplier}"
        total = 0
        conn.execute("INSERT INTO purchase_orders (order_no,supplier_code,status,due_date,total_amount,created_at) VALUES (?,?,?,?,?,?)",
                     (po_no, supplier, "已下单", TODAY, 0, now_str()))
        for r, qty in items:
            cheapest, _ = best_supplier_for(conn, r["part_code"])
            price = float(cheapest["price"]) if cheapest else 0
            tax = float(cheapest["tax_rate"]) if cheapest else 0.13
            conn.execute("INSERT INTO purchase_order_items (order_no,part_code,qty,price,received_qty,tax_rate) VALUES (?,?,?,?,?,?)",
                         (po_no, r["part_code"], qty, price, 0, tax))
            total += qty * price * (1 + tax)
        conn.execute("UPDATE purchase_orders SET total_amount=? WHERE order_no=?", (round(total, 2), po_no))
        po_nos.append(po_no)
    conn.commit(); conn.close()
    return {"ok": True, "sales_order_no": sales_no, "purchase_orders": po_nos, "production_orders": production_created}

# ---------- 采购申请（一级审批） ----------
@app.get("/api/purchase/requisitions")
def list_requisitions():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM purchase_requisitions ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        d = row_to_dict(r)
        items = conn.execute("SELECT * FROM purchase_requisition_items WHERE req_no=?", (r["req_no"],)).fetchall()
        d["items"] = [row_to_dict(x) for x in items]
        out.append(d)
    conn.close()
    return out

@app.post("/api/purchase/requisitions")
def create_requisition(payload: Dict[str, Any]):
    """新建采购申请（大白话：要买东西，先打个申请，批了才能下采购单）"""
    req_no = payload.get("req_no") or f"PR-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    items = payload.get("items", [])
    if not items:
        raise HTTPException(400, "采购申请至少要有一条明细")
    conn = get_conn()
    try:
        conn.execute("INSERT INTO purchase_requisitions (req_no,applicant,reason,status,created_at) VALUES (?,?,?,?,?)",
                     (req_no, payload.get("applicant", ""), payload.get("reason", ""), "新建", now_str()))
        for it in items:
            conn.execute("INSERT INTO purchase_requisition_items (req_no,part_code,qty,price,supplier_code) VALUES (?,?,?,?,?)",
                         (req_no, it["part_code"], float(it["qty"]), float(it.get("price", 0) or 0), it.get("supplier_code", "")))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"采购申请号 {req_no} 已存在")
    conn.close()
    return {"ok": True, "req_no": req_no, "status": "新建"}

def _requisition_transition(req_no: str, from_status: str, to_status: str):
    conn = get_conn()
    r = conn.execute("SELECT * FROM purchase_requisitions WHERE req_no=?", (req_no,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, f"采购申请 {req_no} 不存在")
    if r["status"] != from_status:
        conn.close()
        raise HTTPException(400, f"采购申请 {req_no} 状态是 {r['status']}，不能从 {from_status} 转到 {to_status}")
    conn.execute("UPDATE purchase_requisitions SET status=? WHERE req_no=?", (to_status, req_no))
    conn.commit(); conn.close()
    return {"ok": True, "req_no": req_no, "status": to_status}

@app.post("/api/purchase/requisitions/{req_no}/submit")
def submit_requisition(req_no: str):
    return _requisition_transition(req_no, "新建", "已提交")

@app.post("/api/purchase/requisitions/{req_no}/approve")
def approve_requisition(req_no: str):
    """一级审批：批准（大白话：领导点了同意）"""
    return _requisition_transition(req_no, "已提交", "已批准")

@app.post("/api/purchase/requisitions/{req_no}/reject")
def reject_requisition(req_no: str):
    return _requisition_transition(req_no, "已提交", "已拒绝")

@app.post("/api/purchase/requisitions/{req_no}/convert")
def convert_requisition(req_no: str):
    """申请转采购单：已批准的申请按供应商分组生成采购单（同供应商合并）"""
    conn = get_conn()
    r = conn.execute("SELECT * FROM purchase_requisitions WHERE req_no=?", (req_no,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, f"采购申请 {req_no} 不存在")
    if r["status"] != "已批准":
        conn.close()
        raise HTTPException(400, f"采购申请 {req_no} 状态是 {r['status']}，只有已批准才能转采购单")
    items = conn.execute("SELECT * FROM purchase_requisition_items WHERE req_no=?", (req_no,)).fetchall()
    groups: Dict[str, List] = {}
    for it in items:
        sup = it["supplier_code"] or "SUP-001"
        groups.setdefault(sup, []).append(it)
    po_nos = []
    for sup, grp in groups.items():
        po_no = f"PO-{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{sup}"
        total = 0
        conn.execute("INSERT INTO purchase_orders (order_no,supplier_code,status,due_date,total_amount,created_at) VALUES (?,?,?,?,?,?)",
                     (po_no, sup, "已下单", TODAY, 0, now_str()))
        for it in grp:
            conn.execute("INSERT INTO purchase_order_items (order_no,part_code,qty,price,received_qty,tax_rate) VALUES (?,?,?,?,?,?)",
                         (po_no, it["part_code"], float(it["qty"]), float(it["price"]), 0, 0.13))
            total += float(it["qty"]) * float(it["price"]) * 1.13
        conn.execute("UPDATE purchase_orders SET total_amount=? WHERE order_no=?", (round(total, 2), po_no))
        po_nos.append(po_no)
    conn.execute("UPDATE purchase_requisitions SET status='已转采购单' WHERE req_no=?", (req_no,))
    conn.commit(); conn.close()
    return {"ok": True, "req_no": req_no, "purchase_orders": po_nos}

# ---------- 采购 ----------
@app.get("/api/purchase")
def list_purchase():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM purchase_orders ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        d = row_to_dict(r)
        d["is_overdue"] = is_overdue(r["due_date"], r["status"])
        d["overdue_days"] = overdue_days(r["due_date"]) if d["is_overdue"] else 0
        items = conn.execute("SELECT * FROM purchase_order_items WHERE order_no=?", (r["order_no"],)).fetchall()
        d["items"] = [row_to_dict(x) for x in items]
        out.append(d)
    conn.close()
    return out

@app.post("/api/purchase")
def create_purchase(payload: Dict[str, Any]):
    """手动新建采购单（带明细）"""
    order_no = payload.get("order_no") or f"PO-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    items = payload.get("items", [])
    if not items:
        raise HTTPException(400, "采购单至少要有一条明细")
    conn = get_conn()
    try:
        total = 0
        conn.execute("INSERT INTO purchase_orders (order_no,supplier_code,status,due_date,total_amount,created_at) VALUES (?,?,?,?,?,?)",
                     (order_no, payload.get("supplier_code", "SUP-001"), "新建", payload.get("due_date", TODAY), 0, now_str()))
        for it in items:
            price = float(it.get("price", 0) or 0); tax = float(it.get("tax_rate", 0.13) or 0.13)
            conn.execute("INSERT INTO purchase_order_items (order_no,part_code,qty,price,received_qty,tax_rate) VALUES (?,?,?,?,?,?)",
                         (order_no, it["part_code"], float(it["qty"]), price, 0, tax))
            total += float(it["qty"]) * price * (1 + tax)
        conn.execute("UPDATE purchase_orders SET total_amount=? WHERE order_no=?", (round(total, 2), order_no))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"采购单号 {order_no} 已存在")
    conn.close()
    return {"ok": True, "order_no": order_no}

@app.post("/api/purchase/from-mrp")
def purchase_from_mrp(payload: Dict[str, Any]):
    """
    从 MRP 一键生成采购单（兼容 MVP）
    逻辑：取 MRP 结果（调整后数量优先），按零件默认供应商分组，同一供应商合并成一张采购单。
    指定 supplier_code 时，全部归到该供应商一张单。
    """
    sales_no = payload.get("sales_order_no", "")
    supplier_override = payload.get("supplier_code")
    conn = get_conn()
    mrp = conn.execute("SELECT * FROM mrp_results WHERE sales_order_no=? AND need_type='采购'", (sales_no,)).fetchall()
    if not mrp:
        conn.close()
        raise HTTPException(400, "MRP 结果为空或全部不缺料，请先跑 MRP")
    groups: Dict[str, List] = {}
    for r in mrp:
        qty = _mrp_effective_qty(r)
        if qty <= 0:
            continue
        if supplier_override:
            sup = supplier_override
        else:
            part = conn.execute("SELECT supplier_code FROM parts WHERE code=?", (r["part_code"],)).fetchone()
            sup = (part["supplier_code"] if part and part["supplier_code"] else "SUP-001")
        groups.setdefault(sup, []).append((r, qty))
    if not groups:
        conn.close()
        raise HTTPException(400, "MRP 结果全部不缺料，无需生成采购单")
    po_nos = []
    first_total = 0
    for sup, items in groups.items():
        order_no = f"PO-{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{sup}"
        total = 0
        conn.execute("INSERT INTO purchase_orders (order_no,supplier_code,status,due_date,total_amount,created_at) VALUES (?,?,?,?,?,?)",
                     (order_no, sup, "已下单", TODAY, 0, now_str()))
        for r, qty in items:
            part = conn.execute("SELECT * FROM parts WHERE code=?", (r["part_code"],)).fetchone()
            price = float(part["price"]) if part else 0
            conn.execute("INSERT INTO purchase_order_items (order_no,part_code,qty,price,received_qty,tax_rate) VALUES (?,?,?,?,?,?)",
                         (order_no, r["part_code"], qty, price, 0, 0.13))
            total += qty * price
        total_with_tax = round(total * 1.13, 2)
        conn.execute("UPDATE purchase_orders SET total_amount=? WHERE order_no=?", (total_with_tax, order_no))
        po_nos.append(order_no)
        first_total = total_with_tax
    conn.commit(); conn.close()
    # 兼容 MVP：返回单个 order_no（单供应商时就是那张单）
    return {"ok": True, "order_no": po_nos[0], "purchase_orders": po_nos, "total_amount_with_tax": first_total}

@app.post("/api/purchase/merge")
def merge_purchase(payload: Dict[str, Any]):
    """采购单合并（大白话：同供应商的几张单合并成一张，省运费、好对账）"""
    order_nos = payload.get("order_nos", [])
    supplier_override = payload.get("supplier_code")
    if len(order_nos) < 2:
        raise HTTPException(400, "合并至少需要 2 张采购单")
    conn = get_conn()
    pos = []
    for no in order_nos:
        po = conn.execute("SELECT * FROM purchase_orders WHERE order_no=?", (no,)).fetchone()
        if not po:
            conn.close()
            raise HTTPException(404, f"采购单 {no} 不存在")
        if po["status"] not in ("新建", "已下单"):
            conn.close()
            raise HTTPException(400, f"采购单 {no} 状态是 {po['status']}，只有新建/已下单才能合并")
        pos.append(po)
    suppliers = {p["supplier_code"] for p in pos}
    if len(suppliers) > 1 and not supplier_override:
        conn.close()
        raise HTTPException(400, f"待合并采购单供应商不一致 {suppliers}，请指定统一供应商")
    supplier = supplier_override or pos[0]["supplier_code"]
    merged: Dict[str, Dict] = {}
    for po in pos:
        for it in conn.execute("SELECT * FROM purchase_order_items WHERE order_no=?", (po["order_no"],)).fetchall():
            key = it["part_code"]
            if key in merged:
                merged[key]["qty"] += float(it["qty"])
            else:
                merged[key] = {"part_code": key, "qty": float(it["qty"]), "price": float(it["price"]), "tax_rate": float(it["tax_rate"])}
    new_no = f"PO-{datetime.now().strftime('%Y%m%d%H%M%S%f')}-MERGE"
    total = sum(v["qty"] * v["price"] * (1 + v["tax_rate"]) for v in merged.values())
    conn.execute("INSERT INTO purchase_orders (order_no,supplier_code,status,due_date,total_amount,created_at) VALUES (?,?,?,?,?,?)",
                 (new_no, supplier, "已下单", TODAY, round(total, 2), now_str()))
    for v in merged.values():
        conn.execute("INSERT INTO purchase_order_items (order_no,part_code,qty,price,received_qty,tax_rate) VALUES (?,?,?,?,?,?)",
                     (new_no, v["part_code"], v["qty"], v["price"], 0, v["tax_rate"]))
    for po in pos:
        conn.execute("UPDATE purchase_orders SET status='已合并' WHERE order_no=?", (po["order_no"],))
    conn.commit(); conn.close()
    return {"ok": True, "order_no": new_no, "merged_from": order_nos, "total_amount_with_tax": round(total, 2)}

@app.post("/api/purchase/receive")
def purchase_receive(r: ReceiveIn):
    """采购到货入库：合格 → 库存增加；不合格（质检可选）→ 进冻结，不进可用库存"""
    conn = get_conn()
    po = conn.execute("SELECT * FROM purchase_orders WHERE order_no=?", (r.order_no,)).fetchone()
    if not po:
        conn.close()
        raise HTTPException(404, f"采购单 {r.order_no} 不存在")
    item = conn.execute("SELECT * FROM purchase_order_items WHERE order_no=? AND part_code=?", (r.order_no, r.part_code)).fetchone()
    if not item:
        conn.close()
        raise HTTPException(404, f"采购单 {r.order_no} 里没有零件 {r.part_code}")
    if float(item["received_qty"]) + float(r.qty) > float(item["qty"]) + 1e-9:
        conn.close()
        raise HTTPException(400, "到货数量超过采购数量")
    conn.execute("UPDATE purchase_order_items SET received_qty=received_qty+? WHERE id=?", (r.qty, item["id"]))
    items2 = conn.execute("SELECT * FROM purchase_order_items WHERE order_no=?", (r.order_no,)).fetchall()
    total_qty = sum(float(x["qty"]) for x in items2)
    total_recv = sum(float(x["received_qty"]) for x in items2)
    if total_recv >= total_qty - 1e-9:
        new_status = "全部到货"
    elif total_recv > 0:
        new_status = "部分到货"
    else:
        new_status = "已下单"
    if new_status == "全部到货":
        conn.execute("UPDATE purchase_orders SET status=?, received_date=? WHERE order_no=?", (new_status, TODAY, r.order_no))
    else:
        conn.execute("UPDATE purchase_orders SET status=? WHERE order_no=?", (new_status, r.order_no))
    conn.commit()
    conn.close()
    if r.qc_result == "不合格":
        # 质检不合格：不进库存，进冻结（大白话：货到了但不能用，等处理）
        conn2 = get_conn()
        conn2.execute("INSERT INTO frozen_stock (part_code,qty,reason,ref_no,created_at) VALUES (?,?,?,?,?)",
                      (r.part_code, float(r.qty), "质检不合格冻结", r.order_no, now_str()))
        conn2.commit(); conn2.close()
        add_ledger(r.part_code, 0, "质检不合格", r.order_no, f"到货 {r.qty} 质检不合格，已冻结")
        return {"ok": True, "order_no": r.order_no, "part_code": r.part_code, "received": r.qty, "status": new_status, "qc": "不合格，已冻结"}
    batch = r.batch or "BATCH-20261019"
    location = r.location or "A区-01"
    adjust_stock(r.part_code, float(r.qty), "采购入库", r.order_no, "主仓", location, batch)
    return {"ok": True, "order_no": r.order_no, "part_code": r.part_code, "received": r.qty, "status": new_status, "qc": "合格"}

@app.put("/api/purchase/{order_no}/reconcile")
def reconcile_purchase(order_no: str, payload: Dict[str, Any]):
    """对账字段（大白话：供应商拿发票来对账，记下发票号和发票金额，勾上已对账）"""
    conn = get_conn()
    po = conn.execute("SELECT * FROM purchase_orders WHERE order_no=?", (order_no,)).fetchone()
    if not po:
        conn.close()
        raise HTTPException(404, f"采购单 {order_no} 不存在")
    invoice_no = payload.get("invoice_no", po["invoice_no"])
    invoice_amount = float(payload.get("invoice_amount", po["invoice_amount"] or 0) or 0)
    reconciled = int(bool(payload.get("reconciled", True)))
    conn.execute("UPDATE purchase_orders SET invoice_no=?, invoice_amount=?, reconciled=? WHERE order_no=?",
                 (invoice_no, invoice_amount, reconciled, order_no))
    conn.commit(); conn.close()
    return {"ok": True, "order_no": order_no, "invoice_no": invoice_no, "invoice_amount": invoice_amount, "reconciled": reconciled}

# ---------- 生产 ----------
@app.get("/api/production")
def list_production():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM production_orders ORDER BY id DESC").fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

def _allocate_for_production(conn, order_no: str, product_code: str, qty: float):
    """生产单创建时占用库存（大白话：先把料占上，别人就不能重复算这批料）"""
    leaves = bom_leaf_explode(conn, product_code, qty)
    for it in leaves:
        conn.execute("INSERT INTO allocations (ref_no,part_code,qty,created_at) VALUES (?,?,?,?)",
                     (order_no, it["part_code"], it["qty"], now_str()))

@app.post("/api/production")
def create_production(payload: Dict[str, Any]):
    """创建生产单（带计划开始/结束，自动占用所需原材料）"""
    order_no = payload.get("order_no", f"MO-{datetime.now().strftime('%Y%m%d%H%M%S%f')}")
    product = payload.get("product_code", "FG-001")
    qty = float(payload.get("qty", 100))
    sales_no = payload.get("sales_order_no", "")
    planned_start = payload.get("planned_start", "")
    planned_end = payload.get("planned_end", "")
    if qty <= 0:
        raise HTTPException(400, "生产数量必须 > 0")
    if planned_start and planned_end and planned_end < planned_start:
        raise HTTPException(400, "计划结束不能早于计划开始")
    conn = get_conn()
    try:
        conn.execute("INSERT INTO production_orders (order_no,product_code,qty,status,sales_order_no,created_at,planned_start,planned_end) VALUES (?,?,?,?,?,?,?,?)",
                     (order_no, product, qty, "新建", sales_no, now_str(), planned_start, planned_end))
        _allocate_for_production(conn, order_no, product, qty)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"生产单号 {order_no} 已存在")
    conn.close()
    return {"ok": True, "order_no": order_no}

@app.post("/api/production/{order_no}/plan")
def plan_production(order_no: str, payload: Dict[str, Any]):
    """修改生产单计划开始/结束"""
    conn = get_conn()
    mo = conn.execute("SELECT * FROM production_orders WHERE order_no=?", (order_no,)).fetchone()
    if not mo:
        conn.close()
        raise HTTPException(404, f"生产单 {order_no} 不存在")
    ps, pe = payload.get("planned_start", mo["planned_start"]), payload.get("planned_end", mo["planned_end"])
    if ps and pe and pe < ps:
        conn.close()
        raise HTTPException(400, "计划结束不能早于计划开始")
    conn.execute("UPDATE production_orders SET planned_start=?, planned_end=? WHERE order_no=?", (ps, pe, order_no))
    conn.commit(); conn.close()
    return {"ok": True, "order_no": order_no, "planned_start": ps, "planned_end": pe}

@app.post("/api/production/consume")
def production_consume(payload: Dict[str, Any]):
    """生产领料（多层 BOM：按叶子零件领料，领料时释放占用，库存减少）"""
    order_no = payload.get("order_no", "")
    conn = get_conn()
    mo = conn.execute("SELECT * FROM production_orders WHERE order_no=?", (order_no,)).fetchone()
    if not mo:
        conn.close()
        raise HTTPException(404, f"生产单 {order_no} 不存在")
    if mo["status"] != "新建":
        conn.close()
        raise HTTPException(400, f"生产单 {order_no} 状态是 {mo['status']}，已领过料，不能重复领")
    leaves = bom_leaf_explode(conn, mo["product_code"], float(mo["qty"]))
    for it in leaves:
        onhand = float(conn.execute("SELECT COALESCE(SUM(qty),0) FROM stock WHERE part_code=?", (it["part_code"],)).fetchone()[0] or 0)
        if onhand + 1e-9 < it["qty"]:
            conn.close()
            raise HTTPException(400, f"零件 {it['part_code']} 库存不足：需要 {it['qty']}，现有 {onhand}，请先采购入库")
    conn.execute("UPDATE production_orders SET status='已领料' WHERE order_no=?", (order_no,))
    conn.execute("DELETE FROM allocations WHERE ref_no=?", (order_no,))
    conn.commit()
    conn.close()
    for it in leaves:
        adjust_stock(it["part_code"], -it["qty"], "生产领料", order_no)
    return {"ok": True, "order_no": order_no, "status": "已领料"}

@app.post("/api/production/complete")
def production_complete(payload: Dict[str, Any]):
    """生产完工入库（大白话：成品做好了，放进仓库，成品库存增加）"""
    order_no = payload.get("order_no", "")
    conn = get_conn()
    mo = conn.execute("SELECT * FROM production_orders WHERE order_no=?", (order_no,)).fetchone()
    if not mo:
        conn.close()
        raise HTTPException(404, f"生产单 {order_no} 不存在")
    if mo["status"] != "已领料":
        conn.close()
        raise HTTPException(400, f"生产单 {order_no} 状态是 {mo['status']}，请先领料再完工")
    conn.execute("UPDATE production_orders SET status='已完工' WHERE order_no=?", (order_no,))
    conn.commit()
    conn.close()
    adjust_stock(mo["product_code"], float(mo["qty"]), "生产入库", order_no)
    return {"ok": True, "order_no": order_no, "status": "已完工", "finished_qty": float(mo["qty"])}

# ---------- 看板（四块 + 红黄绿） ----------
@app.get("/api/dashboard")
def dashboard():
    conn = get_conn()
    # 1) 缺料预警：按可用量（不是现有量）判断
    parts = conn.execute("SELECT * FROM parts ORDER BY code").fetchall()
    conn.close()
    shortage_items = []
    for p in parts:
        code = p["code"]
        avail = get_available(code)
        st = stock_status(avail, float(p["safety_stock"] or 0), float(p["max_stock"] or 0))
        if st in ("红", "黄"):
            reason = "已断货" if avail <= 0 else ("低于安全库存" if avail < float(p["safety_stock"] or 0) else "高于最高库存")
            shortage_items.append({"part_code": code, "part_name": p["name"], "available": avail,
                                   "safety_stock": p["safety_stock"], "max_stock": p["max_stock"],
                                   "status": st, "reason": reason})
    shortage_status = "红" if any(x["status"] == "红" for x in shortage_items) else ("黄" if shortage_items else "绿")
    # 2) 拖期采购
    conn = get_conn()
    pos = conn.execute("SELECT * FROM purchase_orders WHERE status IN ('新建','已下单','部分到货')").fetchall()
    conn.close()
    overdue_items = []
    for po in pos:
        if is_overdue(po["due_date"], po["status"]):
            overdue_items.append({"order_no": po["order_no"], "supplier_code": po["supplier_code"],
                                  "due_date": po["due_date"], "status": po["status"],
                                  "overdue_days": overdue_days(po["due_date"]), "color": "红"})
    overdue_status = "红" if overdue_items else "绿"
    # 3) 供应商拖期排行：拖期单数从多到少
    ranking_map: Dict[str, int] = {}
    for it in overdue_items:
        ranking_map[it["supplier_code"]] = ranking_map.get(it["supplier_code"], 0) + 1
    ranking = [{"supplier_code": k, "overdue_count": v} for k, v in sorted(ranking_map.items(), key=lambda x: -x[1])]
    ranking_status = "红" if ranking else "绿"
    # 4) 生产缺料：未领料生产单，按 BOM 叶子需求 vs 可用量
    conn = get_conn()
    mos = conn.execute("SELECT * FROM production_orders WHERE status='新建'").fetchall()
    conn.close()
    prod_shortage = []
    for mo in mos:
        conn2 = get_conn()
        leaves = bom_leaf_explode(conn2, mo["product_code"], float(mo["qty"]))
        conn2.close()
        missing = []
        for it in leaves:
            avail = get_available(it["part_code"])
            # 可用量里已包含本单的占用，要把本单占用加回来才是“本单能不能领”
            own_alloc = 0
            conn3 = get_conn()
            r = conn3.execute("SELECT COALESCE(SUM(qty),0) FROM allocations WHERE ref_no=? AND part_code=?", (mo["order_no"], it["part_code"])).fetchone()
            conn3.close()
            own_alloc = float(r[0] or 0)
            if avail + own_alloc + 1e-9 < it["qty"]:
                missing.append({"part_code": it["part_code"], "need": it["qty"], "available_for_this_order": avail + own_alloc})
        if missing:
            prod_shortage.append({"order_no": mo["order_no"], "product_code": mo["product_code"], "qty": mo["qty"],
                                  "planned_start": mo["planned_start"], "planned_end": mo["planned_end"],
                                  "missing": missing, "status": "红"})
    prod_status = "红" if prod_shortage else "绿"
    return {
        "shortage": {"title": "缺料预警", "status": shortage_status, "count": len(shortage_items), "items": shortage_items},
        "overdue_purchase": {"title": "拖期采购", "status": overdue_status, "count": len(overdue_items), "items": overdue_items},
        "supplier_ranking": {"title": "供应商拖期排行", "status": ranking_status, "count": len(ranking), "items": ranking},
        "production_shortage": {"title": "生产缺料", "status": prod_status, "count": len(prod_shortage), "items": prod_shortage},
        # 兼容 MVP 的旧字段
        "low_stock": shortage_items,
        "pending_purchase": [row_to_dict(r) if hasattr(r, "keys") else r for r in pos],
        "stock_count": len(parts),
    }

# ---------- Excel 导入导出 ----------
COLUMN_ALIASES = {
    "code": ["编码", "零件编码", "物料编码", "材料编码", "料号", "code", "part_code", "零件编号", "物料编号"],
    "name": ["名称", "零件名称", "物料名称", "材料名称", "品名", "name"],
    "spec": ["规格", "规格型号", "spec"],
    "unit": ["单位", "unit"],
    "category": ["类别", "分类", "category"],
    "safety_stock": ["安全库存", "安全库存量", "safety_stock"],
    "min_stock": ["最低库存", "min_stock"],
    "max_stock": ["最高库存", "max_stock"],
    "lead_time_days": ["提前期", "提前期天数", "lead_time_days", "交期天数"],
    "price": ["单价", "未税单价", "价格", "price"],
    "tax_rate": ["税率", "tax_rate"],
    "qty": ["数量", "用量", "库存数量", "库存", "qty", "期末数量", "结存数量"],
    "warehouse": ["仓库", "warehouse"],
    "location": ["库位", "货位", "location"],
    "batch": ["批次", "批次号", "batch"],
    "supplier_code": ["供应商编码", "供应商", "supplier_code"],
    "supplier_name": ["供应商名称", "供应商名"],
    "order_no": ["单号", "采购单号", "采购单", "order_no", "单据号", "销售单号", "生产单号"],
    "part_code": ["零件编码", "物料编码", "材料编码", "料号", "part_code"],
    "parent_code": ["父件编码", "父件", "成品编码", "成品", "parent_code"],
    "child_code": ["子件编码", "子件", "child_code"],
    "version": ["版本号", "版本", "version"],
    "effective_date": ["生效日期", "生效时间", "effective_date"],
    "loss_rate": ["损耗率", "损耗", "loss_rate"],
    "customer": ["客户", "客户名称", "customer"],
    "product_code": ["成品编码", "产品编码", "product_code"],
    "due_date": ["交期", "需求日期", "到期日期", "due_date"],
    "status": ["状态", "status"],
    "planned_start": ["计划开始", "计划开始日期", "planned_start"],
    "planned_end": ["计划结束", "计划结束日期", "planned_end"],
    "sales_order_no": ["关联销售单", "销售单号", "sales_order_no"],
    "contact": ["联系人", "contact"],
    "phone": ["电话", "手机", "phone"],
    "address": ["地址", "address"],
    "payment": ["结算方式", "payment"],
    "rating": ["评级", "合格评级", "rating"],
    "gross": ["毛需求", "gross"],
    "onhand": ["可用库存", "现有库存", "onhand"],
    "in_transit": ["在途", "in_transit"],
    "safety": ["安全库存", "safety"],
    "net": ["净需求", "net"],
    "adjusted_net": ["调整后数量", "调整数量", "adjusted_net"],
    "latest_order_date": ["最晚下单日", "latest_order_date"],
    "applicant": ["申请人", "applicant"],
    "reason": ["原因", "申请原因", "reason"],
}

def normalize_header(header: str) -> str:
    if header is None:
        return ""
    return str(header).strip().replace(" ", "").replace("　", "").lower()

def build_header_map(headers: List[str]) -> Dict[str, int]:
    """灵活映射：先到先得（大白话：同一个叫法只认第一个字段，避免互相抢）"""
    norm_headers = [normalize_header(h) for h in headers]
    alias_to_field: Dict[str, str] = {}
    for field, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            na = normalize_header(a)
            if na not in alias_to_field:
                alias_to_field[na] = field
    mapping: Dict[str, int] = {}
    for idx, nh in enumerate(norm_headers):
        if nh in alias_to_field:
            field = alias_to_field[nh]
            if field not in mapping:
                mapping[field] = idx
    return mapping

def excel_to_rows(file_bytes: bytes):
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise HTTPException(500, "openpyxl 未安装，请 pip install openpyxl")
    wb = load_workbook(filename=io.BytesIO(file_bytes), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], []
    headers = [str(c) if c is not None else "" for c in rows[0]]
    data = [r for r in rows[1:] if any(v is not None and str(v).strip() != "" for v in r)]
    return headers, data

def rows_to_excel(headers: List[str], data: List[List[Any]], sheet_name: str = "数据") -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="4472C4")
    for col, h in enumerate(headers, start=1):
        c = ws.cell(row=1, column=col, value=h)
        c.font = header_font; c.fill = header_fill
        c.alignment = Alignment(horizontal="center", vertical="center")
    for r_idx, row in enumerate(data, start=2):
        for c_idx, v in enumerate(row, start=1):
            ws.cell(row=r_idx, column=c_idx, value=v)
    for col in ws.columns:
        max_len = 0
        for cell in col:
            if cell.value is not None:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 30)
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()

def _excel_response(content: bytes, filename: str):
    return StreamingResponse(io.BytesIO(content), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename={filename}"})

TEMPLATES = {
    "parts": (["零件编码", "名称", "规格", "单位", "类别", "安全库存", "最低库存", "最高库存", "提前期天数", "未税单价", "税率"],
              [["RM-101", "示例零件", "示例规格", "个", "原材料", 20, 10, 200, 7, 2.5, 0.13]]),
    "stock": (["零件编码", "仓库", "库位", "批次号", "数量"], [["RM-001", "主仓", "A区-01", "BATCH-001", 100]]),
    "purchase": (["采购单号", "供应商编码", "零件编码", "数量", "未税单价", "税率"], [["PO-001", "SUP-001", "RM-001", 100, 2.5, 0.13]]),
    "bom": (["父件编码", "子件编码", "用量", "损耗率", "版本号", "生效日期"], [["FG-001", "RM-001", 1, 0, "V1", "2026-01-01"]]),
    "supplier": (["供应商编码", "供应商名称", "联系人", "电话", "地址", "结算方式", "交期天数", "评级", "状态"],
                 [["SUP-101", "示例供应商", "王经理", "13800000000", "深圳市", "月结30天", 7, "合格", "合格"]]),
    "sales": (["销售单号", "客户", "成品编码", "数量", "交期"], [["SO-101", "示例客户", "FG-001", 100, "2026-12-31"]]),
    "production": (["生产单号", "成品编码", "数量", "计划开始", "计划结束", "关联销售单"],
                   [["MO-101", "FG-001", 100, "2026-11-01", "2026-11-10", "SO-101"]]),
}

@app.get("/api/excel/template/{kind}")
def excel_template(kind: str):
    if kind not in TEMPLATES:
        raise HTTPException(404, f"模板 {kind} 不存在，可选 {','.join(TEMPLATES)}")
    headers, data = TEMPLATES[kind]
    return _excel_response(rows_to_excel(headers, data, sheet_name="模板"), f"template_{kind}.xlsx")

@app.get("/api/excel/export/{kind}")
def excel_export(kind: str, sales_order_no: Optional[str] = None):
    conn = get_conn()
    if kind == "parts":
        headers = ["零件编码", "名称", "规格", "单位", "类别", "安全库存", "最低库存", "最高库存", "提前期天数", "未税单价", "税率", "供应商编码"]
        rows = conn.execute("SELECT code,name,spec,unit,category,safety_stock,min_stock,max_stock,lead_time_days,price,tax_rate,supplier_code FROM parts ORDER BY code").fetchall()
        data = [list(r) for r in rows]
    elif kind == "stock":
        headers = ["零件编码", "名称", "仓库", "库位", "批次号", "数量", "单位", "安全库存", "最高库存"]
        rows = conn.execute("SELECT s.part_code,p.name,s.warehouse,s.location,s.batch,s.qty,p.unit,p.safety_stock,p.max_stock FROM stock s LEFT JOIN parts p ON p.code=s.part_code ORDER BY s.part_code").fetchall()
        data = [list(r) for r in rows]
    elif kind == "purchase":
        headers = ["采购单号", "供应商编码", "状态", "交期", "含税总额", "发票号", "发票金额", "是否已对账", "零件编码", "数量", "未税单价", "已到货数量"]
        rows = conn.execute("""
            SELECT po.order_no,po.supplier_code,po.status,po.due_date,po.total_amount,po.invoice_no,po.invoice_amount,po.reconciled,poi.part_code,poi.qty,poi.price,poi.received_qty
            FROM purchase_orders po LEFT JOIN purchase_order_items poi ON poi.order_no=po.order_no ORDER BY po.order_no
        """).fetchall()
        data = [list(r) for r in rows]
    elif kind == "bom":
        headers = ["父件编码", "子件编码", "用量", "损耗率", "版本号", "生效日期"]
        rows = conn.execute("SELECT parent_code,child_code,qty,loss_rate,version,effective_date FROM bom_items ORDER BY parent_code, version, child_code").fetchall()
        data = [list(r) for r in rows]
    elif kind == "supplier":
        headers = ["供应商编码", "供应商名称", "联系人", "电话", "地址", "结算方式", "交期天数", "评级", "状态"]
        rows = conn.execute("SELECT code,name,contact,phone,address,payment,lead_time_days,rating,status FROM suppliers ORDER BY code").fetchall()
        data = [list(r) for r in rows]
    elif kind == "sales":
        headers = ["销售单号", "客户", "成品编码", "数量", "交期", "状态"]
        rows = conn.execute("SELECT order_no,customer,product_code,qty,due_date,status FROM sales_orders ORDER BY order_no").fetchall()
        data = [list(r) for r in rows]
    elif kind == "production":
        headers = ["生产单号", "成品编码", "数量", "计划开始", "计划结束", "状态", "关联销售单"]
        rows = conn.execute("SELECT order_no,product_code,qty,planned_start,planned_end,status,sales_order_no FROM production_orders ORDER BY order_no").fetchall()
        data = [list(r) for r in rows]
    elif kind == "mrp":
        headers = ["销售单号", "零件编码", "零件名称", "层级", "需求类型", "毛需求", "可用库存", "在途", "安全库存", "净需求", "调整后数量", "最晚下单日"]
        if sales_order_no:
            rows = conn.execute("SELECT sales_order_no,part_code,part_name,level,need_type,gross,onhand,in_transit,safety,net,adjusted_net,latest_order_date FROM mrp_results WHERE sales_order_no=? ORDER BY level, part_code", (sales_order_no,)).fetchall()
        else:
            rows = conn.execute("SELECT sales_order_no,part_code,part_name,level,need_type,gross,onhand,in_transit,safety,net,adjusted_net,latest_order_date FROM mrp_results ORDER BY sales_order_no, level, part_code").fetchall()
        data = [list(r) for r in rows]
    else:
        conn.close()
        raise HTTPException(404, f"导出 {kind} 不存在，可选 parts/stock/purchase/bom/supplier/sales/production/mrp")
    conn.close()
    return _excel_response(rows_to_excel(headers, data, sheet_name=kind), f"export_{kind}.xlsx")

@app.post("/api/excel/import/{kind}")
async def excel_import(kind: str, file: UploadFile = File(...)):
    """
    导入 Excel（灵活映射 + 行号校验）。
    支持 kinds：parts / stock / purchase / bom / supplier / sales / production
    """
    content = await file.read()
    headers, data_rows = excel_to_rows(content)
    if not headers:
        raise HTTPException(400, "Excel 为空，请先下载模板填写")
    mapping = build_header_map(headers)
    errors: List[str] = []
    imported = 0
    conn = get_conn()
    try:
        def get(row, *fields, default=""):
            for f in fields:
                idx = mapping.get(f)
                if idx is not None and idx < len(row) and row[idx] is not None:
                    return row[idx]
            return default
        if kind == "parts":
            if "code" not in mapping:
                raise HTTPException(400, f"表头未识别出零件编码。识别到的表头：{headers}，映射：{mapping}")
            for i, row in enumerate(data_rows, start=2):
                code = str(get(row, "code", default="")).strip()
                name = str(get(row, "name", default="")).strip()
                if not code or not name:
                    errors.append(f"第 {i} 行：零件编码/名称不能为空"); continue
                try:
                    safety = float(get(row, "safety_stock", default=0) or 0)
                    maxs = float(get(row, "max_stock", default=0) or 0)
                    price = float(get(row, "price", default=0) or 0)
                    tax = float(get(row, "tax_rate", default=0.13) or 0.13)
                    lead = int(float(get(row, "lead_time_days", default=7) or 7))
                except ValueError:
                    errors.append(f"第 {i} 行：安全库存/最高库存/单价/税率/提前期不是数字"); continue
                existing = conn.execute("SELECT id FROM parts WHERE code=?", (code,)).fetchone()
                if existing:
                    conn.execute("UPDATE parts SET name=?,spec=?,unit=?,category=?,safety_stock=?,max_stock=?,price=?,tax_rate=?,lead_time_days=? WHERE code=?",
                                 (name, str(get(row, "spec", default="")), str(get(row, "unit", default="个") or "个"), str(get(row, "category", default="原材料") or "原材料"), safety, maxs, price, tax, lead, code))
                else:
                    conn.execute("INSERT INTO parts (code,name,spec,unit,category,is_purchase,safety_stock,min_stock,max_stock,lead_time_days,price,tax_rate,supplier_code,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (code, name, str(get(row, "spec", default="")), str(get(row, "unit", default="个") or "个"), str(get(row, "category", default="原材料") or "原材料"), 1, safety, 0, maxs, lead, price, tax, str(get(row, "supplier_code", default="")), now_str()))
                imported += 1
        elif kind == "stock":
            if "part_code" not in mapping and "code" not in mapping:
                raise HTTPException(400, f"表头未识别出零件编码。识别到的表头：{headers}")
            for i, row in enumerate(data_rows, start=2):
                code = str(get(row, "part_code", "code", default="")).strip()
                if not code:
                    errors.append(f"第 {i} 行：零件编码不能为空"); continue
                try:
                    qty = float(get(row, "qty", default=0) or 0)
                except ValueError:
                    errors.append(f"第 {i} 行：数量不是数字"); continue
                warehouse = str(get(row, "warehouse", default="主仓") or "主仓")
                location = str(get(row, "location", default="A区-01") or "A区-01")
                batch = str(get(row, "batch", default="BATCH-IMPORT") or "BATCH-IMPORT")
                existing = conn.execute("SELECT id,qty FROM stock WHERE part_code=? AND warehouse=? AND location=? AND batch=?", (code, warehouse, location, batch)).fetchone()
                if existing:
                    delta = qty - float(existing["qty"])
                    conn.execute("UPDATE stock SET qty=? WHERE id=?", (qty, existing["id"]))
                else:
                    delta = qty
                    conn.execute("INSERT INTO stock (part_code,warehouse,location,batch,qty) VALUES (?,?,?,?,?)", (code, warehouse, location, batch, qty))
                if delta != 0:
                    conn.execute("INSERT INTO stock_ledger (part_code,warehouse,batch,change_qty,type,ref_no,remark,created_at) VALUES (?,?,?,?,?,?,?,?)",
                                 (code, warehouse, batch, delta, "期初导入", "Excel导入", "Excel导入库存", now_str()))
                imported += 1
        elif kind == "purchase":
            if "order_no" not in mapping:
                raise HTTPException(400, f"表头未识别出采购单号。识别到的表头：{headers}")
            grouped: Dict[str, List[Dict]] = {}
            for i, row in enumerate(data_rows, start=2):
                order_no = str(get(row, "order_no", default="")).strip()
                part_code = str(get(row, "part_code", "code", default="")).strip()
                if not order_no or not part_code:
                    errors.append(f"第 {i} 行：采购单号/零件编码不能为空"); continue
                try:
                    qty = float(get(row, "qty", default=0) or 0)
                    price = float(get(row, "price", default=0) or 0)
                except ValueError:
                    errors.append(f"第 {i} 行：数量/单价不是数字"); continue
                grouped.setdefault(order_no, []).append({"supplier": str(get(row, "supplier_code", default="SUP-001") or "SUP-001"),
                    "part_code": part_code, "qty": qty, "price": price, "tax": float(get(row, "tax_rate", default=0.13) or 0.13)})
            for order_no, items in grouped.items():
                if conn.execute("SELECT id FROM purchase_orders WHERE order_no=?", (order_no,)).fetchone():
                    errors.append(f"采购单 {order_no} 已存在，跳过（避免重复导入）"); continue
                total = sum(it["qty"] * it["price"] * (1 + it["tax"]) for it in items)
                conn.execute("INSERT INTO purchase_orders (order_no,supplier_code,status,due_date,total_amount,created_at) VALUES (?,?,?,?,?,?)",
                             (order_no, items[0]["supplier"], "已下单", TODAY, round(total, 2), now_str()))
                for it in items:
                    conn.execute("INSERT INTO purchase_order_items (order_no,part_code,qty,price,received_qty,tax_rate) VALUES (?,?,?,?,?,?)",
                                 (order_no, it["part_code"], it["qty"], it["price"], 0, it["tax"]))
                imported += 1
        elif kind == "bom":
            if "parent_code" not in mapping:
                raise HTTPException(400, f"表头未识别出父件编码。识别到的表头：{headers}，映射：{mapping}")
            for i, row in enumerate(data_rows, start=2):
                parent = str(get(row, "parent_code", default="")).strip()
                child = str(get(row, "child_code", "part_code", "code", default="")).strip()
                if not parent or not child:
                    errors.append(f"第 {i} 行：父件编码/子件编码不能为空"); continue
                if parent == child:
                    errors.append(f"第 {i} 行：BOM 不能自己引用自己：{parent}"); continue
                try:
                    qty = float(get(row, "qty", default=0) or 0)
                    loss = float(get(row, "loss_rate", default=0) or 0)
                except ValueError:
                    errors.append(f"第 {i} 行：用量/损耗率不是数字"); continue
                if qty <= 0:
                    errors.append(f"第 {i} 行：用量必须 > 0"); continue
                version = str(get(row, "version", default="V1") or "V1")
                eff = str(get(row, "effective_date", default="") or "")
                conn.execute("INSERT INTO bom_versions (parent_code,version,effective_date,status,remark) VALUES (?,?,?,?,?) ON CONFLICT(parent_code,version) DO UPDATE SET effective_date=excluded.effective_date",
                             (parent, version, eff, "生效" if eff and eff <= TODAY else "未生效", "Excel导入"))
                conn.execute("INSERT INTO bom_items (parent_code,child_code,qty,loss_rate,version,effective_date) VALUES (?,?,?,?,?,?) ON CONFLICT(parent_code,child_code,version) DO UPDATE SET qty=excluded.qty, loss_rate=excluded.loss_rate, effective_date=excluded.effective_date",
                             (parent, child, qty, loss, version, eff))
                imported += 1
        elif kind == "supplier":
            if "supplier_code" not in mapping:
                raise HTTPException(400, f"表头未识别出供应商编码。识别到的表头：{headers}")
            for i, row in enumerate(data_rows, start=2):
                code = str(get(row, "supplier_code", default="")).strip()
                name = str(get(row, "supplier_name", "name", default="")).strip()
                if not code or not name:
                    errors.append(f"第 {i} 行：供应商编码/名称不能为空"); continue
                try:
                    lead = int(float(get(row, "lead_time_days", default=7) or 7))
                except ValueError:
                    errors.append(f"第 {i} 行：交期天数不是数字"); continue
                conn.execute("INSERT INTO suppliers (code,name,contact,phone,address,payment,lead_time_days,rating,status) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name, contact=excluded.contact, phone=excluded.phone, address=excluded.address, payment=excluded.payment, lead_time_days=excluded.lead_time_days, rating=excluded.rating, status=excluded.status",
                             (code, name, str(get(row, "contact", default="")), str(get(row, "phone", default="")), str(get(row, "address", default="")), str(get(row, "payment", default="")), lead, str(get(row, "rating", default="合格") or "合格"), str(get(row, "status", default="合格") or "合格")))
                imported += 1
        elif kind == "sales":
            if "order_no" not in mapping:
                raise HTTPException(400, f"表头未识别出销售单号。识别到的表头：{headers}")
            for i, row in enumerate(data_rows, start=2):
                order_no = str(get(row, "order_no", default="")).strip()
                product = str(get(row, "product_code", "parent_code", default="")).strip()
                if not order_no or not product:
                    errors.append(f"第 {i} 行：销售单号/成品编码不能为空"); continue
                try:
                    qty = float(get(row, "qty", default=0) or 0)
                except ValueError:
                    errors.append(f"第 {i} 行：数量不是数字"); continue
                conn.execute("INSERT INTO sales_orders (order_no,customer,product_code,qty,due_date,status,created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(order_no) DO UPDATE SET customer=excluded.customer, product_code=excluded.product_code, qty=excluded.qty, due_date=excluded.due_date",
                             (order_no, str(get(row, "customer", default="")), product, qty, str(get(row, "due_date", default="") or ""), "新建", now_str()))
                imported += 1
        elif kind == "production":
            if "order_no" not in mapping:
                raise HTTPException(400, f"表头未识别出生产单号。识别到的表头：{headers}")
            for i, row in enumerate(data_rows, start=2):
                order_no = str(get(row, "order_no", default="")).strip()
                product = str(get(row, "product_code", "parent_code", default="")).strip()
                if not order_no or not product:
                    errors.append(f"第 {i} 行：生产单号/成品编码不能为空"); continue
                try:
                    qty = float(get(row, "qty", default=0) or 0)
                except ValueError:
                    errors.append(f"第 {i} 行：数量不是数字"); continue
                ps = str(get(row, "planned_start", default="") or "")
                pe = str(get(row, "planned_end", default="") or "")
                if ps and pe and pe < ps:
                    errors.append(f"第 {i} 行：计划结束不能早于计划开始"); continue
                existing = conn.execute("SELECT id FROM production_orders WHERE order_no=?", (order_no,)).fetchone()
                if existing:
                    conn.execute("UPDATE production_orders SET product_code=?, qty=?, planned_start=?, planned_end=?, sales_order_no=? WHERE order_no=?",
                                 (product, qty, ps, pe, str(get(row, "sales_order_no", default="") or ""), order_no))
                else:
                    conn.execute("INSERT INTO production_orders (order_no,product_code,qty,status,sales_order_no,created_at,planned_start,planned_end) VALUES (?,?,?,?,?,?,?,?)",
                                 (order_no, product, qty, "新建", str(get(row, "sales_order_no", default="") or ""), now_str(), ps, pe))
                    _allocate_for_production(conn, order_no, product, qty)
                imported += 1
        else:
            raise HTTPException(404, f"导入 {kind} 不存在，可选 parts/stock/purchase/bom/supplier/sales/production")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "imported": imported, "errors": errors, "mapping": mapping, "headers": headers}

# ---------- 静态前端 ----------
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")
if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

@app.get("/health")
def health():
    return {"ok": True, "msg": "PMC 第一阶段运行正常"}
