# PMC 系统 MVP（最小验证版）

> 中文界面，中文注释，专业术语带“大白话”解释。
> 独立重写，未复制 GPL 代码。参考思路：ERPNext（MRP 公式、单据流）、InvenTree（库存流水+汇总、 BOM 爆炸）。

## 这是什么

PMC = Production Material Control（生产物料控制，大白话：管缺啥料、买多少、什么时候买）。
MVP 目标：证明核心链路能跑通：销售单 → MRP → 采购 → 入库 → 生产 → 成品入库。

演示例子：LED 小夜灯
- 成品：小夜灯 FG-001
- 零件 5 个：外壳 RM-001、LED灯珠 RM-002、电池 RM-003、开关 RM-004、电路板 RM-005
- BOM：做 1 个小夜灯，各需 1 个零件
- 期初库存（故意设成不够做 100 个，用来验证 MRP）：外壳 20、灯珠 30、电池 10、开关 50、电路板 15、安全库存各 20

## 怎么跑

### 方式 A：本地直接跑（最快）
```bash
cd ~/workspace/pmc/mvp
pip install -r requirements.txt
uvicorn backend.app:app --reload --port 8000
# 打开 http://localhost:8000
```

### 方式 B：Docker（大白话：打包成箱子，一键跑）
```bash
cd ~/workspace/pmc/mvp
docker compose up --build
# 打开 http://localhost:8000
```

## 端到端验证（手动）

1. 销售单：创建 SO-001，100 个小夜灯
2. MRP：对 SO-001 运行 MRP，应看到缺料：
   - 外壳：毛需求 100 - 库存 20 - 在途 0 + 安全 20 = 净需求 100
   - 灯珠：100 - 30 + 20 = 90
   - 电池：100 - 10 + 20 = 110
   - 开关：100 - 50 + 20 = 70
   - 电路板：100 - 15 + 20 = 105
3. 采购：从 MRP 一键生成采购单
4. 到货入库：按采购单数量逐个到货入库
5. 生产：创建 MO-001 100 个 → 领料 → 完工入库，成品库存应为 100
6. 看板：检查缺料预警、待跟进采购

自动验证脚本：`python backend/test_end_to_end.py`

## Excel / WPS

- 导出：零件 / 库存 / 采购，`.xlsx`，WPS / Excel 都能打开
- 导入：零件 / 库存 / 采购，支持灵活映射：
  物料编码 / 材料编码 / 料号 / 零件编码 都能映射到零件编码；
  名称 / 品名 / 物料名称 都能映射到名称，等等。见 `backend/app.py` 的 `COLUMN_ALIASES`。
- 模板：`/api/excel/template/{parts,stock,purchase}`

## 文件结构

- `backend/app.py`：后端全部逻辑（FastAPI + SQLite）
- `backend/test_end_to_end.py`：端到端自动验证
- `frontend/index.html`：简单中文网页界面
- `requirements.txt` / `Dockerfile` / `docker-compose.yml`
- `验证报告.md`：验证结果

## 下一步（不在 MVP 里）

多仓库、多公司、多币种、审批流、财务对账、权限细化、报表。见 `~/workspace/pmc/PMC系统方案.md` 第 5.2 / 5.3 节。
