# Governance / CI Maintainer Review

## Mainstream Development Governance

统一流程：`main → feature/fix branch → implementation → automated tests → required CI → main`。
Repository Maintainer/Admin 审查并接纳 Governance / CI candidate；review 是 integration decision，不是 CI failure。Worktree 仅用于并行开发；可用 START / RESUME 自动 fetch 并建立工作区，也可直接建立 feature/fix branch，不存在项目存在审批。

Registry 是 project/module metadata、ownership documentation、test mapping、impact analysis、runtime target 和维护责任登记，不是普通源码修改的 hard authorization。无 owner 不阻止合法项目启动；同一个明确的 Governance / CI candidate 可以原子修改 Registry、owner、test mapping、Admission、workflow、相关实现、测试和文档。不得借 metadata 偷偷扩大无关模块 Scope、减少 required tests 或取得生产权限。

Business 运行 scoped required tests；Shared / Infra 增加 impact/consumer tests，未映射 shared source 使用更广回归；Governance / CI 运行 governance regression 和相关平台 CI，并记录 `CHANGE_CLASS = GOVERNANCE_OR_CI`、`MAINTAINER_REVIEW_REQUIRED = YES`。技术验证 PASS 时 `trusted-main-admission-v1` PASS；failing tests、Scope violation、required test deletion、Registry 减测和未授权 production mutation 始终 FAIL，review 不能覆盖失败。

required plan 保留 base required/future/impact obligations，并 UNION candidate 新增/修改测试及 candidate 新增 mapping。candidate tree 中的测试版本必须真正执行，collection 非空，失败和 skip 均不得记为 PASS。Windows-specific required tests 使用 `windows-2022`；跨平台测试使用 Linux。最终 required check 聚合同一 base / candidate Commit/Tree、plan 和 workflow run/attempt 的实际平台 job；缺少必需平台结果即 FAIL。没有 Windows dependency 的 Business 不启动 Windows suite。

候选 workflow 是 Maintainer review 的信任对象；不承诺防御恶意 Maintainer 修改自己的 CI。一个 candidate 完成相关实现和 CI，无须先安装 owner 或 executor、无须分阶段 main transition。没有 candidate approval server、审批 token 或额外 GitHub App。

main Ruleset 保持 Active、required check `trusted-main-admission-v1`、non-fast-forward/deletion protection；不使用 bypass 作为正常开发路径，不修改 GitHub settings。提交前检查 diff/scope/tests；只显式 git add 文件。任何新 commit 都重新取得 exact hosted evidence。取得测试 PASS 后，main 接纳仍按任务授权，由 Maintainer 决定；本地 main 保持 clean 镜像，接纳只用普通 fast-forward，不 force、rebase、squash 或额外 merge commit。

`PROJECT_EXISTENCE_APPROVAL_REQUIRED = NO`；`ORDINARY_BUSINESS_NEEDS_HUMAN_APPROVAL = NO`；`REGISTRY_IS_HARD_AUTHORIZATION = NO`；`STAGED_GOVERNANCE_MIGRATION_REQUIRED = NO`。

`main != production`。生产单独授权；Approved identity、Commit/Tree/Image、Manifest、release gate、rollback、candidate 不写 production 均保留。Registry ownership、CI PASS 和进入 main 不授予 server deployment、production data mutation、FULL DAILY 或 runtime grant 权限。Production Release 合同独立执行。

核对清单：精确 base/Commit/Tree；完整 hosted Linux/Windows 结果；changed-file Scope；required coverage；生产未触碰；Maintainer 接纳决定。本合同不执行 main push 或 production release。
