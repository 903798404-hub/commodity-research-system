# Governance Root Transition

Repository Maintainer/Admin 是 Governance Root of Trust。自动化验证技术事实；极少数治理根升级由 Maintainer/Admin 决定。本文只定义合同，具体冻结 SHA、hosted run 和是否 ready 见每次候选报告。尚未接纳的 feature 文档不表示 main 已启用新政策。

## 四条路径

| 修改 | 正常路径 |
| --- | --- |
| BUSINESS | main → feature/fix branch/worktree → tests → trusted admission PASS → main |
| STRICT_SHARED | main → feature → strict/impact/consumer tests → trusted admission PASS → main |
| GOVERNANCE_TRANSITION | main → governance feature → trusted technical validation + full governance regression → GOVERNANCE_ROOT_APPROVAL_REQUIRED → Maintainer/Admin 接纳精确对象 → main |
| Production | main != production；独立 release |

`PROJECT_EXISTENCE_APPROVAL_REQUIRED = NO`；`ORDINARY_BUSINESS_NEEDS_HUMAN_APPROVAL = NO`。Soybean、Domestic Spread、USDA、普通 UI 和 producer consumer business 修改，只要保持各自可信 ownership、未修改治理根，就不需要治理审批、Registry 改动、manual bootstrap 或 bypass。真正 shared producer 继续 strict，不改变类别绕过检查。

`CANDIDATE_CONTROLLED_SELF_APPROVAL = NO`；`MAINTAINER_GOVERNANCE_ROOT_OVERRIDE = YES_BY_DESIGN`。前者表示可信 validator 没有 candidate 可调用的审批接口；后者是 GitHub 管理权限，属于 Maintainer/Admin，不属于 candidate。这里不声称管理员无法覆盖自己的 Ruleset。现有 check context + App 不能单独证明 workflow 来源；治理根接纳必须核验执行来源、可信政策和精确 artifact，不能只看 candidate 同名绿色 check。

## 机器结果

trusted main 决定 Registry、ownership、Scope、required tests、owned test namespaces、impact/consumer/full plan。正式执行 candidate 的 source/test 版本，计划仍为 trusted required UNION candidate changed/added owned tests。禁止 required 删除、policy/test config 减测、ownership 自扩、测试中修改对象、scope violation 和未授权 production mutation。

治理根包括当前 Admission TRUST_FILES、04_scripts/quality、.github、AGENTS/test config、requirements、trusted dev-governance required/future required tests。按真实 diff 分类，candidate Registry 或 branch 名称不能改变判断。

- 技术失败：final_result=FAIL，TECHNICAL_VALIDATION=FAIL，MAIN_ENTRY=FAIL，保留 failure_codes。
- Business/Shared 全通过：final_result=PASS，TECHNICAL_VALIDATION=PASS，MAIN_ENTRY=PASS。
- Governance 全通过：final_result=GOVERNANCE_ROOT_APPROVAL_REQUIRED，TECHNICAL_VALIDATION=PASS，MAIN_ENTRY=GOVERNANCE_ROOT_APPROVAL_REQUIRED，failure_codes 为空。

最后一种是正常待接纳状态，CLI 仍非零、required check 不自动放行。candidate 的文件、代码、message、branch、Registry 字段或 workflow metadata 都不能把它变成 PASS。没有 approval API verifier、签发 job、审批 token 或审批文件。任何新 Commit/Tree/base 都需重新验证，不能复用旧对象批准。

执行中的关键 workflow 配置变更仍作技术风险拒绝，不能让同一 workflow 修改跳过自己的可信检查；此类迁移需先在独立治理任务中审查和升级当前可信验证规则，再验证后续精确对象。Admin 不得把实际失败伪称 PASS。

## Admin 接纳边界

只允许 Governance trusted-root transition 或灾难恢复；不得用于普通 Business check 失败、普通测试失败、scope violation、production release 或数据质量 gate 绕过。灾难恢复也需明确范围、证据和独立授权，不能成为日常修复入口。

推荐长期保持 Ruleset Active、唯一 required check 不变、bypass_actors=[]。治理升级极少，临时添加/删除 Admin 会多两次操作，但常态权限更小，且能保持原 Commit。长期 Repository admins / For pull requests only 能减少设置操作并保留 PR 审计，但权限持续存在，不能机器限定治理类型，且 squash/rebase/merge 会产生不同对象；不适合当前要求 exact candidate SHA 的流程。

## FINAL_BOOTSTRAP_PLAN（仅计划）

每次冻结一个最终 candidate，直接从 fresh main 接纳这个完整对象，不要求先接纳旧 7bb 或上一轮 34bf。历史候选仅用于差异和回归对比。

前置必须全部满足：fresh GitHub/origin/local main 一致；base Commit/Tree、candidate Commit/Tree 已冻结；base 是 candidate 祖先且 behind=0；exact SHA hosted governance 全回归 PASS；diff boundary PASS；无 production mutation；用户明确批准精确 SHA/Tree/base。旧 main policy 的已确认 ESCALATION_REQUIRED 与真正技术失败分别记录。Ready 只表示可以提交人工批准，不代表已经批准或执行。

