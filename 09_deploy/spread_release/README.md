# spread-dashboard 发布环境契约

本目录是 `spread-dashboard` 预定的唯一正式切换与回滚入口，但当前工具状态
仍为**候选**。代码和模拟契约测试已经完成；真实 `docker compose config`
验证尚未完成。pyarrow 曾受 Windows 应用控制策略阻断，但 2026-07-19 最终
复审已在仓库 `.venv` 和另一套独立 Python 环境通过相关测试；当前指定测试
结果为 154 passed、3 skipped，3 个 skipped 均因本机没有 Docker Compose。
pyarrow 已不再是当前环境阻塞项。真实 Compose 门槛通过前，不得把本目录称为
正式生产发布工具，也不得用它执行生产部署。

本工具只管理 spread 服务，不重建或切换 USDA、Oil World，也不改变业务数据
更新方式。USDA 和 Oil World 的一条式发布工具仍待实现。

## 五层身份边界

发布流程把以下事实分开记录，避免用一个渲染后的 Compose 哈希承担所有职责：

1. Git 提交和 Tree SHA 标识软件源代码及其树对象，必须使用完整 40 位值。
2. `release.json` 标识不可变软件发布，包括 Release ID、版本化镜像引用、
   Image ID、OCI revision、镜像内 `RELEASE.json`、Dockerfile、
   `.dockerignore`、Compose 模板和数据基线。
3. 候选环境标识候选验收时使用的运行时 URL 和 Compose 渲染结果。
4. `deployment_plan.json` 标识一次具体生产切换的非敏感环境、生产 Compose
   渲染结果、候选与生产的语义差异以及精确回滚对象。
5. `deployment_result.json` 记录计划目标 Git SHA、容器 inspect 实测运行时
   Git SHA、二者核验结果、实际容器 Image ID、OCI revision、镜像内版本文件
   和 HTTP 验收结果。

`Config.Image` 只是容器创建时使用的字符串，不能作为下一次发布或回滚的目标
依据。最终硬校验依据始终是清单中的完整 Image ID。

## 不可变发布清单

候选镜像必须具有版本化引用：

```text
market-data-spread-dashboard:spread-YYYYMMDD-<Git前缀>-bNN
```

禁止 `latest`、`new`、无标签引用和 Compose 自动生成镜像名。候选构建只能执行
一次，镜像的 OCI `revision` 只记录并必须等于完整 Git commit SHA，OCI
`version` 必须等于 Release ID。Tree SHA 不要求写入 OCI 标签；它由可信
`tool_repo_root` 的 `HEAD^{tree}`、Release 产物、镜像内
`/app/RELEASE.json`、候选结果、部署计划、部署结果及各自 Manifest 传播和
验证。`/app/RELEASE.json` 的 Git SHA 和 Tree SHA 必须与 Release 一致。
构建器必须显式传入以下两个身份参数，不能使用默认值：

```text
--build-arg MARKET_DATA_GIT_HEAD=<完整目标 Git SHA>
--build-arg MARKET_DATA_GIT_TREE=<完整目标 Tree SHA>
```

密封命令：

