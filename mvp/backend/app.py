# -*- coding: utf-8 -*-
"""
PMC 系统 MVP - 后端
技术：Python + FastAPI + SQLite（大白话：轻量单文件数据库，MVP 先用它，后期再切 PostgreSQL）
参考出处（先查再做，严禁猜测）：
- MRP 公式：净需求 = 毛需求 - 可用库存 - 在途 + 安全库存
  来源：ERPNext / 通用 MRP 教材，见验证报告“参考来源”一节
  具体：Gross Requirements - (Scheduled Receipts + Available Inventory) = Net Requirements
  参考链接见 验证报告.md
- 库存流水：库存汇总 + 库存流水分开，流水用于追溯，汇总用于快速查
  来源：InvenTree 的 Part / Stock 思路（库存流水表 + 汇总），ERPNext 的 Stock Ledger 思路
- Excel 导入导出：openpyxl + SQLite + FastAPI FileResponse 模式
  来源：社区常见做法（见验证报告）
注意：本代码为独立重写，未复制 GPL 代码（ERPNext 是 GPLv3，只学思路不抄原文）。
作者：Muse（PMC MVP 建造）
"""
import os
import sqlite3
import io
from datetime import date, datetime
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 大白话：数据库文件路径，MVP 用 SQLite 单文件，放在 backend 目录下
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "pmc_mvp.db")

