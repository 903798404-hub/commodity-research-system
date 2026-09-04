# ASYNC-CONTRACT-ROLLOUT — 未提交候选

Project: shared contract rollout。遵守根 AGENTS.md，不是新的治理权威来源。
Baseline: `08640a255e02014d577b990a1878ecc03c4d90c1`；Tree: `df1fceff92aee3065c2dafd44a07225d86e0f415`。
Worktree: `market-data-worktrees/async-contract-rollout`；branch: `feat/async-contract-rollout`。
Windows / PowerShell 7.6.5 / Python 3.12.10。附件直接可读，不需要跨环境传输。
Runtime: isolated-dev pytest fixtures；禁止正式更新、服务器连接、部署、commit/push/Approved 修改。

Owned paths:

- `03_src/agri_research_agent/shared/async_update.py`
- `03_src/agri_research_agent/pipelines/async_contract_rollout.py`
- `03_src/agri_research_agent/pipelines/tankan_goal_a.py`
- `03_src/agri_research_agent/pipelines/lutou_goal_b.py`
- `03_src/agri_research_agent/pipelines/lutou_goal_b_soil.py`
- `03_src/agri_research_agent/pipelines/lutou_weather.py`
- `03_src/agri_research_agent/pipelines/public_data_providers.py`
- `03_src/agri_research_agent/pipelines/public_data_refresh.py`
- `03_src/agri_research_agent/pipelines/public_data_daily.py`
- `08_tests/pipelines/test_async_contract_rollout.py`
- `07_docs/projects/AsyncContractRollout候选合同.md`

只允许 Producer promotion 前的核算调用；不改计算、mapping、数据源配置、canonical/Public schema。
Domestic Basis 政策和源码不变。Prewarm candidate、PM、旧 FULL DAILY 保持冻结。
Tests: 六域三维状态/失败、一致性、existing Producer、Async/Basis、Daily/wrapper、rollback、scope、compile、UTF8。

## 身份与日期审计

Market: 原 MARKET_STABLE_KEY 去掉 business_date（exchange/product/instrument_id/price_type/session）；
series_id 只到商品粒度，不能替代合约 identity。Required = 合法 previous 和 canonical window identity 并集。
FX: 原 FX_STABLE_KEY 去掉 quote_date（base_currency/quote_currency/tenor/rate_type），独立于 Market。
Three-Oil、Soil: catalog series_id；row key 为 series_id + business_date。
Weather: catalog series_id，Observation 和 Forecast 按 data_family 分组；row key 保留 valid_date + forecast_run_id。
Forecast 新 run 即使 valid-through 不变也可能有新记录；不能把 future valid_date 误报未来业务日期错误。

核算输入为既有标准化/Canonical gate 后的完整窗口和 next state，不重新查询来源或修改结果。
source_latest_date 明确是本次已验证 canonical extraction window 内最大日期；不冒充全表 inventory。
窗口没有某 identity 时 source_latest_date=null，previous/next 日期仍独立保留，source_window_complete 语义显式标识。
完整空 source window、mapping/normalization 等原 Producer 门禁不放宽；只有已通过既有门禁的输出进入核算。
不统一发布日期，不造行、不推进日期。保留原 rollback transaction boundary。

## Freshness

本轮未发现可直接转用的六域逐 series freshness threshold；均 UNASSESSED / threshold=null。
既有 consumer freshness 与 Weather forecast_horizon_days（提取上限）不是逐 series age threshold，均不转换。
普通 age_days 是实际日历差；Forecast 单列 horizon_days，未来 valid-through 不代表 FRESH。
共享 helper 显式提供 forecast-valid/content-update 扩展；Domestic Basis 默认 date-advance 语义不变。

## 增量模式边界

本轮只接入 FULL DAILY 已使用的 `full_load=False`。初始建库仍使用原 Producer 政策，不生成本轮增量报告。
原因：已有 Weather 初始建库测试允许负降雨只留在 Candidate、从 Current 排除；不能借推广改写其政策。
既有全源空窗口、mapping/schema、retained exception 与 normalization gate 均保留。
本轮不承诺把一个在既有 Producer gate 阶段已失败的 source window 改判 NO_CHANGE。

报告位于隔离 runtime 的 `async-contract-reports/<producer-run-id>/manifest.json`，promotion 前密封。
异常沿原 Producer/Provider failure 路径进入原 rollback，事务边界不变。
Provider performance 的 `async_updates` 透传报告；统一编排在 rollback 决策前校验三维计数/逐项一致性。
Daily 新增 `async_updates`、`dataset_update_summary`、`async_summary_complete`，旧字段保持兼容。
旧 run / mock / 未执行域如果没有报告，不猜 count、不冒充完整，`async_summary_complete=false`。
初始建库与失败在前置 schema/mapping gate 的运行不会产生虚构的逐 series 成功报告。

## 候选验证记录

- 综合回归：368 passed（16 个专项文件，包含原 Producer、Basis/retained exceptions、shared Async、
  Daily/wrapper、refresh/rollback 和 quality/Git Scope tests）。
- Weather 最后增加 Standard usable 行到完整 Next 的独立 lineage 核对后，相关专项 57 passed；与综合回归存在重叠，不累加为独立测试总数。
- 新 rollout 专项 30 项，包括六域参数化状态、源行丢失、schema/mapping/重复冲突、summary 篡改、
  Forecast 同 valid-through 新 run、独立 Market/FX、完整七域 Daily summary 与原事务回滚。
- 四个原 Producer 使用原 offline client fixtures 建库再增量，实际验证六域 reports 已接入，
  NO_CHANGE 不 promotion，不伪造日期。没有对真实源执行查询或更新。
- PROJECT_SCOPE=PASS；OUT_OF_SCOPE=0；11 个 owned 文件。
- compileall、UTF-8 严格往返、git diff --check：PASS。
- main clean、ahead/behind=0/0；Approved 仍为 `08640a255e02014d577b990a1878ecc03c4d90c1`。
- PM：HEAD `5e796f33a9e77135a7ebd382f55f7b34ff4a7f8f`，Tree `c093540fafd4927d6fbdfdad521fac5832bc2e91`，clean。
- 旧 FULL DAILY：HEAD `274c6cc5ecd2c9693d262a3b7fae0c64c8ac05aa`，Tree `c10b1acdc323ad8f488d0263ff9c45e9ae4f90c0`，clean。
- Prewarm 未提交 candidate 八个文件的逐文件 SHA-256 前后相同，未新增任何浏览器依赖。
- 未 commit/push、未修改 Approved/正式数据、未连接服务器、未运行真实 FULL DAILY、未部署。

READY 仅表示本轮增量 candidate 实现和离线验证完成，不表示主线生效或真实生产验收。
初始建库推广、超大生产数据量下新增 Arrow accounting 的资源开销，以及最终主线闭包/真实验收，均未在本轮执行。

## 2026-09-04 代码主线闭包

用户在候选验收后确认“正式闭包”。上述候选记录保留为历史阶段证据；本阶段允许对上述
11 个 owned 文件显式提交，在独立 `integration/async-contract-rollout` worktree 验收后，
从该分支普通 fast-forward 更新远程 main，最后同步 clean local main。
本阶段不扩大实现范围，不合入 Prewarm，不修改正式数据、不运行真实 FULL DAILY、不部署。
Approved Production Commit 保持 `08640a255e02014d577b990a1878ecc03c4d90c1`；
代码主线闭包不等于生产启用，后续切换 Approved 和真实生产验收需另行确认。
精确提交 SHA、Tree 和独立集成测试结果以闭包执行回执为准。