```bash
python3 09_deploy/spread_release/create_release_manifest.py \
  --repository /path/to/isolated-readonly-shallow-clone \
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

生成目录包含 `release.json`、`release.manifest.json`、`release.env` 和
`checksums.sha256`，写入后不得静默覆盖。Schema 2.3 的 `release.json`
记录 Git SHA 和 Tree SHA，并固定 Compose 模板 SHA-256，但不固定某个运行
环境渲染出的最终 Compose SHA-256。

`release.manifest.json` 密封 `release.json` 的文件名、SHA-256、字节大小、
目标 Schema 版本、生成时间、Release Git SHA、Tree SHA 和 Image ID。
`release.env` 与 `checksums.sha256` 暂时保留为兼容产物；后者仍只校验
`release.json` 和 `release.env`，不能替代 `release.manifest.json`。

## 健康就绪契约

`release.json` 和 `deployment_plan.json` 同时密封唯一一组就绪参数：总等待
90 秒、轮询间隔 2 秒、单请求超时 3 秒、连续成功 2 次，端点固定为
`/_stcore/health`，成功响应必须是 HTTP 200 且正文去除首尾空白后严格等于
`ok`。候选验收、正式部署和回滚都只能调用
`wait_for_service_ready.py`，不得各自实现一次性 HTTP 请求。

每次探测前先验证容器仍在运行、实际 Image ID 未改变且 `RestartCount`
没有增加。连接拒绝、连接重置、空回复、请求超时、502、503 和正文不符在
容器身份与状态正常时属于可重试冷启动现象；容器退出或死亡、镜像不符、
重启次数增加属于永久失败并立即停止。成功和失败结果都保存同一有限日志摘要：
采集时间、请求的最后 200 行、原始采样字节数、最多 64 KiB 的脱敏尾部、
尾部 SHA-256、是否截断、实际尾部行数及 warning/error 数量。日志读取失败时
记录不含原始错误文本的受控 `unavailable` warning；不得保存完整无限日志、
Docker inspect 环境变量或密钥值。

候选容器创建后先执行版本身份硬校验，再立即进入同一轮询器：

```bash
bash 09_deploy/spread_release/validate_spread_candidate.sh \
  "09_deploy/releases/${RELEASE_ID}" \
  "http://127.0.0.1:${CANDIDATE_PORT}/_stcore/health" \
  "/path/to/candidate_readiness.json"
```

候选身份、就绪、HTTP、页面、正式环境未变化和数据未变化检查全部通过后，使用
正式入口生成候选结果：

```bash
python3 09_deploy/spread_release/create_candidate_result.py \
  --tool-repo-root /path/to/isolated-readonly-shallow-clone \
  --manifest "/path/to/release/release.json" \
  --readiness-result "/path/to/candidate_readiness.json" \
  --checks-file "/path/to/candidate_checks.json"
```

该命令不可覆盖地生成 `candidate_result.json` 和
`candidate_result.manifest.json`，记录 Release Git SHA、Tree SHA、候选
Image ID、候选容器身份、容器 inspect 实测的
`MARKET_DATA_GIT_HEAD`、验收状态、有限脱敏日志摘要、关键检查摘要和生成
时间。实测运行时 Git SHA 不接受人工参数，必须由 Docker inspect 的
`Config.Env` 读取并与目标完整 SHA 核对。日志摘要作为
`candidate_result.json` 必填内容，并由目标文件 SHA-256 纳入
`candidate_result.manifest.json` 的密封身份。失败候选的就绪/失败证据也由
同一轮询器不可覆盖地写入相同有限摘要，不依赖操作人员手工复制完整日志。
随后删除候选容器并确认其不存在；只有完成这些步骤后才允许生成部署计划。

进口利润 Stage A 候选通过 `prepare_spread_candidate.py` 的受控 runtime 参数增加
唯一的 `/app/runtime/import_profit` 读写 bind，并注入
`IMPORT_PROFIT_RUNTIME_ROOT`。宿主绝对路径只保留在服务器本地候选 Compose，
密封结果和 Manifest 只记录安全批次身份。真实 09:00 门禁未完成时结果状态为
`candidate-waiting-gate`；工具删除候选容器、保留镜像，并明确跳过部署计划。
等待态不是最终候选通过状态。
Stage B 只能通过 `create_candidate_result.py --prior-waiting-candidate-result ...
--completed-gate-evidence ... --output <new-directory>/candidate_result.json` 生成新的版本化
结果；工具验证 Stage A Manifest 后继承其安全 runtime 身份，拒绝原地覆盖，并要求
完整的业务日期、捕获时间、目标合约、`snapshot_batch_id` 和候选 SHA。

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
根 Compose 仅向 `spread-dashboard` 注入 `MARKET_DATA_GIT_HEAD`，并使用缺失
即失败的插值；USDA 和 Oil World 的运行环境不因本工具而改变。

## 生产部署计划

候选最终验收通过后，使用目标 Release SHA 的独立只读浅克隆中的契约代码、正式
Compose 文件、正式 Compose 项目目录和唯一生产环境文件生成计划。候选结果及
其 Manifest 必须已验证，且候选容器已经删除：

```bash
python3 09_deploy/spread_release/create_deployment_plan.py \
  --tool-repo-root /path/to/isolated-readonly-shallow-clone \
  --manifest "/path/to/release/release.json" \
  --candidate-result "/path/to/release/candidate_result.json" \
  --production-compose-file /home/ubuntu/market-data/docker-compose.yml \
  --production-project-dir /home/ubuntu/market-data \
  --production-env-file /home/ubuntu/.config/market-data/spread-production.env
