# -*- coding: utf-8 -*-
"""
PMC 系统 第三阶段 - 后端
技术：Python + FastAPI + SQLite（大白话：轻量单文件数据库，第三阶段仍用它，后期再切 PostgreSQL，见 README）
由第二阶段（~/workspace/pmc/phase2）复制扩展而来，第二阶段已验证的端到端流程保持兼容。
由第二阶段复制扩展，新增：简单登录（用户名+密码，密码哈希存库，Token 鉴权，替代演示用 X-User）、
智能补货建议（再订货点公式，见 /api/replenishment 注释）、供应商自动推荐（最便宜/交期最短/均衡）、
AI 助手自然语言查库存/查订单（关键词+规则解析，不用大模型猜）、移动端适配（前端响应式）。

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
import hashlib
import logging
import math
import secrets
import contextvars
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, UploadFile, File, Header
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 大白话：数据库文件路径，第二阶段仍用 SQLite 单文件，放在 backend 目录下
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 打包/便携场景允许用 PMC_DB_PATH 指定数据库位置（一键启动入口、自检脚本用）；未设置时仍放 backend 目录
DB_PATH = os.environ.get("PMC_DB_PATH") or os.path.join(BASE_DIR, "pmc_phase3.db")
TODAY = date.today().isoformat()

app = FastAPI(title="PMC 系统 第三阶段", description="生产物料控制（大白话：管缺啥料、买多少、什么时候买）第三阶段：简单登录+智能补货+供应商自动推荐+助手查询+移动端适配")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ---------- 第三阶段：简单登录（演示版，正式需换服务端鉴权，见 README） ----------
# 大白话：登录就是“用户名+密码对上 → 发一张临时通行证 Token，之后每次请求都带上它”。
# Token 有效期 24 小时，库里只存 Token 的哈希（不是 Token 本身），密码只存哈希（不是明文）。
def hash_password(password: str, salt_hex: str = "") -> str:
    """密码哈希：PBKDF2（大白话：把密码 + 一把随机盐反复搅拌 20 万次，得到一串不可逆的指纹）。返回格式 salt$hash。"""
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, 200_000)
    return salt.hex() + "$" + dk.hex()

def verify_password(password: str, stored: str) -> bool:
    """校验密码：用存着的盐重新算一遍，对得上就是对的（大白话：只比指纹，不看原文）。"""
    try:
        salt_hex, want = str(stored).split("$", 1)
        return secrets.compare_digest(hash_password(password, salt_hex), str(stored))
    except Exception:
        return False

def token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

logger = logging.getLogger("pmc_phase3")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

# 大白话：每个请求登记自己开过的数据库连接，请求结束时统一兜底关闭。
# 这样即使某个接口半路抛异常、忘了 close，未提交的写事务也会被回滚，不会再出现
# “一个坏请求把 SQLite 写锁卡死到重启”的问题（P0-2 的根治措施之一）。
_REQ_CONNS: "contextvars.ContextVar[Optional[list]]" = contextvars.ContextVar("pmc_req_conns", default=None)


def close_quietly(conn) -> None:
    """关连接：先回滚未提交事务再关闭，任何异常都吞掉（兜底清理用）。"""
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


@app.middleware("http")
async def auth_middleware(request, call_next):
    """登录态校验中间件（大白话：门口保安）。
    /api/ 开头的接口必须带有效的 Bearer Token，否则 401；登录接口 /health /前端页面/预检请求放行。
    校验通过后，把 Token 对应的用户名写进 X-User 请求头，后面所有老接口不用改就能认出当前用户。
    注意：X-User 直传已不再被信任（第二阶段的演示身份已废弃），必须走登录拿 Token。
    另外：本请求内开过、但接口忘记关闭的数据库连接，在这里统一 rollback + close（P0-2 兜底）。"""
    path = request.url.path
    req_conns: list = []
    _token = _REQ_CONNS.set(req_conns)
    try:
        if request.method == "OPTIONS" or path in ("/", "/health", "/api/auth/login", "/openapi.json", "/docs", "/redoc") \
                or path.startswith("/docs") or not path.startswith("/api/"):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        username = ""
        if token:
            try:
                conn = get_conn()
                row = conn.execute("SELECT username FROM auth_tokens WHERE token_hash=? AND expires_at>?",
                                   (token_hash(token), now_str())).fetchone()
                conn.close()
                if row:
                    username = row["username"]
            except Exception:
                username = ""
        if not username:
            return JSONResponse(status_code=401, content={"detail": "未登录或登录已过期，请先 POST /api/auth/login 登录（默认 admin / admin，首次登录请改密码）"})
        # 把用户名注入 X-User 头（Starlette 的 scope headers 是可改的列表）
        headers = [(k, v) for k, v in request.scope.get("headers", []) if k.lower() != b"x-user"]
        headers.append((b"x-user", username.encode("utf-8")))
        request.scope["headers"] = headers
        return await call_next(request)
    finally:
        for _c in req_conns:
            close_quietly(_c)
        _REQ_CONNS.reset(_token)


# ---------- 数据库 ----------
def get_conn():
    """拿一个数据库连接（大白话：打开数据库大门）。
    timeout=5 + busy_timeout：别人正在写时最多等 5 秒，而不是立刻报 database is locked；
    check_same_thread=False：允许请求结束后的兜底清理线程关闭连接（每个请求各用各的连接）。"""
    conn = sqlite3.connect(DB_PATH, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    reg = _REQ_CONNS.get()
    if reg is not None:
        reg.append(conn)
    return conn


@contextmanager
def db_tx():
    """写操作统一事务：正常结束 commit；抛异常 rollback；无论如何都 close。
    大白话：要么全成、要么全不成，绝不把“写了一半的锁”留给下一个请求。"""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def num_field(value, field: str = "数量", *, required: bool = False, default: float = 0.0,
              positive: bool = False) -> float:
    """把请求里的数量/金额安全转成数字（大白话：填错就好好说“这不是数字”，而不是崩成 500）。
    非法输入返回 422（和 FastAPI 的字段校验同类），不再让 float() 直接抛 ValueError。"""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if required:
            raise HTTPException(422, f"{field}不能为空")
        v = float(default)
    else:
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise HTTPException(422, f"{field}必须是数字，收到：{value!r}")
        if not math.isfinite(v):
            raise HTTPException(422, f"{field}必须是有效数字，收到：{value!r}")
    if positive and v <= 0:
        raise HTTPException(422, f"{field}必须大于 0，收到：{v}")
    return v


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    """兜底异常处理（大白话：真出意外了，给一句人话 + 记日志，别只吐一坨 500）。"""
    logger.exception("未处理异常 %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": f"服务器内部错误：{type(exc).__name__}（详细日志见服务端控制台）"})


@app.exception_handler(sqlite3.OperationalError)
async def sqlite_operational_error_handler(request, exc):
    """数据库忙/被锁时返回 503 并提示重试，而不是裸 500。"""
    msg = str(exc)
    logger.error("数据库操作失败 %s %s: %s", request.method, request.url.path, msg)
    if "locked" in msg.lower() or "busy" in msg.lower():
        return JSONResponse(status_code=503, content={"detail": "数据库正忙（有别的写操作没释放锁），请稍后重试"})
    return JSONResponse(status_code=500, content={"detail": f"数据库操作失败：{msg}"})

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def parse_date(s):
    """把 'YYYY-MM-DD' 解析成 date，解析失败返回 None（大白话：日期写错了就当没写）"""
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


# ========== 第二阶段通用工具（大白话：权限/单号/汇率/审计/仓库归属） ==========
def resolve_user(x_user):
    """解析当前用户：请求头 X-User 没带就当 admin（大白话：老接口没传用户时默认管理员，保证第一阶段流程兼容）。
    内部直接调用接口函数时，FastAPI 的 Header 默认值对象会传进来，非字符串一律当 admin。"""
    if not isinstance(x_user, str):
        return "admin"
    u = x_user.strip()
    return u if u else "admin"

def user_roles_of(conn, username):
    rows = conn.execute("SELECT role FROM user_roles WHERE username=?", (username,)).fetchall()
    return [r["role"] for r in rows]

def require_perm(username, module, action):
    """RBAC 权限检查（大白话：这个人有没有这个模块的这个权力，没有就 403 拒绝）。
    思路来源：ERPNext 角色权限（Role Permission）+ 用户权限（User Permission），见验证报告参考来源。
    action 取值：view / create / edit / approve / delete。"""
    col = {"view": "can_view", "create": "can_create", "edit": "can_edit", "approve": "can_approve", "delete": "can_delete"}.get(action, "can_view")
    conn = get_conn()
    try:
        u = conn.execute("SELECT username FROM users WHERE username=?", (username,)).fetchone()
        if not u:
            raise HTTPException(403, f"用户 {username} 不存在，无权操作")
        roles = user_roles_of(conn, username)
        if "系统管理员" in roles:
            return True
        for role in roles:
            r = conn.execute(f"SELECT {col} FROM role_permissions WHERE role=? AND module=?", (role, module)).fetchone()
            if r and int(r[0]) == 1:
                return True
        raise HTTPException(403, f"用户 {username}（角色 {','.join(roles) or '无'}）没有 {module} 模块的 {action} 权限")
    finally:
        conn.close()

def allowed_warehouses(username):
    """用户能操作的仓库集合：没有配置行 = 全部仓库（大白话：没限制就是都能看）。返回 None 表示全部。"""
    conn = get_conn()
    try:
        roles = user_roles_of(conn, username)
        if "系统管理员" in roles:
            return None
        rows = conn.execute("SELECT warehouse FROM user_warehouses WHERE username=?", (username,)).fetchall()
        if not rows:
            return None
        return {r["warehouse"] for r in rows}
    finally:
        conn.close()

def allowed_companies(username):
    """用户能操作的公司集合：没有配置行 = 全部公司。返回 None 表示全部。"""
    conn = get_conn()
    try:
        roles = user_roles_of(conn, username)
        if "系统管理员" in roles:
            return None
        rows = conn.execute("SELECT company_code FROM user_companies WHERE username=?", (username,)).fetchall()
        if not rows:
            return None
        return {r["company_code"] for r in rows}
    finally:
        conn.close()

def check_scope(username, warehouse=None, company_code=None):
    """仓库/公司范围检查：用户被限制在某些仓库/公司时，越界操作直接拒绝。"""
    if warehouse:
        aw = allowed_warehouses(username)
        if aw is not None and warehouse not in aw:
            raise HTTPException(403, f"用户 {username} 没有仓库 {warehouse} 的操作权限")
    if company_code:
        ac = allowed_companies(username)
        if ac is not None and company_code not in ac:
            raise HTTPException(403, f"用户 {username} 没有公司 {company_code} 的操作权限")

def warehouse_company(conn, warehouse):
    """查仓库属于哪个公司：查不到就默认 COMP-001（大白话：老数据/临时仓库名都归总公司，避免报错）。"""
    if not warehouse:
        return "COMP-001"
    try:
        r = conn.execute("SELECT company_code FROM warehouses WHERE code=?", (warehouse,)).fetchone()
        return r["company_code"] if r else "COMP-001"
    except Exception:
        return "COMP-001"

def next_doc_no(conn, series):
    """数据库序列单号（大白话：单号从一张计数表里取，取完计数+1）。
    并发安全做法：先用 BEGIN IMMEDIATE 抢写锁再读号，避免两个请求同时读到同一个号；
    抢不到锁（database is locked）时自动重试 3 次，不再让用户看到“单号已存在”。
    格式：PO-2026-0001 / MO-2026-0001 / PR-2026-0001 / PAY-2026-0001。"""
    import time as _time
    year = date.today().year
    key = f"{series}-{year}"
    prefix = f"{series}-{year}-"
    for attempt in range(3):
        own_tx = False
        try:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
                own_tx = True
            row = conn.execute("SELECT next_no FROM doc_sequences WHERE series_key=?", (key,)).fetchone()
            if row is None:
                n = 1
                try:
                    conn.execute("INSERT INTO doc_sequences (series_key,prefix,next_no) VALUES (?,?,?)", (key, prefix, 2))
                except sqlite3.IntegrityError:
                    row = conn.execute("SELECT next_no FROM doc_sequences WHERE series_key=?", (key,)).fetchone()
                    n = int(row["next_no"])
                    conn.execute("UPDATE doc_sequences SET next_no=? WHERE series_key=?", (n + 1, key))
            else:
                n = int(row["next_no"])
                conn.execute("UPDATE doc_sequences SET next_no=? WHERE series_key=?", (n + 1, key))
            if own_tx:
                conn.commit()
            return f"{prefix}{n:04d}"
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            try:
                conn.rollback()
            except Exception:
                pass
            if attempt == 2:
                raise HTTPException(503, "单号生成繁忙（数据库锁等待超时），请稍后重试")
            _time.sleep(0.05)


def get_exchange_rate(conn, from_cur, to_cur, on_date=None):
    """查汇率：同币种=1；有直接汇率用直接的；只有反向汇率用倒数；都没有返回 None（调用方决定报错还是默认）。"""
    if not from_cur or not to_cur or from_cur == to_cur:
        return 1.0
    d = on_date or TODAY
    r = conn.execute("SELECT rate FROM exchange_rates WHERE from_currency=? AND to_currency=? AND effective_date<=? ORDER BY effective_date DESC LIMIT 1",
                     (from_cur, to_cur, d)).fetchone()
    if r:
        return float(r["rate"])
    r2 = conn.execute("SELECT rate FROM exchange_rates WHERE from_currency=? AND to_currency=? AND effective_date<=? ORDER BY effective_date DESC LIMIT 1",
                      (to_cur, from_cur, d)).fetchone()
    if r2 and float(r2["rate"]) != 0:
        return 1.0 / float(r2["rate"])
    return None

def resolve_exchange_rate(conn, from_cur, to_cur, given=None):
    """解析汇率：调用方给了就用给的；没给就查汇率表；查不到且不是同币种就报错（大白话：外币单必须有汇率，不能瞎算）。"""
    if given is not None and float(given) > 0:
        return float(given)
    rate = get_exchange_rate(conn, from_cur, to_cur)
    if rate is None:
        raise HTTPException(400, f"没有 {from_cur}→{to_cur} 的汇率，请先在 /api/exchange-rates 维护汇率")
    return rate

def log_audit(user, action, entity_type, entity_id, field_name="", old_value="", new_value="", remark="", company_code="", conn=None):
    """写审计日志（大白话：谁、什么时候、对哪张单/哪个零件、改了哪个字段、旧值→新值，全记下来，只能追加不能改）。
    思路来源：Frappe Version（文档保存时做新旧值差异对比并存入时间线），见 https://frappe.io/blog/erpnext-features/versioning-and-audit-trail"""
    own = False
    if conn is None:
        conn = get_conn()
        own = True
    try:
        conn.execute("INSERT INTO audit_log (username,action,entity_type,entity_id,field_name,old_value,new_value,remark,company_code,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (user, action, entity_type, str(entity_id), str(field_name), str(old_value), str(new_value), str(remark), company_code or "", now_str()))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()

def ensure_column(conn, table, column, ddl):
    """给已存在的表补列（大白话：老库升级时缺列就加上，新库建表时已有则跳过）。"""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

def get_batch_frozen(conn, part_code, warehouse=None, batch=None, company_code=None):
    """查某批次的冻结量（大白话：这个批次被冻住了多少）。"""
    sql = "SELECT COALESCE(SUM(qty),0) FROM frozen_stock WHERE part_code=?"
    args = [part_code]
    if warehouse:
        sql += " AND warehouse=?"; args.append(warehouse)
    if batch:
        sql += " AND batch=?"; args.append(batch)
    if company_code:
        sql += " AND company_code=?"; args.append(company_code)
    return float(conn.execute(sql, args).fetchone()[0] or 0)

def get_batch_available(conn, part_code, warehouse, batch, company_code=None):
    """某批次可用量 = 该批次现有 - 该批次冻结（大白话：这个批次真正能用的数）。"""
    row = conn.execute("SELECT COALESCE(SUM(qty),0) FROM stock WHERE part_code=? AND warehouse=? AND batch=?", (part_code, warehouse, batch)).fetchone()
    onhand = float(row[0] or 0)
    return onhand - get_batch_frozen(conn, part_code, warehouse, batch, company_code)

def approval_steps_for(conn, doc_type, amount_base, company_code):
    """按金额算需要哪几级审批（大白话：金额越大，审批的人级别越高、级数越多）。
    规则：<=10000 自动通过（无步骤）；10000<金额<=100000 采购经理批；>100000 采购经理+采购总监依次批。
    自定义规则（is_custom=1）存在时只用自定义规则，覆盖默认分级。"""
    rows = conn.execute("SELECT * FROM approval_rules WHERE doc_type=? AND active=1 AND (company_code='' OR company_code=?) ORDER BY step_order",
                        (doc_type, company_code or "")).fetchall()
    customs = [r for r in rows if int(r["is_custom"] or 0) == 1]
    pool = customs if customs else rows
    steps = []
    for r in pool:
        mn = float(r["min_amount"] or 0)
        mx = r["max_amount"]
        if amount_base > mn and (mx is None or amount_base <= float(mx)):
            steps.append(r)
    steps.sort(key=lambda r: int(r["step_order"] or 1))
    return steps

def create_approval_records(conn, doc_type, doc_no, amount_base, company_code):
    """为一张单创建待审批记录。返回步骤数；0 表示自动通过。"""
    steps = approval_steps_for(conn, doc_type, amount_base, company_code)
    for s in steps:
        conn.execute("INSERT INTO approval_records (doc_type,doc_no,step_order,approver_role,approver_user,status,amount,company_code,created_at) VALUES (?,?,?,?,?,'待审批',?,?,?)",
                     (doc_type, doc_no, int(s["step_order"] or 1), s["approver_role"] or "", s["approver_user"] or "", float(amount_base), company_code or "", now_str()))
    return len(steps)

def advance_approval(conn, doc_type, doc_no, username, comment, approve=True):
    """推进审批：校验当前待审批步骤的审批人（指定人或角色），通过则进下一步或全部通过，拒绝则整单拒绝。
    返回 (最终状态, 剩余待审批数)；最终状态为 待审批/已批准/已拒绝。"""
    rec = conn.execute("SELECT * FROM approval_records WHERE doc_type=? AND doc_no=? AND status='待审批' ORDER BY step_order LIMIT 1",
                       (doc_type, doc_no)).fetchone()
    if not rec:
        raise HTTPException(400, f"{doc_no} 没有待审批步骤")
    roles = user_roles_of(conn, username)
    if "系统管理员" not in roles:
        if rec["approver_user"]:
            if username != rec["approver_user"]:
                raise HTTPException(403, f"本步骤指定审批人是 {rec['approver_user']}，{username} 不能审批")
        elif rec["approver_role"]:
            if rec["approver_role"] not in roles:
                raise HTTPException(403, f"本步骤需要角色 {rec['approver_role']}，{username}（{','.join(roles) or '无角色'}）不能审批")
    new_status = "已批准" if approve else "已拒绝"
    conn.execute("UPDATE approval_records SET status=?, action_by=?, action_at=?, comment=? WHERE id=?",
                 (new_status, username, now_str(), comment or "", rec["id"]))
    log_audit(username, "审批通过" if approve else "审批拒绝", doc_type, doc_no, "审批步骤", "待审批", new_status, comment or "", "", conn)
    if not approve:
        conn.execute("UPDATE approval_records SET status='已作废' WHERE doc_type=? AND doc_no=? AND status='待审批'", (doc_type, doc_no))
        return "已拒绝", 0
    remain = conn.execute("SELECT COUNT(*) FROM approval_records WHERE doc_type=? AND doc_no=? AND status='待审批'", (doc_type, doc_no)).fetchone()[0]
    return ("已批准" if remain == 0 else "待审批"), int(remain)

def init_db():
    """建表 + 种子数据（第一次运行时把表建好，并放进小夜灯例子数据）"""
    conn = get_conn()
    cur = conn.cursor()
    # 第二阶段：给第一阶段已有的表补列（老库升级用，新库建表时已含则跳过）
    for _tbl, _col, _ddl in [
        ("parts", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("stock", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("stock_ledger", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("frozen_stock", "warehouse", "TEXT DEFAULT '主仓'"),
        ("frozen_stock", "location", "TEXT DEFAULT ''"),
        ("frozen_stock", "batch", "TEXT DEFAULT ''"),
        ("frozen_stock", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("allocations", "warehouse", "TEXT DEFAULT '主仓'"),
        ("allocations", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("suppliers", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("sales_orders", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("sales_orders", "warehouse", "TEXT DEFAULT '主仓'"),
        ("sales_orders", "currency", "TEXT DEFAULT 'CNY'"),
        ("sales_orders", "exchange_rate", "REAL DEFAULT 1"),
        ("purchase_requisitions", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("purchase_requisitions", "warehouse", "TEXT DEFAULT '主仓'"),
        ("purchase_requisitions", "currency", "TEXT DEFAULT 'CNY'"),
        ("purchase_requisitions", "exchange_rate", "REAL DEFAULT 1"),
        ("purchase_requisitions", "total_amount", "REAL DEFAULT 0"),
        ("purchase_requisitions", "total_amount_base", "REAL DEFAULT 0"),
        ("purchase_requisitions", "approval_status", "TEXT DEFAULT ''"),
        ("purchase_orders", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("purchase_orders", "warehouse", "TEXT DEFAULT '主仓'"),
        ("purchase_orders", "currency", "TEXT DEFAULT 'CNY'"),
        ("purchase_orders", "exchange_rate", "REAL DEFAULT 1"),
        ("purchase_orders", "total_amount_base", "REAL DEFAULT 0"),
        ("purchase_orders", "subtotal_amount", "REAL DEFAULT 0"),
        ("purchase_orders", "tax_amount", "REAL DEFAULT 0"),
        ("purchase_orders", "approval_status", "TEXT DEFAULT ''"),
        ("purchase_order_items", "warehouse", "TEXT DEFAULT '主仓'"),
        ("purchase_order_items", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("production_orders", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("production_orders", "warehouse", "TEXT DEFAULT '主仓'"),
        ("production_orders", "parent_order_no", "TEXT DEFAULT ''"),
        ("mrp_results", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("mrp_results", "warehouse", "TEXT DEFAULT '主仓'"),
        # 第三阶段：用户表补密码哈希列（老库升级用；新库建表时已含则跳过）
        ("users", "password_hash", "TEXT DEFAULT ''"),
        ("users", "password_changed", "INTEGER DEFAULT 0"),
        # 第四轮迭代：仓库默认库位（P1-5）+ 流水补库位/经办人/单价（P2-5）+ 领料部门/用途（P2-6）
        ("warehouses", "default_location", "TEXT DEFAULT 'A区-01'"),
        ("stock_ledger", "location", "TEXT DEFAULT ''"),
        ("stock_ledger", "operator", "TEXT DEFAULT ''"),
        ("stock_ledger", "unit_price", "REAL DEFAULT 0"),
        ("production_consumptions", "dept", "TEXT DEFAULT ''"),
        ("production_consumptions", "purpose", "TEXT DEFAULT ''"),
    ]:
        try:
            ensure_column(conn, _tbl, _col, _ddl)
        except Exception:
            pass  # 表还没建好时跳过，后面建表语句会含新列
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
            location TEXT DEFAULT '', batch TEXT DEFAULT '', change_qty REAL NOT NULL,
            type TEXT NOT NULL, ref_no TEXT DEFAULT '', operator TEXT DEFAULT '',
            unit_price REAL DEFAULT 0, remark TEXT DEFAULT '', created_at TEXT DEFAULT ''
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
    # ---------- 第二阶段新表 ----------
    cur.execute("""CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
        base_currency TEXT DEFAULT 'CNY', country TEXT DEFAULT '中国', tax_rate REAL DEFAULT 0.13,
        address TEXT DEFAULT '', created_at TEXT DEFAULT '')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS warehouses (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
        company_code TEXT DEFAULT 'COMP-001', type TEXT DEFAULT '综合', address TEXT DEFAULT '',
        default_location TEXT DEFAULT 'A区-01', created_at TEXT DEFAULT '')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS currencies (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
        symbol TEXT DEFAULT '', created_at TEXT DEFAULT '')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS exchange_rates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, from_currency TEXT NOT NULL, to_currency TEXT NOT NULL,
        rate REAL NOT NULL, effective_date TEXT DEFAULT '', created_at TEXT DEFAULT '')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS doc_sequences (
        id INTEGER PRIMARY KEY AUTOINCREMENT, series_key TEXT UNIQUE NOT NULL,
        prefix TEXT DEFAULT '', next_no INTEGER DEFAULT 1)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, name TEXT DEFAULT '',
        status TEXT DEFAULT '启用', created_at TEXT DEFAULT '')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS roles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, remark TEXT DEFAULT '')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS role_permissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, module TEXT NOT NULL,
        can_view INTEGER DEFAULT 0, can_create INTEGER DEFAULT 0, can_edit INTEGER DEFAULT 0,
        can_approve INTEGER DEFAULT 0, can_delete INTEGER DEFAULT 0,
        UNIQUE(role, module))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS user_roles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, role TEXT NOT NULL,
        UNIQUE(username, role))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS user_warehouses (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, warehouse TEXT NOT NULL,
        UNIQUE(username, warehouse))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS user_companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, company_code TEXT NOT NULL,
        UNIQUE(username, company_code))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS approval_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT, doc_type TEXT NOT NULL, name TEXT DEFAULT '',
        min_amount REAL DEFAULT 0, max_amount REAL,
        approver_role TEXT DEFAULT '', approver_user TEXT DEFAULT '',
        step_order INTEGER DEFAULT 1, company_code TEXT DEFAULT '',
        active INTEGER DEFAULT 1, is_custom INTEGER DEFAULT 0, created_at TEXT DEFAULT '')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS approval_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT, doc_type TEXT NOT NULL, doc_no TEXT NOT NULL,
        step_order INTEGER DEFAULT 1, approver_role TEXT DEFAULT '', approver_user TEXT DEFAULT '',
        status TEXT DEFAULT '待审批', action_by TEXT DEFAULT '', action_at TEXT DEFAULT '',
        comment TEXT DEFAULT '', amount REAL DEFAULT 0, company_code TEXT DEFAULT '',
        created_at TEXT DEFAULT '')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS supplier_invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_no TEXT UNIQUE NOT NULL,
        po_no TEXT DEFAULT '', supplier_code TEXT NOT NULL, company_code TEXT DEFAULT 'COMP-001',
        currency TEXT DEFAULT 'CNY', exchange_rate REAL DEFAULT 1,
        subtotal_amount REAL DEFAULT 0, tax_amount REAL DEFAULT 0, total_amount REAL DEFAULT 0,
        amount_base REAL DEFAULT 0, expected_amount REAL DEFAULT 0, diff_amount REAL DEFAULT 0,
        paid_amount REAL DEFAULT 0, status TEXT DEFAULT '待对账',
        invoice_date TEXT DEFAULT '', due_date TEXT DEFAULT '', remark TEXT DEFAULT '',
        created_at TEXT DEFAULT '')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, payment_no TEXT UNIQUE NOT NULL,
        invoice_no TEXT NOT NULL, supplier_code TEXT NOT NULL, company_code TEXT DEFAULT 'COMP-001',
        currency TEXT DEFAULT 'CNY', exchange_rate REAL DEFAULT 1,
        amount REAL DEFAULT 0, amount_base REAL DEFAULT 0,
        payment_date TEXT DEFAULT '', method TEXT DEFAULT '银行转账', remark TEXT DEFAULT '',
        created_at TEXT DEFAULT '')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS production_consumptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, order_no TEXT NOT NULL, part_code TEXT NOT NULL,
        qty REAL DEFAULT 0, unit_cost REAL DEFAULT 0, total_cost REAL DEFAULT 0,
        batch TEXT DEFAULT '', warehouse TEXT DEFAULT '主仓', company_code TEXT DEFAULT 'COMP-001',
        dept TEXT DEFAULT '', purpose TEXT DEFAULT '', created_at TEXT DEFAULT '')""")
    # 第二阶段：建表后再补列（保证老表也有新列；新表建表语句已含新列则跳过）
    for _tbl, _col, _ddl in [
        ("parts", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("stock", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("stock_ledger", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("frozen_stock", "warehouse", "TEXT DEFAULT '主仓'"),
        ("frozen_stock", "location", "TEXT DEFAULT ''"),
        ("frozen_stock", "batch", "TEXT DEFAULT ''"),
        ("frozen_stock", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("allocations", "warehouse", "TEXT DEFAULT '主仓'"),
        ("allocations", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("suppliers", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("sales_orders", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("sales_orders", "warehouse", "TEXT DEFAULT '主仓'"),
        ("sales_orders", "currency", "TEXT DEFAULT 'CNY'"),
        ("sales_orders", "exchange_rate", "REAL DEFAULT 1"),
        ("purchase_requisitions", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("purchase_requisitions", "warehouse", "TEXT DEFAULT '主仓'"),
        ("purchase_requisitions", "currency", "TEXT DEFAULT 'CNY'"),
        ("purchase_requisitions", "exchange_rate", "REAL DEFAULT 1"),
        ("purchase_requisitions", "total_amount", "REAL DEFAULT 0"),
        ("purchase_requisitions", "total_amount_base", "REAL DEFAULT 0"),
        ("purchase_requisitions", "approval_status", "TEXT DEFAULT ''"),
        ("purchase_orders", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("purchase_orders", "warehouse", "TEXT DEFAULT '主仓'"),
        ("purchase_orders", "currency", "TEXT DEFAULT 'CNY'"),
        ("purchase_orders", "exchange_rate", "REAL DEFAULT 1"),
        ("purchase_orders", "total_amount_base", "REAL DEFAULT 0"),
        ("purchase_orders", "subtotal_amount", "REAL DEFAULT 0"),
        ("purchase_orders", "tax_amount", "REAL DEFAULT 0"),
        ("purchase_orders", "approval_status", "TEXT DEFAULT ''"),
        ("purchase_order_items", "warehouse", "TEXT DEFAULT '主仓'"),
        ("purchase_order_items", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("production_orders", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("production_orders", "warehouse", "TEXT DEFAULT '主仓'"),
        ("production_orders", "parent_order_no", "TEXT DEFAULT ''"),
        ("mrp_results", "company_code", "TEXT DEFAULT 'COMP-001'"),
        ("mrp_results", "warehouse", "TEXT DEFAULT '主仓'"),
        ("users", "password_hash", "TEXT DEFAULT ''"),
        ("users", "password_changed", "INTEGER DEFAULT 0"),
    ]:
        ensure_column(conn, _tbl, _col, _ddl)
    # 第三阶段：登录 Token 表（只存 Token 哈希 + 过期时间，不存 Token 明文）
    cur.execute("""CREATE TABLE IF NOT EXISTS auth_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT UNIQUE NOT NULL,
        username TEXT NOT NULL, created_at TEXT DEFAULT '', expires_at TEXT DEFAULT '')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT DEFAULT '', action TEXT DEFAULT '',
        entity_type TEXT DEFAULT '', entity_id TEXT DEFAULT '', field_name TEXT DEFAULT '',
        old_value TEXT DEFAULT '', new_value TEXT DEFAULT '', remark TEXT DEFAULT '',
        company_code TEXT DEFAULT '', created_at TEXT DEFAULT '')""")
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
                            (code,"主仓","BATCH-20261019",qty,"期初","期初","第二阶段种子期初",now))
        # ---------- 第二阶段种子：公司/仓库/币种/汇率/用户/角色/权限/审批规则 ----------
        for code, name, cur_, country in [("COMP-001", "深圳总公司", "CNY", "中国"), ("COMP-002", "上海分公司", "CNY", "中国")]:
            cur.execute("INSERT OR IGNORE INTO companies (code,name,base_currency,country,tax_rate,created_at) VALUES (?,?,?,?,?,?)",
                        (code, name, cur_, country, 0.13, now))
        for code, name, comp, typ, dloc in [("主仓", "主仓", "COMP-001", "综合", "A区-01"),
                                               ("分仓", "分仓", "COMP-001", "原材料", "B区-01"),
                                               ("成品仓", "成品仓", "COMP-001", "成品", "C区-01"),
                                               ("上海仓", "上海仓", "COMP-002", "综合", "D区-01")]:
            cur.execute("INSERT OR IGNORE INTO warehouses (code,name,company_code,type,default_location,created_at) VALUES (?,?,?,?,?,?)",
                        (code, name, comp, typ, dloc, now))
        # P1-5：老库升级时给已存在的仓库补上合理的默认库位（只补还没改过的，避免覆盖用户手工配置）
        for _wcode, _wloc in [("主仓", "A区-01"), ("分仓", "B区-01"), ("成品仓", "C区-01"), ("上海仓", "D区-01")]:
            cur.execute("UPDATE warehouses SET default_location=? WHERE code=? AND (default_location IS NULL OR default_location='' OR default_location='A区-01')",
                        (_wloc, _wcode))
        for code, name, sym in [("CNY", "人民币", "¥"), ("USD", "美元", "$"), ("EUR", "欧元", "€"), ("HKD", "港币", "HK$")]:
            cur.execute("INSERT OR IGNORE INTO currencies (code,name,symbol,created_at) VALUES (?,?,?,?)", (code, name, sym, now))
        for f, t, rate in [("USD", "CNY", 7.20), ("EUR", "CNY", 7.80), ("HKD", "CNY", 0.92), ("CNY", "CNY", 1.0)]:
            if not cur.execute("SELECT id FROM exchange_rates WHERE from_currency=? AND to_currency=? AND effective_date=?", (f, t, "2026-01-01")).fetchone():
                cur.execute("INSERT INTO exchange_rates (from_currency,to_currency,rate,effective_date,created_at) VALUES (?,?,?,?,?)", (f, t, rate, "2026-01-01", now))
        for role, remark in [("系统管理员", "全部权限"), ("采购经理", "采购审批（一级）"), ("采购总监", "大额采购审批（二级）"),
                              ("采购员", "采购申请/下单"), ("仓库员", "库存操作"), ("财务", "财务对账/付款"),
                              ("审计员", "只读审计"), ("生产员", "生产操作")]:
            cur.execute("INSERT OR IGNORE INTO roles (name,remark) VALUES (?,?)", (role, remark))
        # RBAC 矩阵：(角色, 模块, 查看, 新建, 修改, 审批, 删除)
        matrix = []
        for mod in ["零件", "库存", "供应商", "采购", "生产", "销售", "财务", "报表", "系统"]:
            matrix.append(("系统管理员", mod, 1, 1, 1, 1, 1))
        for mod, v, c, e, a, d in [("采购", 1, 1, 1, 1, 0), ("供应商", 1, 0, 0, 0, 0), ("库存", 1, 0, 0, 0, 0), ("报表", 1, 0, 0, 0, 0)]:
            matrix.append(("采购经理", mod, v, c, e, a, d))
        for mod, v, c, e, a, d in [("采购", 1, 0, 0, 1, 0), ("报表", 1, 0, 0, 0, 0), ("财务", 1, 0, 0, 0, 0)]:
            matrix.append(("采购总监", mod, v, c, e, a, d))
        for mod, v, c, e, a, d in [("采购", 1, 1, 1, 0, 0), ("供应商", 1, 0, 0, 0, 0), ("库存", 1, 0, 0, 0, 0), ("报表", 1, 0, 0, 0, 0)]:
            matrix.append(("采购员", mod, v, c, e, a, d))
        for mod, v, c, e, a, d in [("库存", 1, 1, 1, 0, 0), ("生产", 1, 0, 0, 0, 0), ("报表", 1, 0, 0, 0, 0)]:
            matrix.append(("仓库员", mod, v, c, e, a, d))
        for mod, v, c, e, a, d in [("财务", 1, 1, 1, 1, 0), ("采购", 1, 0, 0, 0, 0), ("报表", 1, 0, 0, 0, 0)]:
            matrix.append(("财务", mod, v, c, e, a, d))
        for mod in ["零件", "库存", "供应商", "采购", "生产", "销售", "财务", "报表"]:
            matrix.append(("审计员", mod, 1, 0, 0, 0, 0))
        for mod, v, c, e, a, d in [("生产", 1, 1, 1, 0, 0), ("库存", 1, 0, 0, 0, 0), ("报表", 1, 0, 0, 0, 0)]:
            matrix.append(("生产员", mod, v, c, e, a, d))
        for role, mod, v, c, e, a, d in matrix:
            cur.execute("INSERT OR IGNORE INTO role_permissions (role,module,can_view,can_create,can_edit,can_approve,can_delete) VALUES (?,?,?,?,?,?,?)",
                        (role, mod, v, c, e, a, d))
        for username, name, roles in [("admin", "系统管理员", ["系统管理员"]),
                                      ("manager", "采购经理", ["采购经理"]),
                                      ("director", "采购总监", ["采购总监"]),
                                      ("buyer", "采购员", ["采购员"]),
                                      ("keeper", "仓库员", ["仓库员"]),
                                      ("finance", "财务", ["财务"]),
                                      ("auditor", "审计员", ["审计员"]),
                                      ("worker", "生产员", ["生产员"])]:
            cur.execute("INSERT OR IGNORE INTO users (username,name,created_at) VALUES (?,?,?)", (username, name, now))
            for r in roles:
                cur.execute("INSERT OR IGNORE INTO user_roles (username,role) VALUES (?,?)", (username, r))
        # 仓库员 keeper 只限主仓 + 总公司（演示按仓库/公司隔离）；其余用户不限
        cur.execute("INSERT OR IGNORE INTO user_warehouses (username,warehouse) VALUES (?,?)", ("keeper", "主仓"))
        cur.execute("INSERT OR IGNORE INTO user_companies (username,company_code) VALUES (?,?)", ("keeper", "COMP-001"))
        # 审批规则种子：>10000 经理批；>100000 经理+总监批；<=10000 自动通过（无规则行）
        for doc_type in ["采购申请", "采购单"]:
            for mn, mx, role, step in [(10000, 100000, "采购经理", 1), (100000, None, "采购经理", 1), (100000, None, "采购总监", 2)]:
                if not cur.execute("SELECT id FROM approval_rules WHERE doc_type=? AND min_amount=? AND approver_role=? AND step_order=? AND is_custom=0",
                                   (doc_type, mn, role, step)).fetchone():
                    cur.execute("INSERT INTO approval_rules (doc_type,name,min_amount,max_amount,approver_role,step_order,company_code,active,is_custom,created_at) VALUES (?,?,?,?,?,?,?,1,0,?)",
                                (doc_type, f"{doc_type}金额分级审批", mn, mx, role, step, "", now))
        # 种子库存归属总公司
        cur.execute("UPDATE stock SET company_code='COMP-001' WHERE company_code IS NULL OR company_code=''")
        cur.execute("UPDATE frozen_stock SET company_code='COMP-001' WHERE company_code IS NULL OR company_code=''")
        conn.commit()
    # 第三阶段：给没有密码的种子/老用户补默认密码（默认密码 = 用户名，如 admin/admin；首次登录请改密码）。
    # 只补空密码，已设置过密码的用户不受影响。
    try:
        for _u in cur.execute("SELECT username, password_hash FROM users").fetchall():
            if not (_u["password_hash"] or "").strip():
                cur.execute("UPDATE users SET password_hash=?, password_changed=0 WHERE username=?",
                            (hash_password(_u["username"]), _u["username"]))
        conn.commit()
    except Exception:
        pass
    conn.close()

init_db()

# ---------- 工具函数 ----------
def row_to_dict(r):
    return dict(r) if r else None

def get_onhand(part_code: str, warehouse: Optional[str] = None, company_code: Optional[str] = None) -> float:
    conn = get_conn()
    sql = "SELECT COALESCE(SUM(qty),0) FROM stock WHERE part_code=?"
    args: List[Any] = [part_code]
    if warehouse:
        sql += " AND warehouse=?"; args.append(warehouse)
    if company_code:
        sql += " AND company_code=?"; args.append(company_code)
    v = conn.execute(sql, args).fetchone()[0]
    conn.close()
    return float(v or 0)

def get_in_transit(part_code: str, warehouse: Optional[str] = None, company_code: Optional[str] = None) -> float:
    """在途：已下采购单但还没到货的（大白话：已经买了，还在路上），可按仓库/公司过滤"""
    conn = get_conn()
    sql = """
        SELECT COALESCE(SUM(poi.qty - poi.received_qty),0)
        FROM purchase_order_items poi JOIN purchase_orders po ON po.order_no = poi.order_no
        WHERE poi.part_code=? AND po.status IN ('新建','已下单','部分到货','已批准')
    """
    args: List[Any] = [part_code]
    if warehouse:
        sql += " AND poi.warehouse=?"; args.append(warehouse)
    if company_code:
        sql += " AND po.company_code=?"; args.append(company_code)
    v = conn.execute(sql, args).fetchone()[0]
    conn.close()
    return float(v or 0)

def get_allocated(part_code: str, warehouse: Optional[str] = None, company_code: Optional[str] = None) -> float:
    """已分配：已被未领料生产单占用的量，可按仓库/公司过滤"""
    conn = get_conn()
    sql = "SELECT COALESCE(SUM(qty),0) FROM allocations WHERE part_code=?"
    args: List[Any] = [part_code]
    if warehouse:
        sql += " AND warehouse=?"; args.append(warehouse)
    if company_code:
        sql += " AND company_code=?"; args.append(company_code)
    v = conn.execute(sql, args).fetchone()[0]
    conn.close()
    return float(v or 0)

def get_frozen(part_code: str, warehouse: Optional[str] = None, batch: Optional[str] = None, company_code: Optional[str] = None) -> float:
    """冻结：被质检/盘点冻住、暂时不能用的量，可按仓库/批次/公司过滤（第二阶段：按批次冻结）"""
    conn = get_conn()
    sql = "SELECT COALESCE(SUM(qty),0) FROM frozen_stock WHERE part_code=?"
    args: List[Any] = [part_code]
    if warehouse:
        sql += " AND warehouse=?"; args.append(warehouse)
    if batch:
        sql += " AND batch=?"; args.append(batch)
    if company_code:
        sql += " AND company_code=?"; args.append(company_code)
    v = conn.execute(sql, args).fetchone()[0]
    conn.close()
    return float(v or 0)

def get_available(part_code: str, warehouse: Optional[str] = None, company_code: Optional[str] = None) -> float:
    """可用量 = 现有 + 在途 - 已分配 - 冻结（核心公式，第一阶段新增，第二阶段按仓库/公司/批次过滤），可按仓库/公司过滤"""
    return get_onhand(part_code, warehouse, company_code) + get_in_transit(part_code, warehouse, company_code) - get_allocated(part_code, warehouse, company_code) - get_frozen(part_code, warehouse, None, company_code)

def available_in_scope(part_code: str, user: str) -> float:
    """按登录用户的仓库/公司权限算可用量（P0-4：看板不再把别的公司库存算进来）。
    管理员/未配置范围 = 全量；配置了范围就按范围内的仓库×公司逐个求和。"""
    ac = allowed_companies(user); aw = allowed_warehouses(user)
    if not ac and not aw:
        return get_available(part_code)
    comps = list(ac) if ac else [None]
    whs = list(aw) if aw else [None]
    return sum(get_available(part_code, w, c) for c in comps for w in whs)

def stock_status(available: float, safety: float, max_stock: float) -> str:
    """红黄绿：可用量<=0 红（断货），<安全库存 黄（预警），>最高库存 黄（积压），其余 绿"""
    if available <= 0:
        return "红"
    if available < safety:
        return "黄"
    if max_stock and max_stock > 0 and available > max_stock:
        return "黄"
    return "绿"

# 兜底库位/批次（大白话：正常情况不再直接用这两个值，库位先去仓库主数据查、批次按当天日期生成；
# 只有仓库不在主数据里时才用这里的环境变量兜底，见 warehouse_default_location / today_batch）
DEFAULT_LOCATION = os.environ.get("PMC_DEFAULT_LOCATION", "A区-01")
DEFAULT_BATCH = os.environ.get("PMC_DEFAULT_BATCH", "BATCH-20261019")

def warehouse_default_location(conn, warehouse: str) -> str:
    """取仓库的默认库位（大白话：这个仓库平时把货放哪个库位，在仓库主数据里配，P1-5 用户选 A 方案）。
    仓库不在主数据里或没配默认库位时，才用环境变量 PMC_DEFAULT_LOCATION 兜底。"""
    try:
        row = conn.execute("SELECT default_location FROM warehouses WHERE code=?", (warehouse,)).fetchone()
        if row and row["default_location"]:
            return str(row["default_location"])
    except Exception:
        pass
    return DEFAULT_LOCATION

def today_batch() -> str:
    """当天日期生成的批次号（大白话：入库不填批次时用今天日期，比如 BATCH-20261020，不再写死 BATCH-20261019，P1-5 用户选 A 方案）。"""
    return "BATCH-" + date.today().strftime("%Y%m%d")

def add_ledger(part_code, qty_change, typ, ref_no, remark="", warehouse="主仓", batch=None, company_code=None, conn=None,
               location=None, operator="", unit_price=0.0):
    """记库存流水（大白话：库存每动一次就写一条账）。传了 conn 就复用同一个事务，不再自己提交。
    P2-5：流水补记 库位/经办人/单价，出库入库表导出就靠这三列；不填批次时按当天日期生成（P1-5）。"""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        if company_code is None:
            company_code = warehouse_company(conn, warehouse)
        conn.execute("INSERT INTO stock_ledger (part_code,warehouse,location,batch,change_qty,type,ref_no,operator,unit_price,remark,created_at,company_code) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (part_code, warehouse, location or "", batch or today_batch(), qty_change, typ, ref_no, operator or "", float(unit_price or 0), remark, now_str(), company_code))
        if own:
            conn.commit()
    except Exception:
        if own:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if own:
            conn.close()

def adjust_stock(part_code, delta, typ, ref_no, warehouse="主仓", location=None, batch=None, company_code=None, conn=None,
                 operator="", unit_price=0.0):
    """调库存（大白话：入库就加，出库就减），库存变更和流水写在同一个事务里（P1-6：不再“库存变了却没流水”）。
    P1-5：不填库位就用该仓库的默认库位（仓库主数据），不填批次就按当天日期生成批次；不再写死 A区-01 / BATCH-20261019。
    P2-5：流水里记下经办人、单价（单价=含税口径按零件档案算），给入库/出库表导出用。"""
    own = conn is None
    if own:
        conn = get_conn()
    location = location or warehouse_default_location(conn, warehouse)
    batch = batch or today_batch()
    try:
        if company_code is None:
            company_code = warehouse_company(conn, warehouse)
        row = conn.execute("SELECT * FROM stock WHERE part_code=? AND warehouse=? AND location=? AND batch=?", (part_code, warehouse, location, batch)).fetchone()
        if row:
            conn.execute("UPDATE stock SET qty=qty+? WHERE id=?", (delta, row["id"]))
        else:
            conn.execute("INSERT INTO stock (part_code,warehouse,location,batch,qty,company_code) VALUES (?,?,?,?,?,?)", (part_code, warehouse, location, batch, delta, company_code))
        conn.execute("INSERT INTO stock_ledger (part_code,warehouse,location,batch,change_qty,type,ref_no,operator,unit_price,remark,created_at,company_code) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (part_code, warehouse, location, batch, delta, typ, ref_no, operator or "", float(unit_price or 0), f"{typ} {delta}", now_str(), company_code))
        if own:
            conn.commit()
    except Exception:
        if own:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if own:
            conn.close()

def _version_key(v: str):
    """版本号排序键（大白话：V10 要排在 V9 后面，不能按字符串比大小）。
    取版本号里的数字；解析不出数字的排最后，再按原字符串兜底。"""
    import re as _re
    m = _re.search(r"(\d+)", str(v or ""))
    return (0, int(m.group(1)), str(v)) if m else (1, 0, str(v))

def resolve_bom_version(conn, parent_code: str, version: Optional[str] = None) -> str:
    """解析 BOM 版本：指定了就用指定的；没指定就用“生效日期 <= 今天”的最新版本（数字比较，V10 > V9）；
    一个生效版本都没有时才退回最新版本（P1-7）。"""
    if version:
        row = conn.execute("SELECT version FROM bom_items WHERE parent_code=? AND version=? LIMIT 1", (parent_code, version)).fetchone()
        if row:
            return version
        raise HTTPException(404, f"父件 {parent_code} 没有 BOM 版本 {version}")
    rows = conn.execute("SELECT DISTINCT version FROM bom_items WHERE parent_code=? AND (effective_date IS NULL OR effective_date='' OR effective_date<=?)",
                        (parent_code, TODAY)).fetchall()
    if rows:
        return max((r["version"] for r in rows), key=_version_key)
    rows = conn.execute("SELECT DISTINCT version FROM bom_items WHERE parent_code=?", (parent_code,)).fetchall()
    if rows:
        return max((r["version"] for r in rows), key=_version_key)
    return "V1"

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

def direct_bom_children(conn, parent: str, qty: float, version: Optional[str] = None):
    """直接子件（大白话：配方表上直接写的那一层，不展开到底层）。
    返回 [{"part_code","qty","is_subassembly","category","is_purchase"}]。
    半成品判定：类别是半成品，或不可采购（自制件），或自己有 BOM。"""
    ver = resolve_bom_version(conn, parent, version)
    rows = conn.execute("SELECT * FROM bom_items WHERE parent_code=? AND version=? ORDER BY child_code", (parent, ver)).fetchall()
    out = []
    for r in rows:
        child = r["child_code"]
        need = qty * float(r["qty"]) * (1 + float(r["loss_rate"] or 0))
        part = conn.execute("SELECT * FROM parts WHERE code=?", (child,)).fetchone()
        has_bom = conn.execute("SELECT 1 FROM bom_items WHERE parent_code=? LIMIT 1", (child,)).fetchone()
        is_sub = bool(has_bom) or (part and part["category"] == "半成品") or (part and int(part["is_purchase"] or 1) == 0)
        out.append({"part_code": child, "qty": need, "is_subassembly": is_sub,
                    "category": part["category"] if part else "", "is_purchase": int(part["is_purchase"]) if part and part["is_purchase"] is not None else 1})
    return out

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
    company_code: str = "COMP-001"

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
    company_code: str = "COMP-001"; warehouse: str = "主仓"; currency: str = "CNY"; exchange_rate: float = 0

class ReceiveIn(BaseModel):
    order_no: str; part_code: str; qty: float
    qc_result: str = "合格"; batch: str = ""; location: str = ""; warehouse: str = ""

# ---------- 零件 ----------
@app.get("/api/parts")
def list_parts(company_code: Optional[str] = None, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "零件", "view")
    ac = allowed_companies(user)
    conn = get_conn()
    sql = "SELECT * FROM parts WHERE 1=1"; args: List[Any] = []
    if company_code:
        check_scope(user, company_code=company_code)
        sql += " AND company_code=?"; args.append(company_code)
    elif ac is not None:
        sql += " AND company_code IN (%s)" % ",".join("?" * len(ac)); args.extend(sorted(ac))
    sql += " ORDER BY code"
    rows = conn.execute(sql, args).fetchall()
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
def create_part(p: PartIn, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "零件", "create")
    check_scope(user, company_code=p.company_code)
    conn = get_conn()
    try:
        conn.execute("INSERT INTO parts (code,name,spec,unit,category,is_purchase,safety_stock,min_stock,max_stock,lead_time_days,price,tax_rate,supplier_code,created_at,company_code) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (p.code,p.name,p.spec,p.unit,p.category,p.is_purchase,p.safety_stock,p.min_stock,p.max_stock,p.lead_time_days,p.price,p.tax_rate,p.supplier_code,now_str(),p.company_code))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"零件编码 {p.code} 已存在")
    conn.close()
    log_audit(user, "创建", "零件", p.code, "名称", "", p.name, f"新建零件 {p.name}", p.company_code)
    return {"ok": True, "code": p.code}

@app.put("/api/parts/{code}")
def update_part(code: str, p: PartUpdate, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "零件", "edit")
    conn = get_conn()
    r = conn.execute("SELECT * FROM parts WHERE code=?", (code,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, f"零件 {code} 不存在")
    check_scope(user, company_code=r["company_code"])
    updates = {k: v for k, v in p.dict().items() if v is not None}
    for k, v in updates.items():
        conn.execute(f"UPDATE parts SET {k}=? WHERE code=?", (v, code))
        log_audit(user, "修改", "零件", code, k, str(r[k]) if k in r.keys() else "", str(v), "", r["company_code"], conn)
    conn.commit(); conn.close()
    return {"ok": True, "code": code}

@app.delete("/api/parts/{code}")
def delete_part(code: str, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "零件", "delete")
    conn = get_conn()
    used = conn.execute("SELECT COUNT(*) FROM bom_items WHERE parent_code=? OR child_code=?", (code, code)).fetchone()[0]
    if used:
        conn.close()
        raise HTTPException(400, f"零件 {code} 已在 BOM 中使用，不能删除，请先删除相关 BOM")
    conn.execute("DELETE FROM parts WHERE code=?", (code,))
    conn.commit(); conn.close()
    log_audit(user, "删除", "零件", code, "", code, "", "")
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
def create_supplier(s: SupplierIn, x_user: Optional[str] = Header(None)):
    _u = resolve_user(x_user); require_perm(_u, "供应商", "create")
    conn = get_conn()
    try:
        conn.execute("INSERT INTO suppliers (code,name,contact,phone,address,payment,lead_time_days,rating,status) VALUES (?,?,?,?,?,?,?,?,?)",
                     (s.code,s.name,s.contact,s.phone,s.address,s.payment,s.lead_time_days,s.rating,s.status))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"供应商编码 {s.code} 已存在")
    conn.close()
    log_audit(_u, "创建", "供应商", s.code, "名称", "", s.name, "")
    return {"ok": True, "code": s.code}

@app.put("/api/suppliers/{code}")
def update_supplier(code: str, s: SupplierUpdate, x_user: Optional[str] = Header(None)):
    _u = resolve_user(x_user); require_perm(_u, "供应商", "edit")
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
def delete_supplier(code: str, x_user: Optional[str] = Header(None)):
    _u = resolve_user(x_user); require_perm(_u, "供应商", "delete")
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
def blacklist_supplier(code: str, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "供应商", "edit")
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
def approve_supplier(code: str, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "供应商", "approve")
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
def compare_suppliers(part_code: str, need_date: Optional[str] = None, prefer: str = "balanced"):
    """自动比价（大白话：买这个零件，找谁最便宜、找谁到货最快，一眼能看）。
    第三阶段新增：带上 need_date（哪天要货）+ prefer（balanced/cheapest/fastest），直接给推荐供应商和理由。"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT sp.*, s.name as supplier_name, s.status as supplier_status
        FROM supplier_parts sp LEFT JOIN suppliers s ON s.code=sp.supplier_code
        WHERE sp.part_code=? ORDER BY sp.price ASC
    """, (part_code,)).fetchall()
    if not rows:
        conn.close()
        raise HTTPException(404, f"零件 {part_code} 没有任何供应商报价")
    usable = [r for r in rows if (r["supplier_status"] or "合格") != "黑名单"]
    cheapest_code = usable[0]["supplier_code"] if usable else None
    fastest_code = min(usable, key=lambda r: r["lead_time_days"] or 999)["supplier_code"] if usable else None
    rec, reason, eta = recommend_supplier_for(conn, part_code, need_date, prefer)
    conn.close()
    out = []
    for r in rows:
        d = row_to_dict(r)
        d["is_cheapest"] = (r["supplier_code"] == cheapest_code)
        d["is_fastest"] = (r["supplier_code"] == fastest_code)
        d["is_recommended"] = bool(rec is not None and r["supplier_code"] == rec["supplier_code"])
        d["skipped_blacklist"] = ((r["supplier_status"] or "合格") == "黑名单")
        d["eta_date"] = (date.today() + timedelta(days=int(r["lead_time_days"] or 7))).isoformat()
        out.append(d)
    return {"part_code": part_code, "cheapest_supplier": cheapest_code, "fastest_supplier": fastest_code,
            "recommended_supplier": rec["supplier_code"] if rec is not None else "",
            "recommended_supplier_name": rec["supplier_name"] if rec is not None else "",
            "recommend_reason": reason, "eta_date": eta, "options": out}

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
def create_bom_item(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    _u = resolve_user(x_user); require_perm(_u, "零件", "edit")
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
    log_audit(_u, "修改", "BOM", parent, "子件用量", "", f"{child}={qty}({version})", "")
    return {"ok": True, "parent_code": parent, "child_code": child, "version": version}

@app.delete("/api/bom")
def delete_bom_item(parent_code: str, child_code: str, version: str = "V1", x_user: Optional[str] = Header(None)):
    _u = resolve_user(x_user); require_perm(_u, "零件", "edit")
    conn = get_conn()
    conn.execute("DELETE FROM bom_items WHERE parent_code=? AND child_code=? AND version=?", (parent_code, child_code, version))
    conn.commit(); conn.close()
    log_audit(_u, "删除", "BOM", parent_code, "子件", child_code, "", "")
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
def bom_explode_api(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "零件", "view")
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
def list_stock(warehouse: Optional[str] = None, company_code: Optional[str] = None, batch: Optional[str] = None,
               x_user: Optional[str] = Header(None)):
    """库存列表：可按仓库/公司/批次过滤（第二阶段：多仓库）。仓库受限用户只能看自己的仓库。"""
    user = resolve_user(x_user)
    require_perm(user, "库存", "view")
    check_scope(user, warehouse=warehouse, company_code=company_code)
    aw = allowed_warehouses(user); ac = allowed_companies(user)
    conn = get_conn()
    sql = """SELECT s.*, p.name as part_name, p.unit, p.safety_stock, p.max_stock
        FROM stock s LEFT JOIN parts p ON p.code=s.part_code WHERE 1=1"""
    args: List[Any] = []
    if warehouse:
        sql += " AND s.warehouse=?"; args.append(warehouse)
    elif aw is not None:
        sql += " AND s.warehouse IN (%s)" % ",".join("?" * len(aw)); args.extend(sorted(aw))
    if company_code:
        sql += " AND s.company_code=?"; args.append(company_code)
    elif ac is not None:
        sql += " AND s.company_code IN (%s)" % ",".join("?" * len(ac)); args.extend(sorted(ac))
    if batch:
        sql += " AND s.batch=?"; args.append(batch)
    sql += " ORDER BY s.part_code, s.warehouse, s.batch"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.get("/api/stock/frozen")
def list_frozen(part_code: Optional[str] = None, batch: Optional[str] = None, warehouse: Optional[str] = None,
                x_user: Optional[str] = Header(None)):
    """冻结明细：按零件/批次/仓库查谁被冻了多少（第二阶段：按批次冻结）。"""
    user = resolve_user(x_user)
    require_perm(user, "库存", "view")
    conn = get_conn()
    sql = "SELECT * FROM frozen_stock WHERE 1=1"; args: List[Any] = []
    if part_code:
        sql += " AND part_code=?"; args.append(part_code)
    if batch:
        sql += " AND batch=?"; args.append(batch)
    if warehouse:
        sql += " AND warehouse=?"; args.append(warehouse)
    sql += " ORDER BY id"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.get("/api/stock/available/batch")
def batch_available(part_code: str, warehouse: str = "主仓", batch: str = "", x_user: Optional[str] = Header(None)):
    """某批次可用量 = 该批次现有 - 该批次冻结。"""
    user = resolve_user(x_user)
    require_perm(user, "库存", "view")
    conn = get_conn()
    avail = get_batch_available(conn, part_code, warehouse, batch)
    onhand = float(conn.execute("SELECT COALESCE(SUM(qty),0) FROM stock WHERE part_code=? AND warehouse=? AND batch=?",
                                (part_code, warehouse, batch)).fetchone()[0] or 0)
    frozen = get_batch_frozen(conn, part_code, warehouse, batch)
    conn.close()
    return {"part_code": part_code, "warehouse": warehouse, "batch": batch,
            "onhand": onhand, "frozen": frozen, "available": avail}

@app.get("/api/stock/ledger")
def list_ledger(part_code: Optional[str] = None, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "库存", "view")
    conn = get_conn()
    if part_code:
        rows = conn.execute("SELECT * FROM stock_ledger WHERE part_code=? ORDER BY id DESC LIMIT 200", (part_code,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM stock_ledger ORDER BY id DESC LIMIT 200").fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.get("/api/stock/trace/{part_code}")
def trace_stock(part_code: str, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "库存", "view")
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
def list_available(warehouse: Optional[str] = None, company_code: Optional[str] = None, x_user: Optional[str] = Header(None)):
    """可用量总览：每个零件的 现有/在途/已分配/冻结/可用量 + 红黄绿，可按仓库/公司过滤"""
    user = resolve_user(x_user)
    require_perm(user, "库存", "view")
    check_scope(user, warehouse=warehouse, company_code=company_code)
    conn = get_conn()
    parts = conn.execute("SELECT * FROM parts ORDER BY code").fetchall()
    conn.close()
    ac_list = allowed_companies(user)
    if ac_list:
        parts = [p for p in parts if (p["company_code"] if "company_code" in p.keys() else "COMP-001") in ac_list]
    out = []
    for p in parts:
        code = p["code"]
        onhand = get_onhand(code, warehouse, company_code); it = get_in_transit(code, warehouse, company_code)
        al = get_allocated(code, warehouse, company_code); fr = get_frozen(code, warehouse, None, company_code)
        avail = onhand + it - al - fr
        out.append({"part_code": code, "part_name": p["name"], "onhand": onhand, "in_transit": it,
                    "allocated": al, "frozen": fr, "available": avail,
                    "safety_stock": p["safety_stock"], "max_stock": p["max_stock"],
                    "status": stock_status(avail, float(p["safety_stock"] or 0), float(p["max_stock"] or 0))})
    return out

@app.get("/api/stock/available/{part_code}")
def one_available(part_code: str, warehouse: Optional[str] = None, company_code: Optional[str] = None, x_user: Optional[str] = Header(None)):
    """单个零件可用量（P0-4：补权限 + 仓库/公司范围校验；以前谁登录都能看别的公司/仓库）"""
    user = resolve_user(x_user)
    require_perm(user, "库存", "view")
    conn = get_conn()
    p = conn.execute("SELECT * FROM parts WHERE code=?", (part_code,)).fetchone()
    conn.close()
    if not p:
        raise HTTPException(404, f"零件 {part_code} 不存在")
    # P0-4：按零件所属公司校验范围（以前只校验 query 参数，keeper 直接传零件号就能看别家公司）
    check_scope(user, warehouse=warehouse, company_code=company_code or (p["company_code"] if "company_code" in p.keys() else None))
    onhand = get_onhand(part_code, warehouse, company_code); it = get_in_transit(part_code, warehouse, company_code)
    al = get_allocated(part_code, warehouse, company_code); fr = get_frozen(part_code, warehouse, None, company_code)
    avail = onhand + it - al - fr
    return {"part_code": part_code, "part_name": p["name"], "onhand": onhand, "in_transit": it,
            "allocated": al, "frozen": fr, "available": avail,
            "safety_stock": p["safety_stock"], "max_stock": p["max_stock"],
            "status": stock_status(avail, float(p["safety_stock"] or 0), float(p["max_stock"] or 0))}

@app.post("/api/stock/in")
def stock_in(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """入库（大白话：货进仓库，库存增加）。type 如：采购入库/其他入库/生产入库"""
    user = resolve_user(x_user)
    require_perm(user, "库存", "create")
    code = payload.get("part_code", ""); qty = num_field(payload.get("qty"), "数量", required=True, positive=True)
    warehouse = payload.get("warehouse", "主仓")
    check_scope(user, warehouse=warehouse)
    if not code:
        raise HTTPException(400, "零件编码必填")
    # P1-5：库位/批次不填就用仓库默认库位+当天日期批次（adjust_stock 里按仓库主数据兜底）
    adjust_stock(code, qty, payload.get("type", "其他入库"), payload.get("ref_no", "手动入库"),
                 warehouse, payload.get("location"), payload.get("batch"), operator=user)
    log_audit(user, "入库", "库存", code, "数量", "", f"+{qty}@{warehouse}", payload.get("ref_no", "手动入库"))
    return {"ok": True, "part_code": code, "qty": qty, "onhand": get_onhand(code, warehouse)}

@app.post("/api/stock/out")
def stock_out(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """出库（大白话：货出仓库，库存减少）。
    指定批次：出库前检查该批次可用库存（现有-该批次冻结）够不够；
    不指定批次：按批次 FIFO（先进先出）扣，和生产领料一个逻辑，不再写死从 BATCH-20261019 扣。"""
    user = resolve_user(x_user)
    require_perm(user, "库存", "edit")
    code = payload.get("part_code", ""); qty = num_field(payload.get("qty"), "数量", required=True, positive=True)
    warehouse = payload.get("warehouse", "主仓")
    location = payload.get("location")
    batch = payload.get("batch")
    check_scope(user, warehouse=warehouse)
    if not code:
        raise HTTPException(400, "零件编码必填")
    conn = get_conn()
    try:
        loc = location or warehouse_default_location(conn, warehouse)
        if batch:
            batch_avail = get_batch_available(conn, code, warehouse, batch)
            if batch_avail + 1e-9 < qty:
                raise HTTPException(400, f"零件 {code} 批次 {batch} 可用量不足：需要 {qty}，该批次可用 {batch_avail}（现有-该批次冻结）")
            adjust_stock(code, -qty, payload.get("type", "其他出库"), payload.get("ref_no", "手动出库"),
                         warehouse, loc, batch, conn=conn, operator=user)
        else:
            # 未指定批次：按批次 FIFO 逐个扣，跳过被冻结的批次量
            comp = warehouse_company(conn, warehouse)
            rows = conn.execute("SELECT * FROM stock WHERE part_code=? AND warehouse=? AND location=? AND company_code=? AND qty>0 ORDER BY batch ASC",
                                (code, warehouse, loc, comp)).fetchall()
            remain = qty
            for row in rows:
                if remain <= 1e-9:
                    break
                take_avail = float(row["qty"]) - get_batch_frozen(conn, code, warehouse, row["batch"], comp)
                take = min(take_avail, remain)
                if take <= 1e-9:
                    continue
                adjust_stock(code, -take, payload.get("type", "其他出库"), payload.get("ref_no", "手动出库"),
                             warehouse, loc, row["batch"], comp, conn=conn, operator=user)
                remain -= take
            if remain > 1e-9:
                raise HTTPException(400, f"零件 {code} 仓库 {warehouse}/{loc} 可用量不足：需要 {qty}，只够 {round(qty - remain, 4)}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log_audit(user, "出库", "库存", code, "数量", "", f"-{qty}@{warehouse}/{batch or 'FIFO'}", payload.get("ref_no", "手动出库"))
    return {"ok": True, "part_code": code, "qty": qty, "onhand": get_onhand(code, warehouse)}

@app.post("/api/stock/transfer")
def stock_transfer(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """调拨（大白话：同仓库换库位，或跨仓库调货，总数不变，只换位置；跨公司不允许调拨，公司隔离）。
    兼容第一阶段：只传 warehouse 时当同仓库调拨；传 from_warehouse/to_warehouse 时当跨仓库调拨。"""
    user = resolve_user(x_user)
    require_perm(user, "库存", "edit")
    code = payload.get("part_code", ""); qty = num_field(payload.get("qty"), "数量", required=True, positive=True)
    from_wh = payload.get("from_warehouse") or payload.get("warehouse", "主仓")
    to_wh = payload.get("to_warehouse") or payload.get("warehouse", "主仓")
    check_scope(user, warehouse=from_wh); check_scope(user, warehouse=to_wh)
    if not code:
        raise HTTPException(400, "零件编码必填")
    conn = get_conn()
    try:
        # P1-5：调出库位默认取调出仓库的默认库位；调入库位默认取调入仓库的默认库位（不再写死 A区-01 / B区-01）；
        # 调出批次不填时取该库位最早有量的批次（FIFO），不再写死 BATCH-20261019
        from_loc = payload.get("from_location") or warehouse_default_location(conn, from_wh)
        to_loc = payload.get("to_location") or warehouse_default_location(conn, to_wh)
        from_batch = payload.get("from_batch")
        if not from_batch:
            _brow = conn.execute("SELECT batch FROM stock WHERE part_code=? AND warehouse=? AND location=? AND qty>0 ORDER BY batch ASC LIMIT 1",
                                 (code, from_wh, from_loc)).fetchone()
            if not _brow:
                raise HTTPException(400, f"调出库位无库存：{from_wh}/{from_loc}")
            from_batch = _brow["batch"]
        to_batch = payload.get("to_batch") or from_batch
        if from_wh == to_wh and from_loc == to_loc and from_batch == to_batch:
            raise HTTPException(400, "调出与调入库位/批次相同，不用调拨")
        fc = warehouse_company(conn, from_wh); tc = warehouse_company(conn, to_wh)
        if fc != tc:
            raise HTTPException(400, f"不能跨公司调拨：{from_wh}（{fc}）→ {to_wh}（{tc}），公司隔离")
        row = conn.execute("SELECT * FROM stock WHERE part_code=? AND warehouse=? AND location=? AND batch=?", (code, from_wh, from_loc, from_batch)).fetchone()
        if not row:
            raise HTTPException(400, f"调出库位无库存：{from_wh}/{from_loc}/{from_batch}")
        batch_avail = float(row["qty"]) - get_batch_frozen(conn, code, from_wh, from_batch)
        if batch_avail + 1e-9 < qty:
            raise HTTPException(400, f"调出批次可用量不足：{from_wh}/{from_batch} 可用 {batch_avail}（现有 {float(row['qty'])} - 该批次冻结），要调 {qty}")
        conn.execute("UPDATE stock SET qty=qty-? WHERE id=?", (qty, row["id"]))
        dest = conn.execute("SELECT * FROM stock WHERE part_code=? AND warehouse=? AND location=? AND batch=?", (code, to_wh, to_loc, to_batch)).fetchone()
        if dest:
            conn.execute("UPDATE stock SET qty=qty+? WHERE id=?", (qty, dest["id"]))
        else:
            conn.execute("INSERT INTO stock (part_code,warehouse,location,batch,qty,company_code) VALUES (?,?,?,?,?,?)", (code, to_wh, to_loc, to_batch, qty, tc))
        note = f"从 {from_wh}/{from_loc}/{from_batch} 调到 {to_wh}/{to_loc}/{to_batch}"
        add_ledger(code, -qty, "调拨出", payload.get("ref_no", "调拨"), note, from_wh, from_batch, conn=conn, location=from_loc, operator=user)
        add_ledger(code, qty, "调拨入", payload.get("ref_no", "调拨"), note, to_wh, to_batch, conn=conn, location=to_loc, operator=user)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log_audit(user, "调拨", "库存", code, "数量", "", f"{qty} {from_wh}→{to_wh}", payload.get("ref_no", "调拨"))
    return {"ok": True, "part_code": code, "qty": qty, "from": f"{from_wh}/{from_loc}/{from_batch}", "to": f"{to_wh}/{to_loc}/{to_batch}"}

@app.post("/api/stock/count")
def stock_count(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """盘点（大白话：实地数一数，数出来多少库存就是多少，差额记盘盈/盘亏）"""
    user = resolve_user(x_user)
    require_perm(user, "库存", "edit")
    code = payload.get("part_code", "")
    counted = num_field(payload.get("counted_qty"), "实盘数量", required=True)
    warehouse = payload.get("warehouse", "主仓")
    check_scope(user, warehouse=warehouse)
    if not code:
        raise HTTPException(400, "零件编码必填")
    conn = get_conn()
    try:
        # P1-5：库位默认取仓库默认库位。
        # 盘点不指定批次时，必须落在“该库位已有库存的批次”上：否则会凭空新建当天批次，
        # 旧批次数量不扣、库存被重复计算（实测：20 件盘成 100 件后总数变 120）。
        location = payload.get("location") or warehouse_default_location(conn, warehouse)
        batch = payload.get("batch")
        if not batch:
            _brow = conn.execute("SELECT batch FROM stock WHERE part_code=? AND warehouse=? AND location=? AND qty>0 ORDER BY batch ASC LIMIT 1",
                                 (code, warehouse, location)).fetchone()
            batch = _brow["batch"] if _brow else today_batch()
        row = conn.execute("SELECT * FROM stock WHERE part_code=? AND warehouse=? AND location=? AND batch=?", (code, warehouse, location, batch)).fetchone()
        old = float(row["qty"]) if row else 0
        diff = counted - old
        comp = warehouse_company(conn, warehouse)
        if row:
            conn.execute("UPDATE stock SET qty=? WHERE id=?", (counted, row["id"]))
        else:
            conn.execute("INSERT INTO stock (part_code,warehouse,location,batch,qty,company_code) VALUES (?,?,?,?,?,?)", (code, warehouse, location, batch, counted, comp))
        if abs(diff) > 1e-9:
            add_ledger(code, diff, "盘点", payload.get("ref_no", "盘点"), f"盘点：账面 {old} → 实盘 {counted}，差异 {diff}", warehouse, batch, conn=conn, location=location, operator=user)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if abs(diff) > 1e-9:
        log_audit(user, "盘点", "库存", code, "数量", str(old), str(counted), f"差异 {diff}@{warehouse}/{batch}")
    return {"ok": True, "part_code": code, "old_qty": old, "counted_qty": counted, "diff": diff}

@app.post("/api/stock/freeze")
def stock_freeze(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """按批次冻结（大白话：冻住某个仓库、某个批次的货，那个批次就不能用了，其他批次不受影响）。
    思路来源：ERPNext 按批次/序列预留（特定批次预留给某单，不只是按零件总数），见验证报告参考来源。
    传 batch 则只冻该批次（校验该批次可用量）；不传 batch 则按汇总可用量冻（兼容第一阶段）。"""
    user = resolve_user(x_user)
    require_perm(user, "库存", "edit")
    code = payload.get("part_code", ""); qty = num_field(payload.get("qty"), "数量", required=True, positive=True)
    warehouse = payload.get("warehouse", "主仓")
    batch = payload.get("batch", "")
    check_scope(user, warehouse=warehouse)
    if not code:
        raise HTTPException(400, "零件编码必填")
    conn = get_conn()
    comp = warehouse_company(conn, warehouse)
    if batch:
        avail = get_batch_available(conn, code, warehouse, batch)
        if avail + 1e-9 < qty:
            conn.close()
            raise HTTPException(400, f"零件 {code} 批次 {batch} 可用量不足，无法冻结 {qty}（该批次可用 {avail}）")
    else:
        avail = get_available(code, warehouse)
        if avail + 1e-9 < qty:
            conn.close()
            raise HTTPException(400, f"零件 {code} 可用量不足，无法冻结 {qty}（可用 {avail}）")
    conn.execute("INSERT INTO frozen_stock (part_code,qty,reason,ref_no,created_at,warehouse,location,batch,company_code) VALUES (?,?,?,?,?,?,?,?,?)",
                 (code, qty, payload.get("reason", "冻结"), payload.get("ref_no", ""), now_str(), warehouse, payload.get("location", ""), batch, comp))
    conn.commit(); conn.close()
    log_audit(user, "冻结", "库存", code, "冻结量", "", str(qty), f"{warehouse}/{batch or '全部批次'} {payload.get('reason','冻结')}")
    return {"ok": True, "part_code": code, "frozen_qty": qty, "warehouse": warehouse, "batch": batch, "available": get_available(code, warehouse)}

@app.post("/api/stock/unfreeze")
def stock_unfreeze(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """按批次解冻（大白话：之前冻住的某批次货又能用了）。传 batch/warehouse 则只解该批次；不传则按先进先出解最早的冻结。"""
    user = resolve_user(x_user)
    require_perm(user, "库存", "edit")
    code = payload.get("part_code", ""); qty = num_field(payload.get("qty"), "数量", required=True, positive=True)
    warehouse = payload.get("warehouse", "")
    batch = payload.get("batch", "")
    check_scope(user, warehouse=warehouse or None)
    if not code:
        raise HTTPException(400, "零件编码必填")
    conn = get_conn()
    sql = "SELECT * FROM frozen_stock WHERE part_code=?"; args: List[Any] = [code]
    if warehouse:
        sql += " AND warehouse=?"; args.append(warehouse)
    if batch:
        sql += " AND batch=?"; args.append(batch)
    sql += " ORDER BY id"
    rows = conn.execute(sql, args).fetchall()
    total = sum(float(r["qty"]) for r in rows)
    if total + 1e-9 < qty:
        conn.close()
        raise HTTPException(400, f"零件 {code} 冻结量只有 {total}（{warehouse or '全部仓库'}/{batch or '全部批次'}），无法解冻 {qty}")
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
    log_audit(user, "解冻", "库存", code, "冻结量", "", f"-{qty}", f"{warehouse or '全部仓库'}/{batch or '全部批次'}")
    return {"ok": True, "part_code": code, "unfrozen_qty": qty, "available": get_available(code)}

# ---------- 销售单（轻量） ----------
@app.get("/api/sales")
def list_sales(company_code: Optional[str] = None, warehouse: Optional[str] = None, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "销售", "view")
    conn = get_conn()
    sql = "SELECT * FROM sales_orders WHERE 1=1"; args: List[Any] = []
    if company_code:
        check_scope(user, company_code=company_code)
        sql += " AND company_code=?"; args.append(company_code)
    if warehouse:
        sql += " AND warehouse=?"; args.append(warehouse)
    sql += " ORDER BY id DESC"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.post("/api/sales")
def create_sales(o: SalesOrderIn, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "销售", "create")
    check_scope(user, warehouse=o.warehouse, company_code=o.company_code)
    conn = get_conn()
    rate = resolve_exchange_rate(conn, o.currency, "CNY", o.exchange_rate or None) if o.currency != "CNY" else 1.0
    try:
        conn.execute("INSERT INTO sales_orders (order_no,customer,product_code,qty,due_date,status,created_at,company_code,warehouse,currency,exchange_rate) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (o.order_no,o.customer,o.product_code,o.qty,o.due_date,"新建",now_str(),o.company_code,o.warehouse,o.currency,rate))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"销售单号 {o.order_no} 已存在")
    conn.close()
    log_audit(user, "创建", "销售单", o.order_no, "数量", "", str(o.qty), f"{o.product_code} x {o.qty}", o.company_code)
    return {"ok": True, "order_no": o.order_no}

@app.put("/api/sales/{order_no}")
def update_sales(order_no: str, payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "销售", "edit")
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
def delete_sales(order_no: str, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "销售", "delete")
    conn = get_conn()
    conn.execute("DELETE FROM sales_orders WHERE order_no=?", (order_no,))
    conn.execute("DELETE FROM mrp_results WHERE sales_order_no=?", (order_no,))
    conn.commit(); conn.close()
    return {"ok": True, "order_no": order_no}

@app.post("/api/sales/{order_no}/create-production")
def sales_create_production(order_no: str, payload: Dict[str, Any] = None, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "生产", "create")
    """销售单触发生产需求（大白话：客户下单了，就自动生成一张生产单，不做 CRM）"""
    conn = get_conn()
    so = conn.execute("SELECT * FROM sales_orders WHERE order_no=?", (order_no,)).fetchone()
    conn.close()
    if not so:
        raise HTTPException(404, f"销售单 {order_no} 不存在")
    mo_no = (payload or {}).get("order_no") or ""
    return create_production({"order_no": mo_no, "product_code": so["product_code"], "qty": float(so["qty"]),
                              "sales_order_no": order_no,
                              "company_code": so["company_code"], "warehouse": so["warehouse"],
                              "planned_start": (payload or {}).get("planned_start", ""),
                              "planned_end": (payload or {}).get("planned_end", "") or so["due_date"]})

# ---------- MRP ----------
@app.post("/api/mrp")
def run_mrp(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """
    MRP 计算（多层 BOM 版）
    步骤：1) 多层 BOM 爆炸得毛需求  2) 查可用量  3) 算净需求  4) 按提前期偏移算最晚下单日  5) 存结果
    公式：净需求 = 毛需求 - 可用量 + 安全库存；可用量 = 现有 + 在途 - 已分配 - 冻结
    （P0-3：以前只减“现有+在途”，没减已分配/冻结，和库存/看板/补货的口径不一致，会少买料）
    最晚下单日 = 需求日期 - 提前期天数
    """
    _u = resolve_user(x_user)
    require_perm(_u, "采购", "view")
    sales_no = payload.get("sales_order_no", "")
    conn = get_conn()
    try:
        so = conn.execute("SELECT * FROM sales_orders WHERE order_no=?", (sales_no,)).fetchone()
        if not so:
            raise HTTPException(404, f"销售单 {sales_no} 不存在")
        product = so["product_code"]
        order_qty = float(so["qty"])
        need_date = parse_date(so["due_date"]) or date.today()
        mrp_company = payload.get("company_code") or so["company_code"] or "COMP-001"
        mrp_warehouse = payload.get("warehouse") or so["warehouse"] or "主仓"
        check_scope(_u, mrp_warehouse, mrp_company)
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
            # 与“库存/看板/补货/领料”统一口径：现有 + 在途 - 已分配 - 冻结
            available = get_available(child, mrp_warehouse, mrp_company)
            onhand = get_onhand(child, mrp_warehouse, mrp_company)
            in_transit = get_in_transit(child, mrp_warehouse, mrp_company)
            net = gross - available + safety
            if net < 0:
                net = 0
            latest = (need_date - timedelta(days=lead)).isoformat()
            need_type = "采购" if is_purchase else "生产"
            conn.execute("INSERT INTO mrp_results (sales_order_no,part_code,part_name,gross,onhand,in_transit,safety,net,adjusted_net,level,need_type,latest_order_date,created_at,company_code,warehouse) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (sales_no, child, part_name, gross, onhand, in_transit, safety, net, None, it["level"], need_type, latest, now_str(), mrp_company, mrp_warehouse))
            results.append({"part_code": child, "part_name": part_name, "gross": gross, "onhand": onhand,
                            "in_transit": in_transit, "available": available, "safety": safety,
                            "net": net, "adjusted_net": None,
                            "level": it["level"], "need_type": need_type, "latest_order_date": latest})
        conn.commit()
        return {"sales_order_no": sales_no, "results": results}
    finally:
        conn.close()

@app.get("/api/mrp/{sales_no}")
def get_mrp(sales_no: str, warehouse: Optional[str] = None, company_code: Optional[str] = None, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "采购", "view")
    check_scope(user, warehouse=warehouse, company_code=company_code)
    conn = get_conn()
    sql = "SELECT * FROM mrp_results WHERE sales_order_no=?"; args: List[Any] = [sales_no]
    if warehouse:
        sql += " AND warehouse=?"; args.append(warehouse)
    if company_code:
        sql += " AND company_code=?"; args.append(company_code)
    sql += " ORDER BY level, part_code"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.put("/api/mrp/adjust")
def adjust_mrp(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "采购", "edit")
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
def mrp_generate(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "采购", "create")
    """
    MRP 一键生成：采购件按推荐供应商分组生成采购单（第三阶段：均衡推荐——最便宜且能按时到货的优先，
    赶不上需求日时在能按时到货的里面选最便宜的，黑名单跳过），半成品/成品需求生成生产单。返回生成的单号。
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
            rec, _reason, _eta = recommend_supplier_for(conn, r["part_code"], r["latest_order_date"], "balanced")
            supplier = rec["supplier_code"] if rec is not None else "SUP-001"
            groups.setdefault(supplier, []).append((r, qty))
        else:
            # 半成品需求：生成生产单（成品本身的需求由销售单触发的生产单覆盖，这里只对半成品）
            part = conn.execute("SELECT * FROM parts WHERE code=?", (r["part_code"],)).fetchone()
            if part and part["category"] == "半成品":
                mo_no = next_doc_no(conn, "MO")
                _comp = r["company_code"] if "company_code" in r.keys() else "COMP-001"
                _wh = r["warehouse"] if "warehouse" in r.keys() else "主仓"
                conn.execute("INSERT INTO production_orders (order_no,product_code,qty,status,sales_order_no,created_at,company_code,warehouse) VALUES (?,?,?,?,?,?,?,?)",
                             (mo_no, r["part_code"], qty, "新建", sales_no, now_str(), _comp, _wh))
                _allocate_for_production(conn, mo_no, r["part_code"], qty, _wh, _comp)
                production_created.append(mo_no)
    po_nos = []
    for supplier, items in groups.items():
        po_no = next_doc_no(conn, "PO")
        total = 0; subtotal = 0; tax_total = 0
        _comp = items[0][0]["company_code"] if "company_code" in items[0][0].keys() else "COMP-001"
        _wh = items[0][0]["warehouse"] if "warehouse" in items[0][0].keys() else "主仓"
        conn.execute("INSERT INTO purchase_orders (order_no,supplier_code,status,due_date,total_amount,created_at,company_code,warehouse,currency,exchange_rate,total_amount_base,subtotal_amount,tax_amount) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (po_no, supplier, "已下单", TODAY, 0, now_str(), _comp, _wh, "CNY", 1, 0, 0, 0))
        for r, qty in items:
            rec, _reason2, _eta2 = recommend_supplier_for(conn, r["part_code"], r["latest_order_date"], "balanced")
            price = float(rec["price"]) if rec is not None else 0
            tax = float(rec["tax_rate"]) if rec is not None else 0.13
            conn.execute("INSERT INTO purchase_order_items (order_no,part_code,qty,price,received_qty,tax_rate,warehouse,company_code) VALUES (?,?,?,?,?,?,?,?)",
                         (po_no, r["part_code"], qty, price, 0, tax, _wh, _comp))
            subtotal += qty * price; tax_total += qty * price * tax; total += qty * price * (1 + tax)
        conn.execute("UPDATE purchase_orders SET total_amount=?, total_amount_base=?, subtotal_amount=?, tax_amount=? WHERE order_no=?",
                     (round(total, 2), round(total, 2), round(subtotal, 2), round(tax_total, 2), po_no))
        po_nos.append(po_no)
    conn.commit(); conn.close()
    return {"ok": True, "sales_order_no": sales_no, "purchase_orders": po_nos, "production_orders": production_created}

# ---------- 采购申请（一级审批） ----------
@app.get("/api/purchase/requisitions")
def list_requisitions(company_code: Optional[str] = None, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "采购", "view")
    conn = get_conn()
    sql = "SELECT * FROM purchase_requisitions WHERE 1=1"; args: List[Any] = []
    if company_code:
        sql += " AND company_code=?"; args.append(company_code)
    sql += " ORDER BY id DESC"
    rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = row_to_dict(r)
        items = conn.execute("SELECT * FROM purchase_requisition_items WHERE req_no=?", (r["req_no"],)).fetchall()
        d["items"] = [row_to_dict(x) for x in items]
        d["approvals"] = [row_to_dict(x) for x in conn.execute("SELECT * FROM approval_records WHERE doc_type='采购申请' AND doc_no=? ORDER BY step_order", (r["req_no"],)).fetchall()]
        out.append(d)
    conn.close()
    return out

@app.post("/api/purchase/requisitions")
def create_requisition(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """新建采购申请（大白话：要买东西，先打个申请，批了才能下采购单）"""
    user = resolve_user(x_user)
    require_perm(user, "采购", "create")
    items = payload.get("items", [])
    if not items:
        raise HTTPException(400, "采购申请至少要有一条明细")
    company_code = payload.get("company_code", "COMP-001")
    warehouse = payload.get("warehouse", "主仓")
    currency = payload.get("currency", "CNY")
    check_scope(user, warehouse=warehouse, company_code=company_code)
    conn = get_conn()
    rate = resolve_exchange_rate(conn, currency, "CNY", payload.get("exchange_rate") or None) if currency != "CNY" else 1.0
    # P1-2：按零件档案税率算税，不再整张单写死 13%
    subtotal = 0.0; tax_total = 0.0
    for it in items:
        if not isinstance(it, dict) or not it.get("part_code"):
            raise HTTPException(422, "采购申请明细缺少 part_code（零件编码）")
        qty = num_field(it.get("qty"), "明细数量", required=True, positive=True)
        price = num_field(it.get("price", 0), "明细单价")
        p = conn.execute("SELECT tax_rate FROM parts WHERE code=?", (it["part_code"],)).fetchone()
        tax = float(p["tax_rate"]) if p and p["tax_rate"] is not None else 0.13
        it["_qty"] = qty; it["_price"] = price; it["_tax"] = tax
        subtotal += qty * price; tax_total += qty * price * tax
    total = subtotal + tax_total
    req_no = payload.get("req_no") or next_doc_no(conn, "PR")
    try:
        conn.execute("INSERT INTO purchase_requisitions (req_no,applicant,reason,status,created_at,company_code,warehouse,currency,exchange_rate,total_amount,total_amount_base) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (req_no, payload.get("applicant", ""), payload.get("reason", ""), "新建", now_str(), company_code, warehouse, currency, rate, round(total, 2), round(total * rate, 2)))
        for it in items:
            conn.execute("INSERT INTO purchase_requisition_items (req_no,part_code,qty,price,supplier_code) VALUES (?,?,?,?,?)",
                         (req_no, it["part_code"], it["_qty"], it["_price"], it.get("supplier_code", "")))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(400, f"采购申请号 {req_no} 已存在")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log_audit(user, "创建", "采购申请", req_no, "金额", "", str(round(total, 2)), f"{currency} 合计 {round(total,2)}", company_code)
    return {"ok": True, "req_no": req_no, "status": "新建", "total_amount": round(total, 2)}

def _requisition_transition(req_no: str, from_status: str, to_status: str):
    # 保留兼容：第一阶段的简单流转辅助，第二阶段审批走分级接口（submit/approve/reject 已重写）
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
def submit_requisition(req_no: str, payload: Dict[str, Any] = None, x_user: Optional[str] = Header(None)):
    """提交采购申请 → 按金额分级生成审批步骤（大白话：小钱自动过，大钱一级级往上报）。
    <=10000 自动批准；>10000 经理批；>100000 经理+总监依次批。"""
    user = resolve_user(x_user)
    require_perm(user, "采购", "edit")
    conn = get_conn()
    r = conn.execute("SELECT * FROM purchase_requisitions WHERE req_no=?", (req_no,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, f"采购申请 {req_no} 不存在")
    if r["status"] != "新建":
        conn.close()
        raise HTTPException(400, f"采购申请 {req_no} 状态是 {r['status']}，只有新建才能提交")
    amount_base = float(r["total_amount_base"] or r["total_amount"] or 0)
    n = create_approval_records(conn, "采购申请", req_no, amount_base, r["company_code"])
    new_status = "已批准" if n == 0 else "待审批"
    if n == 0:
        conn.execute("INSERT INTO approval_records (doc_type,doc_no,step_order,approver_role,approver_user,status,action_by,action_at,comment,amount,company_code,created_at) VALUES ('采购申请',?,0,'系统','系统','已批准','系统',?,'金额<=10000自动通过',?,?,?)",
                     (req_no, now_str(), amount_base, r["company_code"], now_str()))
    conn.execute("UPDATE purchase_requisitions SET status=? WHERE req_no=?", (new_status, req_no))
    conn.commit(); conn.close()
    log_audit(user, "提交审批", "采购申请", req_no, "状态", "新建", new_status, f"金额(本位币) {amount_base}，审批步骤 {n} 级", r["company_code"])
    return {"ok": True, "req_no": req_no, "status": new_status, "approval_steps": n}

@app.post("/api/purchase/requisitions/{req_no}/approve")
def approve_requisition(req_no: str, payload: Dict[str, Any] = None, x_user: Optional[str] = Header(None)):
    """分级审批：批准当前待审批步骤（大白话：轮到谁批，谁点同意；都批完才算批准）。"""
    user = resolve_user(x_user)
    # 审批权限由 advance_approval 按当前步骤的指定人/角色校验（轮到谁批谁才能批；自定义审批人也能批自己那一步）
    conn = get_conn()
    if not conn.execute("SELECT username FROM users WHERE username=?", (user,)).fetchone():
        conn.close()
        raise HTTPException(403, f"用户 {user} 不存在，无权审批")
    r = conn.execute("SELECT * FROM purchase_requisitions WHERE req_no=?", (req_no,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, f"采购申请 {req_no} 不存在")
    if r["status"] not in ("待审批", "已提交"):
        conn.close()
        raise HTTPException(400, f"采购申请 {req_no} 状态是 {r['status']}，没有待审批步骤")
    final, remain = advance_approval(conn, "采购申请", req_no, user, (payload or {}).get("comment", ""), approve=True)
    conn.execute("UPDATE purchase_requisitions SET status=? WHERE req_no=?", (final, req_no))
    conn.commit(); conn.close()
    return {"ok": True, "req_no": req_no, "status": final, "remaining_steps": remain}

@app.post("/api/purchase/requisitions/{req_no}/reject")
def reject_requisition(req_no: str, payload: Dict[str, Any] = None, x_user: Optional[str] = Header(None)):
    """分级审批：拒绝（大白话：这级不同意，整单拒绝，后面不用再批了）。"""
    user = resolve_user(x_user)
    # 审批权限由 advance_approval 按当前步骤的指定人/角色校验
    conn = get_conn()
    if not conn.execute("SELECT username FROM users WHERE username=?", (user,)).fetchone():
        conn.close()
        raise HTTPException(403, f"用户 {user} 不存在，无权审批")
    r = conn.execute("SELECT * FROM purchase_requisitions WHERE req_no=?", (req_no,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, f"采购申请 {req_no} 不存在")
    if r["status"] not in ("待审批", "已提交"):
        conn.close()
        raise HTTPException(400, f"采购申请 {req_no} 状态是 {r['status']}，没有待审批步骤")
    final, remain = advance_approval(conn, "采购申请", req_no, user, (payload or {}).get("comment", ""), approve=False)
    conn.execute("UPDATE purchase_requisitions SET status=? WHERE req_no=?", (final, req_no))
    conn.commit(); conn.close()
    return {"ok": True, "req_no": req_no, "status": final}

@app.post("/api/purchase/requisitions/{req_no}/convert")
def convert_requisition(req_no: str, x_user: Optional[str] = Header(None)):
    """申请转采购单：已批准的申请按供应商分组生成采购单（同供应商合并）。
    P0-4 补权限；P1-2 税率改为取零件档案税率，不再全写死 0.13。"""
    user = resolve_user(x_user)
    require_perm(user, "采购", "create")
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM purchase_requisitions WHERE req_no=?", (req_no,)).fetchone()
        if not r:
            raise HTTPException(404, f"采购申请 {req_no} 不存在")
        check_scope(user, warehouse=r["warehouse"], company_code=r["company_code"])
        if r["status"] != "已批准":
            raise HTTPException(400, f"采购申请 {req_no} 状态是 {r['status']}，只有已批准才能转采购单")
        items = conn.execute("SELECT * FROM purchase_requisition_items WHERE req_no=?", (req_no,)).fetchall()
        groups: Dict[str, List] = {}
        for it in items:
            sup = it["supplier_code"] or "SUP-001"
            groups.setdefault(sup, []).append(it)
        po_nos = []
        for sup, grp in groups.items():
            po_no = next_doc_no(conn, "PO")
            total = 0; subtotal = 0; tax_total = 0
            conn.execute("INSERT INTO purchase_orders (order_no,supplier_code,status,due_date,total_amount,created_at,company_code,warehouse,currency,exchange_rate,total_amount_base,subtotal_amount,tax_amount) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (po_no, sup, "已下单", TODAY, 0, now_str(), r["company_code"], r["warehouse"], r["currency"], r["exchange_rate"], 0, 0, 0))
            for it in grp:
                p = conn.execute("SELECT tax_rate FROM parts WHERE code=?", (it["part_code"],)).fetchone()
                tax = float(p["tax_rate"]) if p and p["tax_rate"] is not None else 0.13
                qty = float(it["qty"]); price = float(it["price"])
                conn.execute("INSERT INTO purchase_order_items (order_no,part_code,qty,price,received_qty,tax_rate,warehouse,company_code) VALUES (?,?,?,?,?,?,?,?)",
                             (po_no, it["part_code"], qty, price, 0, tax, r["warehouse"], r["company_code"]))
                subtotal += qty * price; tax_total += qty * price * tax
                total += qty * price * (1 + tax)
            conn.execute("UPDATE purchase_orders SET total_amount=?, total_amount_base=?, subtotal_amount=?, tax_amount=? WHERE order_no=?",
                         (round(total, 2), round(total * float(r["exchange_rate"] or 1), 2), round(subtotal, 2), round(tax_total, 2), po_no))
            po_nos.append(po_no)
        conn.execute("UPDATE purchase_requisitions SET status='已转采购单' WHERE req_no=?", (req_no,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "req_no": req_no, "purchase_orders": po_nos}

# ---------- 采购 ----------
@app.get("/api/purchase")
def list_purchase(company_code: Optional[str] = None, warehouse: Optional[str] = None, supplier_code: Optional[str] = None,
                  status: Optional[str] = None, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "采购", "view")
    check_scope(user, warehouse=warehouse, company_code=company_code)
    aw = allowed_warehouses(user); ac = allowed_companies(user)
    conn = get_conn()
    sql = "SELECT * FROM purchase_orders WHERE 1=1"; args: List[Any] = []
    if company_code:
        sql += " AND company_code=?"; args.append(company_code)
    elif ac is not None:
        sql += " AND company_code IN (%s)" % ",".join("?" * len(ac)); args.extend(sorted(ac))
    if warehouse:
        sql += " AND warehouse=?"; args.append(warehouse)
    elif aw is not None:
        sql += " AND warehouse IN (%s)" % ",".join("?" * len(aw)); args.extend(sorted(aw))
    if supplier_code:
        sql += " AND supplier_code=?"; args.append(supplier_code)
    if status:
        sql += " AND status=?"; args.append(status)
    sql += " ORDER BY id DESC"
    rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = row_to_dict(r)
        d["is_overdue"] = is_overdue(r["due_date"], r["status"])
        d["overdue_days"] = overdue_days(r["due_date"]) if d["is_overdue"] else 0
        items = conn.execute("SELECT * FROM purchase_order_items WHERE order_no=?", (r["order_no"],)).fetchall()
        d["items"] = [row_to_dict(x) for x in items]
        d["approvals"] = [row_to_dict(x) for x in conn.execute("SELECT * FROM approval_records WHERE doc_type='采购单' AND doc_no=? ORDER BY step_order", (r["order_no"],)).fetchall()]
        out.append(d)
    conn.close()
    return out

@app.post("/api/purchase")
def create_purchase(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """手动新建采购单（带明细）。含税/未税/税额分开算；外币单按汇率算本位币金额；单号用数据库序列。"""
    user = resolve_user(x_user)
    require_perm(user, "采购", "create")
    items = payload.get("items", [])
    if not items:
        raise HTTPException(400, "采购单至少要有一条明细")
    company_code = payload.get("company_code", "COMP-001")
    warehouse = payload.get("warehouse", "主仓")
    currency = payload.get("currency", "CNY")
    check_scope(user, warehouse=warehouse, company_code=company_code)
    conn = get_conn()
    rate = resolve_exchange_rate(conn, currency, "CNY", payload.get("exchange_rate") or None) if currency != "CNY" else 1.0
    order_no = payload.get("order_no") or next_doc_no(conn, "PO")
    try:
        subtotal = 0; tax_total = 0; total = 0
        conn.execute("INSERT INTO purchase_orders (order_no,supplier_code,status,due_date,total_amount,created_at,company_code,warehouse,currency,exchange_rate,total_amount_base,subtotal_amount,tax_amount) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (order_no, payload.get("supplier_code", "SUP-001"), "新建", payload.get("due_date", TODAY), 0, now_str(), company_code, warehouse, currency, rate, 0, 0, 0))
        for it in items:
            if not isinstance(it, dict) or not it.get("part_code"):
                raise HTTPException(422, "采购明细缺少 part_code（零件编码）")
            qty = num_field(it.get("qty"), "明细数量", required=True, positive=True)
            price = num_field(it.get("price", 0), "明细单价")
            tax = num_field(it.get("tax_rate", 0.13), "明细税率", default=0.13)
            conn.execute("INSERT INTO purchase_order_items (order_no,part_code,qty,price,received_qty,tax_rate,warehouse,company_code) VALUES (?,?,?,?,?,?,?,?)",
                         (order_no, it["part_code"], qty, price, 0, tax, warehouse, company_code))
            subtotal += qty * price; tax_total += qty * price * tax; total += qty * price * (1 + tax)
        conn.execute("UPDATE purchase_orders SET total_amount=?, total_amount_base=?, subtotal_amount=?, tax_amount=? WHERE order_no=?",
                     (round(total, 2), round(total * rate, 2), round(subtotal, 2), round(tax_total, 2), order_no))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(400, f"采购单号 {order_no} 已存在")
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log_audit(user, "创建", "采购单", order_no, "金额", "", str(round(total, 2)), f"{currency} 合计 {round(total,2)}，本位币 {round(total*rate,2)}", company_code)
    return {"ok": True, "order_no": order_no, "total_amount": round(total, 2), "total_amount_base": round(total * rate, 2)}

@app.post("/api/purchase/{order_no}/submit-approval")
def submit_po_approval(order_no: str, x_user: Optional[str] = Header(None)):
    """采购单提交审批：按金额分级（<=10000 自动批准；>10000 经理；>100000 经理+总监）。"""
    user = resolve_user(x_user)
    require_perm(user, "采购", "edit")
    conn = get_conn()
    po = conn.execute("SELECT * FROM purchase_orders WHERE order_no=?", (order_no,)).fetchone()
    if not po:
        conn.close()
        raise HTTPException(404, f"采购单 {order_no} 不存在")
    if po["status"] != "新建":
        conn.close()
        raise HTTPException(400, f"采购单 {order_no} 状态是 {po['status']}，只有新建才能提交审批")
    n = create_approval_records(conn, "采购单", order_no, float(po["total_amount_base"] or po["total_amount"] or 0), po["company_code"])
    new_status = "已批准" if n == 0 else "待审批"
    if n == 0:
        conn.execute("INSERT INTO approval_records (doc_type,doc_no,step_order,approver_role,approver_user,status,action_by,action_at,comment,amount,company_code,created_at) VALUES ('采购单',?,0,'系统','系统','已批准','系统',?,'金额<=10000自动通过',?,?,?)",
                     (order_no, now_str(), float(po["total_amount_base"] or 0), po["company_code"], now_str()))
    conn.execute("UPDATE purchase_orders SET status=? WHERE order_no=?", (new_status, order_no))
    conn.commit(); conn.close()
    log_audit(user, "提交审批", "采购单", order_no, "状态", "新建", new_status, f"审批步骤 {n} 级", po["company_code"])
    return {"ok": True, "order_no": order_no, "status": new_status, "approval_steps": n}

@app.post("/api/purchase/{order_no}/approve")
def approve_po(order_no: str, payload: Dict[str, Any] = None, x_user: Optional[str] = Header(None)):
    """采购单分级审批：批准当前步骤，全部通过后状态=已批准。"""
    user = resolve_user(x_user)
    # 审批权限由 advance_approval 按当前步骤的指定人/角色校验
    conn = get_conn()
    if not conn.execute("SELECT username FROM users WHERE username=?", (user,)).fetchone():
        conn.close()
        raise HTTPException(403, f"用户 {user} 不存在，无权审批")
    po = conn.execute("SELECT * FROM purchase_orders WHERE order_no=?", (order_no,)).fetchone()
    if not po:
        conn.close()
        raise HTTPException(404, f"采购单 {order_no} 不存在")
    if po["status"] != "待审批":
        conn.close()
        raise HTTPException(400, f"采购单 {order_no} 状态是 {po['status']}，没有待审批步骤")
    final, remain = advance_approval(conn, "采购单", order_no, user, (payload or {}).get("comment", ""), approve=True)
    conn.execute("UPDATE purchase_orders SET status=? WHERE order_no=?", (final, order_no))
    conn.commit(); conn.close()
    return {"ok": True, "order_no": order_no, "status": final, "remaining_steps": remain}

@app.post("/api/purchase/{order_no}/reject")
def reject_po(order_no: str, payload: Dict[str, Any] = None, x_user: Optional[str] = Header(None)):
    """采购单分级审批：拒绝，整单拒绝。"""
    user = resolve_user(x_user)
    # 审批权限由 advance_approval 按当前步骤的指定人/角色校验
    conn = get_conn()
    if not conn.execute("SELECT username FROM users WHERE username=?", (user,)).fetchone():
        conn.close()
        raise HTTPException(403, f"用户 {user} 不存在，无权审批")
    po = conn.execute("SELECT * FROM purchase_orders WHERE order_no=?", (order_no,)).fetchone()
    if not po:
        conn.close()
        raise HTTPException(404, f"采购单 {order_no} 不存在")
    if po["status"] != "待审批":
        conn.close()
        raise HTTPException(400, f"采购单 {order_no} 状态是 {po['status']}，没有待审批步骤")
    final, remain = advance_approval(conn, "采购单", order_no, user, (payload or {}).get("comment", ""), approve=False)
    conn.execute("UPDATE purchase_orders SET status=? WHERE order_no=?", (final, order_no))
    conn.commit(); conn.close()
    return {"ok": True, "order_no": order_no, "status": final}

@app.post("/api/purchase/from-mrp")
def purchase_from_mrp(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """
    从 MRP 一键生成采购单（兼容 MVP）
    逻辑：取 MRP 结果（调整后数量优先），按推荐供应商分组（第三阶段：均衡推荐，最便宜优先，黑名单跳过），
    同一供应商合并成一张采购单。指定 supplier_code 时，全部归到该供应商一张单。
    P0-4：补权限与公司/仓库范围校验。
    """
    user = resolve_user(x_user)
    require_perm(user, "采购", "create")
    sales_no = payload.get("sales_order_no", "")
    supplier_override = payload.get("supplier_code")
    conn = get_conn()
    try:
        mrp = conn.execute("SELECT * FROM mrp_results WHERE sales_order_no=? AND need_type='采购'", (sales_no,)).fetchall()
        if not mrp:
            raise HTTPException(400, "MRP 结果为空或全部不缺料，请先跑 MRP")
        for r in mrp:
            check_scope(user, warehouse=r["warehouse"] if "warehouse" in r.keys() else None,
                        company_code=r["company_code"] if "company_code" in r.keys() else None)
        groups: Dict[str, List] = {}
        for r in mrp:
            qty = _mrp_effective_qty(r)
            if qty <= 0:
                continue
            if supplier_override:
                sup = supplier_override
            else:
                rec, _rs, _et = recommend_supplier_for(conn, r["part_code"])
                sup = rec["supplier_code"] if rec is not None else "SUP-001"
            groups.setdefault(sup, []).append((r, qty))
        if not groups:
            raise HTTPException(400, "MRP 结果全部不缺料，无需生成采购单")
        po_nos = []
        first_total = 0
        for sup, items in groups.items():
            order_no = next_doc_no(conn, "PO")
            total = 0; subtotal = 0; tax_total = 0
            _comp = items[0][0]["company_code"] if "company_code" in items[0][0].keys() else "COMP-001"
            _wh = items[0][0]["warehouse"] if "warehouse" in items[0][0].keys() else "主仓"
            conn.execute("INSERT INTO purchase_orders (order_no,supplier_code,status,due_date,total_amount,created_at,company_code,warehouse,currency,exchange_rate,total_amount_base,subtotal_amount,tax_amount) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (order_no, sup, "已下单", TODAY, 0, now_str(), _comp, _wh, "CNY", 1, 0, 0, 0))
            for r, qty in items:
                part = conn.execute("SELECT * FROM parts WHERE code=?", (r["part_code"],)).fetchone()
                if supplier_override:
                    price = float(part["price"]) if part else 0
                    tax_rate = float(part["tax_rate"]) if part else 0.13
                else:
                    rec, _rs2, _et2 = recommend_supplier_for(conn, r["part_code"])
                    price = float(rec["price"]) if rec is not None else (float(part["price"]) if part else 0)
                    tax_rate = float(rec["tax_rate"]) if rec is not None else 0.13
                conn.execute("INSERT INTO purchase_order_items (order_no,part_code,qty,price,received_qty,tax_rate,warehouse,company_code) VALUES (?,?,?,?,?,?,?,?)",
                             (order_no, r["part_code"], qty, price, 0, tax_rate, _wh, _comp))
                subtotal += qty * price; tax_total += qty * price * tax_rate; total += qty * price * (1 + tax_rate)
            total_with_tax = round(total, 2)
            # 修复：subtotal/tax 拆开存（以前把含税总额又写进 subtotal，导致“未税金额=含税金额”）
            conn.execute("UPDATE purchase_orders SET total_amount=?, total_amount_base=?, subtotal_amount=?, tax_amount=? WHERE order_no=?",
                         (total_with_tax, total_with_tax, round(subtotal, 2), round(tax_total, 2), order_no))
            po_nos.append(order_no)
            first_total = total_with_tax
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    # 兼容 MVP：返回单个 order_no（单供应商时就是那张单）
    return {"ok": True, "order_no": po_nos[0], "purchase_orders": po_nos, "total_amount_with_tax": first_total}

@app.post("/api/purchase/merge")
def merge_purchase(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """采购单合并（大白话：同供应商的几张单合并成一张，省运费、好对账）。P0-4：补权限与范围校验"""
    user = resolve_user(x_user)
    require_perm(user, "采购", "edit")
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
            raise HTTPException(400, f"采购单 {no} 状态是 {po['status']}，只有新建/已下单才能合并")
        check_scope(user, warehouse=po["warehouse"], company_code=po["company_code"])
        pos.append(po)
    companies = {p["company_code"] for p in pos}
    if len(companies) > 1:
        conn.close()
        raise HTTPException(400, f"待合并采购单属于不同公司 {companies}，公司隔离，不能合并")
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
    new_no = next_doc_no(conn, "PO")
    total = sum(v["qty"] * v["price"] * (1 + v["tax_rate"]) for v in merged.values())
    _comp = pos[0]["company_code"]; _wh = pos[0]["warehouse"]; _cur = pos[0]["currency"]; _rate = float(pos[0]["exchange_rate"] or 1)
    conn.execute("INSERT INTO purchase_orders (order_no,supplier_code,status,due_date,total_amount,created_at,company_code,warehouse,currency,exchange_rate,total_amount_base) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (new_no, supplier, "已下单", TODAY, round(total, 2), now_str(), _comp, _wh, _cur, _rate, round(total * _rate, 2)))
    for v in merged.values():
        conn.execute("INSERT INTO purchase_order_items (order_no,part_code,qty,price,received_qty,tax_rate,warehouse,company_code) VALUES (?,?,?,?,?,?,?,?)",
                     (new_no, v["part_code"], v["qty"], v["price"], 0, v["tax_rate"], _wh, _comp))
    for po in pos:
        conn.execute("UPDATE purchase_orders SET status='已合并' WHERE order_no=?", (po["order_no"],))
    conn.commit(); conn.close()
    log_audit(user, "合并", "采购单", new_no, "来源单", ",".join(order_nos), new_no, f"合并后含税 {round(total,2)}")
    return {"ok": True, "order_no": new_no, "merged_from": order_nos, "total_amount_with_tax": round(total, 2)}

@app.post("/api/purchase/receive")
def purchase_receive(r: ReceiveIn, x_user: Optional[str] = Header(None)):
    """采购到货入库：合格 → 库存增加；不合格（质检可选）→ 按批次进冻结，不进可用库存。
    未批准（待审批/已拒绝）的采购单不能到货；已合并/已取消的也不能到货。"""
    user = resolve_user(x_user)
    require_perm(user, "采购", "edit")
    conn = get_conn()
    po = conn.execute("SELECT * FROM purchase_orders WHERE order_no=?", (r.order_no,)).fetchone()
    if not po:
        conn.close()
        raise HTTPException(404, f"采购单 {r.order_no} 不存在")
    if po["status"] in ("待审批", "已拒绝", "已合并", "已取消"):
        conn.close()
        raise HTTPException(400, f"采购单 {r.order_no} 状态是 {po['status']}，不能到货（请先审批通过）")
    check_scope(user, warehouse=po["warehouse"], company_code=po["company_code"])
    item = conn.execute("SELECT * FROM purchase_order_items WHERE order_no=? AND part_code=?", (r.order_no, r.part_code)).fetchone()
    if not item:
        conn.close()
        raise HTTPException(404, f"采购单 {r.order_no} 里没有零件 {r.part_code}")
    if float(item["received_qty"]) + float(r.qty) > float(item["qty"]) + 1e-9:
        conn.close()
        raise HTTPException(400, "到货数量超过采购数量")
    wh = r.warehouse or po["warehouse"]
    # P1-5：到货可手填库位/批次，不填就用该仓库默认库位+当天日期批次（不再写死 A区-01 / BATCH-20261019）
    location = r.location or warehouse_default_location(conn, wh)
    batch = r.batch or today_batch()
    try:
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
        if r.qc_result == "不合格":
            # 质检不合格：不进可用库存，按“这次到货的批次/库位”整批冻结（P1-5：批次不再写死）
            conn.execute("INSERT INTO frozen_stock (part_code,qty,reason,ref_no,created_at,warehouse,location,batch,company_code) VALUES (?,?,?,?,?,?,?,?,?)",
                         (r.part_code, float(r.qty), "质检不合格冻结", r.order_no, now_str(), wh, location, batch, po["company_code"]))
            add_ledger(r.part_code, 0, "质检不合格", r.order_no, f"到货 {r.qty} 质检不合格，已冻结（{wh}/{batch}）", wh, batch, po["company_code"], conn=conn, location=location, operator=user)
        else:
            # P2-5：入库流水记下采购单价，给入库表导出的“单价/金额”用
            adjust_stock(r.part_code, float(r.qty), "采购入库", r.order_no, wh, location, batch, po["company_code"], conn=conn, operator=user, unit_price=float(item["price"] or 0))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if r.qc_result == "不合格":
        log_audit(user, "质检不合格冻结", "采购单", r.order_no, "冻结量", "", str(r.qty), f"{r.part_code} {wh}/{batch}", po["company_code"])
        return {"ok": True, "order_no": r.order_no, "part_code": r.part_code, "received": r.qty, "status": new_status, "qc": "不合格，已冻结"}
    log_audit(user, "到货入库", "采购单", r.order_no, "到货量", "", str(r.qty), f"{r.part_code} {wh}/{batch}", po["company_code"])
    return {"ok": True, "order_no": r.order_no, "part_code": r.part_code, "received": r.qty, "status": new_status, "qc": "合格"}

@app.put("/api/purchase/{order_no}/reconcile")
def reconcile_purchase(order_no: str, payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """对账字段（大白话：供应商拿发票来对账，记下发票号和发票金额，勾上已对账）。
    第二阶段：同时做简易三方核对（采购单金额 vs 发票金额），差额记入审计日志；完整三方核对走 /api/finance/invoices。"""
    user = resolve_user(x_user)
    require_perm(user, "财务", "edit")
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
    diff = round(invoice_amount - float(po["total_amount"] or 0), 2)
    log_audit(user, "对账", "采购单", order_no, "发票金额", str(po["invoice_amount"] or 0), str(invoice_amount),
              f"发票 {invoice_no}，采购单含税 {po['total_amount']}，差额 {diff}，已对账={reconciled}", po["company_code"])
    return {"ok": True, "order_no": order_no, "invoice_no": invoice_no, "invoice_amount": invoice_amount,
            "reconciled": reconciled, "diff_vs_po": diff}

# ---------- 生产 ----------
@app.get("/api/production")
def list_production(company_code: Optional[str] = None, warehouse: Optional[str] = None, status: Optional[str] = None,
                    x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "生产", "view")
    conn = get_conn()
    sql = "SELECT * FROM production_orders WHERE 1=1"; args: List[Any] = []
    if company_code:
        sql += " AND company_code=?"; args.append(company_code)
    if warehouse:
        sql += " AND warehouse=?"; args.append(warehouse)
    if status:
        sql += " AND status=?"; args.append(status)
    sql += " ORDER BY id DESC"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.get("/api/production/{order_no}/consumptions")
def list_consumptions(order_no: str, x_user: Optional[str] = Header(None)):
    """某生产单的领料成本明细（大白话：这张单领了哪些料、每种花了多少钱）。含 P2-6 的部门/用途。"""
    user = resolve_user(x_user)
    require_perm(user, "生产", "view")
    conn = get_conn()
    rows = conn.execute("SELECT * FROM production_consumptions WHERE order_no=? ORDER BY id", (order_no,)).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

def kitting_for_order(conn, order_no: str) -> Dict[str, Any]:
    """算一张生产单的齐套（大白话：这张单要的料，现在手头到齐了几成，P2-6 4c）。
    口径（和领料/生产缺料同一口径）：
    - 按 BOM 直接子件逐项算：需求量 need = 单件用量 × 生产数量；
    - 本单可用 = 全局可用量（现有+在途-已分配-冻结）+ 本单自己的占用（占用本来就是给本单留的）；
    - 在制不算可用（P2-6 4b）：正在生产、还没完工入库的量不在库存里，自然不进可用量，本函数也不另外加；
    - 到齐率（按数量）= Σ min(本单可用, 需求) ÷ Σ 需求 × 100%；另给“到齐项数/总项数”供看板参考。
    """
    mo = conn.execute("SELECT * FROM production_orders WHERE order_no=?", (order_no,)).fetchone()
    if not mo:
        raise HTTPException(404, f"生产单 {order_no} 不存在")
    warehouse = mo["warehouse"]; company_code = mo["company_code"]
    items = []
    total_need = 0.0
    total_ready = 0.0
    ready_items = 0
    for ch in direct_bom_children(conn, mo["product_code"], float(mo["qty"])):
        need = float(ch["qty"])
        avail = get_available(ch["part_code"], warehouse, company_code)
        own = float(conn.execute("SELECT COALESCE(SUM(qty),0) FROM allocations WHERE ref_no=? AND part_code=?",
                                 (order_no, ch["part_code"])).fetchone()[0] or 0)
        can = avail + own
        shortage = max(0.0, need - can)
        is_ready = can + 1e-9 >= need
        if is_ready:
            ready_items += 1
        total_need += need
        total_ready += min(can, need)
        part = conn.execute("SELECT name,unit FROM parts WHERE code=?", (ch["part_code"],)).fetchone()
        items.append({"part_code": ch["part_code"], "part_name": part["name"] if part else "",
                      "unit": part["unit"] if part else "", "need": need,
                      "available": round(can, 4), "shortage": round(shortage, 4),
                      "is_ready": is_ready, "is_subassembly": bool(ch["is_subassembly"])})
    rate = round(100.0 * total_ready / total_need, 2) if total_need > 1e-9 else 100.0
    return {"order_no": order_no, "product_code": mo["product_code"], "qty": float(mo["qty"]),
            "status": mo["status"], "warehouse": warehouse, "company_code": company_code,
            "kitting_rate": rate, "ready_items": ready_items, "total_items": len(items),
            "items": items}

@app.get("/api/production/{order_no}/kitting")
def production_kitting(order_no: str, x_user: Optional[str] = Header(None)):
    """齐套查询（大白话：这张生产单要的料，现在到齐了几成、还缺哪几种）。P2-6 4c。"""
    user = resolve_user(x_user)
    require_perm(user, "生产", "view")
    conn = get_conn()
    mo = conn.execute("SELECT * FROM production_orders WHERE order_no=?", (order_no,)).fetchone()
    if not mo:
        conn.close()
        raise HTTPException(404, f"生产单 {order_no} 不存在")
    check_scope(user, warehouse=mo["warehouse"], company_code=mo["company_code"])
    d = kitting_for_order(conn, order_no)
    conn.close()
    return d

def _allocate_for_production(conn, order_no: str, product_code: str, qty: float, warehouse: str = "主仓", company_code: str = "COMP-001"):
    """生产单创建时占用库存（大白话：先把料占上，别人就不能重复算这批料）。
    第二阶段：只占用直接子件（逐级领料），半成品只占半成品本身，不占其底层原材料。"""
    for it in direct_bom_children(conn, product_code, qty):
        conn.execute("INSERT INTO allocations (ref_no,part_code,qty,created_at,warehouse,company_code) VALUES (?,?,?,?,?,?)",
                     (order_no, it["part_code"], it["qty"], now_str(), warehouse, company_code))

@app.post("/api/production")
def create_production(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """创建生产单（带计划开始/结束，自动占用直接子件库存）"""
    user = resolve_user(x_user)
    require_perm(user, "生产", "create")
    product = payload.get("product_code", "FG-001")
    qty = num_field(payload.get("qty", 100), "生产数量", required=True, positive=True)
    sales_no = payload.get("sales_order_no", "")
    planned_start = payload.get("planned_start", "")
    planned_end = payload.get("planned_end", "")
    company_code = payload.get("company_code", "COMP-001")
    warehouse = payload.get("warehouse", "主仓")
    check_scope(user, warehouse=warehouse, company_code=company_code)
    if planned_start and planned_end and planned_end < planned_start:
        raise HTTPException(400, "计划结束不能早于计划开始")
    conn = get_conn()
    order_no = payload.get("order_no") or ""
    try:
        order_no = payload.get("order_no") or next_doc_no(conn, "MO")
        conn.execute("INSERT INTO production_orders (order_no,product_code,qty,status,sales_order_no,created_at,planned_start,planned_end,company_code,warehouse,parent_order_no) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (order_no, product, qty, "新建", sales_no, now_str(), planned_start, planned_end, company_code, warehouse, payload.get("parent_order_no", "")))
        _allocate_for_production(conn, order_no, product, qty, warehouse, company_code)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(400, f"生产单号 {order_no} 已存在")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log_audit(user, "创建", "生产单", order_no, "数量", "", str(qty), f"{product} x {qty}@{warehouse}", company_code)
    return {"ok": True, "order_no": order_no}

@app.post("/api/production/{order_no}/plan")
def plan_production(order_no: str, payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    _u = resolve_user(x_user); require_perm(_u, "生产", "edit")
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
    log_audit(_u, "修改计划", "生产单", order_no, "计划日期", f"{mo['planned_start']}~{mo['planned_end']}", f"{ps}~{pe}", "")
    return {"ok": True, "order_no": order_no, "planned_start": ps, "planned_end": pe}

@app.post("/api/production/consume")
def production_consume(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """生产领料（第二阶段：逐级领料）。
    思路来源：ERPNext 多层 BOM（Work Order 自动带出子装配，子装配要先生产，见 https://erpnext.com/manufacturing/work-orders）
    + InvenTree Build（Build 消耗库存里的零件，子件不足时要先补生产，见 https://inventree.org/functions）。
    规则：
    1) 只扣直接子件，不直接扣底层原材料。
    2) 直接子件是半成品时：先扣半成品库存；半成品不够时不扣其底层原材料，本次领料失败并返回缺口，
       提示先用 /api/production/{单号}/ensure-subassemblies 生成半成品生产单、先生半成品。
    3) 扣库存按批次 FIFO（先进先出），跳过被冻结的批次量；全部检查通过后一次性扣（原子操作）。
    4) 领用部门/用途（P2-6 4a，用户选填）：payload 里 dept（领用部门）、purpose（用途），选填，
       会随领料明细一起存进 production_consumptions，方便对账时查“哪部门领的、干嘛用的”。
    """
    user = resolve_user(x_user)
    require_perm(user, "生产", "edit")
    order_no = payload.get("order_no", "")
    dept = str(payload.get("dept") or "").strip()
    purpose = str(payload.get("purpose") or "").strip()
    conn = get_conn()
    mo = conn.execute("SELECT * FROM production_orders WHERE order_no=?", (order_no,)).fetchone()
    if not mo:
        conn.close()
        raise HTTPException(404, f"生产单 {order_no} 不存在")
    if mo["status"] != "新建":
        conn.close()
        raise HTTPException(400, f"生产单 {order_no} 状态是 {mo['status']}，已领过料，不能重复领")
    check_scope(user, warehouse=mo["warehouse"], company_code=mo["company_code"])
    children = direct_bom_children(conn, mo["product_code"], float(mo["qty"]))
    warehouse = mo["warehouse"]; company_code = mo["company_code"]
    # 先检查：半成品缺口 + 原材料缺口（都不动库存）
    missing_sub = []
    missing_raw = []
    for ch in children:
        part_code = ch["part_code"]; need = float(ch["qty"])
        avail = get_available(part_code, warehouse, company_code)
        own = float(conn.execute("SELECT COALESCE(SUM(qty),0) FROM allocations WHERE ref_no=? AND part_code=?", (order_no, part_code)).fetchone()[0] or 0)
        can_use = avail + own
        if can_use + 1e-9 < need:
            entry = {"part_code": part_code, "need": need, "available_for_this_order": can_use, "shortage": round(need - can_use, 4)}
            if ch["is_subassembly"]:
                missing_sub.append(entry)
            else:
                missing_raw.append(entry)
    if missing_sub or missing_raw:
        conn.close()
        raise HTTPException(400, f"生产单 {order_no} 领料失败：半成品缺口 {missing_sub}，原材料缺口 {missing_raw}。"
                              f"半成品不足时请先生产半成品（POST /api/production/{order_no}/ensure-subassemblies 生成半成品生产单），"
                              f"不要直接扣底层原材料。")
    # 检查全部通过：按批次 FIFO 扣直接子件，跳过冻结批次；记领料成本与流水
    consumed = []
    for ch in children:
        part_code = ch["part_code"]; need = float(ch["qty"]); remain = need
        part = conn.execute("SELECT * FROM parts WHERE code=?", (part_code,)).fetchone()
        unit_cost = float(part["price"]) if part else 0
        rows = conn.execute("SELECT * FROM stock WHERE part_code=? AND warehouse=? AND company_code=? AND qty>0 ORDER BY batch ASC",
                            (part_code, warehouse, company_code)).fetchall()
        for row in rows:
            if remain <= 1e-9:
                break
            batch_avail = float(row["qty"]) - get_batch_frozen(conn, part_code, warehouse, row["batch"], company_code)
            take = min(batch_avail, remain)
            if take <= 1e-9:
                continue
            conn.execute("UPDATE stock SET qty=qty-? WHERE id=?", (take, row["id"]))
            # P2-5：领料流水记下真实库位/经办人/单位成本；P2-6：领料明细记下部门/用途
            conn.execute("INSERT INTO stock_ledger (part_code,warehouse,location,batch,change_qty,type,ref_no,operator,unit_price,remark,created_at,company_code) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (part_code, warehouse, row["location"], row["batch"], -take, "生产领料", order_no, user, unit_cost, f"生产领料 {take}（逐级）", now_str(), company_code))
            conn.execute("INSERT INTO production_consumptions (order_no,part_code,qty,unit_cost,total_cost,batch,warehouse,company_code,dept,purpose,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (order_no, part_code, take, unit_cost, round(take * unit_cost, 2), row["batch"], warehouse, company_code, dept, purpose, now_str()))
            consumed.append({"part_code": part_code, "qty": take, "batch": row["batch"], "unit_cost": unit_cost})
            remain -= take
        if remain > 1e-9:
            conn.rollback(); conn.close()
            raise HTTPException(400, f"生产单 {order_no} 领料失败：零件 {part_code} 按批次扣完仍缺 {remain}（可能有批次被冻结），请检查批次冻结")
    conn.execute("UPDATE production_orders SET status='已领料' WHERE order_no=?", (order_no,))
    conn.execute("DELETE FROM allocations WHERE ref_no=?", (order_no,))
    conn.commit(); conn.close()
    log_audit(user, "领料", "生产单", order_no, "状态", "新建", "已领料", f"逐级领料 {len(consumed)} 条{('，部门：'+dept) if dept else ''}{('，用途：'+purpose) if purpose else ''}", company_code)
    return {"ok": True, "order_no": order_no, "status": "已领料", "dept": dept, "purpose": purpose, "consumed": consumed}

@app.post("/api/production/{order_no}/ensure-subassemblies")
def ensure_subassemblies(order_no: str, x_user: Optional[str] = Header(None)):
    """为缺半成品的生产单生成半成品生产单（大白话：成品缺半成品时，一键生成造半成品的单子，先造半成品再造成品）。
    只对“半成品库存不够”的直接子件生成，数量 = 缺口数；已生成的（同父单+同半成品+未领料）不重复生成。"""
    user = resolve_user(x_user)
    require_perm(user, "生产", "create")
    conn = get_conn()
    mo = conn.execute("SELECT * FROM production_orders WHERE order_no=?", (order_no,)).fetchone()
    if not mo:
        conn.close()
        raise HTTPException(404, f"生产单 {order_no} 不存在")
    if mo["status"] != "新建":
        conn.close()
        raise HTTPException(400, f"生产单 {order_no} 状态是 {mo['status']}，只有新建才能补半成品生产单")
    created = []
    for ch in direct_bom_children(conn, mo["product_code"], float(mo["qty"])):
        if not ch["is_subassembly"]:
            continue
        need = float(ch["qty"])
        avail = get_available(ch["part_code"], mo["warehouse"], mo["company_code"])
        own = float(conn.execute("SELECT COALESCE(SUM(qty),0) FROM allocations WHERE ref_no=? AND part_code=?", (order_no, ch["part_code"])).fetchone()[0] or 0)
        shortage = need - (avail + own)
        if shortage <= 1e-9:
            continue
        dup = conn.execute("SELECT order_no FROM production_orders WHERE parent_order_no=? AND product_code=? AND status='新建'", (order_no, ch["part_code"])).fetchone()
        if dup:
            created.append(dup["order_no"])
            continue
        sub_no = next_doc_no(conn, "MO")
        conn.execute("INSERT INTO production_orders (order_no,product_code,qty,status,sales_order_no,created_at,company_code,warehouse,parent_order_no) VALUES (?,?,?,?,?,?,?,?,?)",
                     (sub_no, ch["part_code"], round(shortage, 4), "新建", mo["sales_order_no"], now_str(), mo["company_code"], mo["warehouse"], order_no))
        _allocate_for_production(conn, sub_no, ch["part_code"], round(shortage, 4), mo["warehouse"], mo["company_code"])
        created.append(sub_no)
    conn.commit(); conn.close()
    log_audit(user, "生成半成品生产单", "生产单", order_no, "半成品生产单", "", ",".join(created), "")
    return {"ok": True, "parent_order_no": order_no, "sub_production_orders": created}

@app.post("/api/production/complete")
def production_complete(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """生产完工入库（大白话：成品做好了，放进仓库，成品库存增加）"""
    user = resolve_user(x_user)
    require_perm(user, "生产", "edit")
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
    # P1-6：完工状态 + 成品入库 + 库存流水必须在同一个事务里，不能“状态已完工但入库失败”
    # P1-5：成品入库不填库位/批次时用该仓库默认库位+当天日期批次
    try:
        adjust_stock(mo["product_code"], float(mo["qty"]), "生产入库", order_no, mo["warehouse"],
                     None, None, mo["company_code"], conn=conn, operator=user)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log_audit(user, "完工入库", "生产单", order_no, "状态", "已领料", "已完工", f"{mo['product_code']} x {mo['qty']}@{mo['warehouse']}", mo["company_code"])
    return {"ok": True, "order_no": order_no, "status": "已完工", "finished_qty": float(mo["qty"])}

# ---------- 看板（四块 + 红黄绿） ----------
@app.get("/api/dashboard")
def dashboard(x_user: Optional[str] = Header(None)):
    """经营看板。P0-4：补权限（报表 view）+ 按公司/仓库范围过滤，非管理员看不到别家公司数据。"""
    user = resolve_user(x_user)
    require_perm(user, "报表", "view")
    ac = allowed_companies(user); aw = allowed_warehouses(user)
    conn = get_conn()
    # 1) 缺料预警：按可用量（不是现有量）判断；只看权限范围内的零件
    parts = conn.execute("SELECT * FROM parts ORDER BY code").fetchall()
    conn.close()
    if ac:
        parts = [p for p in parts if (p["company_code"] if "company_code" in p.keys() else "COMP-001") in ac]
    shortage_items = []
    for p in parts:
        code = p["code"]
        avail = available_in_scope(code, user)
        st = stock_status(avail, float(p["safety_stock"] or 0), float(p["max_stock"] or 0))
        if st in ("红", "黄"):
            reason = "已断货" if avail <= 0 else ("低于安全库存" if avail < float(p["safety_stock"] or 0) else "高于最高库存")
            shortage_items.append({"part_code": code, "part_name": p["name"], "available": avail,
                                   "safety_stock": p["safety_stock"], "max_stock": p["max_stock"],
                                   "status": st, "reason": reason})
    shortage_status = "红" if any(x["status"] == "红" for x in shortage_items) else ("黄" if shortage_items else "绿")
    # 2) 拖期采购（按范围过滤）
    po_sql = "SELECT * FROM purchase_orders WHERE status IN ('新建','已下单','部分到货')"
    po_args: List[Any] = []
    if ac:
        po_sql += " AND company_code IN (%s)" % ",".join("?" * len(ac)); po_args.extend(ac)
    if aw:
        po_sql += " AND warehouse IN (%s)" % ",".join("?" * len(aw)); po_args.extend(aw)
    conn = get_conn()
    pos = conn.execute(po_sql, po_args).fetchall()
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
    if ac:
        mos = [m for m in mos if (m["company_code"] if "company_code" in m.keys() else "COMP-001") in ac]
    prod_shortage = []
    for mo in mos:
        conn2 = get_conn()
        leaves = [{"part_code": c["part_code"], "qty": c["qty"]} for c in direct_bom_children(conn2, mo["product_code"], float(mo["qty"]))]
        conn2.close()
        missing = []
        for it in leaves:
            avail = available_in_scope(it["part_code"], user)
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
    # 6) P2-6 4c 齐套看板：新建生产单按单算到齐百分比（大白话：每张单的料到齐了几成），最低的排前面
    kitting_items = []
    for mo in mos:
        try:
            _conn_k = get_conn()
            _kd = kitting_for_order(_conn_k, mo["order_no"])
            _conn_k.close()
            _rate = _kd["kitting_rate"]
            kitting_items.append({"order_no": mo["order_no"], "product_code": mo["product_code"], "qty": mo["qty"],
                                  "planned_start": mo["planned_start"], "planned_end": mo["planned_end"],
                                  "kitting_rate": _rate, "ready_items": _kd["ready_items"], "total_items": _kd["total_items"],
                                  "status": "绿" if _rate >= 100 - 1e-9 else "红"})
        except Exception:
            pass
    kitting_items.sort(key=lambda x: x["kitting_rate"])
    kit_status = "红" if any(x["kitting_rate"] < 100 - 1e-9 for x in kitting_items) else "绿"
    # 5) 第三阶段：智能补货建议（再订货点公式，见 /api/replenishment 注释；失败时不拖垮看板）
    try:
        conn_r = get_conn()
        repl_items = []
        repl_parts = conn_r.execute("SELECT * FROM parts ORDER BY code").fetchall()
        if ac:
            repl_parts = [p for p in repl_parts if (p["company_code"] if "company_code" in p.keys() else "COMP-001") in ac]
        for p in repl_parts:
            it = replenishment_for_part(conn_r, p, 30)
            if it["need_reorder"]:
                repl_items.append(it)
        conn_r.close()
        repl_items.sort(key=lambda x: -x["suggested_qty"])
    except Exception:
        repl_items = []
    repl_status = "红" if any(x["available"] <= 0 for x in repl_items) else ("黄" if repl_items else "绿")
    return {
        "replenishment": {"title": "智能补货建议", "status": repl_status, "count": len(repl_items), "items": repl_items[:10],
                          "formula": "再订货点 = 日均用量×提前期 + 安全库存；建议补货 = max(0, 安全库存×2 + 日均用量×提前期 − 可用量)"},
        "shortage": {"title": "缺料预警", "status": shortage_status, "count": len(shortage_items), "items": shortage_items},
        "overdue_purchase": {"title": "拖期采购", "status": overdue_status, "count": len(overdue_items), "items": overdue_items},
        "supplier_ranking": {"title": "供应商拖期排行", "status": ranking_status, "count": len(ranking), "items": ranking},
        "production_shortage": {"title": "生产缺料", "status": prod_status, "count": len(prod_shortage), "items": prod_shortage},
        "kitting": {"title": "齐套看板", "status": kit_status, "count": len(kitting_items), "items": kitting_items[:10],
                    "formula": "到齐率(按数量)=Σmin(本单可用,需求)÷Σ需求×100%；在制不计入可用量"},
        # 兼容 MVP 的旧字段
        "low_stock": shortage_items,
        "pending_purchase": [row_to_dict(r) if hasattr(r, "keys") else r for r in pos],
        "stock_count": len(parts),
    }

# ---------- Excel 导入导出 ----------
COLUMN_ALIASES = {
    "code": ["编码", "零件编码", "物料编码", "材料编码", "料号", "code", "part_code", "零件编号", "物料编号", "零件号", "物料号"],
    "name": ["名称", "零件名称", "物料名称", "材料名称", "品名", "name", "物料说明", "零件名"],
    "spec": ["规格", "规格型号", "spec"],
    "unit": ["单位", "unit"],
    "category": ["类别", "分类", "category"],
    "safety_stock": ["安全库存", "安全库存量", "安全存量", "safety_stock"],
    "min_stock": ["最低库存", "min_stock"],
    "max_stock": ["最高库存", "max_stock"],
    "lead_time_days": ["提前期", "提前期天数", "lead_time_days", "交期天数", "采购提前期", "生产提前期"],
    "price": ["单价", "未税单价", "价格", "price"],
    "tax_rate": ["税率", "tax_rate"],
    "qty": ["数量", "用量", "库存数量", "库存", "qty", "期末数量", "结存数量",
            "需求数量", "需求数", "需求量", "建议数量", "补货数量", "到货数量"],
    "warehouse": ["仓库", "warehouse", "仓库名称", "库房"],
    "location": ["库位", "货位", "location", "库位号"],
    "batch": ["批次", "批次号", "batch", "批号"],
    "supplier_code": ["供应商编码", "供应商", "supplier_code"],
    "is_purchase": ["是否采购件", "采购件", "is_purchase"],
    "supplier_name": ["供应商名称", "供应商名"],
    "order_no": ["单号", "采购单号", "采购单", "order_no", "单据号", "销售单号", "生产单号",
                 "订单号", "订单编号", "采购订单号", "销售订单号", "生产订单号"],
    "part_code": ["零件编码", "物料编码", "材料编码", "料号", "part_code", "零件号", "物料号"],
    "parent_code": ["父件编码", "父件", "成品编码", "成品", "parent_code"],
    "child_code": ["子件编码", "子件", "child_code"],
    "version": ["版本号", "版本", "version"],
    "effective_date": ["生效日期", "生效时间", "effective_date"],
    "loss_rate": ["损耗率", "损耗", "loss_rate"],
    "customer": ["客户", "客户名称", "customer"],
    "product_code": ["成品编码", "产品编码", "product_code", "产品", "产品名称", "产品型号"],
    "due_date": ["交期", "需求日期", "到期日期", "due_date", "交货日期", "要求交期"],
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
    "in_transit": ["在途", "in_transit", "在制", "在制品", "在途数量"],
    "safety": ["安全库存", "safety"],
    "net": ["净需求", "net"],
    "adjusted_net": ["调整后数量", "调整数量", "adjusted_net"],
    "latest_order_date": ["最晚下单日", "latest_order_date"],
    "company_code": ["公司编码", "公司", "company_code"],
    "currency": ["币种", "货币", "currency"],
    "exchange_rate": ["汇率", "exchange_rate"],
    "applicant": ["申请人", "applicant", "跟进人", "采购员", "经办人"],
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

def excel_to_rows(file_bytes: bytes, sheet: Optional[str] = None):
    """读 Excel：默认读当前活动工作表；传 sheet 名可指定读哪一页（大白话：一个文件里多页时，点名读哪页）。"""
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise HTTPException(500, "openpyxl 未安装，请 pip install openpyxl")
    wb = load_workbook(filename=io.BytesIO(file_bytes), data_only=True)
    if sheet:
        if sheet not in wb.sheetnames:
            raise HTTPException(400, f"工作表 {sheet} 不存在，可选：{wb.sheetnames}")
        ws = wb[sheet]
    else:
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
    "parts": (["零件编码", "名称", "规格", "单位", "类别", "是否采购件", "安全库存", "最低库存", "最高库存", "提前期天数", "未税单价", "税率", "供应商编码", "公司编码"],
              [["RM-101", "示例零件", "示例规格", "个", "原材料", 1, 20, 10, 200, 7, 2.5, 0.13, "SUP-001", "COMP-001"]]),
    "stock": (["零件编码", "公司编码", "仓库", "库位", "批次号", "数量"], [["RM-001", "COMP-001", "主仓", "A区-01", "BATCH-001", 100]]),
    "purchase": (["采购单号", "公司编码", "仓库", "币种", "供应商编码", "零件编码", "数量", "未税单价", "税率"], [["PO-001", "COMP-001", "主仓", "CNY", "SUP-001", "RM-001", 100, 2.5, 0.13]]),
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
def excel_export(kind: str, sales_order_no: Optional[str] = None, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "报表", "view")
    conn = get_conn()
    if kind == "parts":
        headers = ["零件编码", "名称", "规格", "单位", "类别", "安全库存", "最低库存", "最高库存", "提前期天数", "未税单价", "税率", "供应商编码"]
        rows = conn.execute("SELECT code,name,spec,unit,category,safety_stock,min_stock,max_stock,lead_time_days,price,tax_rate,supplier_code FROM parts ORDER BY code").fetchall()
        data = [list(r) for r in rows]
    elif kind == "stock":
        headers = ["零件编码", "名称", "公司编码", "仓库", "库位", "批次号", "数量", "单位", "安全库存", "最高库存"]
        rows = conn.execute("SELECT s.part_code,p.name,s.company_code,s.warehouse,s.location,s.batch,s.qty,p.unit,p.safety_stock,p.max_stock FROM stock s LEFT JOIN parts p ON p.code=s.part_code ORDER BY s.part_code").fetchall()
        data = [list(r) for r in rows]
    elif kind == "purchase":
        headers = ["采购单号", "公司编码", "仓库", "币种", "汇率", "本位币总额", "供应商编码", "状态", "交期", "含税总额", "发票号", "发票金额", "是否已对账", "零件编码", "数量", "未税单价", "已到货数量"]
        rows = conn.execute("""
            SELECT po.order_no,po.company_code,po.warehouse,po.currency,po.exchange_rate,po.total_amount_base,po.supplier_code,po.status,po.due_date,po.total_amount,po.invoice_no,po.invoice_amount,po.reconciled,poi.part_code,poi.qty,poi.price,poi.received_qty
            FROM purchase_orders po LEFT JOIN purchase_order_items poi ON poi.order_no=po.order_no ORDER BY po.order_no
        """).fetchall()
        data = [list(r) for r in rows]
    elif kind in ("stock_balance", "purchase_summary", "production_summary", "payable", "audit_log"):
        headers, data = build_report(kind, {"sales_order_no": sales_order_no} if sales_order_no else {})
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
    elif kind in ("stock_in", "stock_out"):
        # P2-5 入库表 / 出库表（用户 2026-10-19 定的表头，见 字段字典.md）：
        # 单号=RK/CK-流水号(stock_ledger.id)、日期=流水时间、类型映射到工厂常用说法、
        # 数量取绝对值、金额=数量×单价、关联单号=业务单号（采购单/生产单/调拨号）
        headers = ["单号", "日期", "类型", "仓库", "库位", "零件编码", "零件名称", "规格型号", "批次", "数量", "单价", "金额", "经办人", "关联单号", "备注"]
        _is_in = kind == "stock_in"
        if _is_in:
            _cond = "(l.type IN ('采购入库','生产入库','期初','期初导入','其他入库','调拨入') OR (l.type='盘点' AND l.change_qty>0))"
        else:
            _cond = "(l.type IN ('生产领料','领料出库','其他出库','调拨出','销售出库') OR (l.type='盘点' AND l.change_qty<0))"
        _rows = conn.execute(f"""
            SELECT l.id,l.created_at,l.type,l.warehouse,l.location,l.part_code,p.name,p.spec,l.batch,l.change_qty,l.unit_price,l.operator,l.ref_no,l.remark
            FROM stock_ledger l LEFT JOIN parts p ON p.code=l.part_code
            WHERE {_cond}
            ORDER BY l.id
        """).fetchall()
        _disp = {"生产领料": "领料出库", "调拨入": "调拨", "调拨出": "调拨", "期初": "期初入库", "期初导入": "期初入库"}
        data = []
        for _r in _rows:
            _lid, _created, _typ, _wh, _loc, _code, _pname, _spec, _batch, _chg, _price, _op, _ref, _remark = _r
            _qty = abs(float(_chg or 0))
            _disp_type = ("盘点入库" if _chg > 0 else "盘点出库") if _typ == "盘点" else _disp.get(_typ, _typ)
            data.append([f"{'RK' if _is_in else 'CK'}-{str(_lid).zfill(6)}", _created, _disp_type, _wh, _loc or "", _code,
                         _pname or "", _spec or "", _batch or "", _qty, float(_price or 0), round(_qty * float(_price or 0), 2),
                         _op or "", _ref or "", _remark or ""])
    else:
        conn.close()
        raise HTTPException(404, f"导出 {kind} 不存在，可选 parts/stock/purchase/bom/supplier/sales/production/mrp/stock_in/stock_out/stock_balance/purchase_summary/production_summary/payable/audit_log")
    conn.close()
    return _excel_response(rows_to_excel(headers, data, sheet_name=kind), f"export_{kind}.xlsx")

@app.post("/api/excel/import/{kind}")
async def excel_import(kind: str, file: UploadFile = File(...), sheet: Optional[str] = None, x_user: Optional[str] = Header(None)):
    """
    导入 Excel（灵活映射 + 行号校验）。
    支持 kinds：parts / stock / purchase / bom / supplier / sales / production
    sheet 参数：指定读哪个工作表（如“模拟数据-已填好.xlsx”里有 零件/库存期初/采购/BOM 四页时，导入 BOM 就传 sheet=BOM）。
    P0-4：按导入数据类型校验模块权限，不再“谁都能导入”。
    """
    user = resolve_user(x_user)
    _imp = {"parts": ("零件", "create"), "stock": ("库存", "create"), "purchase": ("采购", "create"),
            "bom": ("零件", "create"), "supplier": ("供应商", "create"),
            "sales": ("销售", "create"), "production": ("生产", "create")}
    _mod, _act = _imp.get(kind, ("系统", "create"))
    require_perm(user, _mod, _act)
    content = await file.read()
    headers, data_rows = excel_to_rows(content, sheet)
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
                    mins = float(get(row, "min_stock", default=0) or 0)
                    maxs = float(get(row, "max_stock", default=0) or 0)
                    price = float(get(row, "price", default=0) or 0)
                    tax = float(get(row, "tax_rate", default=0.13) or 0.13)
                    lead = int(float(get(row, "lead_time_days", default=7) or 7))
                except ValueError:
                    errors.append(f"第 {i} 行：安全库存/最高库存/单价/税率/提前期不是数字"); continue
                comp = str(get(row, "company_code", default="COMP-001") or "COMP-001")
                # 是否采购件：填 1/是 = 外购，0/否 = 自制（成品/半成品一般填 0）；不填时默认 1（原材料）
                # 注意：不能写 `get(...) or ""`，因为 0 是合法值却会被 or 当成空吞掉
                _raw_ip_v = get(row, "is_purchase", default=None)
                _raw_ip = "" if _raw_ip_v is None else str(_raw_ip_v).strip()
                is_purchase = 0 if _raw_ip in ("0", "否", "否 ", "N", "n", "No", "no", "自制") else (1 if _raw_ip in ("1", "是", "Y", "y", "Yes", "yes", "外购") else 1)
                existing = conn.execute("SELECT id FROM parts WHERE code=?", (code,)).fetchone()
                # P1-3：更新时只改“Excel 里真的有的列”，不再整行覆盖（以前导入一份只含编码/名称/安全库存的表，
                # 会把单价、税率、提前期、公司全部冲成默认 0/0.13/7/COMP-001）
                optional_values = {
                    "spec": str(get(row, "spec", default="")),
                    "unit": str(get(row, "unit", default="个") or "个"),
                    "category": str(get(row, "category", default="原材料") or "原材料"),
                    "is_purchase": is_purchase,
                    "safety_stock": safety, "min_stock": mins, "max_stock": maxs,
                    "price": price, "tax_rate": tax, "lead_time_days": lead,
                    "company_code": comp,
                }
                if existing:
                    cols = ["name"]; vals: List[Any] = [name]
                    for f in ("spec", "unit", "category", "is_purchase", "safety_stock", "min_stock",
                              "max_stock", "price", "tax_rate", "lead_time_days", "company_code"):
                        if f in mapping:
                            cols.append(f); vals.append(optional_values[f])
                    conn.execute("UPDATE parts SET " + ",".join(c + "=?" for c in cols) + " WHERE code=?",
                                 (*vals, code))
                else:
                    conn.execute("INSERT INTO parts (code,name,spec,unit,category,is_purchase,safety_stock,min_stock,max_stock,lead_time_days,price,tax_rate,supplier_code,created_at,company_code) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (code, name, str(get(row, "spec", default="")), str(get(row, "unit", default="个") or "个"), str(get(row, "category", default="原材料") or "原材料"), is_purchase, safety, mins, maxs, lead, price, tax, str(get(row, "supplier_code", default="")), now_str(), comp))
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
                # P1-5：导入表里没给库位时用该仓库的默认库位，不再写死 A区-01
                location = str(get(row, "location", default="") or "") or warehouse_default_location(conn, warehouse)
                batch = str(get(row, "batch", default="BATCH-IMPORT") or "BATCH-IMPORT")
                comp = str(get(row, "company_code", default="") or "") or warehouse_company(conn, warehouse)
                existing = conn.execute("SELECT id,qty FROM stock WHERE part_code=? AND warehouse=? AND location=? AND batch=?", (code, warehouse, location, batch)).fetchone()
                if existing:
                    delta = qty - float(existing["qty"])
                    conn.execute("UPDATE stock SET qty=? WHERE id=?", (qty, existing["id"]))
                else:
                    delta = qty
                    conn.execute("INSERT INTO stock (part_code,warehouse,location,batch,qty,company_code) VALUES (?,?,?,?,?,?)", (code, warehouse, location, batch, qty, comp))
                if delta != 0:
                    conn.execute("INSERT INTO stock_ledger (part_code,warehouse,location,batch,change_qty,type,ref_no,operator,remark,created_at,company_code) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (code, warehouse, location, batch, delta, "期初导入", "Excel导入", user, "Excel导入库存", now_str(), comp))
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
                    "part_code": part_code, "qty": qty, "price": price, "tax": float(get(row, "tax_rate", default=0.13) or 0.13),
                    "company_code": str(get(row, "company_code", default="COMP-001") or "COMP-001"),
                    "warehouse": str(get(row, "warehouse", default="主仓") or "主仓"),
                    "currency": str(get(row, "currency", default="CNY") or "CNY"),
                    "exchange_rate": float(get(row, "exchange_rate", default=0) or 0)})
            for order_no, items in grouped.items():
                if conn.execute("SELECT id FROM purchase_orders WHERE order_no=?", (order_no,)).fetchone():
                    errors.append(f"采购单 {order_no} 已存在，跳过（避免重复导入）"); continue
                total = sum(it["qty"] * it["price"] * (1 + it["tax"]) for it in items)
                _cur = items[0]["currency"]; _rate = resolve_exchange_rate(conn, _cur, "CNY", items[0]["exchange_rate"] or None) if _cur != "CNY" else 1.0
                conn.execute("INSERT INTO purchase_orders (order_no,supplier_code,status,due_date,total_amount,created_at,company_code,warehouse,currency,exchange_rate,total_amount_base) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (order_no, items[0]["supplier"], "已下单", TODAY, round(total, 2), now_str(), items[0]["company_code"], items[0]["warehouse"], _cur, _rate, round(total * _rate, 2)))
                for it in items:
                    conn.execute("INSERT INTO purchase_order_items (order_no,part_code,qty,price,received_qty,tax_rate,warehouse,company_code) VALUES (?,?,?,?,?,?,?,?)",
                                 (order_no, it["part_code"], it["qty"], it["price"], 0, it["tax"], it["warehouse"], it["company_code"]))
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
                _comp = str(get(row, "company_code", default="COMP-001") or "COMP-001")
                _wh = str(get(row, "warehouse", default="主仓") or "主仓")
                _cur = str(get(row, "currency", default="CNY") or "CNY")
                _rate = resolve_exchange_rate(conn, _cur, "CNY", float(get(row, "exchange_rate", default=0) or 0) or None) if _cur != "CNY" else 1.0
                conn.execute("INSERT INTO sales_orders (order_no,customer,product_code,qty,due_date,status,created_at,company_code,warehouse,currency,exchange_rate) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(order_no) DO UPDATE SET customer=excluded.customer, product_code=excluded.product_code, qty=excluded.qty, due_date=excluded.due_date, company_code=excluded.company_code, warehouse=excluded.warehouse, currency=excluded.currency",
                             (order_no, str(get(row, "customer", default="")), product, qty, str(get(row, "due_date", default="") or ""), "新建", now_str(), _comp, _wh, _cur, _rate))
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
                _comp = str(get(row, "company_code", default="COMP-001") or "COMP-001")
                _wh = str(get(row, "warehouse", default="主仓") or "主仓")
                existing = conn.execute("SELECT id FROM production_orders WHERE order_no=?", (order_no,)).fetchone()
                if existing:
                    conn.execute("UPDATE production_orders SET product_code=?, qty=?, planned_start=?, planned_end=?, sales_order_no=?, company_code=?, warehouse=? WHERE order_no=?",
                                 (product, qty, ps, pe, str(get(row, "sales_order_no", default="") or ""), _comp, _wh, order_no))
                else:
                    conn.execute("INSERT INTO production_orders (order_no,product_code,qty,status,sales_order_no,created_at,planned_start,planned_end,company_code,warehouse) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 (order_no, product, qty, "新建", str(get(row, "sales_order_no", default="") or ""), now_str(), ps, pe, _comp, _wh))
                    _allocate_for_production(conn, order_no, product, qty, _wh, _comp)
                imported += 1
        else:
            raise HTTPException(404, f"导入 {kind} 不存在，可选 parts/stock/purchase/bom/supplier/sales/production")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "imported": imported, "errors": errors, "mapping": mapping, "headers": headers}


# ========== 第二阶段新增接口：基础主数据 / 权限管理 / 审批 / 财务 / 报表 / 审计 ==========

# ---------- 报表构造器（JSON 接口与 Excel 导出共用） ----------
def build_report(kind: str, filters: Dict[str, Any]):
    """返回 (表头, 数据行)。过滤键：company_code / warehouse / supplier_code / from_date / to_date / user / entity_type。"""
    conn = get_conn()
    try:
        comp = filters.get("company_code") or ""
        wh = filters.get("warehouse") or ""
        if kind == "stock_balance":
            headers = ["公司编码", "仓库", "零件编码", "零件名称", "单位", "现有数量", "冻结数量", "可用量", "未税单价", "库存金额(未税)", "状态"]
            sql = """SELECT s.company_code, s.warehouse, s.part_code, p.name, p.unit, p.price, p.safety_stock, p.max_stock,
                     COALESCE(SUM(s.qty),0) as qty FROM stock s LEFT JOIN parts p ON p.code=s.part_code WHERE 1=1"""
            args: List[Any] = []
            if comp:
                sql += " AND s.company_code=?"; args.append(comp)
            if wh:
                sql += " AND s.warehouse=?"; args.append(wh)
            sql += " GROUP BY s.company_code, s.warehouse, s.part_code ORDER BY s.part_code"
            data = []
            for r in conn.execute(sql, args).fetchall():
                fr = get_frozen(r["part_code"], r["warehouse"], None, r["company_code"])
                avail = get_available(r["part_code"], r["warehouse"], r["company_code"])
                price = float(r["price"] or 0)
                data.append([r["company_code"], r["warehouse"], r["part_code"], r["name"], r["unit"],
                             round(float(r["qty"]), 2), round(fr, 2), round(avail, 2), price,
                             round(float(r["qty"]) * price, 2),
                             stock_status(avail, float(r["safety_stock"] or 0), float(r["max_stock"] or 0))])
            return headers, data
        if kind == "purchase_summary":
            headers = ["供应商编码", "采购单数", "含税总额", "本位币总额", "已到货金额(含税)", "未到货金额(含税)", "拖期单数", "已对账单数"]
            sql = "SELECT supplier_code FROM purchase_orders WHERE 1=1"; args = []
            if comp:
                sql += " AND company_code=?"; args.append(comp)
            if filters.get("supplier_code"):
                sql += " AND supplier_code=?"; args.append(filters["supplier_code"])
            suppliers = sorted({r["supplier_code"] for r in conn.execute(sql, args).fetchall()})
            data = []
            for sup in suppliers:
                pos = conn.execute("SELECT * FROM purchase_orders WHERE supplier_code=? AND (?='' OR company_code=?)", (sup, comp, comp)).fetchall()
                cnt = len(pos)
                total = sum(float(p["total_amount"] or 0) for p in pos)
                total_base = sum(float(p["total_amount_base"] or 0) for p in pos)
                received_amt = 0; overdue = 0; reconciled_n = 0
                for p in pos:
                    items = conn.execute("SELECT * FROM purchase_order_items WHERE order_no=?", (p["order_no"],)).fetchall()
                    received_amt += sum(float(i["received_qty"]) * float(i["price"]) * (1 + float(i["tax_rate"] or 0)) for i in items)
                    if is_overdue(p["due_date"], p["status"]):
                        overdue += 1
                    if int(p["reconciled"] or 0) == 1:
                        reconciled_n += 1
                data.append([sup, cnt, round(total, 2), round(total_base, 2), round(received_amt, 2),
                             round(total - received_amt, 2), overdue, reconciled_n])
            return headers, data
        if kind == "production_summary":
            headers = ["成品编码", "生产单数", "计划产量", "已完工单数", "完工产量", "领料成本(直接材料)", "完工率%"]
            sql = "SELECT product_code FROM production_orders WHERE 1=1"; args = []
            if comp:
                sql += " AND company_code=?"; args.append(comp)
            if wh:
                sql += " AND warehouse=?"; args.append(wh)
            products = sorted({r["product_code"] for r in conn.execute(sql, args).fetchall()})
            data = []
            for prod in products:
                mos = conn.execute("SELECT * FROM production_orders WHERE product_code=? AND (?='' OR company_code=?) AND (?='' OR warehouse=?)",
                                   (prod, comp, comp, wh, wh)).fetchall()
                planned = sum(float(m["qty"]) for m in mos)
                done = [m for m in mos if m["status"] == "已完工"]
                done_qty = sum(float(m["qty"]) for m in done)
                cost = 0
                for m in mos:
                    cost += float(conn.execute("SELECT COALESCE(SUM(total_cost),0) FROM production_consumptions WHERE order_no=?", (m["order_no"],)).fetchone()[0] or 0)
                rate = round(done_qty / planned * 100, 1) if planned else 0
                data.append([prod, len(mos), round(planned, 2), len(done), round(done_qty, 2), round(cost, 2), rate])
            return headers, data
        if kind == "payable":
            headers = ["供应商编码", "发票数", "发票总额(本位币)", "已付金额(本位币)", "未付金额(本位币)", "已对账未付", "未对账金额"]
            sql = "SELECT supplier_code FROM supplier_invoices WHERE 1=1"; args = []
            if comp:
                sql += " AND company_code=?"; args.append(comp)
            if filters.get("supplier_code"):
                sql += " AND supplier_code=?"; args.append(filters["supplier_code"])
            suppliers = sorted({r["supplier_code"] for r in conn.execute(sql, args).fetchall()})
            data = []
            for sup in suppliers:
                invs = conn.execute("SELECT * FROM supplier_invoices WHERE supplier_code=? AND (?='' OR company_code=?)", (sup, comp, comp)).fetchall()
                total = sum(float(i["amount_base"] or 0) for i in invs)
                paid = sum(float(i["paid_amount"] or 0) for i in invs)
                reconciled_unpaid = sum(float(i["amount_base"] or 0) - float(i["paid_amount"] or 0) for i in invs if i["status"] in ("已对账", "部分付款"))
                unreconciled = sum(float(i["amount_base"] or 0) for i in invs if i["status"] in ("待对账", "差异待处理"))
                data.append([sup, len(invs), round(total, 2), round(paid, 2), round(total - paid, 2),
                             round(reconciled_unpaid, 2), round(unreconciled, 2)])
            return headers, data
        if kind == "audit_log":
            headers = ["时间", "用户", "动作", "单据类型", "单据号", "字段", "旧值", "新值", "备注"]
            sql = "SELECT * FROM audit_log WHERE 1=1"; args = []
            if filters.get("user"):
                sql += " AND username=?"; args.append(filters["user"])
            if filters.get("entity_type"):
                sql += " AND entity_type=?"; args.append(filters["entity_type"])
            if comp:
                sql += " AND company_code=?"; args.append(comp)
            sql += " ORDER BY id DESC LIMIT 500"
            data = [[r["created_at"], r["username"], r["action"], r["entity_type"], r["entity_id"],
                     r["field_name"], r["old_value"], r["new_value"], r["remark"]]
                    for r in conn.execute(sql, args).fetchall()]
            return headers, data
        raise HTTPException(404, f"报表 {kind} 不存在，可选 stock_balance/purchase_summary/production_summary/payable/audit_log")
    finally:
        conn.close()

def expected_amount_for_po(conn, po_no: str) -> float:
    """三方核对期望金额（大白话：按实际到货数量 × 采购单价 × (1+税率) 算，发票应该和这个数对得上）。"""
    items = conn.execute("SELECT * FROM purchase_order_items WHERE order_no=?", (po_no,)).fetchall()
    return round(sum(float(i["received_qty"]) * float(i["price"]) * (1 + float(i["tax_rate"] or 0)) for i in items), 2)

# ---------- 公司 / 仓库 / 币种 / 汇率 ----------
@app.get("/api/companies")
def list_companies(x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "系统", "view")
    conn = get_conn()
    rows = conn.execute("SELECT * FROM companies ORDER BY code").fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.post("/api/companies")
def create_company(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "系统", "create")
    code = (payload.get("code") or "").strip()
    if not code or not payload.get("name"):
        raise HTTPException(400, "公司编码、公司名称必填")
    conn = get_conn()
    try:
        conn.execute("INSERT INTO companies (code,name,base_currency,country,tax_rate,address,created_at) VALUES (?,?,?,?,?,?,?)",
                     (code, payload.get("name"), payload.get("base_currency", "CNY"), payload.get("country", "中国"),
                      float(payload.get("tax_rate", 0.13) or 0.13), payload.get("address", ""), now_str()))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"公司编码 {code} 已存在")
    conn.close()
    log_audit(user, "创建", "公司", code, "名称", "", str(payload.get("name")), "")
    return {"ok": True, "code": code}

@app.get("/api/warehouses")
def list_warehouses(company_code: Optional[str] = None, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "库存", "view")
    conn = get_conn()
    sql = "SELECT * FROM warehouses WHERE 1=1"; args: List[Any] = []
    if company_code:
        sql += " AND company_code=?"; args.append(company_code)
    sql += " ORDER BY code"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.post("/api/warehouses")
def create_warehouse(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "系统", "create")
    code = (payload.get("code") or "").strip()
    if not code or not payload.get("name"):
        raise HTTPException(400, "仓库编码、仓库名称必填")
    conn = get_conn()
    if not conn.execute("SELECT id FROM companies WHERE code=?", (payload.get("company_code", "COMP-001"),)).fetchone():
        conn.close()
        raise HTTPException(404, f"公司 {payload.get('company_code')} 不存在")
    try:
        conn.execute("INSERT INTO warehouses (code,name,company_code,type,address,default_location,created_at) VALUES (?,?,?,?,?,?,?)",
                     (code, payload.get("name"), payload.get("company_code", "COMP-001"),
                      payload.get("type", "综合"), payload.get("address", ""),
                      (payload.get("default_location") or "A区-01"), now_str()))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"仓库编码 {code} 已存在")
    conn.close()
    log_audit(user, "创建", "仓库", code, "名称", "", str(payload.get("name")), f"公司 {payload.get('company_code','COMP-001')}")
    return {"ok": True, "code": code}

@app.put("/api/warehouses/{code}")
def update_warehouse(code: str, payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """修改仓库（大白话：改个仓库的名字、默认库位这类基本信息；默认库位就是入库不填库位时进的地方，P1-5）。"""
    user = resolve_user(x_user)
    require_perm(user, "系统", "edit")
    conn = get_conn()
    row = conn.execute("SELECT * FROM warehouses WHERE code=?", (code,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, f"仓库 {code} 不存在")
    fields = {}
    for k in ("name", "type", "address", "default_location"):
        if k in payload and payload.get(k) is not None:
            fields[k] = payload.get(k)
    if not fields:
        conn.close()
        raise HTTPException(400, "没有要修改的字段（可改 name/type/address/default_location）")
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE warehouses SET {sets} WHERE code=?", list(fields.values()) + [code])
    conn.commit(); conn.close()
    log_audit(user, "修改", "仓库", code, "字段", "", str({k: v for k, v in fields.items()}), "")
    return {"ok": True, "code": code, "updated": list(fields.keys())}

@app.get("/api/currencies")
def list_currencies(x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "财务", "view")
    conn = get_conn()
    rows = conn.execute("SELECT * FROM currencies ORDER BY code").fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.post("/api/currencies")
def create_currency(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "财务", "create")
    code = (payload.get("code") or "").strip().upper()
    if not code or not payload.get("name"):
        raise HTTPException(400, "币种编码、币种名称必填")
    conn = get_conn()
    try:
        conn.execute("INSERT INTO currencies (code,name,symbol,created_at) VALUES (?,?,?,?)",
                     (code, payload.get("name"), payload.get("symbol", ""), now_str()))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"币种 {code} 已存在")
    conn.close()
    log_audit(user, "创建", "币种", code, "名称", "", str(payload.get("name")), "")
    return {"ok": True, "code": code}

@app.get("/api/exchange-rates")
def list_exchange_rates(from_currency: Optional[str] = None, to_currency: Optional[str] = None, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "财务", "view")
    conn = get_conn()
    sql = "SELECT * FROM exchange_rates WHERE 1=1"; args: List[Any] = []
    if from_currency:
        sql += " AND from_currency=?"; args.append(from_currency)
    if to_currency:
        sql += " AND to_currency=?"; args.append(to_currency)
    sql += " ORDER BY effective_date DESC, id DESC"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.post("/api/exchange-rates")
def create_exchange_rate(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """维护汇率（大白话：1 美元 = 多少人民币，今天的价）。外币单据按这里的汇率折算成本位币。"""
    user = resolve_user(x_user)
    require_perm(user, "财务", "create")
    f = (payload.get("from_currency") or "").strip().upper(); t = (payload.get("to_currency") or "").strip().upper()
    rate = float(payload.get("rate", 0) or 0)
    if not f or not t or rate <= 0:
        raise HTTPException(400, "源币种、目标币种必填，汇率必须 > 0")
    conn = get_conn()
    conn.execute("INSERT INTO exchange_rates (from_currency,to_currency,rate,effective_date,created_at) VALUES (?,?,?,?,?)",
                 (f, t, rate, payload.get("effective_date") or TODAY, now_str()))
    conn.commit(); conn.close()
    log_audit(user, "维护汇率", "汇率", f"{f}→{t}", "汇率", "", str(rate), f"生效 {payload.get('effective_date') or TODAY}")
    return {"ok": True, "from_currency": f, "to_currency": t, "rate": rate}

# ---------- 用户 / 角色 / 权限 ----------
@app.get("/api/users")
def list_users(x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "系统", "view")
    conn = get_conn()
    out = []
    for u in conn.execute("SELECT * FROM users ORDER BY username").fetchall():
        d = row_to_dict(u)
        d["roles"] = user_roles_of(conn, u["username"])
        d["warehouses"] = [r["warehouse"] for r in conn.execute("SELECT warehouse FROM user_warehouses WHERE username=?", (u["username"],)).fetchall()]
        d["companies"] = [r["company_code"] for r in conn.execute("SELECT company_code FROM user_companies WHERE username=?", (u["username"],)).fetchall()]
        out.append(d)
    conn.close()
    return out

@app.post("/api/users")
def create_user(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """新建用户并分配角色/仓库/公司范围（大白话：给新人开账号，顺便说清他能看哪个仓、哪个公司）。"""
    user = resolve_user(x_user)
    require_perm(user, "系统", "create")
    username = (payload.get("username") or "").strip()
    if not username:
        raise HTTPException(400, "用户名必填")
    conn = get_conn()
    try:
        conn.execute("INSERT INTO users (username,name,created_at) VALUES (?,?,?)", (username, payload.get("name", ""), now_str()))
        for r in payload.get("roles", []):
            if conn.execute("SELECT name FROM roles WHERE name=?", (r,)).fetchone():
                conn.execute("INSERT OR IGNORE INTO user_roles (username,role) VALUES (?,?)", (username, r))
        for w in payload.get("warehouses", []):
            conn.execute("INSERT OR IGNORE INTO user_warehouses (username,warehouse) VALUES (?,?)", (username, w))
        for c in payload.get("companies", []):
            conn.execute("INSERT OR IGNORE INTO user_companies (username,company_code) VALUES (?,?)", (username, c))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"用户 {username} 已存在")
    conn.close()
    log_audit(user, "创建", "用户", username, "角色", "", ",".join(payload.get("roles", [])), "")
    return {"ok": True, "username": username}

@app.get("/api/roles")
def list_roles(x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "系统", "view")
    conn = get_conn()
    rows = conn.execute("SELECT * FROM roles ORDER BY name").fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.get("/api/permissions")
def list_permissions(role: Optional[str] = None, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "系统", "view")
    conn = get_conn()
    sql = "SELECT * FROM role_permissions WHERE 1=1"; args: List[Any] = []
    if role:
        sql += " AND role=?"; args.append(role)
    sql += " ORDER BY role, module"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.post("/api/permissions")
def set_permission(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """设置角色权限（大白话：某个角色对某个模块能看/能建/能改/能批/能删，打勾就行）。"""
    user = resolve_user(x_user)
    require_perm(user, "系统", "edit")
    role = payload.get("role", ""); module = payload.get("module", "")
    if not role or not module:
        raise HTTPException(400, "角色、模块必填")
    conn = get_conn()
    conn.execute("""INSERT INTO role_permissions (role,module,can_view,can_create,can_edit,can_approve,can_delete) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(role,module) DO UPDATE SET can_view=excluded.can_view, can_create=excluded.can_create,
        can_edit=excluded.can_edit, can_approve=excluded.can_approve, can_delete=excluded.can_delete""",
                 (role, module, int(bool(payload.get("can_view", 0))), int(bool(payload.get("can_create", 0))),
                  int(bool(payload.get("can_edit", 0))), int(bool(payload.get("can_approve", 0))), int(bool(payload.get("can_delete", 0)))))
    conn.commit(); conn.close()
    log_audit(user, "设置权限", "权限", f"{role}/{module}", "权限", "", str(payload), "")
    return {"ok": True, "role": role, "module": module}

@app.get("/api/permissions/check")
def check_permission(module: str, action: str = "view", x_user: Optional[str] = Header(None)):
    """查当前用户有没有某模块某操作的权限（前端做按钮显隐用）。"""
    user = resolve_user(x_user)
    try:
        require_perm(user, module, action)
        return {"ok": True, "user": user, "module": module, "action": action, "allowed": True}
    except HTTPException as e:
        return {"ok": True, "user": user, "module": module, "action": action, "allowed": False, "reason": e.detail}

# ---------- 审批规则 / 审批记录 ----------
@app.get("/api/approvals/rules")
def list_approval_rules(doc_type: Optional[str] = None, company_code: Optional[str] = None, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "采购", "view")
    conn = get_conn()
    sql = "SELECT * FROM approval_rules WHERE 1=1"; args: List[Any] = []
    if doc_type:
        sql += " AND doc_type=?"; args.append(doc_type)
    if company_code is not None:
        sql += " AND company_code=?"; args.append(company_code)
    sql += " ORDER BY doc_type, step_order, min_amount"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.post("/api/approvals/rules")
def create_approval_rule(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """自定义审批规则（大白话：自己定“多少钱以上、谁来批、批几级”；自定义规则存在时覆盖默认分级）。"""
    user = resolve_user(x_user)
    require_perm(user, "采购", "approve")
    if not payload.get("doc_type"):
        raise HTTPException(400, "单据类型必填（采购申请/采购单）")
    conn = get_conn()
    conn.execute("INSERT INTO approval_rules (doc_type,name,min_amount,max_amount,approver_role,approver_user,step_order,company_code,active,is_custom,created_at) VALUES (?,?,?,?,?,?,?,?,1,1,?)",
                 (payload.get("doc_type"), payload.get("name", "自定义审批规则"), float(payload.get("min_amount", 0) or 0),
                  payload.get("max_amount"), payload.get("approver_role", ""), payload.get("approver_user", ""),
                  int(payload.get("step_order", 1) or 1), payload.get("company_code", ""), now_str()))
    conn.commit(); conn.close()
    log_audit(user, "创建审批规则", "审批规则", payload.get("doc_type"), "规则", "", str(payload), "")
    return {"ok": True, "doc_type": payload.get("doc_type")}

@app.put("/api/approvals/rules/{rule_id}")
def update_approval_rule(rule_id: int, payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "采购", "approve")
    conn = get_conn()
    r = conn.execute("SELECT id FROM approval_rules WHERE id=?", (rule_id,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, f"审批规则 {rule_id} 不存在")
    for k in ("name", "min_amount", "max_amount", "approver_role", "approver_user", "step_order", "company_code", "active"):
        if k in payload and payload[k] is not None:
            conn.execute(f"UPDATE approval_rules SET {k}=? WHERE id=?", (payload[k], rule_id))
    conn.commit(); conn.close()
    log_audit(user, "修改审批规则", "审批规则", str(rule_id), "规则", "", str(payload), "")
    return {"ok": True, "id": rule_id}

@app.delete("/api/approvals/rules/{rule_id}")
def delete_approval_rule(rule_id: int, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "采购", "approve")
    conn = get_conn()
    conn.execute("DELETE FROM approval_rules WHERE id=?", (rule_id,))
    conn.commit(); conn.close()
    log_audit(user, "删除审批规则", "审批规则", str(rule_id), "", str(rule_id), "", "")
    return {"ok": True, "id": rule_id}

@app.get("/api/approvals")
def list_approvals(doc_no: Optional[str] = None, doc_type: Optional[str] = None, status: Optional[str] = None,
                   x_user: Optional[str] = Header(None)):
    """审批记录可查（大白话：每张单谁批过、谁还没批、批了什么，全能查到）。"""
    user = resolve_user(x_user)
    require_perm(user, "采购", "view")
    conn = get_conn()
    sql = "SELECT * FROM approval_records WHERE 1=1"; args: List[Any] = []
    if doc_no:
        sql += " AND doc_no=?"; args.append(doc_no)
    if doc_type:
        sql += " AND doc_type=?"; args.append(doc_type)
    if status:
        sql += " AND status=?"; args.append(status)
    sql += " ORDER BY id"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.get("/api/approvals/pending")
def pending_approvals(x_user: Optional[str] = Header(None)):
    """我的待审批（大白话：轮到我批的单子列出来）。"""
    user = resolve_user(x_user)
    require_perm(user, "采购", "view")
    conn = get_conn()
    roles = user_roles_of(conn, user)
    if "系统管理员" in roles:
        rows = conn.execute("SELECT * FROM approval_records WHERE status='待审批' ORDER BY id").fetchall()
    else:
        placeholders = ",".join("?" * len(roles)) if roles else "''"
        rows = conn.execute(f"SELECT * FROM approval_records WHERE status='待审批' AND (approver_user=? OR approver_role IN ({placeholders})) ORDER BY id",
                            [user] + roles).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

# ---------- 财务对账：供应商发票 / 付款 / 应付 ----------
@app.post("/api/finance/invoices")
def create_invoice(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """登记供应商发票并做简易三方核对（大白话：采购单说买了啥、到货说收了啥、发票说要多少钱，三边对得上才能付）。
    思路来源：三方核对（Three-Way Match：PO vs 收货 vs 发票），见 https://www.onphase.com/blog/what-is-a-3-way-match-in-accounts-payable"""
    user = resolve_user(x_user)
    require_perm(user, "财务", "create")
    invoice_no = (payload.get("invoice_no") or "").strip()
    po_no = payload.get("po_no", "")
    if not invoice_no or not po_no:
        raise HTTPException(400, "发票号、采购单号必填")
    conn = get_conn()
    po = conn.execute("SELECT * FROM purchase_orders WHERE order_no=?", (po_no,)).fetchone()
    if not po:
        conn.close()
        raise HTTPException(404, f"采购单 {po_no} 不存在")
    if conn.execute("SELECT id FROM supplier_invoices WHERE invoice_no=?", (invoice_no,)).fetchone():
        conn.close()
        raise HTTPException(400, f"发票号 {invoice_no} 已存在，不能重复登记（防重复付款）")
    currency = payload.get("currency") or po["currency"]
    rate = resolve_exchange_rate(conn, currency, "CNY", payload.get("exchange_rate") or None) if currency != "CNY" else 1.0
    subtotal = float(payload.get("subtotal_amount", 0) or 0)
    total = float(payload.get("total_amount", 0) or 0)
    if total <= 0 and subtotal > 0:
        total = round(subtotal * 1.13, 2)
    tax = float(payload.get("tax_amount", 0) or 0)
    if tax <= 0 and total > 0:
        tax = round(total - total / 1.13, 2)
        subtotal = round(total / 1.13, 2) if subtotal <= 0 else subtotal
    expected = expected_amount_for_po(conn, po_no)
    diff = round(total - expected, 2)
    tolerance = max(0.01, round(expected * 0.01, 2))
    status = "差异待处理" if abs(diff) > tolerance else "待对账"
    conn.execute("""INSERT INTO supplier_invoices (invoice_no,po_no,supplier_code,company_code,currency,exchange_rate,
        subtotal_amount,tax_amount,total_amount,amount_base,expected_amount,diff_amount,paid_amount,status,
        invoice_date,due_date,remark,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)""",
                 (invoice_no, po_no, po["supplier_code"], po["company_code"], currency, rate,
                  round(subtotal, 2), round(tax, 2), round(total, 2), round(total * rate, 2),
                  expected, diff, status, payload.get("invoice_date") or TODAY,
                  payload.get("due_date") or "", payload.get("remark", ""), now_str()))
    conn.commit(); conn.close()
    log_audit(user, "登记发票", "供应商发票", invoice_no, "金额", "", str(total), f"采购单 {po_no}，期望 {expected}，差额 {diff}，状态 {status}", po["company_code"])
    return {"ok": True, "invoice_no": invoice_no, "po_no": po_no, "expected_amount": expected,
            "diff_amount": diff, "tolerance": tolerance, "status": status}

@app.get("/api/finance/invoices")
def list_invoices(supplier_code: Optional[str] = None, status: Optional[str] = None, company_code: Optional[str] = None,
                  x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "财务", "view")
    conn = get_conn()
    sql = "SELECT * FROM supplier_invoices WHERE 1=1"; args: List[Any] = []
    if supplier_code:
        sql += " AND supplier_code=?"; args.append(supplier_code)
    if status:
        sql += " AND status=?"; args.append(status)
    if company_code:
        sql += " AND company_code=?"; args.append(company_code)
    sql += " ORDER BY id DESC"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.post("/api/finance/invoices/{invoice_no}/reconcile")
def reconcile_invoice(invoice_no: str, x_user: Optional[str] = Header(None)):
    """发票对账：重新三方核对，差额在容差内才允许对账通过；通过后回写采购单对账字段。"""
    user = resolve_user(x_user)
    require_perm(user, "财务", "edit")
    conn = get_conn()
    inv = conn.execute("SELECT * FROM supplier_invoices WHERE invoice_no=?", (invoice_no,)).fetchone()
    if not inv:
        conn.close()
        raise HTTPException(404, f"发票 {invoice_no} 不存在")
    if inv["status"] in ("已对账", "部分付款", "已付款"):
        conn.close()
        return {"ok": True, "invoice_no": invoice_no, "status": inv["status"], "note": "已对账过"}
    expected = expected_amount_for_po(conn, inv["po_no"])
    diff = round(float(inv["total_amount"]) - expected, 2)
    tolerance = max(0.01, round(expected * 0.01, 2))
    if abs(diff) > tolerance:
        conn.execute("UPDATE supplier_invoices SET expected_amount=?, diff_amount=?, status='差异待处理' WHERE invoice_no=?",
                     (expected, diff, invoice_no))
        conn.commit(); conn.close()
        raise HTTPException(400, f"发票 {invoice_no} 三方核对不通过：发票 {inv['total_amount']} vs 实际到货 {expected}，差额 {diff} 超出容差 {tolerance}，请先处理差异")
    conn.execute("UPDATE supplier_invoices SET expected_amount=?, diff_amount=?, status='已对账' WHERE invoice_no=?",
                 (expected, diff, invoice_no))
    conn.execute("UPDATE purchase_orders SET invoice_no=?, invoice_amount=?, reconciled=1 WHERE order_no=?",
                 (invoice_no, float(inv["total_amount"]), inv["po_no"]))
    conn.commit(); conn.close()
    log_audit(user, "发票对账", "供应商发票", invoice_no, "状态", inv["status"], "已对账", f"采购单 {inv['po_no']}，差额 {diff}", inv["company_code"])
    return {"ok": True, "invoice_no": invoice_no, "status": "已对账", "diff_amount": diff}

@app.post("/api/finance/invoices/{invoice_no}/pay")
def pay_invoice(invoice_no: str, payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """付款：对已对账的发票付款；付满则已付款，未付满则部分付款。付款单号用数据库序列。"""
    user = resolve_user(x_user)
    require_perm(user, "财务", "create")
    amount = float(payload.get("amount", 0) or 0)
    if amount <= 0:
        raise HTTPException(400, "付款金额必须 > 0")
    conn = get_conn()
    inv = conn.execute("SELECT * FROM supplier_invoices WHERE invoice_no=?", (invoice_no,)).fetchone()
    if not inv:
        conn.close()
        raise HTTPException(404, f"发票 {invoice_no} 不存在")
    if inv["status"] not in ("已对账", "部分付款"):
        conn.close()
        raise HTTPException(400, f"发票 {invoice_no} 状态是 {inv['status']}，请先对账通过再付款")
    remaining = float(inv["amount_base"]) - float(inv["paid_amount"] or 0)
    if amount > remaining + 1e-9:
        conn.close()
        raise HTTPException(400, f"付款金额 {amount} 超出未付金额 {round(remaining, 2)}（本位币）")
    pay_no = next_doc_no(conn, "PAY")
    rate = float(inv["exchange_rate"] or 1)
    conn.execute("INSERT INTO payments (payment_no,invoice_no,supplier_code,company_code,currency,exchange_rate,amount,amount_base,payment_date,method,remark,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pay_no, invoice_no, inv["supplier_code"], inv["company_code"], inv["currency"], rate,
                  round(amount / rate, 2) if rate else amount, round(amount, 2),
                  payload.get("payment_date") or TODAY, payload.get("method", "银行转账"), payload.get("remark", ""), now_str()))
    new_paid = round(float(inv["paid_amount"] or 0) + amount, 2)
    new_status = "已付款" if new_paid >= float(inv["amount_base"]) - 0.01 else "部分付款"
    conn.execute("UPDATE supplier_invoices SET paid_amount=?, status=? WHERE invoice_no=?", (new_paid, new_status, invoice_no))
    conn.commit(); conn.close()
    log_audit(user, "付款", "供应商发票", invoice_no, "已付金额", str(inv["paid_amount"] or 0), str(new_paid), f"付款单 {pay_no}，本次 {amount}（本位币）", inv["company_code"])
    return {"ok": True, "payment_no": pay_no, "invoice_no": invoice_no, "paid_amount_base": new_paid, "status": new_status}

@app.get("/api/finance/payables")
def payables_report(supplier_code: Optional[str] = None, company_code: Optional[str] = None,
                    x_user: Optional[str] = Header(None)):
    """应付报表（大白话：每个供应商还欠多少钱、哪些已对账、哪些还没对账）。"""
    user = resolve_user(x_user)
    require_perm(user, "财务", "view")
    headers, data = build_report("payable", {"supplier_code": supplier_code or "", "company_code": company_code or ""})
    return {"headers": headers, "rows": [{"supplier_code": r[0], "invoice_count": r[1], "total_amount_base": r[2],
                                          "paid_amount_base": r[3], "unpaid_amount_base": r[4],
                                          "reconciled_unpaid": r[5], "unreconciled_amount": r[6]} for r in data]}

@app.get("/api/finance/production-cost/{order_no}")
def production_cost(order_no: str, x_user: Optional[str] = Header(None)):
    """生产成本（大白话：这张生产单领了哪些料、花了多少钱；半成品按其自身生产单单独算成本，不重复计算）。"""
    user = resolve_user(x_user)
    require_perm(user, "财务", "view")
    conn = get_conn()
    mo = conn.execute("SELECT * FROM production_orders WHERE order_no=?", (order_no,)).fetchone()
    if not mo:
        conn.close()
        raise HTTPException(404, f"生产单 {order_no} 不存在")
    items = conn.execute("SELECT * FROM production_consumptions WHERE order_no=? ORDER BY id", (order_no,)).fetchall()
    conn.close()
    total = round(sum(float(i["total_cost"]) for i in items), 2)
    return {"order_no": order_no, "product_code": mo["product_code"], "qty": mo["qty"], "status": mo["status"],
            "material_cost": total, "unit_cost": round(total / float(mo["qty"]), 4) if float(mo["qty"]) else 0,
            "items": [row_to_dict(i) for i in items]}

@app.get("/api/finance/production-cost")
def production_cost_list(company_code: Optional[str] = None, x_user: Optional[str] = Header(None)):
    user = resolve_user(x_user)
    require_perm(user, "财务", "view")
    conn = get_conn()
    sql = "SELECT * FROM production_orders WHERE 1=1"; args: List[Any] = []
    if company_code:
        sql += " AND company_code=?"; args.append(company_code)
    sql += " ORDER BY id DESC"
    out = []
    for mo in conn.execute(sql, args).fetchall():
        cost = float(conn.execute("SELECT COALESCE(SUM(total_cost),0) FROM production_consumptions WHERE order_no=?", (mo["order_no"],)).fetchone()[0] or 0)
        out.append({"order_no": mo["order_no"], "product_code": mo["product_code"], "qty": mo["qty"],
                    "status": mo["status"], "material_cost": round(cost, 2)})
    conn.close()
    return out

# ---------- 报表 ----------
@app.get("/api/reports/{kind}")
def get_report(kind: str, company_code: Optional[str] = None, warehouse: Optional[str] = None,
               supplier_code: Optional[str] = None, x_user: Optional[str] = Header(None)):
    """常用报表：库存结存 / 采购汇总 / 生产汇总 / 应付 / 审计日志。"""
    user = resolve_user(x_user)
    require_perm(user, "报表", "view")
    check_scope(user, warehouse=warehouse, company_code=company_code)
    if kind.endswith(".xlsx"):
        kind = kind[:-5]
        headers, data = build_report(kind, {"company_code": company_code or "", "warehouse": warehouse or "",
                                            "supplier_code": supplier_code or ""})
        return _excel_response(rows_to_excel(headers, data, sheet_name=kind[:20]), f"report_{kind}.xlsx")
    headers, data = build_report(kind, {"company_code": company_code or "", "warehouse": warehouse or "",
                                        "supplier_code": supplier_code or ""})
    cols = [h for h in headers]
    rows = []
    for r in data:
        rows.append({cols[i]: r[i] for i in range(len(cols))})
    return {"kind": kind, "headers": headers, "rows": rows, "count": len(rows)}

# ---------- 审计日志 ----------
@app.get("/api/audit-log")
def list_audit_log(user: Optional[str] = None, entity_type: Optional[str] = None, entity_id: Optional[str] = None,
                   action: Optional[str] = None, company_code: Optional[str] = None, limit: int = 200,
                   x_user: Optional[str] = Header(None)):
    """审计日志查询（大白话：谁改了什么，都能按人/单据/动作查到；日志只能追加，不能修改删除）。"""
    cur = resolve_user(x_user)
    require_perm(cur, "报表", "view")
    # P0-4：审计日志只给审计员/系统管理员看（生产员虽然也有“报表 view”，但不该看全公司审计流水）
    _roles = user_roles_of(get_conn(), cur)
    if "系统管理员" not in _roles and "审计员" not in _roles:
        raise HTTPException(403, f"用户 {cur}（角色 {','.join(_roles) or '无'}）没有查看审计日志的权限（仅审计员/系统管理员）")
    conn = get_conn()
    sql = "SELECT * FROM audit_log WHERE 1=1"; args: List[Any] = []
    if user:
        sql += " AND username=?"; args.append(user)
    if entity_type:
        sql += " AND entity_type=?"; args.append(entity_type)
    if entity_id:
        sql += " AND entity_id=?"; args.append(entity_id)
    if action:
        sql += " AND action=?"; args.append(action)
    if company_code:
        sql += " AND company_code=?"; args.append(company_code)
    sql += " ORDER BY id DESC LIMIT ?"; args.append(max(1, min(int(limit), 1000)))
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

@app.get("/api/audit-log/{entity_type}/{entity_id}")
def audit_for_entity(entity_type: str, entity_id: str, x_user: Optional[str] = Header(None)):
    cur = resolve_user(x_user)
    require_perm(cur, "报表", "view")
    conn = get_conn()
    rows = conn.execute("SELECT * FROM audit_log WHERE entity_type=? AND entity_id=? ORDER BY id", (entity_type, entity_id)).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

# ========== 第三阶段：登录 / 智能补货 / 供应商推荐 / AI 助手 ==========
# ---------- 登录（简单版，演示用；正式需换服务端鉴权，见 README） ----------
@app.post("/api/auth/login")
def login(payload: Dict[str, Any]):
    """登录：用户名+密码对上 → 返回 Token（24 小时有效）。默认 admin / admin，首次登录请改密码。"""
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        raise HTTPException(400, "用户名、密码必填")
    conn = get_conn()
    u = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not u or not verify_password(password, u["password_hash"] or ""):
        conn.close()
        raise HTTPException(401, "用户名或密码错误")
    if (u["status"] or "启用") != "启用":
        conn.close()
        raise HTTPException(403, f"用户 {username} 已停用")
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    # P2-8：每次登录顺手清掉已过期的 Token，避免 auth_tokens 表长期堆积
    conn.execute("DELETE FROM auth_tokens WHERE expires_at<=?", (now_str(),))
    conn.execute("INSERT INTO auth_tokens (token_hash,username,created_at,expires_at) VALUES (?,?,?,?)",
                 (token_hash(token), username, now_str(), expires))
    conn.commit()
    roles = user_roles_of(conn, username)
    conn.close()
    log_audit(username, "登录", "用户", username, "", "", "", "登录成功")
    return {"ok": True, "token": token, "username": username, "name": u["name"],
            "roles": roles, "expires_at": expires,
            "must_change_password": int(u["password_changed"] or 0) == 0}

@app.post("/api/auth/logout")
def logout(x_user: Optional[str] = Header(None), authorization: Optional[str] = Header(None)):
    """退出登录：作废当前 Token（大白话：把这张通行证注销掉）。"""
    auth = authorization or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if token:
        conn = get_conn()
        conn.execute("DELETE FROM auth_tokens WHERE token_hash=?", (token_hash(token),))
        conn.commit(); conn.close()
    return {"ok": True, "user": resolve_user(x_user)}

@app.post("/api/auth/change-password")
def change_password(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """改密码：必须带 Token + 旧密码，新密码至少 6 位。改完后旧 Token 全部作废，需重新登录。"""
    user = resolve_user(x_user)
    old = str(payload.get("old_password") or "")
    new = str(payload.get("new_password") or "")
    if len(new) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    conn = get_conn()
    u = conn.execute("SELECT * FROM users WHERE username=?", (user,)).fetchone()
    if not u or not verify_password(old, u["password_hash"] or ""):
        conn.close()
        raise HTTPException(401, "旧密码错误")
    conn.execute("UPDATE users SET password_hash=?, password_changed=1 WHERE username=?", (hash_password(new), user))
    conn.execute("DELETE FROM auth_tokens WHERE username=?", (user,))
    conn.commit(); conn.close()
    log_audit(user, "改密码", "用户", user, "密码", "***", "***", "用户自行改密码")
    return {"ok": True, "user": user, "msg": "密码已修改，请用新密码重新登录"}

@app.get("/api/auth/me")
def auth_me(x_user: Optional[str] = Header(None)):
    """查当前登录人（大白话：我是谁、有什么角色）。"""
    user = resolve_user(x_user)
    conn = get_conn()
    u = conn.execute("SELECT * FROM users WHERE username=?", (user,)).fetchone()
    roles = user_roles_of(conn, user) if u else []
    conn.close()
    if not u:
        raise HTTPException(401, "未登录")
    return {"username": user, "name": u["name"], "roles": roles,
            "must_change_password": int(u["password_changed"] or 0) == 0}

# ---------- 智能补货建议 ----------
# 公式来源（先查再做，只学思路、自己重写，未复制 GPL 代码）：
# - 再订货点（ROP）= 日均用量 × 提前期 + 安全库存：ERP/库存管理通用公式，
#   见 https://www.knowledgeerp.com/content/blog/reorder-points.htm
#   （reorder point = average daily usage × lead time + safety stock）
#   及 https://beefed.ai/en/safety-stock-reorder-policies-mrp（ROP = d_avg × L + SS）
# - InvenTree/ERPNext 思路：低于再订货点就触发补货；建议量按“补到目标库存”算，
#   目标库存 = 安全库存 + 提前期用量（即 ROP），本系统为留缓冲取目标 = 安全库存×2 + 提前期用量，
#   建议补货 = max(0, 目标库存 − 可用量)。目标里多留一份安全库存，是演示版的保守策略，
#   可用量已含在途、已扣已分配与冻结（沿用本系统 get_available 公式）。
# - 日均用量 = 近 N 天（默认 30 天）生产领料总量 ÷ N，取自 production_consumptions（真实领料记录）；
#   无领料记录时日均用量为 0，建议补货只按安全库存算，并在返回里标注“无历史用量”。
def daily_avg_usage(conn, part_code: str, days: int = 30, warehouse: Optional[str] = None,
                    company_code: Optional[str] = None) -> float:
    """近 N 天日均用量（大白话：这个零件平均每天生产领走多少）。无记录返回 0。"""
    days = max(1, min(int(days or 30), 365))
    cutoff = (date.today() - timedelta(days=days)).isoformat() + " 00:00:00"
    sql = "SELECT COALESCE(SUM(qty),0) FROM production_consumptions WHERE part_code=? AND created_at>=?"
    args: List[Any] = [part_code, cutoff]
    if warehouse:
        sql += " AND warehouse=?"; args.append(warehouse)
    if company_code:
        sql += " AND company_code=?"; args.append(company_code)
    total = float(conn.execute(sql, args).fetchone()[0] or 0)
    return total / days

def replenishment_for_part(conn, part, days: int = 30, warehouse: Optional[str] = None,
                            company_code: Optional[str] = None) -> Dict[str, Any]:
    """对一个零件算补货建议（公式见本节顶部注释，数字全部可解释、可复核）。"""
    code = part["code"]
    safety = float(part["safety_stock"] or 0)
    lead = int(part["lead_time_days"] or 7)
    daily = daily_avg_usage(conn, code, days, warehouse, company_code)
    lead_demand = round(daily * lead, 2)  # 提前期用量：等货这几天大概要用多少
    rop = round(lead_demand + safety, 2)  # 再订货点：库存掉到这个数就该下单
    target = round(safety * 2 + lead_demand, 2)  # 目标库存：补货后希望达到的数（多留一份安全库存）
    avail = get_available(code, warehouse, company_code)
    suggested = max(0.0, target - avail)
    suggested = float(math.ceil(suggested)) if suggested > 0 else 0.0  # 向上取整：宁可多补 1 个，不少补
    # 需要补货 = 可用量掉到再订货点以下，且算出来的建议量 > 0（零库存+零需求+零安全库存的成品不算需要补货，避免误报）
    rec, reason, _eta = recommend_supplier_for(conn, code)
    unit_price = float(rec["price"]) if rec is not None else float(part["price"] or 0)
    tax = float(rec["tax_rate"]) if rec is not None else float(part["tax_rate"] or 0.13)
    return {"part_code": code, "part_name": part["name"], "unit": part["unit"],
            "available": round(avail, 2), "daily_avg_usage": round(daily, 2),
            "usage_days": days, "has_usage_history": daily > 0,
            "lead_time_days": lead, "safety_stock": safety,
            "lead_time_demand": lead_demand, "reorder_point": rop,
            "target_stock": target, "suggested_qty": suggested,
            "need_reorder": bool(avail <= rop and suggested > 0),
            "recommended_supplier": rec["supplier_code"] if rec is not None else "",
            "recommended_supplier_name": rec["supplier_name"] if rec is not None else "",
            "recommend_reason": reason,
            "unit_price": unit_price, "tax_rate": tax,
            "estimated_cost_with_tax": round(suggested * unit_price * (1 + tax), 2),
            "formula": f"日均用量={daily:.2f}（近{days}天领料÷{days}）；再订货点={daily:.2f}×{lead}+{safety}={rop}；"
                       f"目标库存={safety}×2+{daily:.2f}×{lead}={target}；建议补货=max(0,{target}−可用量{avail:.2f})={suggested}"}

@app.get("/api/replenishment")
def get_replenishment(days: int = 30, warehouse: Optional[str] = None, company_code: Optional[str] = None,
                      only_needed: bool = False, x_user: Optional[str] = Header(None)):
    """智能补货建议列表（大白话：哪些料该补了、补多少、找谁买，一张表看清）。
    默认只看当前公司/仓库范围内的零件；only_needed=1 只返回需要补货的。"""
    user = resolve_user(x_user)
    require_perm(user, "库存", "view")
    check_scope(user, warehouse=warehouse, company_code=company_code)
    conn = get_conn()
    sql = "SELECT * FROM parts WHERE 1=1"; args: List[Any] = []
    if company_code:
        sql += " AND company_code=?"; args.append(company_code)
    sql += " ORDER BY code"
    parts = conn.execute(sql, args).fetchall()
    out = []
    for p in parts:
        item = replenishment_for_part(conn, p, days, warehouse, company_code)
        if only_needed and not item["need_reorder"]:
            continue
        out.append(item)
    conn.close()
    out.sort(key=lambda x: (not x["need_reorder"], -x["suggested_qty"]))
    return {"days": days, "warehouse": warehouse or "", "company_code": company_code or "",
            "count": len(out), "need_reorder_count": sum(1 for x in out if x["need_reorder"]), "items": out}

@app.post("/api/replenishment/generate-purchase-orders")
def replenishment_generate(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """补货建议一键生成采购单：按推荐供应商分组，同一供应商合并成一张采购单（和 MRP 生成同款逻辑）。"""
    user = resolve_user(x_user)
    require_perm(user, "采购", "create")
    days = int(payload.get("days") or 30)
    warehouse = payload.get("warehouse") or "主仓"
    company_code = payload.get("company_code") or "COMP-001"
    check_scope(user, warehouse=warehouse, company_code=company_code)
    conn = get_conn()
    parts = conn.execute("SELECT * FROM parts WHERE company_code=? ORDER BY code", (company_code,)).fetchall()
    groups: Dict[str, List] = {}
    skipped = []
    for p in parts:
        item = replenishment_for_part(conn, p, days, warehouse, company_code)
        if item["suggested_qty"] <= 0 or not item["need_reorder"]:
            continue
        sup = item["recommended_supplier"] or ""
        if not sup:
            skipped.append({"part_code": p["code"], "reason": "没有可用供应商报价（或供应商全被拉黑）"})
            continue
        groups.setdefault(sup, []).append(item)
    po_nos = []
    for sup, items in groups.items():
        po_no = next_doc_no(conn, "PO")
        subtotal = sum(it["suggested_qty"] * it["unit_price"] for it in items)
        tax_total = sum(it["suggested_qty"] * it["unit_price"] * it["tax_rate"] for it in items)
        total = subtotal + tax_total
        conn.execute("INSERT INTO purchase_orders (order_no,supplier_code,status,due_date,total_amount,created_at,company_code,warehouse,currency,exchange_rate,total_amount_base,subtotal_amount,tax_amount) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (po_no, sup, "已下单", TODAY, round(total, 2), now_str(), company_code, warehouse, "CNY", 1, round(total, 2), round(subtotal, 2), round(tax_total, 2)))
        for it in items:
            conn.execute("INSERT INTO purchase_order_items (order_no,part_code,qty,price,received_qty,tax_rate,warehouse,company_code) VALUES (?,?,?,?,?,?,?,?)",
                         (po_no, it["part_code"], it["suggested_qty"], it["unit_price"], 0, it["tax_rate"], warehouse, company_code))
        po_nos.append(po_no)
    conn.commit(); conn.close()
    log_audit(user, "补货生成采购单", "采购单", ",".join(po_nos), "", "", "", f"补货建议生成 {len(po_nos)} 张采购单", company_code)
    return {"ok": True, "purchase_orders": po_nos, "skipped": skipped}

# ---------- 供应商自动推荐 ----------
def recommend_supplier_for(conn, part_code: str, need_date: Optional[str] = None,
                            prefer: str = "balanced"):
    """自动推荐供应商（大白话：买这个零件找谁，系统给个理由）。
    prefer：balanced（均衡，默认）/ cheapest（只看最便宜）/ fastest（只看最快）。
    均衡逻辑：最便宜的能在需求日前到货 → 推最便宜；赶不上 → 在能赶上的里面推最便宜的；
    谁都赶不上 → 推最快的并说明。黑名单供应商始终跳过。返回 (行, 理由, 预计到货日)。"""
    rows = conn.execute("""
        SELECT sp.*, s.status as supplier_status, s.name as supplier_name
        FROM supplier_parts sp LEFT JOIN suppliers s ON s.code=sp.supplier_code
        WHERE sp.part_code=? ORDER BY sp.price ASC
    """, (part_code,)).fetchall()
    usable = [r for r in rows if (r["supplier_status"] or "合格") != "黑名单"]
    if not usable:
        return None, "没有可用供应商报价（或供应商全被拉黑）", ""
    cheapest = usable[0]
    fastest = min(usable, key=lambda r: (r["lead_time_days"] if r["lead_time_days"] is not None else 999))
    if prefer == "cheapest":
        eta = (date.today() + timedelta(days=int(cheapest["lead_time_days"] or 7))).isoformat()
        return cheapest, f"按最便宜推荐：{cheapest['supplier_name']} 单价 {cheapest['price']} 最低", eta
    if prefer == "fastest":
        eta = (date.today() + timedelta(days=int(fastest["lead_time_days"] or 7))).isoformat()
        return fastest, f"按交期最短推荐：{fastest['supplier_name']} 交期 {fastest['lead_time_days']} 天最短", eta
    # 均衡：看需求日
    need = parse_date(need_date) if need_date else None
    if not need:
        eta = (date.today() + timedelta(days=int(cheapest["lead_time_days"] or 7))).isoformat()
        return cheapest, f"均衡推荐（未给需求日）：{cheapest['supplier_name']} 单价 {cheapest['price']} 最低", eta
    def _eta(r):
        return date.today() + timedelta(days=int(r["lead_time_days"] or 7))
    if _eta(cheapest) <= need:
        return cheapest, f"均衡推荐：{cheapest['supplier_name']} 最便宜且能在需求日 {need.isoformat()} 前到货（预计 {_eta(cheapest).isoformat()}）", _eta(cheapest).isoformat()
    on_time = [r for r in usable if _eta(r) <= need]
    if on_time:
        pick = min(on_time, key=lambda r: float(r["price"] or 0))
        return pick, f"均衡推荐：最便宜的赶不上需求日 {need.isoformat()}，在能按时到货的里面选最便宜的 {pick['supplier_name']}", _eta(pick).isoformat()
    return fastest, f"均衡推荐：所有供应商都赶不上需求日 {need.isoformat()}，选交期最短的 {fastest['supplier_name']}（预计 {_eta(fastest).isoformat()}），请确认是否接受延期", _eta(fastest).isoformat()

@app.post("/api/suppliers/recommend")
def recommend_supplier(payload: Dict[str, Any]):
    """供应商自动推荐接口（大白话：告诉系统要买啥、要多少、哪天要，系统说找谁买、为什么）。"""
    part_code = str(payload.get("part_code") or "").strip()
    if not part_code:
        raise HTTPException(400, "part_code 必填")
    qty = float(payload.get("qty") or 0)
    conn = get_conn()
    rows = conn.execute("""
        SELECT sp.*, s.name as supplier_name, s.status as supplier_status
        FROM supplier_parts sp LEFT JOIN suppliers s ON s.code=sp.supplier_code
        WHERE sp.part_code=? ORDER BY sp.price ASC
    """, (part_code,)).fetchall()
    if not rows:
        conn.close()
        raise HTTPException(404, f"零件 {part_code} 没有任何供应商报价")
    rec, reason, eta = recommend_supplier_for(conn, part_code, payload.get("need_date"), payload.get("prefer") or "balanced")
    conn.close()
    options = []
    for r in rows:
        d = row_to_dict(r)
        d["skipped_blacklist"] = ((r["supplier_status"] or "合格") == "黑名单")
        d["eta_date"] = (date.today() + timedelta(days=int(r["lead_time_days"] or 7))).isoformat()
        d["total_cost"] = round(qty * float(r["price"] or 0), 2) if qty else None
        d["is_recommended"] = bool(rec is not None and r["supplier_code"] == rec["supplier_code"])
        options.append(d)
    return {"part_code": part_code, "qty": qty, "need_date": payload.get("need_date") or "",
            "prefer": payload.get("prefer") or "balanced",
            "recommended_supplier": rec["supplier_code"] if rec is not None else "",
            "recommended_supplier_name": rec["supplier_name"] if rec is not None else "",
            "recommend_reason": reason, "eta_date": eta,
            "unit_price": float(rec["price"]) if rec is not None else None,
            "total_cost": round(qty * float(rec["price"] or 0), 2) if (rec is not None and qty) else None,
            "options": options}

# ---------- AI 助手：自然语言查库存 / 查订单（规则解析，不用大模型猜） ----------
def _assistant_find_part(conn, text: str):
    """在问题里找零件：优先匹配编码，其次匹配名称（取最长匹配，避免“灯”误匹配“小夜灯”）。"""
    upper = text.upper()
    parts = conn.execute("SELECT * FROM parts ORDER BY LENGTH(name) DESC, code").fetchall()
    for p in parts:
        if p["code"].upper() in upper:
            return p
    best = None
    for p in parts:
        if p["name"] and p["name"] in text:
            if best is None or len(p["name"]) > len(best["name"]):
                best = p
    return best

def _assistant_find_order_no(text: str):
    """在问题里找单号：PO/MO/SO/PR/PAY/INV + 横杠 + 编号（如 PO-2026-0001、PO-DEMO-001）。"""
    import re
    m = re.search(r"\b((?:PO|MO|SO|PR|PAY|INV)-[A-Z0-9]+(?:-[A-Z0-9]+)*)\b", text.upper())
    return m.group(1) if m else None

@app.post("/api/assistant/query")
def assistant_query(payload: Dict[str, Any], x_user: Optional[str] = Header(None)):
    """助手查询：POST {text:"查外壳还剩多少"}。规则解析（提取单号/零件名/关键词），查不到就给提示，不瞎编。"""
    user = resolve_user(x_user)
    require_perm(user, "库存", "view")
    text = str(payload.get("text") or "").strip()
    if not text:
        return {"intent": "空问题", "answer": "请问我点什么？比如：查外壳还剩多少 / 采购单 PO-2026-0001 到哪了 / 哪些料需要补货",
                "data": {}, "suggestions": ["查外壳还剩多少", "哪些料需要补货", "采购单 PO-2026-0001 到哪了"]}
    conn = get_conn()
    order_no = _assistant_find_order_no(text)
    part = _assistant_find_part(conn, text)
    # 1) 带单号：优先查单
    if order_no:
        prefix = order_no.split("-")[0]
        table = {"PO": ("purchase_orders", "order_no"), "MO": ("production_orders", "order_no"),
                 "SO": ("sales_orders", "order_no"), "PR": ("purchase_requisitions", "req_no")}.get(prefix)
        if table:
            tbl, col = table
            row = conn.execute(f"SELECT * FROM {tbl} WHERE {col}=?", (order_no,)).fetchone()
            conn.close()
            if not row:
                return {"intent": "查订单", "answer": f"没找到单号 {order_no}，请检查单号是否写对（格式如 PO-2026-0001）。",
                        "data": {"order_no": order_no}, "suggestions": ["查外壳还剩多少", "哪些料需要补货"]}
            d = row_to_dict(row)
            if prefix == "PO":
                conn2 = get_conn()
                items = conn2.execute("SELECT * FROM purchase_order_items WHERE order_no=?", (order_no,)).fetchall()
                conn2.close()
                recv = sum(float(x["received_qty"] or 0) for x in items)
                total = sum(float(x["qty"] or 0) for x in items)
                ans = (f"采购单 {order_no}：供应商 {d.get('supplier_code')}，状态【{d.get('status')}】，"
                       f"交期 {d.get('due_date') or '未填'}，含税总额 {d.get('total_amount')}，"
                       f"已到货 {recv:g}/{total:g}。")
                return {"intent": "查采购单", "answer": ans, "data": {"order": d, "items": [row_to_dict(x) for x in items]}, "suggestions": []}
            if prefix == "MO":
                ans = f"生产单 {order_no}：生产 {d.get('product_code')} × {d.get('qty')}，状态【{d.get('status')}】，计划 {d.get('planned_start') or ''} ~ {d.get('planned_end') or ''}。"
                return {"intent": "查生产单", "answer": ans, "data": {"order": d}, "suggestions": []}
            if prefix == "SO":
                ans = f"销售单 {order_no}：客户 {d.get('customer')}，{d.get('product_code')} × {d.get('qty')}，交期 {d.get('due_date') or '未填'}，状态【{d.get('status')}】。"
                return {"intent": "查销售单", "answer": ans, "data": {"order": d}, "suggestions": []}
            ans = f"采购申请 {order_no}：状态【{d.get('status')}】，申请人 {d.get('applicant') or ''}，原因 {d.get('reason') or ''}。"
            return {"intent": "查采购申请", "answer": ans, "data": {"order": d}, "suggestions": []}
        conn.close()
        return {"intent": "查订单", "answer": f"单号 {order_no} 的类型我还不支持查询（只支持 PO/MO/SO/PR 开头的单号），请检查单号。",
                "data": {"order_no": order_no}, "suggestions": ["查外壳还剩多少", "哪些料需要补货"]}
    # 2) 补货 / 缺料
    if any(k in text for k in ("补货", "缺料", "缺什么", "该买", "建议买", "再订")):
        if part:
            item = replenishment_for_part(conn, part, 30)
            conn.close()
            ans = (f"{item['part_name']}（{item['part_code']}）：可用量 {item['available']}，再订货点 {item['reorder_point']}，"
                   f"建议补货 {item['suggested_qty']:g}（{item['formula']}）。"
                   + (f"推荐供应商：{item['recommended_supplier_name']}。" if item["recommended_supplier_name"] else "暂无可用供应商。"))
            return {"intent": "查补货", "answer": ans, "data": item, "suggestions": []}
        items = []
        for p in conn.execute("SELECT * FROM parts ORDER BY code").fetchall():
            it = replenishment_for_part(conn, p, 30)
            if it["need_reorder"]:
                items.append(it)
        conn.close()
        if not items:
            return {"intent": "查补货", "answer": "目前没有需要补货的零件（可用量都高于再订货点）。", "data": {"items": []}, "suggestions": []}
        names = "、".join(f"{x['part_name']}建议补{x['suggested_qty']:g}" for x in items[:8])
        return {"intent": "查补货", "answer": f"需要补货的有 {len(items)} 种：{names}。", "data": {"items": items}, "suggestions": []}
    # 3) 比价 / 供应商
    if any(k in text for k in ("比价", "哪个便宜", "哪家便宜", "供应商", "找谁买", "推荐")) and part:
        rec, reason, eta = recommend_supplier_for(conn, part["code"])
        conn.close()
        if not rec:
            return {"intent": "查比价", "answer": f"{part['name']}（{part['code']}）没有可用供应商报价。", "data": {}, "suggestions": []}
        return {"intent": "查比价", "answer": f"{part['name']}（{part['code']}）：{reason}，预计到货 {eta}。",
                "data": {"recommended": row_to_dict(rec), "reason": reason, "eta_date": eta}, "suggestions": []}
    # 4) 查库存（默认意图）：有零件 → 该零件库存；无零件 → 缺料总览
    if any(k in text for k in ("库存", "还剩", "剩下", "多少", "有货", "缺货", "查")) or part:
        if part:
            code = part["code"]
            onhand = get_onhand(code); in_transit = get_in_transit(code)
            avail = get_available(code); frozen = get_frozen(code)
            st = stock_status(avail, float(part["safety_stock"] or 0), float(part["max_stock"] or 0))
            conn.close()
            ans = (f"{part['name']}（{code}）：现有 {onhand:g}，在途 {in_transit:g}，冻结 {frozen:g}，"
                   f"可用量 {avail:g}（可用 = 现有 + 在途 − 已分配 − 冻结），安全库存 {part['safety_stock']:g}，状态【{st}】。")
            return {"intent": "查库存", "answer": ans,
                    "data": {"part_code": code, "part_name": part["name"], "onhand": onhand,
                             "in_transit": in_transit, "frozen": frozen, "available": avail, "status": st}, "suggestions": []}
        conn.close()
        return {"intent": "查库存", "answer": "没找到你说的零件，试试这样问：查外壳还剩多少（零件名要和系统里的名称一致，如 外壳 / LED灯珠 / 电池）。",
                "data": {}, "suggestions": ["查外壳还剩多少", "查电池还剩多少", "哪些料需要补货", "采购单 PO-2026-0001 到哪了"]}
    conn.close()
    return {"intent": "未知", "answer": "没找到，试试这样问：查外壳还剩多少 / 哪些料需要补货 / 外壳找谁买最便宜 / 采购单 PO-2026-0001 到哪了。",
            "data": {}, "suggestions": ["查外壳还剩多少", "哪些料需要补货", "外壳找谁买最便宜", "采购单 PO-2026-0001 到哪了"]}

@app.get("/api/assistant/examples")
def assistant_examples():
    """助手示例问题（大白话：不知道问啥时，点一个试试）。"""
    return {"examples": ["查外壳还剩多少", "查电池还剩多少", "哪些料需要补货", "外壳找谁买最便宜",
                         "采购单 PO-2026-0001 到哪了", "生产单 MO-2026-0001 到哪了"]}

@app.get("/health")
def health():
    # 必须放在静态文件挂载之前，否则 /health 会被静态文件路由吞掉返回 404
    # app_id/data_dir 供一键启动入口严格识别“这是不是 PMC 的服务、是不是这套数据库的实例”
    return {"ok": True, "app_id": "pmc-phase3", "msg": "PMC 第三阶段运行正常",
            "data_dir": os.path.dirname(os.path.abspath(DB_PATH))}

# ---------- 静态前端 ----------
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")
if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


