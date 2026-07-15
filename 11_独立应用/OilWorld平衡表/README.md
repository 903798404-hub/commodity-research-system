# Oil World 供需平衡表

本目录是全新第一版Oil World年度研究看板。数据生成严格以原始Excel和`07_docs/报表映射审计`中的四份正式映射文件为依据，不恢复或依赖旧解析器、旧351个矩阵、旧React前端和旧发布代码。

## 当前范围

- 发布期：`2026-06`
- 固定组合：59个商品—地区组合
- 固定指标：11项
- 映射状态：`direct`、`derived`、`missing`、`not_applicable`、`conflict`
- 前端地址：`http://127.0.0.1:5175/`

Brazil Soybeans和Argentina Sunflowerseed保留Jan–Dec自然年口径说明，但审计标记为冲突的数值不展示。G2和G3只生成审计确认安全的指标。

## 目录

- 原始Excel：`01_原始资料/2026-06/`
- 映射依据：`07_docs/报表映射审计/`
- 发布配置：`02_configs/release_2026-06.json`
- 数据核心：`03_src/oil_world_data/`
- 构建入口：`04_scripts/build_release.py`
- 内部发布：`01_data/releases/2026-06/`
- 前端发布：`public/data/oil_world/releases/2026-06/`
- 看板源码：`05_apps/oil_world_dashboard/`
- 数据测试：`08_tests/`

## 本地运行

在本目录执行：

```powershell
python 04_scripts/build_release.py
python -m unittest discover -s 08_tests -p "test_*.py" -v
pnpm --dir 05_apps/oil_world_dashboard test
pnpm --dir 05_apps/oil_world_dashboard dev
```

首次发布已经生成。生成器默认拒绝覆盖现有`2026-06`目录；`--replace`只用于第一版本地开发，不得用于正常季度发布。

前端通过`import.meta.env.BASE_URL`读取`latest.json`、`releases.json`、发布索引和组合文件，不写死服务器地址。

历史脚本`01_原始资料/2026-06/数据导入.py`继续作为原始资料保留，当前未启用，也不是本系统开发基础。

本项目已通过主仓库`02_configs/report_catalog.yaml`中的独立应用卡片接入主农产品工作台，数据仍由本目录独立维护，不复制到Streamlit项目中，也没有修改USDA项目。
