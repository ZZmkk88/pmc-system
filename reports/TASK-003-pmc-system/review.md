# pmc-system 验收报告

结论：命令行测试和 phase2 / phase3 服务是通的。推荐的 Windows 双击启动在解析阶段就失败。mvp 与 phase1 的首页正常，`/health` 返回 404。许可证类型是 MIT，版权人与 MVP 源码署名不一致。

待审目录 `E:\muse-bridge\in\TASK-003-pmc-system` 未改。运行发生在副本 `E:\muse-bridge\work\TASK-003-pmc-system\src`，虚拟环境在同级 `.venv`（Python 3.11.9）。依赖按 `phase3/requirements.txt` 钉死版本安装，exit 0。Docker 本机没有 `docker` 命令，记为环境限制，未构建镜像。

## 本地跑通记录

### 测试（全部 exit 0）

解释器：`E:\muse-bridge\work\TASK-003-pmc-system\.venv\Scripts\python.exe`。`TEMP`/`TMP` 指向 `E:\muse-bridge\work\TASK-003-pmc-system\tmp`。抓取命令是工作目录里的 `run_tests.py`（子进程 UTF-8）。第一遍 `run-tests.ps1` 也是 7 个脚本 exit 0，日志被代码页弄乱，下面片段以第二遍为准。

| 脚本 | exit | 耗时 | 结果 |
|---|---|---|---|
| `mvp/backend/test_end_to_end.py` | 0 | 2070 ms | 38 行 ✅，末行「全部端到端验证通过 ✅」 |
| `phase1/backend/test_phase1.py` | 0 | 3440 ms | 127 行 ✅，末行「全部第一阶段验证通过 ✅」 |
| `phase2/backend/test_phase2.py` | 0 | 4815 ms | 「共 176 项 ✅」 |
| `phase3/backend/test_phase3.py` | 0 | 5114 ms | 「共 133 项 ✅」 |
| `phase3/backend/test_fixes.py` | 0 | 3698 ms | 「26 项通过，0 项失败」 |
| `phase3/backend/test_p2_round2.py` | 0 | 3378 ms | 「本轮 31 项全部通过 ✅」 |
| `phase3/backend/check_mrp_net2.py` | 0 | 2277 ms | 净需求 100，打印「一致」 |

MVP 采购含税总额在输出里是 `1380.86`（单号 `PO-20261021150216`）。成品库存检查为 100。stderr 仅有一条，七个脚本相同：

```
StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
```

没有因外网抓取失败的用例，没有重试失败项。

### 服务（127.0.0.1:8000，探完已关）

`smoke_server.py` 对副本执行 `python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000`（无 `--reload`）。约 1.6 秒就绪。

- mvp：`GET /` 200，标题「PMC 系统 MVP - 生产物料控制」。`GET /health` 404，正文 `{"detail":"Not Found"}`。
- phase1：`GET /` 200，标题「PMC 系统 第一阶段」。`GET /health` 404。
- phase2：`GET /` 200。`GET /health` 200，`{"ok":true,"msg":"PMC 第二阶段运行正常"}`。
- phase3：`GET /` 200，标题「PMC 生产物料控制系统」。`GET /health` 200，`ok` 为 true，`app_id` 为 `pmc-phase3`，`msg` 为「PMC 第三阶段运行正常」。`POST /api/auth/login` 用 `admin/admin` 返回 200，`must_change_password` 为 true，`expires_at` 为 `2026-10-22 15:05:31`。无 token 的 `GET /api/parts` 为 401；带 token 为 200，首条是 `FG-001` 小夜灯。

进程 `terminate` 后退出码 1。随后 `netstat -ano | findstr "LISTENING" | findstr ":8000"` 无匹配（findstr 退出码 1）。

### 双击入口（失败）

```
cscript //nologo E:\muse-bridge\in\TASK-003-pmc-system\phase3\启动PMC系统.vbs 0
```

退出码 1。原文：

```
E:\muse-bridge\in\TASK-003-pmc-system\phase3\启动PMC系统.vbs(1, 1) Microsoft VBScript 编译器错误: 无效字符
```

`启动PMC系统.vbs` 与 `创建桌面入口.vbs` 文件头都是字节 `239, 187, 191`（UTF-8 BOM `EF BB BF`）。未执行「创建桌面入口」，该脚本会往桌面写 `.lnk`。

原仓库 `git status --short` 为空。原仓库没有 `*.db`。副本 backend 下有四份运行产生的 sqlite，只在工作目录。

## README 与实际不符处

下列核对过且与本次输出一致，不单列缺陷：phase2 的 176 项（`phase2/README.md:12`）、phase3 的 133 项（`phase3/README.md:21`）、`test_fixes.py` 的 26 项（`phase3/README.md:130`）、`test_p2_round2.py` 的 31 项（`README.md:24`）、MVP 含税 1380.86（`README.md:20`，测试输出同数）、12 个页签（`phase3/frontend/index.html:182-185`）、默认账号与 24 小时 token（`phase3/backend/app.py:4314`）。

