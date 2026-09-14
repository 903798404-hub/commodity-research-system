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

`runtime_target=production_container` 的真实容器证据属于独立 production/release gate。main-entry correctness 由 hosted strict lane 验证，不以生产环境访问或签名容器 Completion 作为 main 前置条件。生产候选仍须明确授权、绑定 clean Commit/Tree/Image，candidate 不写 production；正式发布核验 Approved Production SHA、数据 Manifest、同一已验证 Image ID 和 rollback。Completion 若调用仍执行全部 required tests 与签名容器门禁；main PASS 不授予生产权限。

- Windows 本地没有 Docker、Podman 或 WSL 属于正常状态；本地不负责生产镜像构建，本地 Docker 构建不再是 commit、push 或部署的前置条件。
- 不得再建议用户安装 Docker Desktop、Podman 或 WSL，也不得要求用户为本项目安装这些工具。Windows 本地只负责代码修改、Python 和前端测试、Streamlit 启动检查、Dockerfile 与 Compose 静态检查、构建上下文文件存在性检查，以及 Git 差异和工作区检查。
- 涉及 Dockerfile、docker-compose.yml、依赖、字体或部署配置时，必须在服务器隔离候选目录中重建对应镜像，不得在服务器正式仓库中直接构建。
- 服务器候选源码必须来自独立只读浅克隆的完整 SHA；候选镜像必须带不可变 SHA 标签和 `org.opencontainers.image.revision=<完整SHA>`。
- 候选容器只是一次性测试容器，只能绑定服务器本机测试端口；下述清理时点对 Routine MANUAL 等待采用后文例外；成功或失败都必须先保存有限脱敏证据并密封候选结果，再删除候选容器并确认不存在，之后才可生成部署计划。服务器平时只保留 `spread-dashboard`、`usda-dashboard`、`oil-world-dashboard` 三个正式运行容器。
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

## Mainstream Development Governance

### Full Regression Policy

过渡性 baseline-aware `NO_NEW_REGRESSION` 仅用于 full repository regression。Green base → candidate must remain green；legacy-debt base → candidate must introduce no new failures, no new skips and no test deletion。按完整 collected test node identity 动态对比同一次 hosted run 的 authoritative base 与 candidate；新增 tests 必须 PASS，允许既有 failure/skip 保持或修复，不维护固定 failure allowlist。既有节点与数量作为 `TECHNICAL_DEBT` 持续报告，长期目标仍是 authoritative main full suite green。

trusted required、future-required、impact/consumer、platform-required 和 candidate changed/added tests 仍是绝对全绿硬 Gate，FAIL、SKIP、missing 均失败。上述“失败和 skip 不得记为 PASS”约束这些硬 Gate；full suite 的既有债务不标作测试 PASS，只能使 `NO_NEW_REGRESSION` 比较 PASS。full fallback 不得把明确 required/impact obligation 降为债务。

base/candidate 必须绑定同一 runner image/version、Python、依赖、系统包、字体、环境变量、pytest 配置/命令及各自精确 Commit/Tree/test plan。同一次 run/attempt 的完整结果才可比较；collection error、环境准备失败、receipt/plan/身份不一致、运行未完成或 base node 消失直接 FAIL。main 变化后必须重新取得对应 base 的 baseline。Windows-required 保持 Windows hosted 硬 Gate；普通无 Windows 依赖 Business 不启动无关 Windows suite。main-entry PASS 不授予生产发布权限。


统一流程：`main → feature/fix branch → implementation → automated tests → required CI → main`。
Repository Maintainer/Admin 审查并接纳 Governance / CI candidate；review 是 integration decision，不是 CI failure。Worktree 仅用于并行开发；可用 START / RESUME 自动 fetch 并建立工作区，也可直接建立 feature/fix branch，不存在项目存在审批。

Registry 是 project/module metadata、ownership documentation、test mapping、impact analysis、runtime target 和维护责任登记，不是普通源码修改的 hard authorization。无 owner 不阻止合法项目启动；同一个明确的 Governance / CI candidate 可以原子修改 Registry、owner、test mapping、Admission、workflow、相关实现、测试和文档。不得借 metadata 偷偷扩大无关模块 Scope、减少 required tests 或取得生产权限。

