# spread-dashboard 不可变发布契约

本目录是 `spread-dashboard` 唯一正式切换与回滚入口。它只管理主工作台镜像，不修改 USDA、Oil World 或业务数据更新方式。

## 四个不同的身份

- Git 提交回答“源码是哪一版”，必须是 40 位完整提交。
- Release ID 回答“这是该提交的第几次可追踪构建”，格式是 `spread-YYYYMMDD-<至少12位Git前缀>-bNN`。
- 镜像引用回答“人使用哪个不可变版本标签”，正式标签必须与 Release ID 完全相同。
- Image ID 回答“Docker 实际运行哪一组不可变字节”，格式是 `sha256:<64位>`，是部署与回滚的最终硬校验依据。

`Config.Image` 只是容器创建时采用的字符串。校验工具会在切换后审计它，但任何生成、部署或回滚逻辑都不得从旧容器的 `Config.Image` 推导目标镜像。

## 禁止事项

- 正式切换禁止 `latest`、`new`、无标签引用和 Compose 自动生成的镜像名。
- 正式切换禁止构建和重新打标签；只允许使用候选验证过的同一 `image_ref` 与同一 `image_id`。
- HTTP 200 不是版本证据。必须先通过 Image ID、`Config.Image`、OCI revision 和容器内 `/app/RELEASE.json` 校验，之后才允许 HTTP 验收。
- `release.json`、`release.env` 和 `checksums.sha256` 一经生成不得静默覆盖。
- `.env`、密码、Token、API Key、私钥和服务器地址不得写入发布清单或镜像内版本文件。

## Compose 契约

根 `docker-compose.yml` 固定项目名为 `market-data`，不再依赖当前目录名。`spread-dashboard.image` 必须由 `SPREAD_IMAGE` 显式提供：

```text
${SPREAD_IMAGE:?SPREAD_IMAGE must be set to an immutable release tag}
```

缺少该变量时，Compose 配置解析必须失败。正式脚本只传入清单中的版本标签，并使用 `up -d --no-build --no-deps spread-dashboard`。

## 候选构建身份

候选构建阶段必须一次性确定 Release ID，并把以下非敏感参数传给根 Dockerfile：

```text
MARKET_DATA_GIT_HEAD=<40位提交>
MARKET_DATA_RELEASE_ID=<release_id>
MARKET_DATA_BUILD_TIME=<RFC3339时间>
MARKET_DATA_SOURCE=<无凭据的HTTPS仓库地址>
```

Dockerfile 将它们写入 OCI `revision`、`version`、`created`、`source` 标签，并生成只读 `/app/RELEASE.json`。构建命令只能发生在隔离候选流程中；本目录的正式部署和回滚脚本均没有构建或重新打标签能力。

## 生成候选发布清单

候选容器已经使用正式数据只读挂载并通过页面验收后，执行：

```bash
python3 09_deploy/spread_release/create_release_manifest.py \
  --data-host-root /home/ubuntu/market-data \
  --release-id "${RELEASE_ID}" \
  --git-commit "${FULL_GIT_COMMIT}" \
  --image-ref "market-data-spread-dashboard:${RELEASE_ID}" \
  --expected-image-id "${CANDIDATE_IMAGE_ID}" \
  --build-time "${BUILD_TIME}" \
  --source "${OCI_SOURCE}" \
  --candidate-container-name "${CANDIDATE_CONTAINER}" \
  --rollback-image-ref "${ROLLBACK_IMAGE_REF}" \
  --rollback-image-id "${ROLLBACK_IMAGE_ID}" \
  --formal-git-commit "${CURRENT_FORMAL_GIT_COMMIT}"
```

工具在写文件前拒绝以下情况：Git 不干净或 HEAD 不一致、短提交、模糊标签、标签与 Image ID 不符、OCI revision 不符、候选容器版本文件或其 SHA-256 不符、必需配置缺失、Compose 最终 image 不一致、动态数据缺失或哈希失败、清单出现敏感字段。密封清单同时记录候选前的正式 Git 提交，状态固定为 `candidate_sealed`。

成功后生成：

```text
09_deploy/releases/<release_id>/
  release.json
  release.env
  checksums.sha256
```

`release.env` 只包含 `RELEASE_ID`、`SPREAD_IMAGE`、`EXPECTED_IMAGE_ID`、`EXPECTED_GIT_COMMIT`。

真实发布目录产生于镜像构建之后，不能再提交进它所标识的同一 Git 提交，否则会形成版本身份循环。`09_deploy/releases/<release_id>/` 因此是仓库内本地持久、Git 忽略的运行事实目录；契约代码和目录规则本身仍由 Git 管理。

## 数据基线

`release.json.data_baseline` 记录部署时刻的价差、基差、美豆种植进度和美豆优良率文件，包括宿主机/容器路径、SHA-256、大小、mtime、记录数、最新业务日期及主键重复数。它的 `kind` 固定为 `deployment-time`，并明确 `mutable_after_deployment=true`。

这些文件继续由 `/app/01_data` 宿主机绑定挂载提供。定时任务在部署后更新数据属于正常变化，不会使旧发布清单失效。发布清单记录“候选验收与切换时的数据快照”；正式切换结果另写入 `deployment_result.json`，两者不是同一概念。

## 正式切换

```bash
bash 09_deploy/spread_release/deploy_spread_release.sh \
  "09_deploy/releases/${RELEASE_ID}"
```

脚本顺序固定为：

1. 校验清单 Schema、环境文件和校验和。
2. 校验 Git、Dockerfile、`.dockerignore`、必需配置、镜像标签到 Image ID、OCI 标签、镜像内版本文件和 Compose 最终解析结果。
3. 使用清单中的 `SPREAD_IMAGE` 执行 `--no-build --no-deps` 切换。
4. 先核验正式容器实际 Image ID、`Config.Image`、OCI revision 和容器内版本文件。
5. 身份校验通过后才做 HTTP 验收。
6. 再次核验身份，并单独写入 `deployment_result.json`。

切换后任一步失败都会调用清单指定的精确回滚入口。

## 精确回滚

```bash
bash 09_deploy/spread_release/rollback_spread_release.sh \
  "09_deploy/releases/${RELEASE_ID}"
```

回滚只读取 `release.json.rollback_image_ref` 与 `rollback_image_id`。切换前先验证该标签仍指向清单 Image ID；切换后再次验证正式容器实际 Image ID。回滚不使用 `latest`、`new`、无标签引用或人工推断，也不会恢复数据；现有宿主机数据挂载保持不变。

## 本地维护与测试

Windows 本地没有 Docker/Compose 属于正常状态。使用模拟运行时完成清单、标签、Image ID、OCI、容器身份、失败自动回滚顺序和数据基线测试；如果本机有 Compose，再额外执行真实 `docker compose config` 语法测试。不得为了本契约安装 Docker Desktop、Podman 或 WSL。
