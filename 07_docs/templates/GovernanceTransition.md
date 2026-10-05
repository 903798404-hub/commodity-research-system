# Governance / CI Maintainer 审查卡

> 规则引用[标准规范](../03_标准开发与生产发布规范.md)与[操作清单](../04_开发与发布检查清单.md)。本卡记录一次具体的治理改动，不额外创建审批系统或分阶段安装要求。

`CHANGE_CLASS = GOVERNANCE_OR_CI`；`MAINTAINER_REVIEW_REQUIRED = YES`。

| 审查项目 | 填写实际事实 |
|---|---|
| 具体问题与改动后行为 | |
| changed-file Scope / 改动控制点 | |
| authoritative base Commit/Tree | |
| candidate Commit/Tree | |
| required coverage / 新增回归测试 | |
| hosted Linux / 必需 Windows run 与结果 | |
| 同次 full 对照：新增 failure / skip / 删除节点 | |
| 镜像检查 / 发布演练路由与实际结果 | |
| 生产未触碰的核验 | |
| 已知限制与回退方式 | |
| Repository Maintainer/Admin 接纳决定、操作者、时间、精确对象 | |

审查不覆盖失败、required test 删除或 production mutation。新 commit、main 移动或证据身份变化必须重新核验。main != production；生产单独授权，不由本卡授予部署、数据写入或运行 grant。