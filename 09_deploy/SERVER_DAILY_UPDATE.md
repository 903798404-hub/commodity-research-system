# 服务器每日更新价差数据库

本文档说明如何在本地和腾讯云轻量应用服务器上测试 `04_scripts/server_update_spreads.py`。

## 当前数据来源判断

当前看板核心数据库是：

```text
01_data/historical_spread_database.xlsx
01_data/historical_spread_database.parquet
```

Excel 继续保留为可检查、可恢复的正式文件。Streamlit 看板会优先读取 Parquet，以提升页面加载速度；如果 Parquet 不存在，才回退读取 Excel。

它由以下链路生成：

```text
01_data/manual_history/historical_price_base.xlsx
-> 04_scripts/import_historical_prices.py
-> 01_data/historical_price_long.xlsx
-> 04_scripts/calculate_historical_spreads.py
-> 01_data/historical_spread_database.xlsx
```

其中 `import_historical_prices.py` 读取的是手工历史价格 Excel，来源字段和脚本命名都显示历史底座最初依赖本地 Wind/手工文件。

当前 `server_update_spreads.py` 默认只做安全检查、备份和日志记录，不会自动更新。真正自动更新需要显式使用：

```bash
python 04_scripts/server_update_spreads.py --update-from-akshare
```

这个模式会先调用 `04_scripts/update_price_long_from_akshare.py`，通过 AkShare `futures_zh_spot` 获取当日合约价格，幂等写入 `01_data/historical_price_long.xlsx`，再调用 `04_scripts/calculate_historical_spreads.py` 重算 `01_data/historical_spread_database.xlsx`。

`calculate_historical_spreads.py` 会同时生成：

```text
01_data/historical_spread_database.xlsx
01_data/historical_spread_database.parquet
```

生成时先写临时文件：

```text
01_data/historical_spread_database.tmp.xlsx
01_data/historical_spread_database.tmp.parquet
```

写入成功后再替换正式文件，降低看板读到半成品文件的风险。

AkShare 增量脚本优先使用价格字段：

```text
current_price
last_close
last_settle_price
avg_price
```

写入 `historical_price_long.xlsx` 的唯一键是：

```text
date + instrument + delivery_month
```

如果同一天同品种同月份已经存在，真实运行时会覆盖更新，不会重复插入。

## Phase 1 事务安全

每日更新使用跨平台文件锁：

```text
01_data/.server_update.lock
```

默认等待锁最多 60 秒。如果已有更新任务正在运行，本次任务会以 `skipped_locked` 状态退出，不会并发修改数据库。

每次运行都会原子更新：

```text
01_data/update_status.json
```

状态值包括：

```text
running
success
failed
skipped_locked
```

状态文件记录开始和结束时间、服务器日期、最新交易日、候选合约数、成功和失败合约数、失败合约列表、备份路径以及 Excel/Parquet 校验结果。Streamlit 看板会在顶部读取并显示最近一次更新状态；状态文件不存在时不影响看板正常使用。

AkShare 更新默认要求全部必需合约成功。当前常用组合通常为 15 个合约，因此默认必须满足：

```text
success_contracts == required_contracts
failure_contracts == 0
```

任一合约映射失败、价格为空、价格小于等于零、报价时间无效或合约已经过期，都会阻止写入和后续价差重算。

`historical_price_long.xlsx` 使用以下临时文件原子替换：

```text
01_data/historical_price_long.tmp.xlsx
```

真实更新开始前，父任务统一备份：

```text
01_data/historical_price_long.xlsx
01_data/historical_spread_database.xlsx
01_data/historical_spread_database.parquet
```

如果 AkShare 价格写入成功，但后续价差重算或 Excel/Parquet 校验失败，会恢复本次运行前的三个文件。更新前不存在的旧 Parquet 不会被伪造；失败时会删除本次新生成的 Parquet。

## 本地测试

在本地项目根目录执行：

```powershell
.\.venv\Scripts\python.exe 04_scripts\server_update_spreads.py
```

默认行为：

- 检查 `01_data/historical_spread_database.xlsx` 是否存在。
- 备份到 `01_data/backups/historical_spread_database_YYYYMMDD_HHMM.xlsx`。
- 写入 `10_logs/server_update_spreads_YYYYMMDD.log`。
- 不全量重算历史数据。
- 不覆盖旧数据库。

如果你已经确认 `01_data/historical_price_long.xlsx` 是最新的，并且愿意从现有长表重算价差数据库，可以手动运行：

```powershell
.\.venv\Scripts\python.exe 04_scripts\server_update_spreads.py --recalculate-from-existing-price-long
```

这个模式会调用：

```text
04_scripts/calculate_historical_spreads.py
```

如果计算失败，脚本会尝试把备份恢复回 `01_data/historical_spread_database.xlsx`。

本地 AkShare dry-run：

```powershell
.\.venv\Scripts\python.exe 04_scripts\server_update_spreads.py --update-from-akshare --dry-run
```

本地 AkShare 真实运行：

```powershell
.\.venv\Scripts\python.exe 04_scripts\server_update_spreads.py --update-from-akshare
```

dry-run 只抓取和生成报告，不写入 `historical_price_long.xlsx`，也不重算 `historical_spread_database.xlsx`。

## 服务器手动运行一次

服务器项目目录：

```bash
/home/ubuntu/market-data
```

手动运行：

```bash
cd /home/ubuntu/market-data
docker compose exec spread-dashboard python 04_scripts/server_update_spreads.py
```

如果服务器上已经有最新的 `01_data/historical_price_long.xlsx`，并确认可以重算数据库：

