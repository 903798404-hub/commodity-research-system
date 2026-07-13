# 农产品研究 Agent

## 项目用途

本项目是农产品研究系统的唯一 Git 仓库，用于构建市场研究 Agent，并在同一仓库内维护 USDA 平衡表等独立应用。

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
- `11_独立应用/`：保持独立技术栈的应用；当前包含 `USDA平衡表/`

## 快速入口

- 项目入口：`00_项目入口/从这里开始.md`
- 目录索引：`07_docs/目录索引.md`
- 中文化规则：`07_docs/项目中文化规则.md`
- 项目架构：`07_docs/architecture.md`

## 手动基差文件

固定放在：

`01_data/manual/basis/国内现货基差.xlsx`

程序只读取该文件，不会改名或改写。

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
pytest
streamlit run 05_apps/streamlit_app.py
```

USDA 子项目位于 `11_独立应用/USDA平衡表/`。本地开发在该目录运行 `pnpm run dev`，测试和生产构建分别运行 `pnpm run test`、`pnpm exec tsc -b --pretty false` 和 `pnpm run build`。

整套服务使用根目录 `docker-compose.yml` 管理：`spread-dashboard` 提供 Streamlit 看板，`usda-dashboard` 从仓库内 USDA 子项目构建静态站点。服务器只需要部署一个 `market-data` 项目目录。
