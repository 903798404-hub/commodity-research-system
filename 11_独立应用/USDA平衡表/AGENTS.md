# 开发与部署规则

本文件只补充 USDA 数据/UI 保护；最高开发治理为[根 AGENTS](../../AGENTS.md)，
标准开发/发布唯一规则见[根规范](../../07_docs/03_标准开发与生产发布规范.md)。
使用 Project Registry 的 `usda`；独立 feature/integration worktree，local main 只读 clean mirror。

## 代码基线

- 本目录是 `market-data` 唯一 Git 仓库内的正式 USDA 子项目，正式位置为 `11_独立应用/USDA平衡表/`；不得创建嵌套 `.git` 或恢复同级独立开发目录。
- 整个 `market-data` 本地仓库是唯一开发源和唯一代码基线；服务器仅用于生产运行，不作为日常开发环境。
- 所有功能、修复、配置和测试先在本地完成。除紧急线上故障外，不得直接修改服务器正式代码。
- 紧急线上修复必须先同步回本地、完成测试并纳入正式基线，才能继续开发或再次部署。
- 禁止未经差异比较就以整个本地目录覆盖服务器，或以整个服务器目录覆盖本地。版本分叉时，先比较文件内容、Git diff 与哈希，再合并。
- 服务器地址通过环境变量注入；不得将公网 IP 写入源码。本地只保留 `.env.example`，不得提交 `.env`。

## 数据与页面

- `public/data/` 是前端正式数据。`index.json`、`report_version.json`、`presentation_changes.json`、`matrix/` 与 `snapshots/` 均不得随意删除。
- 原始数据与月度快照用于后续构建和修正比较；修改或清理前必须先确认构建、月修和展示页依赖。
- 年度供需展示页内部切换豆系与菜系；不得将豆系或菜系拆成农产品看板左侧的独立导航。
- 所有资源路径与页面跳转必须兼容 `VITE_BASE_PATH=/usda/`。
- 生产必须使用 `Vite build` 产出的静态文件和 Nginx；不得使用 Vite 开发服务器。
- 部署后必须验证 `/usda/`、`/usda/presentation`、`/usda/data/index.json`。

## 测试与部署

不在此复制发布流程。先 Scope Gate `--project usda` 和专项验证，获授权后依根规范进行独立 integration/release、可信检出、候选和同一 Image ID 切换；不得在正式目录构建或直接覆盖代码。

- 修改 USDA 后必须运行前端测试、TypeScript 检查和生产构建。
- 涉及 Dockerfile、docker-compose.yml、依赖、字体或部署配置时，必须重建对应镜像。
- 仅修改一个服务时，不得无必要重建或重启另一个服务。
- `USDA_DASHBOARD_URL` 是正式环境变量；不得硬编码公网 IP。

## 破坏性操作

- 删除、覆盖、迁移、数据同步或容器替换前，必须先只读检查并说明影响范围、备份位置、回滚方案和预计服务影响。
- 默认禁止执行：`docker system prune`、`docker volume prune`、`docker image prune -a`、清空正式数据、覆盖服务器 `.env`、删除当前正式容器或正式镜像、删除唯一有效备份、`rm -rf` 正式项目目录。

## 任务汇报

完成后必须说明：修改文件、修改原因、测试结果、是否修改数据、是否连接服务器、是否部署、是否执行 Git、是否存在未解决风险。