```

`deployment_plan.json` 显式记录 `tool_repo_root`、
`production_compose_file`、`production_project_dir`、Compose 项目名、
服务名、Git SHA、Tree SHA、候选 Image ID、生产环境文件 SHA-256、实际
USDA/Oil World URL、候选与生产各自的 Compose SHA-256、候选结果 SHA-256、
候选容器删除状态、语义哈希、正式服务范围和精确回滚 Git/Image ID。状态固定
为 `deployment_plan_sealed`，并由 `deployment_plan.manifest.json` 密封。

四个目录/身份不得混淆：

- `tool_repo_root`：包含目标 Release SHA 和发布工具的独立只读浅克隆；
- `production_compose_file`：正式切换实际使用且经过哈希验证的绝对 Compose 路径；
- `production_project_dir`：Compose 相对挂载源的解析基准；
- `candidate_image_id`：候选已经验收并在正式切换中复用的精确 Image ID。

正式仓库 HEAD 可以仍是旧提交。预部署只验证 `tool_repo_root` 的 HEAD 和 Tree
SHA；不会也不得在 `/home/ubuntu/market-data` 执行 checkout、依赖安装、测试
或镜像构建。

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

脚本先验证 `release.manifest.json`、`candidate_result.manifest.json` 和
`deployment_plan.manifest.json`，再校验生产环境、隔离工具仓库 Git/Tree、
计划指定的正式 Compose 路径、版本化镜像到 Image ID、OCI revision 和镜像内
版本文件。脚本从计划读取 Compose 文件、项目目录、项目名、服务名和候选
Image ID，然后仅执行：

```text
docker compose --env-file <production-env> \
  --project-name <compose-project> \
  --project-directory <production-project-dir> \
  -f <production-compose-file> \
  up -d --no-build --no-deps <production-service>
```

切换后先证明实际容器 Image ID 与候选结果和计划中的 Image ID 完全相同，
验证 OCI revision、镜像内 `RELEASE.json` 的 Git SHA/Tree SHA，并从 Docker
inspect 的 `Config.Env` 实测 `MARKET_DATA_GIT_HEAD`。实测值必须等于计划目标
完整 SHA；缺失、空值、冲突值或不一致均停止。通过后再按密封参数完成有界就绪
轮询，最后不可覆盖地写入 `deployment_result.json` 和
`deployment_result.manifest.json`。结果区分计划目标 Git SHA、实测运行时 Git
SHA 和验证布尔值；发布清单和部署计划不会被改写。

回滚同样必须提供同一个密封计划：

```bash
bash 09_deploy/spread_release/rollback_spread_release.sh \
  "09_deploy/releases/${RELEASE_ID}" \
  "09_deploy/releases/${RELEASE_ID}/deployment_plan.json"
