# FGIS 服务器更新

原生 Yearly CSV 管道继续负责年度合并、冻结历史、单位转换和修订审计。本入口只补齐服务器调度与受保护交付，不改变业务算法。

每次运行锁定已批准的 commit/tree/image 和数据盘 UUID，网络容器只能读取正式数据，唯一可写挂载是本次独立输出。候选封存后，第二个无网络容器从同一归档 CSV 重跑原生管道，比较业务结果。正式目录切换要求三文件基线身份仍相同；先保留原始来源和修订审计，再用同盘目录 exchange 切换 parquet/manifest，原子更新状态。失败恢复旧目录和状态，历史原始来源及备份保留。

受保护配置 `/etc/market-data/fgis-server/*.json` 为 root:root 0600，闭合字段为 schema_version=fgis-server/1、source_root、commit、tree、image_id、allocation_root、data_disk_uuid。配置与源码、镜像身份必须一致。`run.py --config <配置>` 只生成候选；显式 `--publish` 才交付。

service/timer 模板在 CI、同镜像隔离验证与生产授权后安装。周二至周五北京时间07:15检查官方周一报告并重试。NO_CHANGE 保留旧成功数据并记录本次来源检查时间，不能把检查时间当统计截止日。运行证据及回滚位于 `/var/lib/market-data/production-data-automation/fgis/<run_id>/`。

本流程为追加、可回滚的数据交付，不重启网页，不写其他业务域。未经批准不得删除历史来源、运行记录和备份。
