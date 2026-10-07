# 本电脑生产数据更新与交付

本目录属于独立 shared 项目 `xiaoran-production-data-delivery`。Windows 从固定 Approved
Commit/Tree 的独立、干净、detached Git clone 运行现有业务入口，服务器只验证和发布数据。
producer 的 Commit/Tree 与校验镜像的 Commit/Tree 分别固定，不要求两者相同。

## 四个入口

| domain | 原业务入口 | 正式消费者通道 |
| --- | --- | --- |
| `akshare` | `04_scripts/server_update_spreads.py --update-from-akshare` | 现有 public package 的 `domestic-spread` artifact |
| `soybean_crop_progress` | `04_scripts/soybean_crop_progress/update_soybeans_crop_weekly.py --dry-run` | Crop 两个 stable Parquet 配对发布 |
| `soybean_export_sales` | `04_scripts/soybean_exports/run_fas_export_sales.py --candidate-only` | FAS stable Parquet、主机生成的 manifest/status |
| `canada_canola` | `04_scripts/canada_canola/update_canola.py` 人工准备；同一 Windows 正式入口交付 | Canola stable JSON、来源字节证据、主机生成 status |

Windows 正式入口为 `04_scripts/automation/run_production_data_delta_windows.py`：
该入口的 provider 子环境固定 `NO_PROXY=*`，不继承 Windows 用户代理或 CA
覆盖，并保持 TLS 证书验证；FAS 还通过现有
`--ignore-environment-proxy` 参数明确直连。

```text
<approved-python> -I -B <approved-control-clone>/04_scripts/automation/run_production_data_delta_windows.py --config <private-config.json> --domain <domain>
<approved-python> -I -B <approved-control-clone>/04_scripts/automation/run_production_data_delta_windows.py --config <private-config.json> --domain <domain> --publish
```

AkShare 的普通日更不提供 `--end-date`，由入口在运行开始时固定当天为业务截止日。经批准的
补录或重放可显式追加严格的 `--end-date YYYY-MM-DD`；该值逐层传递到 Domestic Spread
producer 和目标日期完整性门禁。未来日期、非法格式、requested/effective 不一致均 fail
closed。正式 `result.json` 和机器输出同时记录 `requested_end_date` 与
`effective_end_date`。其他 domain 不接受该参数。

### Domestic Spread 历史语义修订

历史修订仍使用上述同一 Windows 正式入口，仅追加
`--domain akshare --historical-reconciliation-manifest <absolute-manifest.json>`；默认只生成
candidate，正式交付仍须独立授权并显式使用 `--publish`。此模式不接受 `--end-date`，也不
从网络重新抓取或 forward-fill 价格。普通日更不带 manifest 时的入口与门禁保持不变。

Manifest 使用 `domestic-spread-historical-reconciliation/1` 的封闭 JSON 字段：
`schema_version`、`operation_type=HISTORICAL_RECONCILIATION`、
`dataset=domestic-spread`、`incident_id`、`reason`、`expected_current`（`id`、
`artifact_sha256`、`manifest_sha256`）、`source_evidence` 和 `audit_evidence`
（各含绝对 `path` 与 `sha256`）、`daily_close`（逐项 `trade_date`、完整
`full_contract`、十进制字符串 `value`）、`non_trading`（逐项 `trade_date`、
`full_contract`）、`derived_scope`（固定 `closure=CONFIG_DERIVED_EXACT` 与精确
`non_trading_derived_keys`）、`counts`（三类精确计数）。Source CSV 逐项核对完整
合约、日期、AkShare 日线 `close`、原始 scoped SHA、唯一来源行和值；spot/current_price
不得冒充收盘价。重复键、通配范围、未知字段、缺失或变更的证据均拒绝。

正式入口在建立 candidate 前读取真实 Public Current 并验证批准的 ID、Domestic Spread
artifact SHA 与 Current manifest SHA；发布前再次核对 Current、manifest 和证据。
独立 producer clone 只追加批准的 DAILY_CLOSE 来源行，保留既有 spot 原始行；原有
calculator、配置驱动依赖闭包、有界物化和 historical publication guard 决定可发布的
精确 derived key。声明为非交易日的来源没有 DAILY_CLOSE，其批准的旧 derived 行必须
精确删除；未列入范围的历史变化仍失败。`result.json` 记录 manifest SHA、预期和实测
Current、批准键数、source/audit SHA，以及发布前 Current ID/SHA。Manifest 是一次
受审输入，不是持久的全历史写权限；main CI PASS 也不授予数据发布权限。

默认只创建候选；`--publish` 是显式交付选择，不能由候选生成成功隐式推导。
这些命令必须使用已批准的独立 control clone，不能从 feature worktree、local main 或
Preview 目录正式运行。每次执行创建唯一运行目录和源副本，核对真实 Git 对象与文件字节，
不安装依赖、不改变业务公式、不调用 FULL DAILY 或 AM/PM capture。

