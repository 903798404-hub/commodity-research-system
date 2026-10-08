# Oil World 供需平衡表

本目录是全新第一版Oil World年度研究看板。数据生成严格以原始Excel和`07_docs/报表映射审计`中的四份正式映射文件为依据，不恢复或依赖旧解析器、旧351个矩阵、旧React前端和旧发布代码。

## 当前范围

开发、发布及宿主存储维护统一遵循[主仓库规范](../../07_docs/03_标准开发与生产发布规范.md)、[发布清单](../../07_docs/04_开发与发布检查清单.md)及[数据盘维护手册](../../07_docs/06_日常运行与数据更新手册.md#12-数据盘迁移与维护)。
执行前重新核验实际数据盘和 Oil World 独立数据挂载；物理迁移不修改原始 Excel、
不可变季度快照、比较记录或 `latest`/`releases` 指针，不隐式更新其他应用。

- 当前正式发布期：`2026-06`
- 固定组合：59个商品—地区组合
- 固定指标：11项
- 映射状态：`direct`、`derived`、`missing`、`not_applicable`、`conflict`
- 前端地址：`http://127.0.0.1:5175/`

Brazil Soybeans和Argentina Sunflowerseed保留Jan–Dec自然年口径说明，但审计标记为冲突的数值不展示。G2和G3只生成审计确认安全的指标。

## 目录

- 原始资料：Git 仓库外统一目录，由 `OILWORLD_RAW_DATA_ROOT` 定位
- 映射依据：`07_docs/报表映射审计/`
- 发布配置：`02_configs/release_2026-06.json`
- 数据核心：`03_src/oil_world_data/`
- 基线构建入口：`04_scripts/build_release.py`
- 季度发布入口：`04_scripts/update_oil_world.py`
- 内部发布：`01_data/releases/2026-06/`
- 前端发布：`public/data/oil_world/releases/2026-06/`
- 相邻比较：`01_data/comparisons/`和`public/data/oil_world/comparisons/`
- 看板源码：`05_apps/oil_world_dashboard/`
- 数据测试：`08_tests/`

## 本地运行

原始资料根目录按以下顺序解析：环境变量 `OILWORLD_RAW_DATA_ROOT`、不进入 Git 的 `02_configs/local_paths.json`、仓库内旧目录兼容回退。本机覆盖文件格式如下：

```json
{
  "OILWORLD_RAW_DATA_ROOT": "D:/data/OilWorld"
}
```

在本目录执行：

```powershell
python 04_scripts/build_release.py
python 04_scripts/update_oil_world.py --release 2026-09 --validate-only
python 04_scripts/update_oil_world.py --release 2026-09
python 04_scripts/update_oil_world.py --release 2026-03 --backfill --validate-only
python 04_scripts/update_oil_world.py --release 2026-03 --backfill
python -m unittest discover -s 08_tests -p "test_*.py" -v
pnpm --dir 05_apps/oil_world_dashboard test
pnpm --dir 05_apps/oil_world_dashboard dev
```

首次发布已经生成。季度更新器默认拒绝覆盖任何既有发布目录，正式发布先在临时目录完成解析和质量检查，再安装为不可变快照。普通模式只接受晚于当前`latest`的发布期；`--backfill`用于历史回补，且不会让`latest`倒退。`--validate-only`完成解析、质量检查和相邻比较预览，但不写正式发布、比较目录或指针。

季度修正独立存放在相邻发布期比较目录中，以`system + product + region + metric + period + unit`匹配。只有口径一致的`direct`或`derived`有效数字参与计算；空值、`missing`、`not_applicable`和`conflict`保持空值。`Stocks/Use Ratio`修正单位为百分点。前端根据`releases.json`中的直接上一发布期加载比较文件，不把修正写回历史快照。

前端通过`import.meta.env.BASE_URL`读取`latest.json`、`releases.json`、发布索引和组合文件，不写死服务器地址。

外部原始资料根目录中的历史脚本`2026-06/数据导入.py`继续作为原始资料保留，当前未启用，也不是本系统开发基础。

本项目已通过主仓库`02_configs/report_catalog.yaml`中的独立应用卡片接入主农产品工作台，数据仍由本目录独立维护，不复制到Streamlit项目中，也没有修改USDA项目。