Business 运行 scoped required tests；Shared / Infra 增加 impact/consumer tests，未映射 shared source 使用更广回归；Governance / CI 运行 governance regression 和相关平台 CI，并记录 `CHANGE_CLASS = GOVERNANCE_OR_CI`、`MAINTAINER_REVIEW_REQUIRED = YES`。技术验证 PASS 时 `trusted-main-admission-v1` PASS；failing tests、Scope violation、required test deletion、Registry 减测和未授权 production mutation 始终 FAIL，review 不能覆盖失败。

required plan 保留 base required/future/impact obligations，并 UNION candidate 新增/修改测试及 candidate 新增 mapping。candidate tree 中的测试版本必须真正执行，collection 非空，失败和 skip 均不得记为 PASS。Windows-specific required tests 使用 `windows-2022`；跨平台测试使用 Linux。最终 required check 聚合同一 base / candidate Commit/Tree、plan 和 workflow run/attempt 的实际平台 job；缺少必需平台结果即 FAIL。没有 Windows dependency 的 Business 不启动 Windows suite。

候选 workflow 是 Maintainer review 的信任对象；不承诺防御恶意 Maintainer 修改自己的 CI。一个 candidate 完成相关实现和 CI，无须先安装 owner 或 executor、无须分阶段 main transition。没有 candidate approval server、审批 token 或额外 GitHub App。

main Ruleset 保持 Active、required check `trusted-main-admission-v1`、non-fast-forward/deletion protection；不使用 bypass 作为正常开发路径，不修改 GitHub settings。提交前检查 diff/scope/tests；只显式 git add 文件。任何新 commit 都重新取得 exact hosted evidence。取得测试 PASS 后，main 接纳仍按任务授权，由 Maintainer 决定；本地 main 保持 clean 镜像，接纳只用普通 fast-forward，不 force、rebase、squash 或额外 merge commit。

`PROJECT_EXISTENCE_APPROVAL_REQUIRED = NO`；`ORDINARY_BUSINESS_NEEDS_HUMAN_APPROVAL = NO`；`REGISTRY_IS_HARD_AUTHORIZATION = NO`；`STAGED_GOVERNANCE_MIGRATION_REQUIRED = NO`。

`main != production`。生产单独授权；Approved identity、Commit/Tree/Image、Manifest、release gate、rollback、candidate 不写 production 均保留。Registry ownership、CI PASS 和进入 main 不授予 server deployment、production data mutation、FULL DAILY 或 runtime grant 权限。Production Release 合同独立执行。

START / RESUME 会 fetch 并写 local Git metadata，不是纯 read-only。开发启动不依赖无关 local main checkout 是否 clean、mirror；只有实际 main 同步才核验镜像。runtime_target=none 不要求 runtime root、marker 或 production evidence。可选工具入口 `start_project.py --project <id> --branch feat/<name> --worktree <path> --create`；直接创建 feature/fix branch 同样合法。普通已授权任务无需重复 commit/push 许可。

执行 `git fetch origin`、`git ls-remote origin refs/heads/main` 核对 fresh main；聊天历史 SHA 不是执行权威。

## Routine Stateless UI Acceptance

只有机器分类为 ROUTINE_STATELESS 才允许选择 MANUAL 或 AUTOMATED；高风险流程不变。
MANUAL 不要求 Playwright/Chromium，AUTOMATED 保留真实浏览器依赖和 DOM 验证。低风险 UI 可选人工验收。
Commit/Tree、CI、risk、exact Image/OCI、fresh grant、runtime preflight、health/HTTP、readonly data 均保持硬 Gate；人工不能覆盖机器失败。
机器通过后 MANUAL 输出 WAITING_FOR_MAINTAINER_UI_ACCEPTANCE，保留隔离 candidate 和 URL/精确实例；这不是最终 PASS。
用户明确验收后记录 operator、timestamp、Commit/Tree/Image/实例和人工结果，无签名审批或审批服务器。
Candidate PASS 后清理候选容器并部署同一镜像，新 production 实例仍需 fresh grant；未确认/拒绝/取消不得切换。
Production 机器通过后再次等待人工 UI；人工 PASS 才完成 release，人工 FAIL 或机器失败按既有合同恢复上一精确镜像与 fresh grant。
等待允许暂留 candidate；取消、失败或验收完成时清理其明确实例并保留普通诊断记录，不删除镜像或业务数据。