Git integrity 硬门槛分别验证获批 Commit/Tree 的完整可达对象、可归档的源码字节，
以及 clone 中存在的 main / 正式 release refs 的完整可达对象；独立 detached control
clone 没有本地 ref 时，获批 Commit 是其固定的 main 快照。目标或正式 ref 的可达对象
缺失、损坏、身份不符或无法归档均停止。对象闭包检查直接基于 Git object database，
显式禁用 commit-graph 优化。仓库级 fsck 仍运行；只有已经证明与目标和正式 refs
无关、且错误类型可识别的历史对象问题才记录 `REPOSITORY_MAINTENANCE_WARNING`。
未知完整性错误仍停止。Commit-graph 是性能与维护元数据，不是 release 身份权威。

已验收的历史修订 candidate 使用同一 Windows 入口的 `--promote-candidate <absolute-package-path>`，
同时提供原 `--historical-reconciliation-manifest`、`--promotion-evidence`、
`--promotion-evidence-sha256` 与三项 `--expected-current-*`。这条路径只重新核验原 candidate
及其 validation、input-hash inventory、producer Commit/Tree 和 continuation 输入字节，
不重新抓取、计算或打包。Promotion evidence 使用封闭
`public-current-candidate-promotion/1` JSON，包含 candidate ID、artifact/manifest/result SHA、
validation 与 inventory 的绝对路径和 SHA、三个 continuation 文件的 SHA、producer Commit/Tree、
reconciliation manifest SHA 和完整 expected Current。调用者须批准此 evidence 的精确 SHA；
临时改写 evidence 或调用参数不能替换已验收的旧 Current 基线。

传输后由 activation image 重新核验同一 manifest/artifact SHA，并声明
`public-current-server-cas/1` 能力；旧镜像不支持该能力时，在 activation 前停止。
服务器的普通发布和历史 candidate 晋升共用 Current 锁；晋升在锁内读取实际旧 Current
的 ID、artifact SHA、manifest SHA，并在切换前再次核验上传 candidate 的同三项身份；
两侧各自三项全等才原子切换指针。若 Current 已移动，返回
`FAIL_STALE_BASE`，保留原指针及上传 candidate，不自动重算、覆盖或回退其他发布。
成功回执单独记录原/新 Current 身份和切换时间，不修改已验收 candidate 文件。
这是生产数据发布操作，仍须独立授权；代码进入 main 本身不触发晋升。

## 配置、基线与凭据

### 加拿大人工交付

加拿大使用同一 `run_production_data_delta_windows.py --domain canada_canola` 入口。
默认仅生成候选，显式 `--publish` 才上传、主机复验及原子发布；不抓取 provider、不安装依赖、
不构建或拉取镜像、不重启网站、不注册定时任务。其他三个域的配置和日常调度保持原合同。

独立配置版本为 `canada-canola-delivery-config/1`，封闭字段：
`schema_version`、`approved_commit`、`approved_tree`、`origin`、`python`、`runtime_root`、
`baseline_root`、`baseline_manifest_sha256`、`candidate_path`、`candidate_sha256`、
`source_root`、`workbook_path`、`workbook_sha256`、`revision_keys`、`ssh_target`、
`publisher`、`publisher_sha256`、`image_id`、`remote_allocation`、`policy`、`policy_sha256`。
policy 两个映射只含 canada_canola；镜像必须为完整 sha256 ID。生产 producer 使用干净独立
detached Approved clone 与固定 Python `-I -B`，不能从 feature worktree 执行正式交付。

`baseline_root/baseline_manifest.json` 使用 `canada-canola-production-baseline/1`，
只含 `schema_version`、`source_root`、`files`。source_root 必须等于实际服务器 allocation；
files 精确包含 `01_data/processed/canada_canola/canola_weekly.json`、同目录
`source_evidence.json` 和 `01_data/update_status/canada_canola.json`，值为文件 SHA/大小或 null。
首次上线前只读确认三项全部不存在才能记录 null；已有基线三项必须完整，并逐字节取回核验。
本地 Preview 不是生产基线。每次发布均重新取得实际服务器快照，主机发布锁内再次检查。

固定上传仅含 `delta_contract.json`、`canola_weekly.json`、`source_evidence.json`。
后者为封闭 `canada-canola-source-evidence/1` JSON，包含 schema_version 和 sources 数组。
每项固定 kind、sha256、bytes_base64、province、source_url、retrieved_at；首次 workbook 项
后三项为 null，report 项必须匹配官方 HTTPS、省份和抓取时间。单来源最多20MB，合计最多100MB。
原文只作为归档证据，官方表格数值仍由 Codex 人工核对，不声称通用 PDF 自动解析。

