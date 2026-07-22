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

## 既有 b05

`spread-20260722-2c2abef5a93b-b05` 没有密封部署前完整身份，因此只能附加仓库外的历史证据缺口说明。后续观察不能重建部署前事实，不得编辑、覆盖、补写或重新生成 b05 的任何 Release 文件。
