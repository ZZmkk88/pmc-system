# TASK-003 pmc-system 本地验收运行记录

开工：2026-10-21。执行方 Grok Build。工作目录 `E:\muse-bridge\in\TASK-003-pmc-system`。只审查与本地验证，不改仓库源码。本文件随步骤追加。

为避免测试导入时 `init_db()` 把 SQLite 写进待审仓库，源码副本在 `E:\muse-bridge\work\TASK-003-pmc-system\src`，虚拟环境在 `E:\muse-bridge\work\TASK-003-pmc-system\.venv`。临时库与日志在同目录 `tmp` / `logs`。待审仓库源码未改。

## 环境

命令：

```
py -3.11 --version
```

输出：

```
Python 3.11.9
```

命令：

```
docker --version
```

输出（本机无 docker 命令，编码为 GBK，含义是无法识别 docker）：

```
docker : 无法将“docker”项识别为 cmdlet、函数、脚本文件或可运行程序的名称。
CategoryInfo : ObjectNotFound: (docker:String) [], CommandNotFoundException
FullyQualifiedErrorId : CommandNotFoundException
```

判定：Docker 路径未跑，记为环境限制，不计入项目缺陷。

命令：

```
netstat -ano | findstr :8000
```

输出只有一条远程 IPv6 地址里含 `8000` 的已建立连接，不是本机 8000 监听。稍后起服务前再查 LISTENING。

## 装依赖

命令（2026-10-21，对 phase3 钉死版本；该版本同时满足 mvp/phase1/phase2 的 `>=` 约束）：

```
E:\muse-bridge\work\TASK-003-pmc-system\.venv\Scripts\python.exe -m pip install -r E:\muse-bridge\work\TASK-003-pmc-system\src\phase3\requirements.txt
```

结果：exit 0。关键行：

```
Successfully installed annotated-doc-0.0.5 annotated-types-0.8.0 anyio-4.15.1 certifi-2026.10.19 click-8.5.0 et-xmlfile-2.0.0 fastapi-0.148.0 h11-0.16.0 httpcore-1.0.9 httptools-0.9.0 httpx-0.28.1 idna-3.20 openpyxl-3.1.5 opentelemetry-api-1.45.1 pydantic-2.14.1 pydantic-core-2.50.1 python-dotenv-1.2.4 python-multipart-0.0.32 pyyaml-6.0.3 starlette-1.9.0 typing-extensions-4.16.0 typing-inspection-0.4.4 uvicorn-0.54.0 watchfiles-1.3.0 websockets-17.3
```

未在仓库内创建 `.venv`（禁止写仓库）。phase3 README 的 Windows 命令是在 `phase3\.venv` 里装，此处路径不同，解释器与 requirements 相同。

## 跑测试

工作目录副本，`TEMP`/`TMP` 指向 `E:\muse-bridge\work\TASK-003-pmc-system\tmp`，避免临时库进系统 TEMP。解释器：`E:\muse-bridge\work\TASK-003-pmc-system\.venv\Scripts\python.exe`。

第一遍用 `run-tests.ps1`（PowerShell 重定向），7 个脚本全部 exit 0，耗时约 5.4s / 3.6s / 4.9s / 6.0s / 3.7s / 3.4s / 2.3s。日志被控制台代码页弄乱，不能当原文引用。

第二遍用 `run_tests.py` 以子进程按 UTF-8 抓取 stdout。这是下面片段的出处。全部 exit 0。stderr 都只有同一条 Starlette 弃用警告（见报告 P2），没有失败。

| 脚本 | exit | 耗时 | 通过行数 |
|---|---|---|---|
| mvp `backend/test_end_to_end.py` | 0 | 2070 ms | 38 行 ✅ |
| phase1 `backend/test_phase1.py` | 0 | 3440 ms | 127 行 ✅ |
| phase2 `backend/test_phase2.py` | 0 | 4815 ms | 176 行 ✅ |
| phase3 `backend/test_phase3.py` | 0 | 5114 ms | 133 行 ✅ |
| phase3 `backend/test_fixes.py` | 0 | 3698 ms | 26 行 [OK] |
| phase3 `backend/test_p2_round2.py` | 0 | 3378 ms | 31 行 [OK] |
| phase3 `backend/check_mrp_net2.py` | 0 | 2277 ms | 打印「一致」，无断言失败 |

MVP 片段（`logs-utf8/mvp-e2e.txt`）：

```
✅ 零件 6 条，实际 6
采购单: {'ok': True, 'order_no': 'PO-20261021150216', 'total_amount_with_tax': 1380.86}
✅ 成品库存 100.0 == 100
全部端到端验证通过 ✅
```

phase1 结尾：

```
✅ 供应商拖期排行含 SUP-002（1 张拖期单）
全部第一阶段验证通过 ✅
```

phase2 结尾：

```
✅ 健康检查：{"ok":true,"msg":"PMC 第二阶段运行正常"}
全部第二阶段验证通过 ✅（共 176 项 ✅）
```

