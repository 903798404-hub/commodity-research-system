# USDA 平衡表本地接入说明

## 用途

农产品看板首页的“USDA 平衡表”卡片会在新标签页打开独立的 USDA React 应用；两个项目不共享进程，也不互相启动对方。

## 本地使用顺序

1. 在 `codex-projects/USDA平衡表` 先按 `pnpm-lock.yaml` 安装依赖：`pnpm install --frozen-lockfile`；再运行 `node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5173`。
2. 在 `codex-projects/market-data` 运行 `python -m streamlit run 05_apps/streamlit_app.py`。
3. 打开 Streamlit 首页，点击“USDA 平衡表”卡片。

USDA 应用的数据构建和生产构建命令分别为 `npm run build:data` 与 `npm run build`。

## 地址配置

默认地址在 `02_configs/report_catalog.yaml` 的 USDA 卡片 `url` 字段中维护。部署或本地端口变化时，优先设置环境变量 `USDA_DASHBOARD_URL`；该变量仅覆盖 USDA 卡片地址，不保存任何密码、密钥或 Token。

若外部地址未配置，首页会显示“未配置地址”，不会影响其他卡片或 Streamlit 看板运行。
