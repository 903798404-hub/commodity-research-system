# Governance Transition contract

状态：feature 实现与验证合同；尚未安装到 authoritative main，不代表已启用。生产发布规范不变。

## 修改分类和正常入口

| trusted main 对完整 diff 的分类 | main-entry 条件 |
| --- | --- |
| BUSINESS | 已授权普通业务直接 branch/worktree → code → tests → 同一 required check PASS → 精确 fast-forward |
| STRICT_SHARED | 当前可信 Scope、required/impact/必要 full tests PASS；不额外要求 Governance approval |
| GOVERNANCE_TRANSITION | 所有可信检查通过，再核验精确 base/Commit/Tree 的外部 maintainer approval |
| 未授权 production/data mutation | FAIL；治理批准不能覆盖 |

PROJECT_EXISTENCE_APPROVAL_REQUIRED = NO

GOVERNANCE_TRUST_ROOT_APPROVAL_REQUIRED = YES

普通 Soybean bug fix 不需要 maintainer governance approval、Registry 改动、Ruleset bypass 或项目存在批准。仍遵守任务明确的暂停、范围和 main 接纳限制。Shared 只有实际修改治理信任根才转 Governance。

分类由 trusted main 的 `main_admission.py:is_governance_transition` 决定：TRUST_FILES 指定 Registry、Scope、Admission、schema、module test map、project_registry、pyproject、依赖与 workflow、transition verifier；另含 `04_scripts/quality/`、`.github/`、AGENTS/test configuration、requirements，以及 trusted dev-governance 的 required/future required tests。具体 ownership 必须来自可信 Registry；candidate Registry 不可自行获取其他路径。

## 审批协议

复用现有 `.github/workflows/trusted-main-admission.yml` 的 workflow_dispatch，不增加 App、服务器、数据库、审批文件或写权限 token。

仅 main 上的 maintainer/admin 手动运行可以签发信号。输入 `transition_intent=TRUSTED_GOVERNANCE_TRANSITION_APPROVED` 及完整 40 位 `base_main`、`candidate_commit`、`candidate_tree`。可信 workflow 的 run-name 将三者绑定，issuer 只检出 main、读取 candidate Git 对象并验证治理分类，不执行 candidate Python 或测试。

Admission 的 `approval_run_id` 只是查询指针，不是凭证。Verifier 从 GitHub API 重新读取：固定 repository；workflow_dispatch；main/head SHA；可信 workflow 路径与 active 状态；精确 run-name；首次 run_attempt；completed/success；actor 与 triggering_actor 都是当前 maintainer/admin 的真实 User，且 permission 返回的用户 ID 一致；fresh main 与 candidate tree。缺失、拒绝、删除、网络失败均 FAIL_CLOSED。actor、branch、commit message、candidate JSON 或环境变量自称 approved 无效。Issuer 尚在运行时只跳过 completed 条件；Admission 必须等待最终成功。