1. Maintainer/Admin 审核精确证据并明确批准；暂停其他 main 写入。重新 fetch/API 比对冻结对象，任何变化立即停止并重验。
2. GitHub 仓库 → Settings → Rules → Rulesets → main-trusted-admission-v1（ID 22616560）。保持 Enforcement=Active、required check trusted-main-admission-v1 / GitHub Actions 15368 及 force-push/deletion/linear-history 保护原配置。
3. Bypass list → Add bypass → Repository admins → Add Selected → Always allow → Save changes。仅短时操作窗口；不要添加 write/maintain 广泛角色或 App。该模式技术上涵盖整套规则，约束依赖已明确授权的 Admin 操作，不声称 API 按 SHA 限权。
4. 使用具有该 Admin 身份的 Git 凭据，在已验证 feature worktree 执行 `git push origin <exact_candidate_commit>:refs/heads/main`。不加 force/lease，不改 branch，不 squash/rebase，不生成新 commit。GitHub PR 页面合并不能保证原 Commit，因此不点击 Merge/Squash/Rebase 按钮。
5. 立即核验远程 main Commit/Tree 等于批准对象。无论推送成功、失败、超时或后续核验异常，立即删除本次临时 bypass：回同一 Ruleset 页面，移除本次 Repository admins bypass 并保存；核验失败时先恢复规则再调查，不保留开放窗口等待修复。
6. API GET /rulesets/22616560 确认 bypass_actors=[]、enforcement=active、required check/integration 和其他 rules 与操作前相同；GET /git/ref/heads/main、GET /git/commits/<SHA> 核验 Commit/Tree，再同步 clean local main 镜像。保存前后快照、操作时间、批准身份、push 结果与 hosted evidence。

页面控件及两种 bypass 模式见 [GitHub 创建 Ruleset 文档](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository)。Always allow 无精确对象过滤是 [GitHub API schema](https://docs.github.com/en/rest/repos/rules) 的限制；在本合同中由 Admin 作为最终信任根承担短时操作责任，不增加候选权限。

BOOTSTRAP_EXECUTION = NOT_PERFORMED。本轮不得替用户修改 Ruleset、添加 bypass 或 push main。

## Production

Approved identity、Commit/Tree/Image、Manifest、release gate、rollback、candidate 不写 production、生产单独授权全部保留。Governance/main transition 不部署、不连接生产服务器、不触发 FULL DAILY 或数据刷新。Public、Domestic Spread、Soybean 生产修复另行授权。

## Hosted required test platform migration（候选准备合同）

本阶段只登记 `windows-wrapper-platform` 的两个精确历史未归属文件，并提供 `04_scripts/quality/platform_test_plan.py` 与 `test_platforms.json` 的规划/聚合原语及回归。它们尚未接入 active Admission；不能把原语的模拟 PASS 称为 Windows hosted PASS，也不能据此称 Linux 平台误报已经修复。Wrapper 源码、测试和当前 workflow 在本阶段不变。

Wrapper 当前静态清单为 52 个函数、75 个参数化用例：46 个函数/68 个用例是跨平台纯逻辑或 mock 测试；6 个函数/7 个用例依赖真实 Windows 路径、NT 文件系统或锁。逐函数理由见 `test_platforms.json`。provider preflight 中工具发现造成的主机依赖属于 fixture 隔离缺口，不是把这些纯逻辑测试移出 Linux 的理由。

接纳此 ownership 准备对象后，后续实现仍必须完成独立可信执行策略升级与 workflow 审查；本阶段没有解除 `TRUSTED_WORKFLOW_EXECUTION_CHANGED`，也没有预批准后续 workflow。正式切换前必须完成：

1. 从独立取得的 trusted main Registry/Scope/required/future/impact plan 生成平台计划，执行 candidate 测试版本。候选平台 metadata 不得影响本轮路由。删除 required 文件、函数或减少参数用例失败；混合文件中新增未分类测试先要求两平台，不得静默漏测。
2. Linux 使用 `ubuntu-24.04`；Windows required 使用官方 `windows-2022`。无 Windows obligation 的普通 Business 不启动 Windows suite。Windows job 的环境只开放执行所需的可信 Python/Git 路径和系统临时目录变量，不继承生产凭据；不得实际运行 FULL DAILY、Tailscale 网络更新或生产任务。
3. 修正 Wrapper 纯逻辑测试的工具发现 fixture；保留 Linux 68 个用例。真实 Windows 的 7 个用例必须在 Windows 上收集、执行并通过，任何 skip 都失败。修改必须使用接纳后的 Wrapper owner，不借 dev-governance ownership。
4. 接入真实 runner executor 和可信最终聚合 job：计划、测试文件哈希、base/candidate Commit/Tree、run ID/attempt、runner OS 必须绑定同一对象。job 状态从 GitHub needs/API 获取，不能信任 candidate 上传的自报成功。缺失、取消、失败、skip、空 collection、缺少 required case 或对象不一致均失败；最终对外仍是 `trusted-main-admission-v1`。
5. 保存同一 exact candidate 的真实 Linux 和 Windows hosted 证据，以及故障注入回归。纯本地 receipt fixture 只能证明聚合逻辑，不能替代实际 hosted job 或来源校验。

该准备不修改 Production release safety、不接纳 release-risk-model candidate、不修改 Soybean，不授予 main/部署/数据更新权限。
