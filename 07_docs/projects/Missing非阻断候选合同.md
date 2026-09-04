# Missing 非阻断政策候选

状态：未提交 candidate；尚未生产启用。基线 50e1b59da7bebce7e8718129b74f5fc45bab4a30。
本任务是获批 shared change；沿用 AGENTS.md 权威链，不替代生产发布规则。

## 范围

Owned paths（精确文件）：

- 03_src/agri_research_agent/shared/async_update.py
- 03_src/agri_research_agent/pipelines/async_contract_rollout.py
- 03_src/agri_research_agent/pipelines/lutou_weather.py
- 03_src/agri_research_agent/pipelines/public_data_providers.py
- 03_src/agri_research_agent/pipelines/public_data_refresh.py
- 08_tests/pipelines/test_async_contract_rollout.py
- 08_tests/pipelines/test_missing_nonblocking_policy.py
- 07_docs/projects/Missing非阻断候选合同.md

不修改 producer 公式、mapping、source policy、Domestic Basis、前端、部署或 Approved。
Weather producer 仅增加可选报告接收容器；无数据转换改动。

## 合同

Catalog 是需要被 accounting 解释的 identity 集，不是非空 observation 承诺。
三维状态仍独立：Coverage PRESENT/MISSING/ERROR；Update UPDATED/NO_CHANGE/ERROR；
Freshness FRESH/STALE/UNASSESSED。不配置、推断或复用统一 7 天阈值。

共享六域 adapter 对通过 schema/mapping 的提取窗口与 last-good、Next 做逐键/值核对，
提交 verified_empty_source 证据；缺失 Next 本身不是 source-empty 证据。
从未有效观测且三者都无合法数据：MISSING / NO_CHANGE / UNASSESSED，
reason=SOURCE_NO_VALID_OBSERVATION，blocking=false，所有日期保持 null。
已有合法 Current、本轮窗口无新数据：PRESENT / NO_CHANGE，保留真实日期。
PRESENT + 已解释 MISSING = accounted_for_count；有 ERROR、不明 identity 或阻断 freshness 则失败。

源合法记录或未被合法修订替换的 last-good 行被丢失、值被无依据改写、
重复键、schema/mapping/normalization 错误、摘要不一致继续 fail closed。
Weather 额外核对 raw_value_text、标准化值与既有 usability/quality 决策；
不把合法数值被误标 NULL 视为允许 Missing。
既有 NON_NUMERIC/NEGATIVE_RAINFALL 源异常过滤语义不变。

纯合同未传 verified_empty_source 时保留原严格模式及 /2 报告形状，以保护本任务明确
不迁移的 Domestic Basis 政策；六域 adapter 默认采用有证据 Missing 非阻断模式，
未来 dataset 可复用该共享入口，不能只删除 error 来获得放行。

## Weather Summary

每份 accounting report 一经生成、通过一致性校验即交给 provider 的本轮报告容器，
不依赖 Weather 最终成功返回；密封、promotion 等后续异常仍向上传递。
外层先保存报告，再校验；对已有 blocking provider failure 不用第二个 accounting
异常覆盖原始失败阶段。报告 consistency 校验仍执行；非阻断 provider 状态不能掩盖
blocking accounting。Daily 收录两域报告，逐条列出 Missing identity、reason、blocking。

## 验证和边界

Germany ECMWF max/min 两个 identity 保留在 457 项 forecast catalog 中。
离线 fixture 和已保存失败 Run 的只读 accounting 回放要求 455 PRESENT / 2 MISSING / 0 ERROR。
不删除 identity，不补 0，不替换 model，不伪造日期。

可选真实产物回放测试：在测试子进程设置 MISSING_POLICY_SAVED_WEATHER_ROOT 指向
full-daily-20260904T064312.933627Z-da3fbbc1 的既有 lutou-weather 审计目录，
只读 Current、standard、Next，前后核对 SHA-256；新报告仅写 pytest 临时 isolated-dev runtime。
它不是 FULL DAILY，也不调用正式 Weather refresh、数据库或服务器。

交付前：Direct Tests → Scope Gate（上述 owned paths / shared）→ 相关回归、Git 测试、
diff --check、冻结工作区与 main/Approved 复核。停在未提交 candidate，等待用户闭包授权。

## 2026-09-04 候选验收结果

- Direct：78 passed / 1 optional saved-evidence test skipped。
- 相关回归（17 个测试文件）：383 passed / 1 optional saved-evidence test skipped，235.26 秒。
  覆盖六域、共享合同、Domestic Basis、retained exceptions、Weather provider、Daily、
  transaction rollback、Windows wrapper、quality/Git governance tests。
- 可选真实保存产物回放单独执行：1 passed，14.56 秒。输入前后 SHA-256 相同。
- Project Scope Gate：PASS；Git connectivity fsck、diff --check、UTF-8 往返：PASS。
- NEW_REGRESSION = 0（以上验收范围，不代表未运行的整个仓库测试）。

回放只重新评价会计报告，不重新构造、写入或 refresh 任何业务 Candidate：

| 数据域 | Catalog | PRESENT | MISSING | ERROR | NO_CHANGE | UNASSESSED |
|---|---:|---:|---:|---:|---:|---:|
| Weather Observation | 229 | 229 | 0 | 0 | 229 | 229 |
| Weather Forecast | 457 | 455 | 2 | 0 | 457 | 457 |

两项 Missing 仍保留：

- weather.temperature_max.rapeseed.eu.germany.forecast.ecmwf
- weather.temperature_min.rapeseed.eu.germany.forecast.ecmwf

均为 SOURCE_NO_VALID_OBSERVATION，blocking=false；previous/source/next latest 均为 null，
new_rows=0，freshness=UNASSESSED，threshold=null。NO_CHANGE 不代表有历史 observation。
所有身份都有明确 accounting，未把 457 改成 455，也没有制造新 observation。

main / origin/main / Approved 保持 50e1b59da7bebce7e8718129b74f5fc45bab4a30，
main clean、0/0。PM、旧 FULL DAILY、Prewarm 的 HEAD 和冻结内容哈希未变。
本轮未运行 FULL DAILY，未修改正式数据/Approved，未连接生产服务器、未部署、
未 commit/push。代码只在 fix/missing-nonblocking-policy 的未提交 candidate 中。