正式 producer 和主机固定镜像校验器都重新检查实际来源字节、封闭数据结构、日期、单位及唯一键。
首次历史导入在临时目录重新读取同 SHA 原 Excel，逐条核对原始日期/值/单元格，并拒绝漏历史。
后续新记录必须有归档官方报告；禁止删除历史。修订必须通过 revision_keys 精确列出
`省份/指标/日期`，实际修订集合必须全等；空列表表示只能追加。正常重复检查产生 NO_CHANGE，
不改稳定数据或网站。原 Excel 只读；原始来源、配置、候选及回执不提交 Git。

主机沿用现有 stage-upload → receive → validate → publish / rollback，固定 approved
producer、受保护 policy、源 clone 和镜像身份。三项稳定文件及 status 仅写加拿大域；发布失败
恢复原域/status，备份不可变。配置和源码进入 main 不启用政策或授予数据发布权限。

配置采用 `production-data-producer-config/1`，由 `validate_config` 拒绝未知字段。
配置必须固定 approved commit/tree、canonical origin、已有 Python、外部 runtime root、
基线目录及 manifest SHA、public package 及 manifest SHA、SSH target、精确 image ID、
allocation/store 路径、受保护 publisher 路径及 SHA、逐域 policy 路径及 SHA、三类固定业务
source、凭据文件及允许的 key，以及本机 FULL DAILY 生命周期锁路径。

配置和凭据保存在 Git 外部的受限本机目录，不进入包、镜像或日志。NASS 只注入
`NASS_API_KEY`，FAS 只注入 `FAS_EXPORT_SALES_API_KEY`；不接受 Preview 凭据名回退。
子进程失败输出不直接回显，最终状态只包含安全的执行阶段、身份、文件哈希和退出结果。

基线是明确标识的正式数据副本；不是开发目录中恰好存在的文件。每次复制核对 manifest
和文件身份，业务执行前后核对源码与不应改动的输入。成功交付后的 continuation 保存在
新的运行目录中，以实际发布证据和逐文件 SHA 绑定，下一次更新继续使用该基线。

AkShare 更新持有与 FULL DAILY 相同的本机 `filelock.FileLock`，覆盖基线选择、业务执行、
打包、正式指针检查、交付和 continuation。它保留当前 public package 的全部公共数据集
及源最大日期，只替换国内价差 artifact；只写旧根目录 Parquet 不算页面数据激活。
旧生产价表基线与页面正在读取的 public package 是独立输入，不能相互冒充。

## Crop 与 FAS 主机发布

上传合同见 `delta_contract.schema.json` 和 `activate_production_data_delta.py` 的严格验证器。
上传只包含 `delta_contract.json` 和该域固定 payload；主机不信任 Windows 的绝对路径、
状态文件或 FAS 消费者 manifest。Policy 位于 `/etc/market-data/production-data-delivery`，
固定 approved producer、独立 source clone、validation image 和 production allocation。

交付顺序为用户隔离上传 → `stage-upload` 保护接收 → `receive` → `validate` → `publish`。
每一步引用前一步实际文件的 SHA，并拒绝路径链接、额外文件、错误来源与正式基线漂移。
生产 storage 必须是 `/var/lib/market-data/production-runtime` 下独立 allocation，
该 allocation 下的 `01_data` 对应页面数据根。

语义验证仅在固定镜像中运行一次 readonly、network-none、非 root worker；只读挂载候选、
基线和批准的主机工具源码。Worker 复用镜像内现有 Crop/FAS 校验器，不查询任何 provider，
不使用 Tailscale，不 build、不 pull。候选必须能被镜像的实际 UID 读取。

发布前重新核对 policy/source/candidate/report/baseline，在域目录 staging 中保留未受影响
文件，使用同文件系统的目录交换完成替换，并生成主机 status。失败时恢复旧域目录和旧
status，保留失败证据及不可变备份；恢复失败必须标识 BROKEN，不能继续发布。显式 rollback
要求当前正式文件仍匹配待回滚 publication receipt，不接受过期或不相干的 receipt。

## 调度与验证

调度安装是单独的受控迁移步骤；本入口不会自动注册任务或移除 cron。迁移前确认实际服务器
时区、精确旧 cron、已有本机任务、锁路径和回滚配置，完成真实来源更新与主机消费验证后，
才启用本机替代任务并停用对应旧条目。记录任务 XML、批准配置哈希和原 cron 备份。

本次审计的服务器和本电脑时区都是 Asia/Shanghai，对应时间为：AkShare 周一至周五 16:30，
Crop 周二、三、四 06:30，FAS 周六 06:15。执行时仍须核对，不能从 UTC 猜测。电脑必须开机
并能访问数据源和服务器；任务错过时间的处理由已审核的 Task Scheduler 设置决定。

定向测试是 `08_tests/test_production_data_delta.py` 和
`08_tests/test_production_data_delta_activation.py`；正式 Completion 还执行 Registry 声明的
治理测试。代码检查、候选有效、主机发布、消费者采用和定时任务启用是分别记录的状态，
不能只凭任一测试 PASS 宣称生产迁移完成。