任何新 candidate commit（即使 tree 相同）或 base main 变化都须重新验证、重新 dispatch。撤回可删除审批 run；rerun 不续签审批。完整原生 run 是审计记录，批准本身不是 main 合并或 production 授权。[GitHub workflow run API](https://docs.github.com/en/rest/actions/workflow-runs) 提供这些运行身份；当前角色从 [collaborator permission API](https://docs.github.com/en/rest/collaborators/collaborators) 核验。

可信 tests 计划继续是 required tests UNION candidate changed/added owned tests；运行 candidate 版本，不强制被合法替换的旧 blob。required 删除、非空集合、policy 减测、测试中修改对象、Scope 和精确身份仍为硬失败。无审批时也先运行能安全执行的可信检查，随后输出 GOVERNANCE_TRANSITION_PENDING，不赋予 PASS。

执行中的 workflow 不得修改自己的执行配置或聚合逻辑；语义变化返回 TRUSTED_WORKFLOW_EXECUTION_CHANGED。关键 workflow 升级必须分阶段：先经外部批准升级可信验证政策，再另行批准后续 workflow 对象；不得让同一 candidate 用自身新政策放行自己。

## Phase A：一次性 bootstrap（本轮不执行）

固定待接纳对象：

- Commit：`7bb5d53d233fb354efaf9df7f598db3cba15206f`
- Tree：`d22e9e493dd941fef4d57c4684e7254f6bdd8ee5`
- 本次审计 base：`c3cb97376574f9edd93ee4381f5c843ed198a634`；操作前必须重新 fetch/ls-remote/API 核对，不能沿用本文静态值。

该对象仅有 Governance V2，不含本合同的新 verifier/workflow。它进入 main 不会自动启用 Phase B；本 feature 是后继对象，需要单独验证、批准和可信安装。不得声称把 7bb 接纳一次就完成本机制安装。

bootstrap 前置顺序：重新核验 exact identities 和祖先关系 → 在 hosted runner 重跑该对象的 governance regression → 读取 Active Ruleset 与唯一 required check/integration → maintainer 明确批准上述三元组 → 仅在平台存在满足限制的 transition 能力时进行 exact fast-forward → 重新核对 main/Ruleset。任一步缺失停止。

本次 Ruleset `22616560` / main-trusted-admission-v1 为 Active，唯一 required check 是 trusted-main-admission-v1，GitHub Actions integration_id=15368，bypass_actors=[]，同时保护 non-fast-forward、deletion、linear history。

GitHub bypass API 不能按 change class 或精确 SHA 限定，只提供 actor 和 always/pull_request/exempt 模式；对 actor 授权适用于该 Ruleset，并非只豁免 admission。PR-only 不能保证原始 7bb Commit 成为 main（merge/squash/rebase 对象不同）；always 又扩大 actor 的非治理绕过能力及同一规则集的其他保护。文档约定不是机器边界。[原生 Ruleset schema](https://docs.github.com/en/rest/repos/rules) 未提供所需的对象条件。

ONE_TIME_BOOTSTRAP_READY = NO

EXACT_MANUAL_ACTION_REQUIRED：当前没有满足全部限制的 GitHub 页面修改步骤。保持 Settings → Rules → Rulesets → main-trusted-admission-v1 的 Active、现有 required check 和空 bypass list；本轮不要点击增加 actor 或禁用。需要用户先决定是否调整约束/采用额外平台保护，才能制定可执行的最小变化；不得把这一决策冒充已批准操作。

## Phase B：稳态操作（仅可信安装后）

1. 从 fresh main 开发，按可信分类运行所有检查。Business/Shared 不调用审批 API。
2. Governance 检查合格后，maintainer 在 Actions → trusted-main-admission-v1 → Run workflow，分支选择 main，填写审批 intent 与精确三元组。不要在 feature 上签发。
3. 记录成功审批 run ID。在同一 workflow 再 Run workflow，选 candidate feature，intent 留空，填写 approval_run_id。分支只是定位入口；Admission 核验实际精确 commit/tree，变更即使同 tree 也失效。
4. 核对 fresh main、同一对象、可信执行来源、artifact 和唯一 required check PASS 后，在任务接纳授权内 exact fast-forward。治理升级不需要永久改 Ruleset。用户明确要求等待 main 批准时必须停下。

## 原生发布者保护的未闭合边界

现有 Ruleset 只固定 check context + GitHub Actions App，不能固定 workflow 文件的可信版本。candidate 工作流也由同一个 App 发布；故仅看绿色同名 check 不足以证明可信 Admission 执行。新 verifier 拒绝伪造 approval，新 trusted validator 拒绝 workflow 聚合变更，但无法阻止恶意 candidate 完全不运行 validator 而发布同名 check。这是平台接纳边界限制，不是 regression PASS 可以代替的保护。[GitHub required checks](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) 的 expected source 是 App。

因此本 feature 不声明端到端 GOVERNANCE_TRANSITION_IMPLEMENTATION_READY=YES；完整接纳能力还需能强制可信 workflow 来源的平台保护及首次安装路径。当前个人私有仓库不得虚构具备组织级 required-workflow 能力，也不得创建新 App 或以永久 bypass 代替。验证合同内部 candidate 无法伪造 maintainer approval；当前平台整体 CANDIDATE_SELF_APPROVAL_POSSIBLE=YES（同名 check 的已有风险，未进行攻击发布）。

## Production 独立边界

main != production。Approved 身份、Commit/Tree/Image、candidate 不写 production、rollback、数据 Manifest 与生产单独授权全部保留。Transition receipt 明确 production_authorization=false；没有任何 issuer 或 Admission job 可以部署、刷新数据或修改 Ruleset。Public、Domestic Spread、Soybean 生产问题不在本轮修改范围。
