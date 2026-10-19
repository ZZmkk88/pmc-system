# PMC 系统 第一阶段（完整版）

> 由 MVP（`~/workspace/pmc/mvp/`）复制扩展，MVP 已验证的端到端流程保持兼容。
> 状态：第一阶段已按方案第 4 节补齐，自动验证全部通过（见 `验证报告.md`）。

## 怎么跑

```bash
cd ~/workspace/pmc/phase1
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python backend/test_phase1.py   # 自动验证，预期：全部第一阶段验证通过 ✅
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

## 第一阶段做了什么（方案第 4 节）

- **零件 + BOM**：BOM 多版本管理（版本号 + 生效日期）、BOM 对比、BOM 多层爆炸 + 循环检测、BOM Excel 导入
- **库存**：入库 / 出库 / 调拨 / 盘点、批次追溯（库存流水）、可用量 = 现有 + 在途 - 已分配 - 冻结、高于最高库存预警（新增）+ 低于安全库存预警
- **供应商**：价格表完整 CRUD、交货准时率统计、合格 / 黑名单标记、采购时自动比价（最便宜 / 交期最短，黑名单自动跳过）
- **采购**：采购申请（新建）→ 审批（一级：提交 → 批准）→ 采购单 → 到货 → 质检可选（不合格进冻结）→ 入库 → 对账字段（发票号 / 发票金额 / 是否已对账）；采购单合并（同供应商）；拖期标红
- **生产 + MRP**：生产单计划开始 / 结束、MRP 结果可手动调整后再生成采购单 / 生产单、MRP 按提前期偏移（最晚下单日）、多层 BOM 的 MRP（半成品需求类型 = 生产）
- **销售轻量**：销售单触发生产需求，不做 CRM
- **看板**：缺料预警、拖期采购、供应商拖期排行、生产缺料，四块 + 红黄绿
- **Excel / WPS**：新增 BOM、供应商、销售、生产、MRP 结果导出；新增 BOM、供应商、销售、生产导入；灵活映射（物料编码 / 材料编码 / 料号 / 零件编码都映射到零件编码，新增“用量”别名）；模板下载；校验错误提示行号；WPS 兼容 .xlsx

## 中国市场适配

中文界面 / 中文注释 + 大白话解释；税率 13%、含税 / 未税分开算；库位、批次；五表一字典思路；单仓库、单公司、单币种（多仓库等是第二阶段，不做）。

## 文件清单

- `backend/app.py`：后端全部逻辑（FastAPI + SQLite，中文注释 + 大白话解释）
- `backend/test_phase1.py`：本阶段自动验证脚本（128 项检查）
- `frontend/index.html`：简单中文网页界面（看板四块 + 快捷查询）
- `requirements.txt` / `Dockerfile` / `docker-compose.yml`
- `验证报告.md`：验证步骤、结果、参考来源、未做 / 已知限制
- `README-MVP.md`：MVP 原 README（参考用）
