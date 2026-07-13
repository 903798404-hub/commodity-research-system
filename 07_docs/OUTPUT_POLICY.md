# 输出产物策略

## 主入口

本项目日常看盘以 Streamlit 动态看板为主入口：

```powershell
.\.venv\Scripts\python.exe -m streamlit run .\05_apps\streamlit_app.py
```

动态看板直接读取核心数据库：

```text
01_data\historical_spread_database.xlsx
```

动态看板不读取 PNG，不读取 `06_outputs\charts\current`，也不依赖静态图表导出结果。

## 日常运行流程

日常看盘主流程是：

1. 更新 `01_data\historical_spread_database.xlsx`。
2. 启动 Streamlit 动态看板。

日常看盘不需要每天生成 PNG，也不需要每天生成 Excel 看板。

只有在需要发日报、存档或离线查看时，才主动运行静态导出脚本，生成 `06_outputs\reports\current` 下的中文 Excel 和图片。

## 核心数据库

以下文件是程序内部数据底座，必须保持英文稳定文件名：

```text
01_data\historical_price_long.xlsx
02_configs\historical_spread_config.xlsx
01_data\historical_spread_database.xlsx
```

其中 `historical_spread_database.xlsx` 是动态看板、静态图表导出和后续日报逻辑的核心输入。

## 静态导出物

PNG 和 Excel 看板只是导出物，用于需要发日报、存档或离线查看时手动生成。

主动运行静态导出脚本后，最新导出物放在：

```text
06_outputs\reports\current\价差图看板.xlsx
06_outputs\reports\current\价差日报图片\*.png
```

日常看盘不需要先生成 PNG，也不需要先生成 Excel 看板。

## 服务器部署提醒

未来部署到云服务器后，主入口仍然是：

```text
05_apps\streamlit_app.py
```

核心数据库仍然是：

```text
01_data\historical_spread_database.xlsx
```

服务器需要同时负责运行看板和更新数据库。不要把敏感数据、Wind 原始数据、API 密钥提交到 GitHub。

如果后续使用 Docker 或服务器定时任务，应保持 `data`、`output`、`logs` 为可持久化目录。

## 命名规则

给用户直接打开看的最终文件可以使用中文名，例如：

```text
价差图看板.xlsx
油脂油料价差日报.xlsx
今日价差监控.xlsx
```

程序内部文件、脚本、配置和数据库应保持英文稳定命名，例如：

```text
historical_spread_database.xlsx
historical_spread_config.xlsx
streamlit_app.py
plot_seasonal_spreads.py
settings.yaml
paths.py
```

原因是内部文件会被脚本、应用、定时任务和测试引用，英文稳定文件名更适合长期维护。

## archive 规则

`06_outputs\archive` 用于保存历史迁移文件、旧版图表和必要的导出存档。

当前旧版时间戳图表已归档到：

```text
06_outputs\archive\legacy_charts_before_streamlit
```

清理规则：

- 不删除 `data` 下的核心数据库和原始数据。
- 不删除 `logs`。
- 不删除当前仍被脚本引用的 `06_outputs\reports\current`。
- 可按需要清理 `06_outputs\archive` 中过旧、已确认无用的历史导出批次。
- 清理 archive 前建议先确认其中没有需要复盘的日报图片或历史输出。


