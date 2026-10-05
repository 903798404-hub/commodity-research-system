# 开发与部署规则

## 权威文档与优先级

- 当前文档唯一入口是 [`07_docs/00_文档索引与适用范围.md`](07_docs/00_文档索引与适用范围.md)。
- 规则优先级：`AGENTS.md` → 当前任务或项目专项契约（只能收紧边界）→ [`07_docs/03_标准开发与生产发布规范.md`](07_docs/03_标准开发与生产发布规范.md)；检查清单、运行手册和模板只帮助执行。archive 不在执行权威链中。
- 系统架构和目录边界见 [`07_docs/01_系统架构与项目边界.md`](07_docs/01_系统架构与项目边界.md)，数据与输出规则见 [`07_docs/02_数据与输出规范.md`](07_docs/02_数据与输出规范.md)，执行环境和跨环境传输细则见 [`07_docs/05_执行环境与跨环境传输规范.md`](07_docs/05_执行环境与跨环境传输规范.md)。
- [`07_docs/archive/`](07_docs/archive/) 和 `legacy-sources/` 只保存历史证据，不得作为当前执行依据。

## 代码基线

## 执行环境预检

- 开始任何超过简单文本修改的任务前，必须先按[执行环境与跨环境传输规范](07_docs/05_执行环境与跨环境传输规范.md)完成预检并输出结果。
- 预检至少确认：操作系统、Shell、当前目录、Git 根目录、HEAD、分支、`git status --short`；所有任务输入的实际绝对路径、存在性、可读性及必要时的 SHA-256；命令所属执行环境、该环境能否直接访问输入、是否需要跨环境传输；本任务依赖的工具与权限是否可用。
- 环境分类以执行环境规范的六类环境为准。路径只在所属环境有效，同名文件不代表同一文件；跨环境后必须重新核验文件身份。
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

Windows 本地没有 Docker、Podman 或 WSL 属于正常状态；本地不负责生产镜像构建；不得再建议用户安装 Docker Desktop、Podman 或 WSL。生产镜像只从获批独立源码构建一次，Candidate 与 Production 正式部署直接使用同一个镜像 ID，不在正式业务目录构建。candidate 不写 production；代码发布不顺带更新数据或运行 FULL DAILY。

## 破坏性操作

- 删除、覆盖、迁移、数据同步或容器替换前，必须先只读检查运行引用和恢复来源，并说明影响范围、备份位置、回滚方案和预计服务影响。
- 默认禁止执行：`docker system prune`、`docker volume prune`、`docker image prune -a`、覆盖服务器 `.env`、删除当前正式容器或正式镜像、删除唯一有效备份、`rm -rf` 正式项目目录。

## 任务汇报

完成后必须说明：修改文件、修改原因、测试结果、是否修改数据、是否连接服务器、是否部署、是否执行 Git、是否存在未解决风险。

## 主流开发路径

统一流程：main → feature/fix branch → implementation → automated tests → required CI → main。使用独立 worktree；可选 start_project.py --project <id> --branch feat/<name> --worktree <path> --create，直接创建 feature/fix branch 同样合法。Registry 只用于 metadata、planning、impact/test mapping 和责任登记，不是普通开发的 hard authorization；缺 owner 不阻止项目启动。PROJECT_EXISTENCE_APPROVAL_REQUIRED = NO；ORDINARY_BUSINESS_NEEDS_HUMAN_APPROVAL = NO；REGISTRY_IS_HARD_AUTHORIZATION = NO；STAGED_GOVERNANCE_MIGRATION_REQUIRED = NO。

Business 跑 scoped required tests；Shared/Infra 加 impact/consumer tests；Governance/CI 加治理及平台测试，MAINTAINER_REVIEW_REQUIRED = YES。Repository Maintainer/Admin 审查 main 接纳，不能覆盖 CI 失败。trusted、future、impact、candidate changed/added 与平台测试必须全部 required 全绿；Windows 专项使用 windows-2022。full regression 暂用 exact base/candidate node 对照，不得新增 failure/skip 或删除 base tests；既有 failure/skip 的节点与数量动态报告为技术债，不计 PASS；main 全绿后删除此过渡 ratchet。trusted-main-admission-v1 是 main required check，不使用 Ruleset bypass。可选 complete_project.py --candidate-record <记录> 若调用仍执行全部 required tests 与签名容器门禁，不是普通 main 前置条件。

START / RESUME 会 fetch 并写 local Git metadata，不是纯 read-only。开发启动不依赖无关 local main checkout 是否 clean、mirror；只有同步 main 镜像时才核验。runtime_target=none 不要求 runtime root、marker 或 production evidence。普通已授权任务不重复要求 commit/push 许可。执行 git fetch origin 与 git ls-remote origin refs/heads/main 核对 fresh main；聊天历史 SHA 不是执行权威。

## 生产发布风险

main != production。生产单独授权；Approved identity、Commit/Tree/Image、Manifest、rollback 和 candidate 不写 production 均保留。Build Once / Deploy Same Image：Application image 只构建一次，同一 exact image 经过 Candidate 后进入 Production；tooling Commit 与 Application Target 可以分别核验。

ROUTINE_STATELESS：无新增持久状态或 migration。Build Once → exact image Candidate machine smoke → Maintainer UI/business acceptance → Deploy Same Image → Production machine smoke → Maintainer final acceptance → deployment record；不要求 recovery rehearsal。

ADDITIVE_REVERSIBLE：新增独立持久状态且旧历史不变、无破坏性或单向迁移、rollback 不需 reverse migration、新数据保留且兼容 re-upgrade 可读。自动兼容性检查后走同一 Candidate/Production 验收路径；FULL_ROLLBACK_REHEARSAL_REQUIRED = NO；TARGETED_RECOVERY_VALIDATION_REQUIRED = NO。真实 targeted recovery 是可选诊断或独立 Recovery Runtime Lifecycle Hardening 技术债。

IRREVERSIBLE / DESTRUCTIVE / UNKNOWN：保持严格 migration/recovery 验证与独立授权；UNKNOWN 不自动 PASS，机器发现 destructive evidence 时人工不得降级。

Candidate 必须独立 project/container/network、localhost-only、不加入 Production network；Production-backed immutable inputs 只读，所需写入仅落 candidate-owned isolated RW，绝不写 Production CNF、AM result 或历史数据。机器核验 Commit/Tree/Image、mount/network/write isolation、fresh grant、runtime preflight、health/HTTP；Maintainer 验收 UI、图表和业务语义，不能覆盖机器失败。低风险 UI 可选 MANUAL / AUTOMATED；MANUAL 不要求 Playwright/Chromium，WAITING_FOR_MAINTAINER_UI_ACCEPTANCE 不是发布 PASS。

严格比较 artifact identity 与有效 runtime semantics，保留 raw evidence。grant、Manifest、Docker compatibility、namespace projection 和 recovery 细节见 09_deploy/runtime_identity/说明.md，不进入普通业务 checklist。main CI PASS 不授予部署或数据写入权限。