```bash
cd /home/ubuntu/market-data
docker compose exec spread-dashboard python 04_scripts/server_update_spreads.py --recalculate-from-existing-price-long
```

服务器 AkShare dry-run：

```bash
cd /home/ubuntu/market-data
docker compose exec spread-dashboard python 04_scripts/server_update_spreads.py --update-from-akshare --dry-run
```

服务器 AkShare 真实运行：

```bash
cd /home/ubuntu/market-data
docker compose exec spread-dashboard python 04_scripts/server_update_spreads.py --update-from-akshare
```

如果本次代码更新包含 `requirements.txt` 变化，例如新增 `pyarrow`，必须进入隔离候选构建和不可变发布流程；不得在正式仓库直接重新构建：

```bash
bash 09_deploy/spread_release/deploy_spread_release.sh \
  "09_deploy/releases/${RELEASE_ID}"
```

候选构建必须先包含依赖变更并完成验证；上述正式切换只复用候选验证过的同一 Image ID。

研究工作台首页依赖镜像内的以下目录：

```text
/app/apps
/app/configs
```

其中 `apps` 包含首页和基差页面骨架，`02_configs/report_catalog.yaml` 控制首页卡片。新增或更新这些目录后，服务器必须重新构建镜像：

```bash
bash 09_deploy/spread_release/deploy_spread_release.sh \
  "09_deploy/releases/${RELEASE_ID}"
```

只执行 `docker compose restart` 不会把新增的应用或配置文件放入旧镜像；也不得用 `up --build` 绕过候选验证和发布清单。

## 配置 cron

### 外资与重点席位（北京时间）

在与现有看板相同的运行环境中增加以下两条幂等任务。数据写入以
`trade_date + exchange + variety + seat_name_normalized` 去重，20:30 的补偿执行不会制造重复记录。

```cron
30 18 * * 1-5 cd /home/ubuntu/market-data && /usr/bin/docker compose exec -T spread-dashboard python 04_scripts/update_foreign_seats.py --recent >> 10_logs/cron_foreign_seats.log 2>&1
30 20 * * 1-5 cd /home/ubuntu/market-data && /usr/bin/docker compose exec -T spread-dashboard python 04_scripts/update_foreign_seats.py --recent >> 10_logs/cron_foreign_seats.log 2>&1
```

先不要直接启用 cron。确认手动运行没有问题后，再配置。

默认安全检查示例：

```cron
30 16 * * 1-5 cd /home/ubuntu/market-data && /usr/bin/docker compose exec -T spread-dashboard python 04_scripts/server_update_spreads.py >> 10_logs/cron_update_spreads.log 2>&1
```

注意：cron 里必须使用 `exec -T`，避免 Docker 分配 TTY 导致定时任务挂住。

AkShare 自动更新示例：

```cron
30 16 * * 1-5 cd /home/ubuntu/market-data && /usr/bin/docker compose exec -T spread-dashboard python 04_scripts/server_update_spreads.py --update-from-akshare >> 10_logs/cron_update_spreads.log 2>&1
```

## 查看日志

脚本日志：

```bash
ls -lh /home/ubuntu/market-data/10_logs/server_update_spreads_*.log
tail -n 100 /home/ubuntu/market-data/10_logs/server_update_spreads_YYYYMMDD.log
```

cron 汇总日志：

```bash
tail -n 100 /home/ubuntu/market-data/10_logs/cron_update_spreads.log
```

Docker 服务日志：

```bash
cd /home/ubuntu/market-data
docker compose logs -f spread-dashboard
```

AkShare 抓取报告：

```bash
ls -lh /home/ubuntu/market-data/06_outputs/daily_price_updates
```

报告文件格式：

```text
akshare_price_update_YYYYMMDD_HHMM.xlsx
```

至少包含：

```text
success
failures
raw_spot
to_append
existing_today_rows
```

## 查看备份

```bash
ls -lh /home/ubuntu/market-data/01_data/backups
```

备份文件名格式：

```text
historical_spread_database_YYYYMMDD_HHMM.xlsx
historical_spread_database_YYYYMMDD_HHMM.parquet
historical_price_long_YYYYMMDD_HHMM.xlsx
```

## 更新失败后恢复备份

如果脚本内部检测到失败，会自动尝试恢复本次备份。

如果需要手工恢复，先停止看板或确认无人使用数据库，然后执行：

```bash
cd /home/ubuntu/market-data
cp 01_data/backups/historical_spread_database_YYYYMMDD_HHMM.xlsx 01_data/historical_spread_database.xlsx
docker compose restart spread-dashboard
```

Streamlit 看板缓存使用数据库文件修改时间。只要 Parquet 或 Excel 文件被替换，缓存会自动失效；文件没有变化时，切换品种不会重复读取数据库。

查看最近一次结构化更新状态：

```bash
cat /home/ubuntu/market-data/01_data/update_status.json
```

## 重要提醒

- 不要把 Wind 原始文件、API 密钥、`.env`、`data`、`logs`、`output` 提交到 GitHub。
- 默认不带参数的脚本仍然只是安全检查。
- 真实自动更新必须使用 `--update-from-akshare`。
- AkShare 行情接口可能受交易时段、网络或字段变化影响。上线 cron 前必须先在服务器手动 dry-run。
- 如果镜像缺少 `pyarrow`，Parquet 生成和读取会失败。修复依赖后重新走隔离候选构建、清单验证和同一 Image ID 正式切换，不在正式仓库执行构建。
- cron 命令不需要因 Phase 1 修改，仍然使用 `server_update_spreads.py --update-from-akshare`。