1. `phase3/README.md:9-12` 把双击 `启动PMC系统.vbs` 写成推荐启动。实机 cscript 在 `phase3/启动PMC系统.vbs:1` 编译失败，错误原文见上一节。`phase3/创建桌面入口.vbs:1` 同样以 UTF-8 BOM 开头。`phase3/怎么用.md:13` 也把双击写成方法 A。
2. `phase3/README.md:157` 写 `.gitignore` 已忽略 `backend/*.db`。`.gitignore:62-63` 只忽略 `db.sqlite3` 与 `db.sqlite3-journal`。`git check-ignore -v --no-index phase3/backend/pmc_phase3.db` 退出码 1。
3. `README.md:21` 与 `phase1/README.md:46` 写第一阶段 128 项检查。本次 `test_phase1.py` 打出 127 行以 ✅ 开头的检查，`phase1/backend/test_phase1.py:352` 的结束语不带项数。
4. `phase3/requirements.txt:2` 写「22 项修复回归」。同目录 `backend/test_fixes.py` 本次输出是「26 项通过，0 项失败」。`phase3/README.md:130` 的 26 与输出一致。
5. `phase3/README.md:90-91` 写小屏按钮撑满宽度且最小高度 44px。`phase3/frontend/index.html:45-51` 的 `@media (max-width:640px)` 只改了内边距、字号和 `.grid` 单列。viewport 元标签在 `index.html:5`，与文档前半句一致。表格横滑容器是 `index.html:36` 的 `.scroll`，不在这条媒体查询里。
6. `phase3/怎么用.md:58` 写健康检查正文是 `{"ok":true,"msg":"PMC 第三阶段运行正常"}`。实机还有 `app_id` 与 `data_dir`，对应 `phase3/backend/app.py:4687-4689`。`ok` 与 `msg` 两字段本身与文档相同。
7. `phase3/怎么用.md:63` 写首次登录不改密就不能用系统。页面确实不进主界面（`phase3/frontend/index.html:143-151`）。服务端不拦截：`phase3/backend/test_p2_round2.py` 本次有「未改密仍可调接口」通过，`phase3/README.md:119` 也写了服务端不拦截。
8. `phase3/怎么用.md:66` 写仓库员 keeper 仅主仓。`phase3/backend/app.py:896-898` 同时限制仓库「主仓」和公司 `COMP-001`。
9. `README.md:42` 的 `docker compose up --build` 没有先进入阶段目录。仓库根目录没有 `docker-compose.yml`，文件在 `phase3/docker-compose.yml:1`（mvp / phase1 / phase2 各有一份，端口都是 `8000:8000`）。本机无 Docker，这一条没有实跑。
10. `mvp/README.md:21`、`phase1/README.md:9`、`phase2/README.md:10`、`phase3/README.md:27` 让用户 `cd ~/workspace/pmc/...`。本仓库的阶段目录是 `mvp/`、`phase1/`、`phase2/`、`phase3/`。根 `README.md:29` 的 `cd phase3` 与目录一致。
11. `phase3/README.md:141-152` 按「本地目录不是 git 仓库」给出 `git init` 和 `git push -u origin main --force`。本仓库已有 `.git`，`origin` 已是 `https://github.com/ZZmkk88/pmc-system.git`，当前提交 `3008619`。
12. `phase3/README.md:63` 的目标库存是「安全库存 × 2 + 日均用量 × 提前期」。返回给接口的公式在 `phase3/backend/app.py:4425`，与 README 相同。同文件 `4377` 行注释前半句写成「目标库存 = 安全库存 + 提前期用量」。
13. `phase3/backend/check_mrp_net2.py:5` 写运行方式为 `F:\pmc-system-main\phase3\.venv\Scripts\python.exe check_mrp_net2.py`。本仓库没有这个路径。用副本里的 `backend/check_mrp_net2.py` 可以跑通。

`phase3/启动PMC系统.vbs:18` 还写死 `phase3\.venv\Scripts\python.exe`。`.gitignore:155` 忽略 `.venv`，仓库里没有这份虚拟环境。本次 cscript 在编译阶段退出，没有走到缺解释器的分支。

## 许可证与署名核查

类型自洽。`README.md:61` 声明 MIT License，并指向 `LICENSE`。`LICENSE:1-21` 是标准 MIT 正文。`LICENSE:3` 版权行为 `Copyright (c) 2026 ZZmkk88`。远程仓库名 `ZZmkk88/pmc-system` 与这个名字一致。

