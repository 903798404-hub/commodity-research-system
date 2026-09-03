# 开发与部署规则

## 权威文档与优先级

- 当前文档唯一入口是 [`07_docs/00_文档索引与适用范围.md`](07_docs/00_文档索引与适用范围.md)。
- 规则优先级：`AGENTS.md` → 当前任务或项目专项契约 → [`07_docs/03_标准开发与生产发布规范.md`](07_docs/03_标准开发与生产发布规范.md) → [`07_docs/04_开发与发布检查清单.md`](07_docs/04_开发与发布检查清单.md) → [`07_docs/06_日常运行与数据更新手册.md`](07_docs/06_日常运行与数据更新手册.md) → [`07_docs/archive/`](07_docs/archive/) 历史材料。
- 系统架构和目录边界见 [`07_docs/01_系统架构与项目边界.md`](07_docs/01_系统架构与项目边界.md)，数据与输出规则见 [`07_docs/02_数据与输出规范.md`](07_docs/02_数据与输出规范.md)，执行环境和跨环境传输细则见 [`07_docs/05_执行环境与跨环境传输规范.md`](07_docs/05_执行环境与跨环境传输规范.md)。
- [`07_docs/archive/`](07_docs/archive/) 和 `legacy-sources/` 只保存历史证据，不得作为当前执行依据。

## 代码基线

- 整个 `market-data` 仓库是农产品研究系统唯一开发源、唯一 Git 根目录和唯一代码基线；服务器仅用于生产运行，不作为日常开发环境。
- GitHub 仓库 `commodity-research-system` 的 `main` 是唯一远程可信主线；本地 `main` checkout 必须保持为 clean `origin/main` 镜像，只用于只读审计和同步，不得作为 feature 开发、临时 merge、集成验收或 release 工作区。所有 feature、integration 和 release 工作都必须使用自己的 branch/worktree；验收通过后从获批 branch 以普通 fast-forward 更新 `origin/main`，再同步本地 `main`。所有可部署代码都必须有明确的完整 Git SHA。
- USDA 子项目正式位置为 `11_独立应用/USDA平衡表/`，继续保持独立前端项目结构，但不得拥有嵌套 `.git`。
- Oil World 子项目正式位置为 `11_独立应用/OilWorld平衡表/`，源码、配置和发布数据受主仓库管理，授权原始资料保存在 Git 仓库外。
- 禁止重新创建与 `market-data` 同级的独立 USDA 开发目录；历史独立目录只能作为过渡备份保留，不得继续开发。
- USDA 子目录中的 `AGENTS.md` 对该子项目继续生效；与根规则同时适用时，以更严格的数据和部署保护规则为准。
- 所有代码、配置、测试和部署文件的功能或修复必须先在本地完成。除紧急线上故障外，不得直接修改服务器正式源码。
- 服务器必须使用仓库专用只读 Deploy Key 和固定 SSH 包装脚本，在全新、独立、干净的浅克隆 `tool_repo_root` 中取得目标完整 SHA，保持 detached HEAD，并核验远程 SHA、本地 HEAD、Tree SHA、clean 状态和必要的 `git fsck`；不得使用 worktree 作为正式流程。
- `/home/ubuntu/market-data` 是 `production_project_dir`，不得在其中为目标 Release 执行 checkout、依赖安装、测试、镜像构建或候选准备；不得依赖来源不明的目录、压缩包或整文件夹覆盖。
- 紧急线上修复必须立即同步回本地，完成测试、commit、push，并重新按正式 Git 提交部署；在完成回流前不得开始下一轮开发或部署。
- 禁止未经差异比较就以整个本地目录覆盖服务器，或以整个服务器目录覆盖本地。版本分叉时，先比较文件内容、Git diff 与哈希，再合并。
- 服务器地址通过环境变量注入；不得将公网 IP 写入源码。本地只保留 `.env.example`，不得提交 `.env`。

## 执行环境预检

- 开始任何超过简单文本修改的任务前，必须先按[执行环境与跨环境传输规范](07_docs/05_执行环境与跨环境传输规范.md)完成预检并输出结果。
- 预检至少确认：操作系统、Shell、当前目录、Git 根目录、HEAD、分支、`git status --short`；所有任务输入的实际绝对路径、存在性、可读性及必要时的 SHA-256；命令所属执行环境、该环境能否直接访问输入、是否需要跨环境传输；本任务依赖的工具与权限是否可用。
- Windows 本地、Codex 执行与附件环境、ChatGPT 会话沙箱、Ubuntu 生产服务器是四个相互隔离的环境。路径只在所属环境有效，同名文件不代表同一文件；跨环境后必须重新核验文件身份。
- 预检失败必须停止正式执行并准确报告单一缺失条件。不得自行安装软件、创建替代文件、从未知目录复制相似文件、修改 `PATH`、扩大权限或绕过身份与主机校验。
- 长命令必须前台运行并返回明确退出码，外层超时必须长于命令内部超时并留有缓冲。禁止以 `nohup`、后台执行、`Start-Job`、忽略退出码或刚启动时输出文件仍为 0 字节来绕过超时或判断失败；不得因此重复启动同一任务。
- 涉及跨环境传输、中文路径、中文正文、JSON 或 Markdown 时，必须执行 UTF-8 严格读取、传输后身份复核和编码往返检查；发现损坏立即停止，不得覆盖正式文件。

