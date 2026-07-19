# USDA 平衡表本地接入说明

> 文档状态：当前项目操作说明。生产发布统一遵守 [`../03_标准开发与生产发布规范.md`](../03_标准开发与生产发布规范.md)。

## 1. 用途和边界

USDA 平衡表位于：

```text
11_独立应用/USDA平衡表/
```

它与主工作台由同一 Git 仓库管理，但运行时是独立应用和独立容器，不互相启动。

本文只负责本地依赖、数据构建、测试、预览和入口配置，不提供绕过候选流程的生产构建命令。

## 2. 本地安装和启动

在 USDA 目录按锁文件安装：

```powershell
pnpm install --frozen-lockfile
pnpm run dev -- --host 127.0.0.1 --port 5173
```

另一个终端回到仓库根目录启动主工作台：

```powershell
python -m streamlit run 05_apps/streamlit_app.py
```

本地端口可以按任务调整，但不得把本地示例地址当作生产默认地址。

## 3. 数据构建和检查

报告月份由 `configs/usda_report_version.json` 管理。原始 CSV 按项目约定放入月份目录，原始数据默认不进入普通 Git 提交。

标准命令：

```powershell
pnpm run build:data
pnpm run check:data
pnpm run compare:data
pnpm run test
```

成功标准：

- 目标月份和前月配置正确；
- 数据构建产生预期矩阵、商品和国家；
- `check:data` 无 fatal；
- 月间差异可解释；
- 测试通过；
- `public/data/report_version.json` 与目标月份一致；
- 页面能打开目标商品和国家，月份、单位和修正正确。

任一步失败都停止，不修改生产。

## 4. 地址配置

正式外部地址只从环境变量 `USDA_DASHBOARD_URL` 读取。

`02_configs/report_catalog.yaml` 可以保留本地开发示例和 `url_env` 登记，但页面不得在环境变量缺失时把示例 URL 当作隐式生产地址。

环境变量缺失时：

- 主工作台继续正常启动；
- USDA 入口显示暂不可用；
- 其他页面和 Oil World 入口不受影响；
- 不抛出未处理异常。

## 5. 数据发布和代码发布

- 重新生成并提交正式 `public/data/` 和必要配置属于 USDA 发布数据更新。
- 修改 `src/`、脚本、依赖、Dockerfile 或 Nginx 属于代码更新。
- USDA 发布数据进入静态镜像时，即使前端源码未变，也需要生成新的候选镜像。

生产发布必须：

1. main-first 推送完整 SHA；
2. 服务器独立只读浅克隆；
3. 在隔离目录构建候选镜像；
4. 候选验收并生成 `candidate_result.json` 和 `candidate_result.manifest.json`；
5. 保存证据、删除候选容器并确认不存在；
6. 生成 `deployment_plan.json` 和 `deployment_plan.manifest.json`；
7. 正式切换复用候选的同一 Image ID；
8. 正式阶段禁止 `docker compose build`；
9. 生成 `deployment_result.json` 和 `deployment_result.manifest.json`。

USDA 自动化工具未完善时，必须记录工具缺口并采用受控步骤，不得降低上述原则。

## 6. 停止条件

- 目标月份原始数据不唯一或不可读；
- 原始数据进入未批准的 Git 变更；
- 数据检查、比较、测试或页面预览失败；
- 生产地址依赖 catalog 本地示例；
- 服务器构建目录是 `/home/ubuntu/market-data`；
- 候选和正式 Image ID 不一致；
- 正式切换命令包含 build；
- 部署结果或 Manifest 缺失。