app = FastAPI(title="PMC 系统 MVP", description="生产物料控制（大白话：管缺啥料、买多少、什么时候买）MVP")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- 数据库（大白话：存数据的仓库） ----------
def get_conn():
    """拿到数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # 大白话：让查询结果能按列名取，如 row["code"]
    return conn

def init_db():
    """建表 + 种子数据（大白话：第一次运行时把表建好，并放进 LED 小夜灯的例子数据）"""
    conn = get_conn()
    cur = conn.cursor()
    # 零件表：存所有零件档案
    cur.execute("""
        CREATE TABLE IF NOT EXISTS parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,  -- 零件编码（大白话：零件的身份证号，全局唯一）
            name TEXT NOT NULL,          -- 名称
            spec TEXT DEFAULT '',        -- 规格
            unit TEXT DEFAULT '个',      -- 单位
            category TEXT DEFAULT '原材料', -- 类别：成品/半成品/原材料
            is_purchase INTEGER DEFAULT 1, -- 是否采购件：1=外购，0=自制
            safety_stock REAL DEFAULT 0,   -- 安全库存（大白话：至少要留多少，防止断货）
            min_stock REAL DEFAULT 0,      -- 最低库存
            lead_time_days INTEGER DEFAULT 7, -- 提前期（大白话：从下单到到货要几天）
            price REAL DEFAULT 0,          -- 未税单价
            tax_rate REAL DEFAULT 0.13,    -- 税率：中国常见 13%
            supplier_code TEXT DEFAULT '', -- 默认供应商编码
            created_at TEXT DEFAULT ''
        )
    """)
    # BOM 明细表：配方表，一行 = 一个成品需要一个子件多少个
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bom_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_code TEXT NOT NULL,  -- 父件编码（成品）
            child_code TEXT NOT NULL,   -- 子件编码（零件）
            qty REAL NOT NULL,          -- 用量（做 1 个父件需要几个子件）
            loss_rate REAL DEFAULT 0,   -- 损耗率（大白话：生产中会坏掉的比例）
            version TEXT DEFAULT 'V1',
            UNIQUE(parent_code, child_code)
        )
    """)
    # 库存表：每个零件、每个仓库、每个批次还剩多少（汇总表）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            part_code TEXT NOT NULL,
            warehouse TEXT DEFAULT '主仓', -- 仓库
            location TEXT DEFAULT '',      -- 库位（大白话：仓库里的具体位置，如 A区-01）
            batch TEXT DEFAULT '',         -- 批次号（大白话：同一批到的料算一批，方便追溯）
            qty REAL DEFAULT 0,            -- 数量
            UNIQUE(part_code, warehouse, location, batch)
        )
    """)
    # 库存流水表：每次入库、出库都记一笔（大白话：账本，用于追溯）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            part_code TEXT NOT NULL,
            warehouse TEXT DEFAULT '主仓',
            batch TEXT DEFAULT '',
            change_qty REAL NOT NULL, -- 变化量：正=入库，负=出库
            type TEXT NOT NULL,       -- 类型：采购入库/生产领料/生产入库/期初/盘点
            ref_no TEXT DEFAULT '',   -- 关联单号
            remark TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        )
    """)
    # 供应商表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            contact TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            address TEXT DEFAULT '',
            payment TEXT DEFAULT '',
            lead_time_days INTEGER DEFAULT 7,
            rating TEXT DEFAULT '合格'
        )
    """)
    # 供应商零件价格表：同一个零件，不同供应商不同价
    cur.execute("""
        CREATE TABLE IF NOT EXISTS supplier_parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_code TEXT NOT NULL,
            part_code TEXT NOT NULL,
            price REAL DEFAULT 0,
            tax_rate REAL DEFAULT 0.13,
            UNIQUE(supplier_code, part_code)
        )
    """)
    # 销售单表（轻量版，大白话：客户要多少成品）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sales_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT UNIQUE NOT NULL,
            customer TEXT DEFAULT '',
            product_code TEXT NOT NULL,
            qty REAL NOT NULL,
            due_date TEXT DEFAULT '',
            status TEXT DEFAULT '新建',
            created_at TEXT DEFAULT ''
        )
    """)
    # 采购单表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS purchase_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT UNIQUE NOT NULL,
            supplier_code TEXT NOT NULL,
            status TEXT DEFAULT '新建', -- 新建/已下单/部分到货/全部到货/已入库
            due_date TEXT DEFAULT '',
            total_amount REAL DEFAULT 0,
            created_at TEXT DEFAULT ''
        )
    """)
    # 采购单明细表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS purchase_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT NOT NULL,
            part_code TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL DEFAULT 0,
            received_qty REAL DEFAULT 0, -- 已到货数量
            tax_rate REAL DEFAULT 0.13
        )
    """)
    # 生产单表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS production_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT UNIQUE NOT NULL,
            product_code TEXT NOT NULL,
            qty REAL NOT NULL,
            status TEXT DEFAULT '新建', -- 新建/已领料/已完工
            sales_order_no TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        )
    """)
    # MRP 结果表（每次计算存一份，方便复核）
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mrp_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sales_order_no TEXT NOT NULL,
            part_code TEXT NOT NULL,
            part_name TEXT DEFAULT '',
            gross REAL DEFAULT 0,   -- 毛需求（大白话：总共需要多少）
            onhand REAL DEFAULT 0,  -- 可用库存
            in_transit REAL DEFAULT 0, -- 在途（大白话：已经下了采购单但还没到货的）
            safety REAL DEFAULT 0,  -- 安全库存
            net REAL DEFAULT 0,     -- 净需求（大白话：真正缺多少）
            created_at TEXT DEFAULT ''
        )
    """)
    conn.commit()

    # ---- 种子数据：LED 小夜灯例子（如果 parts 为空才插入） ----
    cnt = cur.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
    if cnt == 0:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 成品 + 5 个零件
        parts = [
            ("FG-001", "小夜灯", "LED小夜灯成品", "个", "成品", 0, 0, 0, 3, 0, 0.13, ""),
            ("RM-001", "外壳", "小夜灯外壳 白色", "个", "原材料", 1, 20, 10, 7, 2.5, 0.13, "SUP-001"),
            ("RM-002", "LED灯珠", "LED灯珠 3W", "个", "原材料", 1, 20, 10, 7, 0.8, 0.13, "SUP-001"),
            ("RM-003", "电池", "锂电池 18650", "个", "原材料", 1, 20, 10, 7, 5.0, 0.13, "SUP-001"),
            ("RM-004", "开关", "轻触开关", "个", "原材料", 1, 20, 10, 7, 0.5, 0.13, "SUP-001"),
            ("RM-005", "电路板", "小夜灯电路板", "个", "原材料", 1, 20, 10, 7, 3.0, 0.13, "SUP-001"),
        ]
        for p in parts:
            cur.execute("INSERT INTO parts (code,name,spec,unit,category,is_purchase,safety_stock,min_stock,lead_time_days,price,tax_rate,supplier_code,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (*p, now))
        # BOM：做 1 个小夜灯需要各 1 个零件
        for child in ["RM-001","RM-002","RM-003","RM-004","RM-005"]:
            cur.execute("INSERT INTO bom_items (parent_code,child_code,qty,loss_rate,version) VALUES (?,?,?,?,?)", ("FG-001", child, 1, 0, "V1"))
        # 供应商
        cur.execute("INSERT INTO suppliers (code,name,contact,phone,address,payment,lead_time_days,rating) VALUES (?,?,?,?,?,?,?,?)",
                    ("SUP-001","深圳光电供应商A","张经理","13800000001","深圳市宝安区","月结30天",7,"合格"))
        for code, price in [("RM-001",2.5),("RM-002",0.8),("RM-003",5.0),("RM-004",0.5),("RM-005",3.0)]:
            cur.execute("INSERT INTO supplier_parts (supplier_code,part_code,price,tax_rate) VALUES (?,?,?,?)", ("SUP-001", code, price, 0.13))
        # 期初库存：故意设成不够做 100 个，用来验证 MRP 缺料
        # 小夜灯成品 0，外壳 20，灯珠 30，电池 10，开关 50，电路板 15
        init_stock = [("FG-001",0),("RM-001",20),("RM-002",30),("RM-003",10),("RM-004",50),("RM-005",15)]
        for code, qty in init_stock:
            cur.execute("INSERT INTO stock (part_code,warehouse,location,batch,qty) VALUES (?,?,?,?,?)", (code,"主仓","A区-01","BATCH-20261019",qty))
            if qty > 0:
                cur.execute("INSERT INTO stock_ledger (part_code,warehouse,batch,change_qty,type,ref_no,remark,created_at) VALUES (?,?,?,?,?,?,?,?)",
                            (code,"主仓","BATCH-20261019",qty,"期初","期初","MVP种子期初",now))
        conn.commit()
    conn.close()

init_db()

# ---------- 工具函数 ----------
def row_to_dict(r):
    return dict(r) if r else None

def get_stock_qty(part_code: str) -> float:
    """查一个零件的总库存（大白话：所有仓库、所有批次加起来）"""
    conn = get_conn()
    v = conn.execute("SELECT COALESCE(SUM(qty),0) FROM stock WHERE part_code=?", (part_code,)).fetchone()[0]
    conn.close()
    return float(v or 0)

def get_in_transit(part_code: str) -> float:
    """查在途数量（大白话：已下采购单但还没到货的）"""
    conn = get_conn()
    v = conn.execute("""
        SELECT COALESCE(SUM(poi.qty - poi.received_qty),0)
        FROM purchase_order_items poi
        JOIN purchase_orders po ON po.order_no = poi.order_no
        WHERE poi.part_code=? AND po.status IN ('新建','已下单','部分到货')
    """, (part_code,)).fetchone()[0]
    conn.close()
    return float(v or 0)

def add_ledger(part_code, qty_change, typ, ref_no, remark=""):
    conn = get_conn()
    conn.execute("INSERT INTO stock_ledger (part_code,warehouse,batch,change_qty,type,ref_no,remark,created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (part_code,"主仓","BATCH-20261019",qty_change,typ,ref_no,remark,datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def adjust_stock(part_code, delta, typ, ref_no):
    """调库存（大白话：入库就加，出库就减），同时记流水"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM stock WHERE part_code=? AND warehouse='主仓' AND location='A区-01' AND batch='BATCH-20261019'", (part_code,)).fetchone()
    if row:
        conn.execute("UPDATE stock SET qty=qty+? WHERE id=?", (delta, row["id"]))
    else:
        conn.execute("INSERT INTO stock (part_code,warehouse,location,batch,qty) VALUES (?,?,?,?,?)", (part_code,"主仓","A区-01","BATCH-20261019",delta))
    conn.commit()
    conn.close()
    add_ledger(part_code, delta, typ, ref_no, f"{typ} {delta}")

# ---------- Pydantic 模型（大白话：接口接收的数据格式） ----------
class PartIn(BaseModel):
    code: str
    name: str
    spec: str = ""
    unit: str = "个"
    category: str = "原材料"
    is_purchase: int = 1
    safety_stock: float = 0
    min_stock: float = 0
    lead_time_days: int = 7
    price: float = 0
    tax_rate: float = 0.13
    supplier_code: str = ""

class SalesOrderIn(BaseModel):
    order_no: str
    customer: str = ""
    product_code: str = "FG-001"
    qty: float = 100
    due_date: str = ""

class ReceiveIn(BaseModel):
    order_no: str
    part_code: str
    qty: float

# ---------- 零件 ----------
@app.get("/api/parts")
def list_parts():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM parts ORDER BY code").fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.post("/api/parts")
def create_part(p: PartIn):
    conn = get_conn()
    try:
        conn.execute("INSERT INTO parts (code,name,spec,unit,category,is_purchase,safety_stock,min_stock,lead_time_days,price,tax_rate,supplier_code,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (p.code,p.name,p.spec,p.unit,p.category,p.is_purchase,p.safety_stock,p.min_stock,p.lead_time_days,p.price,p.tax_rate,p.supplier_code,datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"零件编码 {p.code} 已存在")
    conn.close()
    return {"ok": True, "code": p.code}

# ---------- BOM ----------
@app.get("/api/bom/{parent_code}")
def get_bom(parent_code: str):
    """查 BOM（大白话：查做一个成品需要哪些零件）"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT b.*, p.name as child_name, p.unit FROM bom_items b
        LEFT JOIN parts p ON p.code=b.child_code
        WHERE b.parent_code=? ORDER BY b.child_code
    """, (parent_code,)).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.post("/api/bom/explode")
def bom_explode(payload: Dict[str, Any]):
    """
    BOM 爆炸（大白话：输入成品 + 数量，自动展开成所有零件清单）
    参考：ERPNext / InvenTree 的 BOM 爆炸思路，递归展开，带循环检测。
    """
    parent = payload.get("parent_code", "FG-001")
    qty = float(payload.get("qty", 1))
    conn = get_conn()
    # 递归展开，MVP 只做单层（小夜灯 BOM 只有一层），但代码保留多层递归结构
    def explode(code, need, depth, seen):
        if depth > 10:
            return []
        if code in seen:
            raise HTTPException(400, f"BOM 循环引用：{code} 重复出现，请检查 BOM")
        seen = seen | {code}
        rows = conn.execute("SELECT * FROM bom_items WHERE parent_code=?", (code,)).fetchall()
        if not rows:
            return [{"part_code": code, "qty": need}]
        out = []
        for r in rows:
            sub = explode(r["child_code"], need * float(r["qty"]) * (1 + float(r["loss_rate"] or 0)), depth+1, seen)
            out.extend(sub)
        return out
    try:
        result = explode(parent, qty, 0, set())
    finally:
        conn.close()
    # 合并同零件
    merged: Dict[str, float] = {}
    for it in result:
        merged[it["part_code"]] = merged.get(it["part_code"], 0) + it["qty"]
    return [{"part_code": k, "qty": v} for k, v in merged.items()]

# ---------- 库存 ----------
@app.get("/api/stock")
def list_stock():
    conn = get_conn()
    rows = conn.execute("""
        SELECT s.*, p.name as part_name, p.unit, p.safety_stock
        FROM stock s LEFT JOIN parts p ON p.code=s.part_code ORDER BY s.part_code
    """).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.get("/api/stock/ledger")
def list_ledger():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM stock_ledger ORDER BY id DESC LIMIT 200").fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

# ---------- 供应商 ----------
@app.get("/api/suppliers")
def list_suppliers():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM suppliers ORDER BY code").fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

# ---------- 销售单 ----------
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
                     (o.order_no,o.customer,o.product_code,o.qty,o.due_date,"新建",datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"销售单号 {o.order_no} 已存在")
    conn.close()
    return {"ok": True, "order_no": o.order_no}

# ---------- MRP（核心算法） ----------
@app.post("/api/mrp")
def run_mrp(payload: Dict[str, Any]):
    """
    MRP 计算（大白话：自动算缺啥料）
    公式（来源见文件头注释）：净需求 = 毛需求 - 可用库存 - 在途 + 安全库存
    步骤：1) BOM 爆炸得毛需求  2) 查库存  3) 查在途  4) 算净需求  5) 存结果
    """
    sales_no = payload.get("sales_order_no", "")
    conn = get_conn()
    so = conn.execute("SELECT * FROM sales_orders WHERE order_no=?", (sales_no,)).fetchone()
    if not so:
        conn.close()
        raise HTTPException(404, f"销售单 {sales_no} 不存在")
    product = so["product_code"]
    order_qty = float(so["qty"])
    # 1) BOM 爆炸
    bom_rows = conn.execute("SELECT * FROM bom_items WHERE parent_code=?", (product,)).fetchall()
    conn.execute("DELETE FROM mrp_results WHERE sales_order_no=?", (sales_no,))
    results = []
    for br in bom_rows:
        child = br["child_code"]
        gross = float(br["qty"]) * order_qty * (1 + float(br["loss_rate"] or 0))  # 毛需求
        part = conn.execute("SELECT * FROM parts WHERE code=?", (child,)).fetchone()
        safety = float(part["safety_stock"]) if part else 0
        part_name = part["name"] if part else child
        onhand = float(conn.execute("SELECT COALESCE(SUM(qty),0) FROM stock WHERE part_code=?", (child,)).fetchone()[0] or 0)
        in_transit = float(conn.execute("""
            SELECT COALESCE(SUM(poi.qty - poi.received_qty),0)
            FROM purchase_order_items poi JOIN purchase_orders po ON po.order_no=poi.order_no
            WHERE poi.part_code=? AND po.status IN ('新建','已下单','部分到货')
        """, (child,)).fetchone()[0] or 0)
        net = gross - onhand - in_transit + safety
        if net < 0:
            net = 0  # 大白话：不缺就记 0，不记负数
        conn.execute("INSERT INTO mrp_results (sales_order_no,part_code,part_name,gross,onhand,in_transit,safety,net,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (sales_no, child, part_name, gross, onhand, in_transit, safety, net, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        results.append({"part_code": child, "part_name": part_name, "gross": gross, "onhand": onhand, "in_transit": in_transit, "safety": safety, "net": net})
    conn.commit()
    conn.close()
    return {"sales_order_no": sales_no, "results": results}

@app.get("/api/mrp/{sales_no}")
def get_mrp(sales_no: str):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM mrp_results WHERE sales_order_no=? ORDER BY part_code", (sales_no,)).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

# ---------- 采购 ----------
@app.get("/api/purchase")
def list_purchase():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM purchase_orders ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        d = row_to_dict(r)
        items = conn.execute("SELECT * FROM purchase_order_items WHERE order_no=?", (r["order_no"],)).fetchall()
        d["items"] = [row_to_dict(x) for x in items]
        out.append(d)
    conn.close()
    return out

@app.post("/api/purchase/from-mrp")
def purchase_from_mrp(payload: Dict[str, Any]):
    """
    从 MRP 一键生成采购单（大白话：缺多少就自动生成要买多少的单子）
    逻辑：取 MRP 结果里 net>0 的行，按供应商分组，同一供应商合并成一张采购单。
    """
    sales_no = payload.get("sales_order_no", "")
    supplier = payload.get("supplier_code", "SUP-001")
    conn = get_conn()
    mrp = conn.execute("SELECT * FROM mrp_results WHERE sales_order_no=? AND net>0", (sales_no,)).fetchall()
    if not mrp:
        conn.close()
        raise HTTPException(400, "MRP 结果为空或全部不缺料，请先跑 MRP")
    order_no = f"PO-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    total = 0
    conn.execute("INSERT INTO purchase_orders (order_no,supplier_code,status,due_date,total_amount,created_at) VALUES (?,?,?,?,?,?)",
                 (order_no, supplier, "已下单", date.today().isoformat(), 0, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    for r in mrp:
        part = conn.execute("SELECT * FROM parts WHERE code=?", (r["part_code"],)).fetchone()
        price = float(part["price"]) if part else 0
        conn.execute("INSERT INTO purchase_order_items (order_no,part_code,qty,price,received_qty,tax_rate) VALUES (?,?,?,?,?,?)",
                     (order_no, r["part_code"], float(r["net"]), price, 0, 0.13))
        total += float(r["net"]) * price
    # 含税总额 = 未税总额 * (1+13%)，大白话：中国常见税率 13%
    total_with_tax = round(total * 1.13, 2)
    conn.execute("UPDATE purchase_orders SET total_amount=? WHERE order_no=?", (total_with_tax, order_no))
    conn.commit()
    conn.close()
    return {"ok": True, "order_no": order_no, "total_amount_with_tax": total_with_tax}

@app.post("/api/purchase/receive")
def purchase_receive(r: ReceiveIn):
    """采购到货入库（大白话：供应商送货来了，点一下库存就增加）"""
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
    # 更新采购单状态
    items = conn.execute("SELECT * FROM purchase_order_items WHERE order_no=?", (r.order_no,)).fetchall()
    total_qty = sum(float(x["qty"]) for x in items)
    total_recv = sum(float(x["received_qty"]) for x in items) + float(r.qty)  # 注意：上面已更新，但此处 items 是旧的，所以 +本次
    # 重新查一次，避免旧数据
    items2 = conn.execute("SELECT * FROM purchase_order_items WHERE order_no=?", (r.order_no,)).fetchall()
    total_recv2 = sum(float(x["received_qty"]) for x in items2)
    if total_recv2 >= total_qty - 1e-9:
        new_status = "全部到货"
    elif total_recv2 > 0:
        new_status = "部分到货"
    else:
        new_status = "已下单"
    conn.execute("UPDATE purchase_orders SET status=? WHERE order_no=?", (new_status, r.order_no))
    conn.commit()
    conn.close()
    # 库存增加 + 记流水（在 adjust_stock 里做）
    adjust_stock(r.part_code, float(r.qty), "采购入库", r.order_no)
    return {"ok": True, "order_no": r.order_no, "part_code": r.part_code, "received": r.qty, "status": new_status}

# ---------- 生产 ----------
@app.get("/api/production")
def list_production():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM production_orders ORDER BY id DESC").fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.post("/api/production")
def create_production(payload: Dict[str, Any]):
    """创建生产单（大白话：车间要生产多少个成品的任务单）"""
    order_no = payload.get("order_no", f"MO-{datetime.now().strftime('%Y%m%d%H%M%S')}")
    product = payload.get("product_code", "FG-001")
    qty = float(payload.get("qty", 100))
    sales_no = payload.get("sales_order_no", "")
    conn = get_conn()
    try:
        conn.execute("INSERT INTO production_orders (order_no,product_code,qty,status,sales_order_no,created_at) VALUES (?,?,?,?,?,?)",
                     (order_no, product, qty, "新建", sales_no, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"生产单号 {order_no} 已存在")
    conn.close()
    return {"ok": True, "order_no": order_no}

@app.post("/api/production/consume")
def production_consume(payload: Dict[str, Any]):
    """生产领料（大白话：车间从仓库领走原材料，库存减少）"""
    order_no = payload.get("order_no", "")
    conn = get_conn()
    mo = conn.execute("SELECT * FROM production_orders WHERE order_no=?", (order_no,)).fetchone()
    if not mo:
        conn.close()
        raise HTTPException(404, f"生产单 {order_no} 不存在")
    if mo["status"] != "新建":
        conn.close()
        raise HTTPException(400, f"生产单 {order_no} 状态是 {mo['status']}，已领过料，不能重复领")
    bom_rows = conn.execute("SELECT * FROM bom_items WHERE parent_code=?", (mo["product_code"],)).fetchall()
    # 先检查库存够不够
    for br in bom_rows:
        need = float(br["qty"]) * float(mo["qty"])
        onhand = float(conn.execute("SELECT COALESCE(SUM(qty),0) FROM stock WHERE part_code=?", (br["child_code"],)).fetchone()[0] or 0)
        if onhand + 1e-9 < need:
            conn.close()
            raise HTTPException(400, f"零件 {br['child_code']} 库存不足：需要 {need}，现有 {onhand}，请先采购入库")
    conn.execute("UPDATE production_orders SET status='已领料' WHERE order_no=?", (order_no,))
    conn.commit()
    conn.close()
    for br in bom_rows:
        need = float(br["qty"]) * float(mo["qty"])
        adjust_stock(br["child_code"], -need, "生产领料", order_no)
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

# ---------- 看板（大白话：一张表看到缺啥、拖期、库存预警） ----------
@app.get("/api/dashboard")
def dashboard():
    conn = get_conn()
    # 缺料预警：库存 < 安全库存
    low = conn.execute("""
        SELECT p.code, p.name, COALESCE(SUM(s.qty),0) as onhand, p.safety_stock
        FROM parts p LEFT JOIN stock s ON s.part_code=p.code
        GROUP BY p.code HAVING onhand < p.safety_stock
    """).fetchall()
    # 拖期采购：状态不是全部到货，且交期已过（MVP 用 due_date，今天之后算拖期简化处理：只要未全部到货都算待跟进）
    overdue_po = conn.execute("SELECT * FROM purchase_orders WHERE status IN ('新建','已下单','部分到货')").fetchall()
    # 库存汇总
    stock_rows = conn.execute("SELECT * FROM stock ORDER BY part_code").fetchall()
    conn.close()
    return {
        "low_stock": [row_to_dict(r) for r in low],
        "pending_purchase": [row_to_dict(r) for r in overdue_po],
        "stock_count": len(stock_rows),
    }

# ---------- Excel 导入导出 ----------
# 灵活映射（大白话：不同公司叫法不同，都能映射到同一个字段）
# 例如：物料编码 / 材料编码 / 料号 / 零件编码 都对应 code
COLUMN_ALIASES = {
    "code": ["编码", "零件编码", "物料编码", "材料编码", "料号", "code", "part_code", "零件编号", "物料编号"],
    "name": ["名称", "零件名称", "物料名称", "材料名称", "品名", "name"],
    "spec": ["规格", "规格型号", "spec"],
    "unit": ["单位", "unit"],
    "category": ["类别", "分类", "category"],
    "safety_stock": ["安全库存", "安全库存量", "safety_stock"],
    "min_stock": ["最低库存", "min_stock"],
    "lead_time_days": ["提前期", "提前期天数", "lead_time_days", "交期天数"],
    "price": ["单价", "未税单价", "价格", "price"],
    "tax_rate": ["税率", "tax_rate"],
    "qty": ["数量", "库存数量", "库存", "qty", "期末数量", "结存数量"],
    "warehouse": ["仓库", "warehouse"],
    "location": ["库位", "货位", "location"],
    "batch": ["批次", "批次号", "batch"],
    "supplier_code": ["供应商编码", "供应商", "supplier_code"],
    "supplier_name": ["供应商名称", "供应商名"],
    "order_no": ["单号", "采购单号", "order_no", "单据号"],
    "part_code": ["零件编码", "物料编码", "材料编码", "料号", "part_code"],
}

def normalize_header(header: str) -> str:
    """把表头归一化（大白话：去掉空格、转小写，方便匹配）"""
    if header is None:
        return ""
    return str(header).strip().replace(" ", "").replace("　", "").lower()

def build_header_map(headers: List[str]) -> Dict[str, int]:
    """
    灵活映射：根据别名表，把 Excel 表头映射到系统字段。
    返回 {系统字段: Excel列索引}，找不到的就不映射。
    """
    norm_headers = [normalize_header(h) for h in headers]
    mapping: Dict[str, int] = {}
    # 先建别名 -> 系统字段的反向表（归一化后）
    alias_to_field: Dict[str, str] = {}
    for field, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            alias_to_field[normalize_header(a)] = field
    for idx, nh in enumerate(norm_headers):
        if nh in alias_to_field:
            field = alias_to_field[nh]
            if field not in mapping:
                mapping[field] = idx
    return mapping

def excel_to_rows(file_bytes: bytes):
    """读 Excel（.xlsx），返回 (headers, data_rows)。用 openpyxl，WPS 也能生成的 .xlsx 都能读。"""
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
    data = rows[1:]
    # 去掉全空行
    data = [r for r in data if any(v is not None and str(v).strip() != "" for v in r)]
    return headers, data

def rows_to_excel(headers: List[str], data: List[List[Any]], sheet_name: str = "数据") -> bytes:
    """把数据写成 Excel（.xlsx），WPS / Excel 都能打开。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    # 表头加粗 + 底色
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="4472C4")
    for col, h in enumerate(headers, start=1):
        c = ws.cell(row=1, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal="center", vertical="center")
    for r_idx, row in enumerate(data, start=2):
        for c_idx, v in enumerate(row, start=1):
            ws.cell(row=r_idx, column=c_idx, value=v)
    # 列宽自适应（简单版）
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

