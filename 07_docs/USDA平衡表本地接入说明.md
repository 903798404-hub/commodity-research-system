# USDA 平衡表本地接入说明

## 用途

农产品看板的“USDA 平衡表”入口会打开仓库内的独立 React 应用。两项服务由同一 Git 仓库管理，但在运行时保持独立进程，不互相启动对方。

## 本地使用顺序

1. 在仓库根目录的 `11_独立应用/USDA平衡表/` 按 `pnpm-lock.yaml` 安装依赖：`pnpm install --frozen-lockfile`；再运行 `pnpm run dev -- --host 127.0.0.1 --port 5173`。
2. 回到仓库根目录运行 `python -m streamlit run 05_apps/streamlit_app.py`。
3. 打开 Streamlit 首页，点击“USDA 平衡表”卡片。

USDA 应用的数据构建和生产构建命令分别为 `pnpm run build:data` 与 `pnpm run build`。

## 地址配置

默认地址在 `02_configs/report_catalog.yaml` 的 USDA 卡片 `url` 字段中维护。部署或本地端口变化时，优先设置环境变量 `USDA_DASHBOARD_URL`；该变量仅覆盖 USDA 卡片地址，不保存任何密码、密钥或 Token。

若外部地址未配置，首页会显示“未配置地址”，不会影响其他卡片或 Streamlit 看板运行。