phase3 结尾：

```
✅ 健康检查 200
全部第三阶段验证通过 ✅（共 133 项 ✅）
```

test_fixes 结尾：`26 项通过，0 项失败`

test_p2_round2 结尾：`本轮 31 项全部通过 ✅`

check_mrp_net2：

```
MRP RM-001: {'gross': 100.0, 'onhand': 120.0, 'available': 20.0, 'safety': 20.0, 'net': 100.0}
新公式: 100.00 - 20.00 + 20.00 = 100.00 ；实际 net = 100.00 ；一致
```

stderr 原文（各测试相同）：

```
StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
```

phase1 文档写 128 项，本次 ✅ 行数是 127。其余文档里的 176 / 133 / 26 / 31 与本次输出一致。

## 起服务

命令（对工作区副本逐个起 uvicorn，不带 `--reload`，探完就终止进程）：

```
E:\muse-bridge\work\TASK-003-pmc-system\.venv\Scripts\python.exe E:\muse-bridge\work\TASK-003-pmc-system\smoke_server.py
```

四个阶段都在 `127.0.0.1:8000`。进程起来约 1.6 秒后返回。访问日志在 `logs-utf8/*-server.txt`。

mvp 日志：

```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     127.0.0.1:49339 - "GET /health HTTP/1.1" 404 Not Found
INFO:     127.0.0.1:49364 - "GET / HTTP/1.1" 200 OK
```

首页片段含 `<title>PMC 系统 MVP - 生产物料控制</title>`。

phase1 日志：

```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     127.0.0.1:62314 - "GET /health HTTP/1.1" 404 Not Found
INFO:     127.0.0.1:59875 - "GET / HTTP/1.1" 200 OK
```

首页标题「PMC 系统 第一阶段」。

phase2 日志：

```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     127.0.0.1:56795 - "GET /health HTTP/1.1" 200 OK
INFO:     127.0.0.1:63778 - "GET / HTTP/1.1" 200 OK
```

`/health` 正文：`{"ok":true,"msg":"PMC 第二阶段运行正常"}`。

phase3：

```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     127.0.0.1:63779 - "GET /health HTTP/1.1" 200 OK
INFO:     127.0.0.1:56211 - "GET / HTTP/1.1" 200 OK
INFO:     127.0.0.1:56212 - "POST /api/auth/login HTTP/1.1" 200 OK
INFO:     127.0.0.1:49596 - "GET /api/parts HTTP/1.1" 401 Unauthorized
INFO:     127.0.0.1:49597 - "GET /api/parts HTTP/1.1" 200 OK
```

`/health`：`{"ok":true,"app_id":"pmc-phase3","msg":"PMC 第三阶段运行正常","data_dir":"E:\\muse-bridge\\work\\TASK-003-pmc-system\\src\\phase3\\backend"}`。

登录 `admin/admin` 返回 200，`must_change_password` 为 true，`expires_at` 为 `2026-10-22 15:05:31`。token 明文不写入本记录。无 token 调 `/api/parts` 为 401；带 token 为 200，首条零件 `FG-001` 小夜灯。

四个进程都已 `terminate`，记录到的退出码是 1（主动终止的典型结果）。之后执行 `netstat -ano | findstr "LISTENING" | findstr ":8000"`，findstr 退出码 1，没有监听行。

## 双击入口

对原仓库文件（只解析，失败时不写日志）：

```
cscript //nologo E:\muse-bridge\in\TASK-003-pmc-system\phase3\启动PMC系统.vbs 0
```

输出：

```
cscript_exit 1
E:\muse-bridge\in\TASK-003-pmc-system\phase3\启动PMC系统.vbs(1, 1) Microsoft VBScript 编译器错误: 无效字符
```

文件头 16 字节：`[239, 187, 191, 39, ...]`，即 UTF-8 BOM `EF BB BF` 加 ASCII `'`。`创建桌面入口.vbs` 头同样是 `239, 187, 191`。未运行「创建桌面入口」，避免往桌面写快捷方式。

## 仓库是否被改写

`git -C E:\muse-bridge\in\TASK-003-pmc-system status --short` 无输出，exit 0。原仓库 `*.db` 数量 0。

`git remote -v`：`origin https://github.com/ZZmkk88/pmc-system.git`。

`git log -1 --oneline`：`3008619 桌面版合并：移除旧 bat 双击入口，统一走 启动PMC系统.vbs（bat 仍在 git 历史中可恢复）`。

`git check-ignore -v --no-index phase3/backend/pmc_phase3.db` 退出码 1（未被忽略）。

副本里因导入即 `init_db()` 以及本次起服务，生成了：

```
src\mvp\backend\pmc_mvp.db
src\phase1\backend\pmc_phase1.db
src\phase2\backend\pmc_phase2.db
src\phase3\backend\pmc_phase3.db
```

这些只在 `E:\muse-bridge\work\TASK-003-pmc-system\src`，不在待审仓库。

