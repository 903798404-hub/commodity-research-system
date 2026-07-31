# USDA / Oil World 正式旁路容器身份契约

本契约仅适用于新生成的 spread-dashboard Release，不迁移或补写既有密封 Release。

## 采集边界

在启动候选容器之前执行：

```bash
python3 09_deploy/spread_release/capture_formal_container_snapshot.py \
  --output /path/to/release/formal_containers_before_candidate.json
```

采集只调用 Docker inspect，不读取或记录容器环境变量值。每个 `usda-dashboard` 和 `oil-world-dashboard` 身份必须包含完整容器 ID、完整 Image ID、显式稳定镜像标签、OCI revision、CreatedAt、StartedAt、RestartCount、运行状态、健康状态、规范化端口和规范化挂载。

候选结果生成时必须显式传入该不可覆盖快照：

```bash
python3 09_deploy/spread_release/create_candidate_result.py \
  --tool-repo-root /path/to/isolated-checkout \
  --manifest /path/to/release/release.json \
  --readiness-result /path/to/candidate_readiness.json \
  --formal-containers-before /path/to/release/formal_containers_before_candidate.json \
  --checks-file /path/to/candidate_checks.json
```

`formal_containers_unchanged` 不再接受操作人员输入。工具逐字段比较前后快照并派生该布尔值；任一容器缺失、任一必填字段缺失、标签无法解析到实际 Image ID、使用 `latest`/`new`/无标签引用，或任一字段发生变化，都会拒绝候选结果。

## 密封链

- `release.json` 密封契约版本、服务名、必填字段、比较方法和 fail-closed 策略。
- `candidate_result.json` 密封候选启动前与候选验收后的完整快照及逐字段比较。
- `deployment_plan.json` 密封候选验收后基线与计划生成时的完整快照及逐字段比较。
- 部署前校验再次采集快照并与计划基线硬比较；不一致时禁止切换。
- `deployment_result.json` 密封计划中的部署前基线与生产验收后的完整快照及逐字段比较。
- 四类 JSON 的伴随 Manifest 记录正式旁路证据对象的规范化 SHA-256；目标 JSON 或正式旁路证据被改写都会验证失败。

正式旁路容器不属于 spread-dashboard Compose 服务切换范围。本契约只观察其身份，不启动、重建、重启或删除 USDA/Oil World 容器。

## 进口利润候选 runtime 与外部门禁

现有 spread 候选工具可为一次候选验收增加唯一一个进口利润 runtime bind。宿主路径必须由调用方显式提供并位于经解析的批准候选根目录内，不得等于正式 runtime、位于 Git 检出内或通过符号链接逃逸。容器路径固定为 `/app/runtime/import_profit`，挂载模式固定为读写，并只向候选服务注入 `IMPORT_PROFIT_RUNTIME_ROOT`。候选工具必须逐项证明正式 Compose 中继承的天气和其他业务挂载没有被改写或放宽；正式 `docker-compose.yml` 不增加该测试挂载。

候选结果只记录批次 ID、容器路径、`rw` 模式、容器 UID/GID 和读写探测结果，不记录宿主机绝对路径。候选测试 runtime 可以验证锁、临时文件、fsync、Release rename 和 Index 原子替换，但不得直接提升为正式 runtime；正式部署必须从干净历史输入和真实当日输入重新初始化。

`candidate-waiting-gate` 表示源码、镜像、容器、页面和已执行检查通过，但真实北京时间 09:00 AkShare `morning_open_snapshot` 门禁仍未完成。该状态必须包含 `real_morning_open_snapshot` 阻塞门禁，不能生成部署计划，也不能进入正式提升。只有 `candidate-validated`、空 `blocking_gates`，且进口利润候选同时密封已完成的真实门禁证据时，才具备进入部署计划阶段的资格。真实门禁证据至少记录业务日期、捕获时间、`snapshot_batch_id` 和候选 SHA；目标合约的完整身份保存在对应候选证据中。

## 既有 b05

`spread-20260722-2c2abef5a93b-b05` 没有密封部署前完整身份，因此只能附加仓库外的历史证据缺口说明。后续观察不能重建部署前事实，不得编辑、覆盖、补写或重新生成 b05 的任何 Release 文件。
