# PMC 系统 第二阶段（大中型公司必须）

> 由第一阶段（`~/workspace/pmc/phase1/`）复制扩展，第一阶段已验证的端到端流程保持兼容。
> 第一阶段原文件、MVP（`~/workspace/pmc/mvp/`）均未改动。
> 状态：第二阶段已按方案第 5.2 节补齐，自动验证全部通过（见 `验证报告.md`）。

## 怎么跑

```bash
cd ~/workspace/pmc/phase2
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python backend/test_phase2.py   # 自动验证，预期：全部第二阶段验证通过 ✅（共 176 项 ✅）
```

启动服务（手动点界面）：

```bash
.venv/bin/uvicorn backend.app:app --reload --port 8000
# 打开 http://localhost:8000
```

Docker：

```bash
docker compose up --build
# 打开 http://localhost:8000
```

## 身份（演示用，非正式登录）

接口用请求头 `X-User` 传用户名，不传默认 `admin`（系统管理员）。
种子用户：`admin`（系统管理员）、`manager`（采购经理）、`director`（采购总监）、
`buyer`（采购员）、`keeper`（仓库员，仅主仓 + 总公司）、`finance`（财务）、
`auditor`（审计员，只读）、`worker`（生产员）。正式上线应换成真正的登录 + Token。

## 第二阶段做了什么（方案第 5.2 节 + 第一阶段已知限制修复）

### 第一阶段已知限制修复（必须做）

- **生产领料改为逐级领料**：只扣 BOM 直接子件，不直接扣底层原材料。直接子件是半成品时，先耗半成品库存；半成品不足时领料失败并返回缺口，提示用 `POST /api/production/{单号}/ensure-subassemblies` 一键生成半成品生产单，先生产半成品再造成品。扣库存按批次 FIFO（先进先出），跳过冻结批次量，全部检查通过后一次性扣（原子操作）。领料成本记入 `production_consumptions`。
- **冻结按批次**：冻结 / 解冻可指定仓库 + 批次，只影响该批次可用量；出库、调拨、领料都按“该批次可用 = 该批次现有 − 该批次冻结”校验；质检不合格按批次冻结；`GET /api/stock/frozen`、`GET /api/stock/available/batch` 按批次可查。
- **单号改用数据库序列 / 锁**：`doc_sequences` 表按单据类型 + 年份计数，同一事务内取号 +1，格式 `PO-2026-0001` / `MO-2026-0001` / `PR-2026-0001` / `PAY-2026-0001`，不再用微秒时间戳。

### 第二阶段新增

- **多公司 / 多仓库 / 多币种**：公司（`COMP-001` 深圳总公司 / `COMP-002` 上海分公司）、仓库（主仓 / 分仓 / 成品仓 / 上海仓）、币种（CNY / USD / EUR / HKD）+ 汇率表。库存、单据带仓库 + 公司 + 币种 + 汇率；采购金额分交易币种与本位币（`total_amount` / `total_amount_base`）；按仓库过滤库存、按公司隔离（跨公司调拨 / 合并拒绝）；用户可限制仓库 / 公司范围（`keeper` 仅主仓 + 总公司）。
- **审批流**：采购申请 + 采购单金额分级：≤10000 自动通过；>10000 采购经理批；>100000 采购经理 + 采购总监依次批。支持自定义审批规则（自定义规则存在时覆盖默认分级，可指定审批人）；审批记录可查（`GET /api/approvals`、`GET /api/approvals/pending`）。待审批采购单不能到货。
- **财务对账**：供应商发票登记 + 简易三方核对（采购单 vs 实际到货 vs 发票，容差 = max(0.01, 期望金额×1%)，差异进“差异待处理”）、发票对账（通过后回写采购单对账字段）、付款（部分付款 / 已付款，付款单号序列）、应付报表（`GET /api/finance/payables`，按供应商：发票总额 / 已付 / 未付 / 已对账未付 / 未对账）、生产成本（`GET /api/finance/production-cost/{单号}`，直接材料成本，半成品按自身生产单单独算，不重复计算）。重复发票号拒绝（防重复付款）。
- **权限细化 RBAC**（基于角色的权限控制，大白话：按角色决定谁能看/改/批）：角色 × 模块 × 查看 / 新建 / 修改 / 审批 / 删除矩阵；用户可配多个角色 + 仓库范围 + 公司范围；`GET /api/permissions/check` 供前端按钮显隐；审批步骤的审批权由当前步骤的指定人 / 角色校验（自定义审批人也能批自己那一步）。
- **报表 + Excel + 审计日志**：常用报表 `GET /api/reports/{stock_balance|purchase_summary|production_summary|payable|audit_log}`（库存结存、采购汇总、生产汇总、应付、审计），与 Excel 导出共用同一构造器（`GET /api/excel/export/{同上}`）；Excel 导入延续并新增公司编码 / 仓库 / 币种列；审计日志 `GET /api/audit-log`（按用户 / 单据类型 / 单据号 / 动作查），只能追加，无修改 / 删除接口。

## 中国市场适配

中文界面 / 中文注释 + 大白话解释；税率 13%、含税 / 未税 / 税额分开算；库位、批次、五表一字典思路；Excel / WPS 灵活映射（.xlsx）。

## 文件清单

- `backend/app.py`：后端全部逻辑（FastAPI + SQLite，中文注释 + 大白话解释）
- `backend/test_phase2.py`：本阶段自动验证脚本（176 项检查）
- `frontend/index.html`：简单中文网页界面（看板四块 + 第二阶段快捷查询，支持 X-User 切换）
- `requirements.txt` / `Dockerfile` / `docker-compose.yml`
- `验证报告.md`：验证步骤、结果、参考来源、未做 / 已知限制
- `README-MVP.md`：MVP 原 README（参考用）
