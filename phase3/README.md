# PMC 系统 第三阶段（智能 + 落地收尾）

> 由第二阶段（`~/workspace/pmc/phase2/`）复制扩展，第二阶段已验证的端到端流程保持兼容。
> 第二阶段原文件、第一阶段、MVP 均未改动。
> 状态：第三阶段已按方案第 5.3 节完成简化版 + 落地收尾，自动验证全部通过（见 `验证报告.md`）。

## 怎么跑

### Windows（推荐：双击 `启动PMC系统.vbs`）

1. 双击 `phase3\启动PMC系统.vbs`（或用桌面上的「PMC系统」快捷方式）：自动启动服务，等就绪后让你选浏览器并打开页面。
2. 浏览器地址 http://127.0.0.1:8000 ，账号 `admin / admin`，首次登录会要求改密码。启动日志见 `phase3\logs\startup.log`。

### 命令行（Windows / macOS / Linux）

```bat
:: Windows
cd /d 你的路径\phase3
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe backend\test_phase3.py   :: 自动验证，预期：全部第三阶段验证通过 ✅（共 133 项 ✅）
.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

```bash
# macOS / Linux
cd ~/workspace/pmc/phase3
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python backend/test_phase3.py
.venv/bin/uvicorn backend.app:app --reload --port 8000
```

- 端口 8000 被占用时换 `--port 8001`（或 8002/8003），浏览器地址跟着改；四个阶段同时在本机跑就分别用 8000/8001/8002/8003，一次只跑一个阶段也可以都占 8000。
- 详细操作步骤见《怎么用.md》（含模拟表单测试全流程）。

Docker：

```bash
docker compose up --build
# 打开 http://localhost:8000
```

详细操作步骤见《怎么用.md》（含模拟表单测试全流程）。

## 登录（第三阶段新增，替代第二阶段的演示身份 X-User）

- 登录接口：`POST /api/auth/login`，传 `{username, password}`，返回 Token（24 小时有效）。
- 之后所有 `/api/*` 接口必须带请求头 `Authorization: Bearer <Token>`，不带返回 401。
- 第二阶段的 `X-User` 直传已废弃：只传 `X-User` 不带 Token 同样返回 401。
- 默认账号：`admin / admin`；其余种子用户默认密码 = 用户名（如 `buyer / buyer`、`manager / manager`、`keeper / keeper`、`worker / worker`、`finance / finance`、`auditor / auditor`、`director / director`），首次登录请改密码（页面上有“改密码”，或调 `POST /api/auth/change-password`，新密码至少 6 位，改完旧 Token 全部作废）。
- 密码存在 SQLite 用户表的 `password_hash` 列，用 PBKDF2（20 万次搅拌）+ 随机盐算出的哈希，**不是明文**；Token 在库里只存哈希，不存明文。
- ⚠️ **演示版，正式需换服务端鉴权**：当前登录是“单服务 + Token 查库”的简单实现，没有 HTTPS 强制、登录限流、Token 刷新、SSO（单点登录）等生产能力。正式上线必须换成真正的服务端鉴权（例如 JWT + 密钥轮换 + HTTPS + 登录失败锁定），不要直接把演示登录暴露到公网。

## 第三阶段做了什么（方案第 5.3 节，简化版）

### 智能补货建议（`GET /api/replenishment`，看板第五块）

公式（每个零件返回里都有完整公式文字，可复核）：

```
日均用量 = 近 30 天生产领料总量 ÷ 30（取真实领料记录 production_consumptions；无记录时为 0）
再订货点 = 日均用量 × 提前期 + 安全库存
目标库存 = 安全库存 × 2 + 日均用量 × 提前期（多留一份安全库存，演示版保守策略）
建议补货 = max(0, 目标库存 − 可用量)，向上取整；可用量 ≤ 再订货点 且 建议补货 > 0 时才标记“需要补货”
```

公式来源（只学思路、自己重写，未复制 GPL 代码）：再订货点通用公式见
https://www.knowledgeerp.com/content/blog/reorder-points.htm（reorder point = average daily usage × lead time + safety stock）
及 https://beefed.ai/en/safety-stock-reorder-policies-mrp（ROP = d_avg × L + SS）；
“低于再订货点触发补货、补到目标库存”是 InvenTree / ERPNext 的通用补货思路。

- `POST /api/replenishment/generate-purchase-orders`：补货建议一键按推荐供应商分组生成采购单。

### 供应商自动比价 + 自动推荐

- `GET /api/suppliers/compare/{零件编码}?need_date=YYYY-MM-DD&prefer=balanced|cheapest|fastest`：比价表 + 推荐供应商 + 推荐理由 + 预计到货日。
- `POST /api/suppliers/recommend`：`{part_code, qty, need_date, prefer}`，返回推荐供应商、理由、预计到货日、各供应商总价。
- 推荐逻辑（均衡，默认）：最便宜的能在需求日前到货 → 推最便宜；赶不上 → 在能按时到货的里面推最便宜的；谁都赶不上 → 推交期最短的并说明“请确认是否接受延期”。黑名单供应商始终跳过。
- MRP 生成采购单（`/api/mrp/generate`、`/api/purchase/from-mrp`）已改为**按推荐供应商分组**，不再按零件默认供应商分组。实测：同一批 MRP 需求，旧分组含税总额 1380.86，推荐分组 1316.45（SUP-001 组 1058.81 + SUP-002 组 257.64）。

### 语音 / AI 助手查库存、查订单（`POST /api/assistant/query`）

- 简单自然语言查询接口，**关键词匹配 + 规则解析，不用大模型猜**：从问题里提取单号（`PO-2026-0001` 这类）/ 零件名（编码或名称，最长匹配）/ 关键词（库存、补货、比价、采购单…），再查库回答。
- 例子：`{"text":"查外壳还剩多少"}`、`{"text":"哪些料需要补货"}`、`{"text":"外壳找谁买最便宜"}`、`{"text":"采购单 PO-2026-0001 到哪了"}`。
- 查不到就返回“没找到，试试…” + 示例问题，不瞎编。示例见 `GET /api/assistant/examples`。
- 前端顶部有助手输入框，输入问题点“问助手”即可。

### 移动端适配（前端）

- `<meta name="viewport">` + CSS `@media (max-width:640px)`：小屏单列、按钮撑满宽度且最小高度 44px（手指好点）、表格包在可横滑容器里。
- 手机浏览器打开同样地址即可登录、看板、问助手、导入 Excel。

### 落地收尾

- 登录（见上）、SQLite 保持不变、Excel 导入支持 `?sheet=工作表名`（已填好文件有 4 页时点名读哪页）。
- 模拟表单见 `模拟表单/`（3 个模板 + 1 份已填好数据），用法见《怎么用.md》。
- 零件导入新增“是否采购件”列（1=外购、0=自制），成品/半成品填 0；另修复一个导入 bug：`0 or ""` 会把 0 吞掉，已改为显式判空。

## 中国市场适配

中文界面 / 中文注释 + 大白话解释；税率 13%、含税 / 未税 / 税额分开算；库位、批次、五表一字典思路；Excel / WPS 灵活映射（.xlsx，表头别名）。

## 怎么从 SQLite 切到 PostgreSQL（以后要切时照着做）

当前仍用 SQLite（单文件，`backend/pmc_phase3.db`，首次启动自动建表 + 种子数据）。切 PostgreSQL 的改动点：

1. 安装驱动：`pip install psycopg2-binary`，用环境变量配连接串，如 `PMC_DB_URL=postgresql://用户:密码@主机:5432/pmc`。
2. `get_conn()` 改成按 `PMC_DB_URL` 连 PostgreSQL（没配就继续用 SQLite，本地开发不受影响）。
3. 占位符：SQLite 用 `?`，PostgreSQL 用 `%s`（把 SQL 里的 `?` 批量换成 `%s`；或用 SQLAlchemy 之类的库统一处理）。
4. 建表语句：`INTEGER PRIMARY KEY AUTOINCREMENT` → `SERIAL PRIMARY KEY`；`INSERT ... ON CONFLICT(...) DO UPDATE` 在 PostgreSQL 里原生支持，写法基本不用动；`TEXT`/`REAL` 类型 PostgreSQL 都有。
5. 数据迁移：先用 SQLite 的 `.dump` 或写个小脚本把现有数据按表导出，再导入 PostgreSQL；切完后跑一遍 `backend/test_phase3.py`（把测试的 `DB_PATH` 换成 PostgreSQL 连接）确认 133 项全过。
6. 生产部署建议：PostgreSQL + 定期备份 + 连接池 + Redis 缓存（方案第 3.2 节），本阶段演示版不做。

## 业务决策记录（用户 2026-10-20 定，第四轮迭代落地）

| 编号 | 问题 | 决定 | 落地 |
|---|---|---|---|
| P1-5 | 仓库主数据 | 选 A：每个仓库设默认库位；入库/领料可手填库位和批次，不填用仓库默认库位＋当天日期批次 | `warehouses.default_location`（种子：主仓=A区-01、分仓=B区-01、成品仓=C区-01、上海仓=D区-01）；`PUT /api/warehouses/{编码}` 可改；批次默认 `BATCH-YYYYMMDD`；不再用写死 A区-01 / BATCH-20261019 兜底 |
| P1-9 | 服务端强制改密 | 选 B：保持现状，只提示改密码，不做服务端强制拦截。**此项已按用户决定关闭，不改代码** | 登录仍返回 `must_change_password` 提示，前端仍有改密卡片；服务端不拦截未改密用户调接口 |
| P2-5 | 入库/出库表 | 选 A：按拟定表头做入库/出库 Excel 导出＋字段字典 | `GET /api/excel/export/stock_in`、`GET /api/excel/export/stock_out`（表头 15 列：单号、日期、类型、仓库、库位、零件编码、零件名称、规格型号、批次、数量、单价、金额、经办人、关联单号、备注）；`stock_ledger` 补记库位/经办人/单价；字段字典见 `字段字典.md` |
| P2-6 4a | 领料部门/用途 | 领用部门＋用途，选填 | 领料明细表 `production_consumptions` 加 `dept`、`purpose`；领料接口、明细接口、前台领料弹窗均支持 |
| P2-6 4b | 在制口径 | 在制（正在生产、还没完工）不算可用库存 | 可用量公式本来就不含在制（现有+在途−已分配−冻结），无需改公式，已在 `字段字典.md` §6 说明 + 测试验证 |
| P2-6 4c | 齐套 | 按生产单算到齐百分比＋齐套看板 | `GET /api/production/{单号}/kitting`；看板第 6 块“齐套看板”；到齐率=Σmin(本单可用,需求)÷Σ需求×100%；口径见 `字段字典.md` §5 |

## 文件清单

- `backend/app.py`：后端全部逻辑（FastAPI + SQLite，中文注释 + 大白话解释）
- `backend/test_phase3.py`：本阶段自动验证脚本（133 项检查）
- `frontend/index.html`：中文网页界面（12 个页签：看板 / 零件 / 库存 / 采购 / 生产 / 销售 / MRP / 补货 / 报表 / 审计 / 导入导出 / 助手查询；响应式，手机可用）
- `backend/test_fixes.py`：本轮修复的回归测试（26 项：锁泄漏、越权、MRP 口径、并发取号、单事务、Excel 部分列导入、报表 xlsx、前端 12 页签等）
- `backend/test_p2_round2.py`：第四轮迭代回归测试（仓库默认库位/日期批次、出入库表导出、领料部门用途、在制口径、齐套接口/看板）
- `字段字典.md`：入库表/出库表 15 字段说明、新表字段说明、齐套/在制口径、业务决策记录
- `模拟表单/`：3 个 Excel 模板 + 1 份已填好模拟数据（见《怎么用.md》）
- `怎么用.md`：从启动到完整流程的操作指南（中文 + 大白话）
- `验证报告.md`：验证步骤、结果、参考来源、未做 / 已知限制
- `requirements.txt` / `Dockerfile` / `docker-compose.yml`
- `README-MVP.md`：MVP 原 README（参考用）

## 怎么把本地改动同步到 GitHub（P2-2）

本地目录不是 git 仓库时，三种办法任选：

1. **网页上传（最省事，适合少量文件）**：打开 https://github.com/ZZmkk88/pmc-system → 进入对应目录 → `Add file` → `Upload files` → 选文件 → `Commit changes`。
2. **命令行（推荐，能保留历史）**：
   ```bat
   cd /d 你的路径\pmc-system-main
   git init
   git add .
   git commit -m "第三阶段：修复 P0/P1 问题 + 前端做单页面 + 文档"
   git branch -M main
   git remote add origin https://github.com/ZZmkk88/pmc-system.git
   git push -u origin main --force
   ```
   （首次 push 会要求登录 GitHub；`--force` 只在确认远端内容可被本地覆盖时用，平时去掉。）
3. **直接改仓库副本**：先把 `https://github.com/ZZmkk88/pmc-system.git` clone 到本地，在 clone 出来的目录里改，再 `git add/commit/push`。

注意：不要提交 `backend/*.db`、`.venv/`、`__pycache__/`（`.gitignore` 已忽略）。

