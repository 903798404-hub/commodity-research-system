# 项目架构

本项目采用“英文工程目录 + 中文说明文档”和“外层中文，底层英文”的维护方式。

## 每日运行顺序

1. 从 `01_data/` 读取原始或人工数据。
2. 从 `02_configs/` 读取配置。
3. 调用 `03_src/agri_research_agent/` 中的核心能力。
4. 通过 `04_scripts/` 执行任务。
5. 通过 `05_apps/` 展示网页。
6. 将 Agent 报告和图表写入 `06_outputs/`。
7. 在 `10_logs/` 记录运行日志。

## 业务分层

- 数据层：接入、清洗和存储行情、基差和报告数据。
- Skill 层：实现报告解读、基差月差和持仓变化等研究能力。
- Agent 层：组合数据和 Skill，执行日报、事件分析与预警。
- 输出层：生成 Streamlit 页面、Markdown、Excel、图表和推送结果。

正式 Pipeline 代码统一放在 `03_src/agri_research_agent/pipelines/`。
