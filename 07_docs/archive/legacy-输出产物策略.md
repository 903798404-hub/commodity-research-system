# 旧版输出产物策略

> 历史归档。本文保存早期动态看板、静态导出和目录清理约定。当前数据与输出规则见 `../02_数据与输出规范.md`。

## 1. 旧版主入口

早期文档将 Streamlit 动态看板作为日常看盘入口：

```powershell
.\.venv\Scripts\python.exe -m streamlit run .\05_apps\streamlit_app.py
```

当时将以下 Excel 描述为核心输入：

```text
01_data\historical_spread_database.xlsx
```

旧文档强调动态看板不依赖 PNG 或静态图表导出。这一职责区分仍有价值。

## 2. 旧版日常流程

当时流程为：

1. 更新 `historical_spread_database.xlsx`；
2. 启动 Streamlit；
3. 只有发日报、存档或离线查看时生成静态 Excel/PNG。

当前源码已经支持并优先读取 `historical_spread_database.parquet`，Excel 是回退，因此“只读 Excel”已经失效。

## 3. 旧版内部数据底座

旧文档登记：

```text
01_data\historical_price_long.xlsx
02_configs\historical_spread_config.xlsx
01_data\historical_spread_database.xlsx
```

这些文件名仍可能被当前脚本引用。它们的当前职责应以源码、`02_configs/app_catalog.yaml` 和数据规范为准。

## 4. 旧版静态导出

旧文档登记过：

```text
06_outputs\reports\current\价差图看板.xlsx
06_outputs\reports\current\价差日报图片\*.png
```

全仓审计时，该目录字符串只在旧 [`legacy-sources/OUTPUT_POLICY.md`](legacy-sources/OUTPUT_POLICY.md) 出现，没有发现当前源码引用，因此不能继续作为通用输出标准。

当前通用输出目录采用：

```text
06_outputs/charts/
06_outputs/excel/
06_outputs/markdown/
06_outputs/push_logs/
06_outputs/audits/
```

## 5. 有效的命名经验

仍应保留：

- 用户直接打开的报告可以使用中文文件名；
- 程序内部数据库、脚本、配置和接口使用稳定英文名；
- 被脚本、应用、定时任务或测试引用的名称不能随意更改；
- 页面运行不应无必要依赖静态导出物。

## 6. 旧版归档和清理记录

旧文档曾登记：

```text
06_outputs\archive\legacy_charts_before_streamlit
```

并提出：

- 不删除核心数据库和原始数据；
- 不删除日志；
- 不删除仍被脚本引用的输出；
- 清理 archive 前检查是否仍有复盘价值。

其中保护思想继续有效，但 `data/output/logs` 等未编号旧目录名已被 `01_data/06_outputs/10_logs` 取代。

## 7. 历史价值

本归档保护了：

- 动态页面和静态导出的职责差异；
- 内部稳定英文名原则；
- 旧 `reports/current` 路径和 archive 路径的历史证据；
- 清理前先查运行引用和复盘价值的经验。

不得从本文件推断当前页面只读 Excel，也不得重新创建旧版顶层目录。
