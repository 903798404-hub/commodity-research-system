# 本电脑生产数据更新与交付

本目录属于独立 shared 项目 `xiaoran-production-data-delivery`。Windows 从固定 Approved
Commit/Tree 的独立、干净、detached Git clone 运行现有业务入口，服务器只验证和发布数据。
producer 的 Commit/Tree 与校验镜像的 Commit/Tree 分别固定，不要求两者相同。

## 三个入口

| domain | 原业务入口 | 正式消费者通道 |
| --- | --- | --- |
| `akshare` | `04_scripts/server_update_spreads.py --update-from-akshare` | 现有 public package 的 `domestic-spread` artifact |
| `soybean_crop_progress` | `04_scripts/soybean_crop_progress/update_soybeans_crop_weekly.py --dry-run` | Crop 两个 stable Parquet 配对发布 |
| `soybean_export_sales` | `04_scripts/soybean_exports/run_fas_export_sales.py --candidate-only` | FAS stable Parquet、主机生成的 manifest/status |

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

默认只创建候选；`--publish` 是显式交付选择，不能由候选生成成功隐式推导。
这些命令必须使用已批准的独立 control clone，不能从 feature worktree、local main 或
Preview 目录正式运行。每次执行创建唯一运行目录和源副本，核对真实 Git 对象与文件字节，
不安装依赖、不改变业务公式、不调用 FULL DAILY 或 AM/PM capture。

## 配置、基线与凭据

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