署名不自洽。`mvp/backend/app.py:15` 写「作者：Muse（PMC MVP 建造）」。phase1 / phase2 / phase3 的 `backend/app.py` 没有版权行，也没有 Muse 或 ZZmkk88。仓库只有根目录这一份 `LICENSE`，没有 NOTICE。

「未复制 GPL」在仓库内部是同一口径：根 README、各阶段 README、四份 `app.py` 文首都写独立重写、只参考 ERPNext（GPLv3）和 InvenTree（MIT）。源码里没有 `frappe` / `inventree` / `odoo` 的 import。本次按任务约束没有抓取上游页面，没有做逐行对照，所以「没有抄 GPL 原文」未核实，不能当成已证明。

InvenTree 若被复制代码，MIT 要求保留其版权声明。仓库里没有 InvenTree 的版权头。在「独立重写」成立时，这是允许的；该前提未核实。

MIT 要求副本保留版权声明与许可声明。整仓分发时根目录 `LICENSE` 满足这一点。单独拿走 `backend/app.py` 时，文件内没有这段声明。

## 分级修复清单

### P0

1. `phase3/启动PMC系统.vbs:1`、`phase3/创建桌面入口.vbs:1`：去掉 UTF-8 BOM，把脚本存成 UTF-16 LE（带 BOM）或系统 ANSI（中文 Windows 为 GBK），否则 Windows 脚本宿主在第 1 个字符就报「无效字符」，推荐启动方式无法执行。

### P1

1. `mvp/backend/app.py:921-924`、`phase1/backend/app.py:1980-1987`：把 `@app.get("/health")` 移到 `app.mount("/")` 之前。现在静态挂载在前，实机 `/health` 为 404。phase2 的正确顺序在 `phase2/backend/app.py:3614-3622`。
2. `phase3/启动PMC系统.vbs:18`：仓库不带 `.venv`（`.gitignore:155`）。修好编码后，双击仍会找不到 `phase3\.venv\Scripts\python.exe`。在脚本里于缺失时按 `phase3/README.md:19-20` 创建虚拟环境并安装依赖，或把「必须先建 `.venv`」写到双击步骤之前作为硬前提。
3. `.gitignore:62`：增加 `*.db`（或 `backend/*.db`）。`phase3/README.md:157` 已经承诺忽略这些文件，当前 `git check-ignore` 对 `phase3/backend/pmc_phase3.db` 不忽略。导入 `app.py` 就会在 backend 目录建库（`phase3/backend/app.py:922` 的 `init_db()`）。
4. `LICENSE:3` 与 `mvp/backend/app.py:15`：版权人统一成一个名字，并在各阶段 `app.py` 文件头保留同一行版权声明与 MIT 指向。

### P2

1. `README.md:21`、`phase1/README.md:46`：把「128 项」改成 127，或在 `phase1/backend/test_phase1.py` 补上缺失的那一项检查并让脚本打印项数。
2. `phase3/requirements.txt:2`：把「22 项修复回归」改成 26，与 `test_fixes.py` 的输出一致。
3. `phase3/frontend/index.html:45-51`：在该媒体查询里给 `button` 加上 `width:100%` 和 `min-height:44px`，与 `phase3/README.md:90-91` 对齐。
4. `phase3/怎么用.md:58`：健康检查示例补上 `app_id` 与 `data_dir`。`phase3/怎么用.md:63` 改成「页面会停在改密卡片，接口不强制拦截」。`phase3/怎么用.md:66` 补上 keeper 还限制总公司 `COMP-001`。
5. `README.md:41`：在 `docker compose` 前写 `cd phase3`。`mvp/README.md:21`、`phase1/README.md:9`、`phase2/README.md:10`、`phase3/README.md:27` 的 `~/workspace/pmc/...` 改成仓库内相对路径。
6. `phase3/README.md:145-152`：删掉对已有仓库的 `git init` 和 `--force`。本仓库已经有 `origin`。
7. `phase3/backend/app.py:4377`：删掉「目标库存 = 安全库存 + 提前期用量」这半句，只保留「安全库存×2 + 提前期用量」。
8. `phase3/backend/check_mrp_net2.py:5`：运行说明改成 `phase3` 目录下的 `.venv\Scripts\python.exe backend\check_mrp_net2.py`。
9. `phase3/requirements.txt:4-8` 钉的 `fastapi==0.148.0` / `httpx==0.28.1` 会拉上 Starlette 1.9，测试时已打印 TestClient 弃用警告。跟踪官方的 httpx2 替换，或钉住一组不再警告的版本。当前测试仍全部通过。
10. `mvp/backend/app.py:235`、`phase1/backend/app.py:265`、`phase2/backend/app.py:700`：模块导入时就 `init_db()`，测试是导入之后才改 `DB_PATH`，所以一跑测试就会在源码旁落下 `pmc_*.db`。把 `init_db()` 挪到启动入口，或像 phase3 一样在导入前读环境变量决定数据库路径。
