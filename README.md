# 农产品研究 Agent

## 项目用途

本项目是农产品研究系统的唯一 Git 仓库，用于构建市场研究 Agent，并在同一仓库内维护 USDA 平衡表、Oil World 供需平衡表等独立应用。

当前已实现国内现货基差、现货一口价、内盘期货收盘价和国内现货价差的读取与标准化。

## 顶层目录顺序

- `00_项目入口/`：人工查看入口
- `01_data/`：数据层
- `02_configs/`：配置层
- `03_src/`：核心代码层
- `04_scripts/`：运行入口层
- `05_apps/`：Streamlit 网页层
- `06_outputs/`：Agent 输出层
- `07_docs/`：中文文档层
- `08_tests/`：测试层
- `09_deploy/`：部署层
- `10_logs/`：日志层
- `11_独立应用/`：保持独立技术栈的应用；当前包含 `USDA平衡表/` 和 `OilWorld平衡表/`

## 快速入口

- 文档唯一入口：[`07_docs/00_文档索引与适用范围.md`](07_docs/00_文档索引与适用范围.md)
- 项目入口：`00_项目入口/从这里开始.md`
- 系统架构与项目边界：[`07_docs/01_系统架构与项目边界.md`](07_docs/01_系统架构与项目边界.md)
- 数据与输出规则：[`07_docs/02_数据与输出规范.md`](07_docs/02_数据与输出规范.md)
- 标准开发与生产发布：[`07_docs/03_标准开发与生产发布规范.md`](07_docs/03_标准开发与生产发布规范.md)
- 开发与发布检查清单：[`07_docs/04_开发与发布检查清单.md`](07_docs/04_开发与发布检查清单.md)
- 执行环境与跨环境传输：[`07_docs/05_执行环境与跨环境传输规范.md`](07_docs/05_执行环境与跨环境传输规范.md)
- 日常运行与数据更新：[`07_docs/06_日常运行与数据更新手册.md`](07_docs/06_日常运行与数据更新手册.md)

## 国内基差正式原始数据

固定放在：

`01_data/manual/榨利表/油脂油料价格.sql`

正式更新入口只流式读取 `basis_price` 表。历史 Excel
`01_data/manual/basis/国内现货基差.xlsx` 保留为只读备份和人工核对资料。

## 标准化输出

输出目录为 `01_data/processed/basis_spread/`：

- `国内现货基差_标准表.csv`
- `国内现货价差_标准表.csv`
- `国内现货一口价_标准表.csv`
- `内盘期货收盘价.csv`

文件名使用中文，CSV 内部字段名保持英文。

## 常用运行命令

```bash
python 04_scripts/run_basis_import.py
python 04_scripts/update_basis_data.py
pytest
streamlit run 05_apps/streamlit_app.py
```

## 本地开发工具链

正式 Python 基线为 3.12，标准本地环境为 `.venv-py312`；旧 `.venv`（Python
3.14）不属于正式验证环境。首次创建和安装使用：

```powershell
py -3.12 -m venv .venv-py312
.\.venv-py312\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.txt
.\.venv-py312\Scripts\python.exe 04_scripts\environment\verify_development_environment.py
```

运行时直接依赖维护在 `requirements.in`，生产锁为 `requirements.txt`；测试工具维护
在 `requirements-dev.in`，开发锁为 `requirements-dev.txt`。两个前端统一使用 Node
24、Corepack 管理的 pnpm 10.12.1，并必须执行 `pnpm install --frozen-lockfile`。
缺少 Node、Corepack 或正式 pnpm 时，环境预检会失败且不会自动安装工具。

USDA 子项目位于 `11_独立应用/USDA平衡表/`。本地开发在该目录运行 `pnpm run dev`，测试和生产构建分别运行 `pnpm run test`、`pnpm exec tsc -b --pretty false` 和 `pnpm run build`。

Oil World 子项目位于 `11_独立应用/OilWorld平衡表/`。本地启动命令为：

```bash
pnpm --dir 05_apps/oil_world_dashboard dev --host 127.0.0.1 --port 5175
```

主工作台通过 `OIL_WORLD_DASHBOARD_URL` 读取 Oil World 地址，本地默认值为 `http://127.0.0.1:5175/`。Oil World 保持独立运行，数据不会复制到 Streamlit 项目中。

三个正式服务使用各自独立 Compose/project；以[系统边界](07_docs/01_系统架构与项目边界.md)为准，不能用根 Compose 顺带操作其他服务。服务器保留一个 `market-data` 正式项目目录。

新开发先读根 `AGENTS.md`，使用 [Project Registry](02_configs/project_registry.json) 的 project_id。
fresh fetch/ls-remote → 独立 feature worktree → Scope Gate `--project` → project tests → 独立 integration/release。
local main 仅为 clean origin/main 镜像；生产 FULL DAILY 只认显式 Approved 与 detached control-plane/tool-repo。

`spread-dashboard` 的发布规则以 [`07_docs/03_标准开发与生产发布规范.md`](07_docs/03_标准开发与生产发布规范.md) 为准。`09_deploy/spread_release/` 当前仍是候选实现，真实 Docker Compose 门槛通过前不得用于生产部署；任何正式切换都不得使用根 Compose 隐式命名、`latest`、`new` 或 `up --build`。