@app.get("/api/excel/template/{kind}")
def excel_template(kind: str):
    """下载标准模板（大白话：空表格，用户按模板填，填完再上传）"""
    if kind == "parts":
        headers = ["零件编码", "名称", "规格", "单位", "类别", "安全库存", "最低库存", "提前期天数", "未税单价", "税率"]
        data = [["RM-101", "示例零件", "示例规格", "个", "原材料", 20, 10, 7, 2.5, 0.13]]
    elif kind == "stock":
        headers = ["零件编码", "仓库", "库位", "批次号", "数量"]
        data = [["RM-001", "主仓", "A区-01", "BATCH-001", 100]]
    elif kind == "purchase":
        headers = ["采购单号", "供应商编码", "零件编码", "数量", "未税单价", "税率"]
        data = [["PO-001", "SUP-001", "RM-001", 100, 2.5, 0.13]]
    else:
        raise HTTPException(404, f"模板 {kind} 不存在，可选 parts/stock/purchase")
    content = rows_to_excel(headers, data, sheet_name="模板")
    return StreamingResponse(io.BytesIO(content), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename=template_{kind}.xlsx"})

@app.get("/api/excel/export/{kind}")
def excel_export(kind: str):
    """导出 Excel（大白话：把系统里的数据导出来变成 Excel）"""
    conn = get_conn()
    if kind == "parts":
        headers = ["零件编码", "名称", "规格", "单位", "类别", "安全库存", "最低库存", "提前期天数", "未税单价", "税率", "供应商编码"]
        rows = conn.execute("SELECT code,name,spec,unit,category,safety_stock,min_stock,lead_time_days,price,tax_rate,supplier_code FROM parts ORDER BY code").fetchall()
        data = [[r[0],r[1],r[2],r[3],r[4],r[5],r[6],r[7],r[8],r[9],r[10]] for r in rows]
    elif kind == "stock":
        headers = ["零件编码", "名称", "仓库", "库位", "批次号", "数量", "单位", "安全库存"]
        rows = conn.execute("SELECT s.part_code,p.name,s.warehouse,s.location,s.batch,s.qty,p.unit,p.safety_stock FROM stock s LEFT JOIN parts p ON p.code=s.part_code ORDER BY s.part_code").fetchall()
        data = [[r[0],r[1],r[2],r[3],r[4],r[5],r[6],r[7]] for r in rows]
    elif kind == "purchase":
        headers = ["采购单号", "供应商编码", "状态", "交期", "含税总额", "零件编码", "数量", "未税单价", "已到货数量"]
        rows = conn.execute("""
            SELECT po.order_no,po.supplier_code,po.status,po.due_date,po.total_amount,poi.part_code,poi.qty,poi.price,poi.received_qty
            FROM purchase_orders po LEFT JOIN purchase_order_items poi ON poi.order_no=po.order_no ORDER BY po.order_no
        """).fetchall()
        data = [[r[0],r[1],r[2],r[3],r[4],r[5],r[6],r[7],r[8]] for r in rows]
    else:
        conn.close()
        raise HTTPException(404, f"导出 {kind} 不存在，可选 parts/stock/purchase")
    conn.close()
    content = rows_to_excel(headers, data, sheet_name=kind)
    return StreamingResponse(io.BytesIO(content), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename=export_{kind}.xlsx"})

@app.post("/api/excel/import/{kind}")
async def excel_import(kind: str, file: UploadFile = File(...)):
    """
    导入 Excel（大白话：把 Excel 里的内容搬进系统）
    支持灵活映射：不同公司叫法不同（物料编码/材料编码/料号）都能映射到零件编码。
    校验：必填为空、数量不是数字、编码重复等会提示是哪一行错了。
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
        if kind == "parts":
            # 大白话：不同公司叫法不同，物料编码/材料编码/料号都可能映射到 part_code，也要认作零件编码
            if ("code" not in mapping and "part_code" not in mapping) or "name" not in mapping:
                raise HTTPException(400, f"表头未识别出零件编码/名称。识别到的表头：{headers}，映射：{mapping}。请用模板里的表头，或用别名如 物料编码/材料编码/料号")
            for i, row in enumerate(data_rows, start=2):
                def get(field, default=""):
                    idx = mapping.get(field)
                    if idx is None or idx >= len(row):
                        return default
                    v = row[idx]
                    return "" if v is None else v
                code = str(get("code") or get("part_code")).strip()
                name = str(get("name")).strip()
                if not code or not name:
                    errors.append(f"第 {i} 行：零件编码/名称不能为空")
                    continue
                try:
                    safety = float(get("safety_stock", 0) or 0)
                    price = float(get("price", 0) or 0)
                    tax = float(get("tax_rate", 0.13) or 0.13)
                    lead = int(float(get("lead_time_days", 7) or 7))
                except ValueError:
                    errors.append(f"第 {i} 行：安全库存/单价/税率/提前期不是数字")
                    continue
                # 存在则更新，不存在则插入（大白话：重复导入不会报错，会更新）
                existing = conn.execute("SELECT id FROM parts WHERE code=?", (code,)).fetchone()
                if existing:
                    conn.execute("UPDATE parts SET name=?,spec=?,unit=?,category=?,safety_stock=?,price=?,tax_rate=?,lead_time_days=? WHERE code=?",
                                 (name, str(get("spec")), str(get("unit") or "个"), str(get("category") or "原材料"), safety, price, tax, lead, code))
                else:
                    conn.execute("INSERT INTO parts (code,name,spec,unit,category,is_purchase,safety_stock,min_stock,lead_time_days,price,tax_rate,supplier_code,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (code, name, str(get("spec")), str(get("unit") or "个"), str(get("category") or "原材料"), 1, safety, 0, lead, price, tax, str(get("supplier_code")), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                imported += 1
        elif kind == "stock":
            if "part_code" not in mapping and "code" not in mapping:
                raise HTTPException(400, f"表头未识别出零件编码。识别到的表头：{headers}")
            for i, row in enumerate(data_rows, start=2):
                def get2(field, default=""):
                    idx = mapping.get(field)
                    if idx is None or idx >= len(row):
                        return default
                    v = row[idx]
                    return "" if v is None else v
                code = str(get2("part_code") or get2("code")).strip()
                if not code:
                    errors.append(f"第 {i} 行：零件编码不能为空")
                    continue
                try:
                    qty = float(get2("qty", 0) or 0)
                except ValueError:
                    errors.append(f"第 {i} 行：数量不是数字")
                    continue
                warehouse = str(get2("warehouse") or "主仓")
                location = str(get2("location") or "A区-01")
                batch = str(get2("batch") or "BATCH-IMPORT")
                # 导入库存 = 直接覆盖该批次数量（期初导入场景），同时记流水
                existing = conn.execute("SELECT id,qty FROM stock WHERE part_code=? AND warehouse=? AND location=? AND batch=?", (code, warehouse, location, batch)).fetchone()
                if existing:
                    delta = qty - float(existing["qty"])
                    conn.execute("UPDATE stock SET qty=? WHERE id=?", (qty, existing["id"]))
                else:
                    delta = qty
                    conn.execute("INSERT INTO stock (part_code,warehouse,location,batch,qty) VALUES (?,?,?,?,?)", (code, warehouse, location, batch, qty))
                if delta != 0:
                    conn.execute("INSERT INTO stock_ledger (part_code,warehouse,batch,change_qty,type,ref_no,remark,created_at) VALUES (?,?,?,?,?,?,?,?)",
                                 (code, warehouse, batch, delta, "期初导入", "Excel导入", "Excel导入库存", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                imported += 1
        elif kind == "purchase":
            # 采购导入：按 order_no 分组，每行是一个明细
            if "order_no" not in mapping:
                raise HTTPException(400, f"表头未识别出采购单号。识别到的表头：{headers}")
            grouped: Dict[str, List[Dict]] = {}
            for i, row in enumerate(data_rows, start=2):
                def get3(field, default=""):
                    idx = mapping.get(field)
                    if idx is None or idx >= len(row):
                        return default
                    v = row[idx]
                    return "" if v is None else v
                order_no = str(get3("order_no")).strip()
                part_code = str(get3("part_code") or get3("code")).strip()
                if not order_no or not part_code:
                    errors.append(f"第 {i} 行：采购单号/零件编码不能为空")
                    continue
                try:
                    qty = float(get3("qty", 0) or 0)
                    price = float(get3("price", 0) or 0)
                except ValueError:
                    errors.append(f"第 {i} 行：数量/单价不是数字")
                    continue
                grouped.setdefault(order_no, []).append({
                    "supplier": str(get3("supplier_code") or "SUP-001"),
                    "part_code": part_code,
                    "qty": qty,
                    "price": price,
                    "tax": float(get3("tax_rate", 0.13) or 0.13),
                })
            for order_no, items in grouped.items():
                supplier = items[0]["supplier"]
                existing = conn.execute("SELECT id FROM purchase_orders WHERE order_no=?", (order_no,)).fetchone()
                if not existing:
                    total = sum(it["qty"] * it["price"] * (1 + it["tax"]) for it in items)
                    conn.execute("INSERT INTO purchase_orders (order_no,supplier_code,status,due_date,total_amount,created_at) VALUES (?,?,?,?,?,?)",
                                 (order_no, supplier, "已下单", date.today().isoformat(), round(total, 2), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    for it in items:
                        conn.execute("INSERT INTO purchase_order_items (order_no,part_code,qty,price,received_qty,tax_rate) VALUES (?,?,?,?,?,?)",
                                     (order_no, it["part_code"], it["qty"], it["price"], 0, it["tax"]))
                    imported += 1
                else:
                    errors.append(f"采购单 {order_no} 已存在，跳过（避免重复导入）")
        else:
            raise HTTPException(404, f"导入 {kind} 不存在，可选 parts/stock/purchase")
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
    return {"ok": True, "msg": "PMC MVP 运行正常"}