```

回滚只接受 `release.json` 中固定的回滚镜像引用和完整 Image ID，切换后再次
校验实际容器 Image ID。回滚不会恢复或修改宿主机业务数据。

## 候选 `01_data` 隔离

正式 Compose 含有 `/app/01_data` bind 时，`prepare_spread_candidate.py`
拒绝原样继承生产 host 路径。调用方必须同时提供：

```text
--data-host-root <候选Manifest数据根，内部含01_data/>
--candidate-data-host-root <候选专属01_data目录>
--candidate-data-approved-root <包含该目录的候选专属批准根>
--candidate-basis-sha256 <密封basis_quotes.parquet SHA-256>
```

`--data-host-root` 继续只表示 Manifest 数据身份根，不改变其既有语义；
`--candidate-data-host-root` 才控制候选容器实际的 `/app/01_data` host 来源。
工具使用规范化绝对路径拒绝候选与生产目录相等、互为父子、符号链接逃逸、
Git 检出内数据以及不匹配的 basis 哈希。候选根挂载保持正式挂载的类型、目标
和模式，同时对 `basis_quotes.parquet` 增加精确只读文件 bind，保证候选进程
不能修改密封数据工件。其他正式挂载保持逐项相等。

Release 数据统计在候选容器启动后通过候选容器读取，不能借用正式容器。候选
Compose 只用于候选验收；后续 `deployment_plan.json` 仍由正式
`production_compose_file`、正式项目目录和正式环境生成，不记录或使用候选
数据路径。

## 数据与敏感信息边界

价差、基差、美豆种植进度和优良率数据由宿主机绑定挂载提供。清单记录候选验收
时的数据快照，包括路径、SHA-256、大小、mtime、记录数、最新业务日期和主键
检查结果；定时任务后续更新这些宿主机文件属于正常运行变化。

`.env`、密码、Token、API Key、SSH 私钥等敏感内容不得进入发布清单、部署
计划或镜像版本文件。生产环境文件仅允许四个已声明的非敏感变量。

## 四类 Manifest

| JSON 产物 | Manifest |
|---|---|
| `release.json` | `release.manifest.json` |
| `candidate_result.json` | `candidate_result.manifest.json` |
| `deployment_plan.json` | `deployment_plan.manifest.json` |
| `deployment_result.json` | `deployment_result.manifest.json` |

任何后续阶段读取上述 JSON 前都先验证对应 Manifest。目标文件内容、Manifest
哈希、字节大小、Schema 版本、Release Git SHA、Tree SHA 或 Image ID 任一
不一致即停止。候选结果和部署结果 Manifest 还密封实测运行时 Git SHA；Release
和部署计划 Manifest 不得伪造该实测字段。进口利润候选的 Manifest 还可密封不含
宿主路径的 runtime mount 身份。Manifest Schema 自身将
`artifact_type`、`target_file` 和 `target_schema_version` 一一绑定。当前
当前实现常量指定的版本分别为：`release.json` 2.6.0、
`candidate_result.json` 1.6.0、`deployment_plan.json` 1.5.0、
`deployment_result.json` 1.4.0，以及四类 Manifest 共用的 1.5.0。
候选结果 1.5.0 与 Artifact Manifest 1.4.0 继续作为只读兼容输入；新产物只写新版本。

四类 JSON 和四类 Manifest 使用同一排他发布实现：内容先在内存中完成
序列化和 Schema 校验，再完整写入同目录临时文件并 `flush`、`fsync`，最后
通过硬链接排他创建正式路径。已有目标不会被 `os.replace` 或其他方式覆盖；
Manifest 发布失败时不会留下可被误认为完整成功链的孤立 JSON。

## 验证

维护时必须运行部署契约测试、生产打包测试、完整仓库测试、JSON Schema
校验、Python 和 Shell 语法检查以及 `git diff --check`。本地没有 Docker
时，真实 `docker compose config` 用例明确跳过，不得把跳过计为通过。
2026-07-19 的两套 Python 环境已经通过 pyarrow 相关测试；当前剩余的工具
转正门槛是真实 Compose 验证。在该门槛通过前，本工具保持候选状态，不得用于
生产部署。
