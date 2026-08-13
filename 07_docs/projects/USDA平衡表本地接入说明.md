# USDA 平衡表本地接入说明

> 文档状态：当前项目操作说明。生产发布统一遵守 [`../03_标准开发与生产发布规范.md`](../03_标准开发与生产发布规范.md)。

## 1. 用途和边界

USDA 平衡表位于：

```text
11_独立应用/USDA平衡表/
```

它与主工作台由同一 Git 仓库管理，但运行时是独立应用和独立容器，不互相启动。

本文负责本地依赖、数据构建、Runtime bundle、测试、预览和入口配置，不提供绕过候选流程的生产构建命令。

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

## 5. Runtime 月度数据契约

普通 USDA 月度数据更新使用独立 Runtime data channel，不再把 `public/data/` 烘焙进镜像：

```text
本地 fetch/build:data/check:data/compare:data/test
→ package immutable Runtime bundle
→ server validate/stage
→ immutable release URL 验证
→ 原子切换 current.json
```

应用启动时只读取一次 `/usda/data/current.json`，取得严格格式的
`usda-YYYY-MM-<16位小写十六进制>` release ID；该页面实例的 index、report version、
matrix、前月 snapshot 和 presentation 全部固定到
`/usda/data/releases/<release_id>/data/`。只有刷新或重新打开页面才读取新的 current。

应用合同由 `/usda/app-contract.json` 暴露，当前 `app_contract_version=1`、
`supported_data_schema_version=1`。Bundle manifest 的 `data_schema_version` 必须受支持，
且 `minimum_app_contract_version` 不得高于运行 Image；兼容性门禁失败时不得 promote。

每个 bundle 必须逐文件、逐字节包含本次正式 build 产生的完整 `public/data/` 发布树，
包括根 JSON、当前 `matrix/`、全部正式 snapshot 和年度供需等兼容资产。Manifest 路径集、
大小和 SHA 必须与输入 `public/data/` 完全一致。Bundle 不包含 raw PSD、`_runs`、
`_legacy`、API Key、代理、日志或开发报告，因为这些内容本来就不属于 `public/data/`。

本地正式工具位于 `09_deploy/usda_release/usda_runtime_data.py`。打包命令必须显式提供
已成功的 fetch manifest，并在 USDA 数据链全部通过后执行：

```powershell
python 09_deploy/usda_release/usda_runtime_data.py package `
  --source-data '11_独立应用/USDA平衡表/public/data' `
  --output-parent '<本地唯一输出目录>' `
  --builder-git-sha '<完整 Git SHA>' `
  --builder-tree-sha '<完整 Tree SHA>' `
  --source-fetch-manifest '11_独立应用/USDA平衡表/data/raw/usda_psd_api/YYYY-MM/manifest.json'
```

Validator 仅使用 Python 标准库，逐文件检查大小/SHA、稳定 bundle SHA、JSON、非有限值、
必要闭包、matrix count、前月 snapshot、非法额外文件、路径逃逸、symlink、hardlink 和
兼容性。服务器只执行 `validate`、`stage`、`promote`、`rollback` 和证据密封；不得 fetch、
build:data、前端 build 或安装 Node/pnpm。

宿主 Runtime 根为 `/home/ubuntu/market-data-runtime/usda/data/`，包含 `incoming/`、
`releases/`、`failed/`、`evidence/`、`current.json`、`current`、`previous`、`legacy`。首次
embedded → Runtime 迁移时，`legacy` 只允许一次性指向已验证的 immutable migration seed；
正常 promote 和 rollback 均不得改变它。旧客户端 `/usda/data/...` 请求由 Nginx 读取该固定
release，新客户端继续使用 `current.json` 和 immutable release URL。Compose 仅把
这个稳定父目录只读挂载到 `/runtime/usda`，不得直接 bind `current` 子目录。Release
目录不可覆盖；候选完整验证后同盘 rename，immutable HTTP 验证后才原子切换 pointer。
失败候选隔离到 `failed/`，旧 current 保持不变。Rollback 只切 Runtime pointer，不切
Image，post-check 失败时自动恢复原 current。

正式首次 seed 必须从明确指定的生产 Image data 目录按实际字节提取；Windows
`public/data` 只能做 JSON 语义对照，不能宣称与 Image 字节 SHA 相同。

## 6. 数据发布和代码发布

- 普通月度数据只走 Runtime bundle，不提交数据、不 push、不构建 Image、不切 Image、不重启容器。
- 修改 `src/`、脚本、依赖、Dockerfile 或 Nginx 属于代码更新。
- 修改数据 Schema、app contract、页面、算法、serving contract、Compose 或 Runtime 工具属于代码更新，必须走 main-first 和候选 Image 链。

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

## 7. 停止条件

- 目标月份原始数据不唯一或不可读；
- 原始数据进入未批准的 Git 变更；
- 数据检查、比较、测试或页面预览失败；
- 生产地址依赖 catalog 本地示例；
- 服务器构建目录是 `/home/ubuntu/market-data`；
- 候选和正式 Image ID 不一致；
- 正式切换命令包含 build；
- 部署结果或 Manifest 缺失。
- Runtime manifest 不完整、release ID 非法、逐文件身份不符或兼容性门禁失败；
- Runtime data 请求回退到 SPA、父目录挂载不是只读，或 incoming/releases 不在同一文件系统。
