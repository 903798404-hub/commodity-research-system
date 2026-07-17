# spread-dashboard 发布环境契约

本目录是 `spread-dashboard` 唯一的正式切换与回滚入口。它只管理
spread 服务，不重建或切换 USDA、Oil World，也不改变业务数据更新方式。

## 五层身份边界

发布流程把以下事实分开记录，避免用一个渲染后的 Compose 哈希承担所有职责：

1. Git 提交标识软件源代码，必须使用完整 40 位提交。
2. `release.json` 标识不可变软件发布，包括 Release ID、版本化镜像引用、
   Image ID、OCI revision、镜像内 `RELEASE.json`、Dockerfile、
   `.dockerignore`、Compose 模板和数据基线。
3. 候选环境标识候选验收时使用的运行时 URL 和 Compose 渲染结果。
4. `deployment_plan.json` 标识一次具体生产切换的非敏感环境、生产 Compose
   渲染结果、候选与生产的语义差异以及精确回滚对象。
5. `deployment_result.json` 只记录切换后的实际容器 Image ID、OCI revision、
   镜像内版本文件和 HTTP 验收结果。

`Config.Image` 只是容器创建时使用的字符串，不能作为下一次发布或回滚的目标
依据。最终硬校验依据始终是清单中的完整 Image ID。

## 不可变发布清单

候选镜像必须具有版本化引用：

```text
market-data-spread-dashboard:spread-YYYYMMDD-<Git前缀>-bNN
```

禁止 `latest`、`new`、无标签引用和 Compose 自动生成镜像名。候选构建只能执行
一次，镜像的 OCI `revision` 必须等于完整 Git 提交，OCI `version` 必须等于
Release ID，且 `/app/RELEASE.json` 必须与二者一致。

密封命令：

```bash
python3 09_deploy/spread_release/create_release_manifest.py \
  --repository /path/to/isolated-release-worktree \
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

生成目录包含 `release.json`、`release.env` 和 `checksums.sha256`。三者写入后
不得静默覆盖。Schema 2.0 的 `release.json` 固定 Compose 模板 SHA-256，
但不固定某个运行环境渲染出的最终 Compose SHA-256。

## 运行环境契约

生产 Compose 要求显式提供以下四个非敏感变量：

```text
SPREAD_IMAGE
MARKET_DATA_GIT_HEAD
USDA_DASHBOARD_URL
OIL_WORLD_DASHBOARD_URL
```

推荐唯一生产环境文件：

```text
/home/ubuntu/.config/market-data/spread-production.env
```

该文件必须只包含上述四项，由部署用户拥有，并禁止组用户和其他用户读取。
`SPREAD_IMAGE` 必须等于 `release.json.image_ref`，
`MARKET_DATA_GIT_HEAD` 必须等于 `release.json.git_commit`。两个 URL 必须显式
提供实际生产地址，禁止 localhost、回环地址、凭据、查询参数和片段。

## 生产部署计划

候选验收通过后，使用新 Release worktree 中的契约代码、正式仓库作为生产项目
目录、唯一生产环境文件生成计划。候选验收结果必须已写入 Release 目录旁的
`candidate_result.json`，且候选容器已经删除：

```bash
python3 09_deploy/spread_release/create_deployment_plan.py \
  --repository /path/to/isolated-release-worktree \
  --manifest "/path/to/release/release.json" \
  --production-project-directory /home/ubuntu/market-data \
  --production-env-file /home/ubuntu/.config/market-data/spread-production.env
```

`deployment_plan.json` 记录生产环境文件 SHA-256、实际 USDA/Oil World URL、
Compose 项目名和文件路径、模板 SHA-256、候选与生产各自的最终 Compose
SHA-256、候选结果 SHA-256、候选容器删除状态、语义哈希、正式服务范围和精确
回滚 Git/Image ID。状态固定为
`deployment_plan_sealed`，写入后不得静默覆盖。

候选与生产允许不同的字段只有：

```text
USDA_DASHBOARD_URL
OIL_WORLD_DASHBOARD_URL
```

镜像引用、Image ID、构建定义、命令、工作目录、挂载、正式端口、restart
policy、网络、健康检查和其他环境变量必须一致。正式挂载必须仍是项目目录下的
`01_data`、`06_outputs`、`10_logs`；正式端口必须仍是 `8501:8501`；服务范围
只能是 `spread-dashboard`。任何未声明差异都会拒绝密封计划。

候选 Compose SHA-256 和生产 Compose SHA-256可以不同，因为 URL 是部署环境
事实。是否允许切换由结构化语义比较决定，不再错误地要求两个完整渲染文本哈希
相等。

## 正式切换与精确回滚

正式切换只接受已密封计划：

```bash
bash 09_deploy/spread_release/deploy_spread_release.sh \
  "09_deploy/releases/${RELEASE_ID}" \
  "09_deploy/releases/${RELEASE_ID}/deployment_plan.json"
```

脚本先重新校验发布清单、生产环境、部署计划、Git、模板、版本化镜像到 Image
ID、OCI revision 和镜像内版本文件，然后仅执行：

```text
docker compose --env-file <production-env> ... \
  up -d --no-build --no-deps spread-dashboard
```

切换后先验证实际容器 Image ID 和版本身份，再做 HTTP 验收，最后单独写入
`deployment_result.json`。发布清单和部署计划不会被改写。

回滚同样必须提供同一个密封计划：

```bash
bash 09_deploy/spread_release/rollback_spread_release.sh \
  "09_deploy/releases/${RELEASE_ID}" \
  "09_deploy/releases/${RELEASE_ID}/deployment_plan.json"
```

回滚只接受 `release.json` 中固定的回滚镜像引用和完整 Image ID，切换后再次
校验实际容器 Image ID。回滚不会恢复或修改宿主机业务数据。

## 数据与敏感信息边界

价差、基差、美豆种植进度和优良率数据由宿主机绑定挂载提供。清单记录候选验收
时的数据快照，包括路径、SHA-256、大小、mtime、记录数、最新业务日期和主键
检查结果；定时任务后续更新这些宿主机文件属于正常运行变化。

`.env`、密码、Token、API Key、SSH 私钥等敏感内容不得进入发布清单、部署
计划或镜像版本文件。生产环境文件仅允许四个已声明的非敏感变量。

## 验证

维护时必须运行部署契约测试、生产打包测试、完整仓库测试、JSON Schema
校验、Python 和 Shell 语法检查以及 `git diff --check`。本地没有 Docker
时，真实 `docker compose config` 用例会跳过；服务器在密封 Release 和
deployment plan 时必须执行真实 Compose 解析和 Image ID 校验。