## 目录与数据

- 必须保持编号目录：`01_data`、`02_configs`、`03_src`、`04_scripts`、`05_apps`、`06_outputs`、`07_docs`、`08_tests`、`09_deploy`、`10_logs`。
- 不得重新创建旧目录：`data`、`scripts`、`output`、`logs`、`docs`、`deploy`。
- 核心代码在 `03_src/agri_research_agent/`，执行入口在 `04_scripts/`，应用页面在 `05_apps/`。
- 不得覆盖或改写 `01_data/manual/` 的人工原始文件；不得清空 `01_data`。
- 部署 market-data 时默认不得覆盖或删除 `01_data/`、`.env`、`10_logs/`、`06_outputs/`。
- 服务器“只读部署”只限制代码获取权限，不代表运行目录不可写；`01_data/`、`06_outputs/`、`10_logs/` 和 `.env` 属于可持续写入的服务器运行内容，部署时必须原样保护。
- 服务器以后只部署一个 `market-data` 项目目录；USDA 镜像从仓库内 `11_独立应用/USDA平衡表/` 构建，不再依赖同级项目目录。
- 数据更新与代码部署是两个独立流程；部署不得顺带覆盖运行数据，数据更新不得隐式替换代码。

## 测试与部署

标准流程：独立 feature branch/worktree → 本地修改 → Direct Tests → Project Scope Gate → Impact/必要 Full Tests → 查看 Git diff → 显式 Git commit → 独立 integration/release 验收 → 从获批 branch 普通 fast-forward 更新 `origin/main` → 同步 clean local main 镜像 → 服务器独立只读浅克隆并核验精确提交 → 构建和验证候选镜像 → 密封候选结果和证据 → 删除候选容器并确认不存在 → 生成部署计划 → 使用同一 Image ID 正式切换且禁止 build → 密封部署结果与 Manifest。

- 日常修改先运行与变更直接相关的定向测试；部署、清理、固定基线、跨应用接口或高风险依赖变更等关键节点运行对应完整回归。
- 普通业务 feature 必须声明 owned paths，并在进入 Impact/完整回归、commit 或 integration 前运行 `04_scripts/quality/audit_changed_scope.py`。默认禁止跨项目以及修改 Weather producer、shared Public refresh、统一 refresh、shared infrastructure 和部署基础设施；出现这些变化时，只有任务事先明确分类为 `shared` change 才可继续。
- Windows 本地没有 Docker、Podman 或 WSL 属于正常状态；本地不负责生产镜像构建，本地 Docker 构建不再是 commit、push 或部署的前置条件。
- 不得再建议用户安装 Docker Desktop、Podman 或 WSL，也不得要求用户为本项目安装这些工具。Windows 本地只负责代码修改、Python 和前端测试、Streamlit 启动检查、Dockerfile 与 Compose 静态检查、构建上下文文件存在性检查，以及 Git 差异和工作区检查。
- 涉及 Dockerfile、docker-compose.yml、依赖、字体或部署配置时，必须在服务器隔离候选目录中重建对应镜像，不得在服务器正式仓库中直接构建。
- 服务器候选源码必须来自独立只读浅克隆的完整 SHA；候选镜像必须带不可变 SHA 标签和 `org.opencontainers.image.revision=<完整SHA>`。
- 候选容器只是一次性测试容器，只能绑定服务器本机测试端口；成功或失败都必须先保存有限脱敏证据并密封候选结果，再删除候选容器并确认不存在，之后才可生成部署计划。服务器平时只保留 `spread-dashboard`、`usda-dashboard`、`oil-world-dashboard` 三个正式运行容器。
- 候选验证通过后保留候选镜像，正式部署直接使用同一个镜像 ID；该镜像必须已经通过验收，不得对同一个提交重新构建第二个正式镜像。
- 候选容器如需正式数据，只允许只读挂载；缓存、日志和临时文件必须写入对应候选目录的 `runtime/`，不得修改正式业务数据。
- 仅修改一个服务时，不得无必要重建或重启另一个服务。
- `USDA_DASHBOARD_URL` 是正式环境变量；不要在页面代码中硬编码地址。
- `09_deploy/spread_release/` 仍是候选实现：代码和模拟契约测试已完成，2026-07-19 指定测试为 154 passed、3 skipped，pyarrow 已不再是当前阻塞项；真实 Docker Compose 验证仍未完成，在该门槛通过前不得用于生产部署或称为正式生产工具。USDA 和 Oil World 的一条式工具仍待实现。

## 破坏性操作

- 删除、覆盖、迁移、数据同步或容器替换前，必须先只读检查运行引用和恢复来源，并说明影响范围、备份位置、回滚方案和预计服务影响。
- 默认禁止执行：`docker system prune`、`docker volume prune`、`docker image prune -a`、覆盖服务器 `.env`、删除当前正式容器或正式镜像、删除唯一有效备份、`rm -rf` 正式项目目录。

## 任务汇报

完成后必须说明：修改文件、修改原因、测试结果、是否修改数据、是否连接服务器、是否部署、是否执行 Git、是否存在未解决风险。
