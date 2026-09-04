# Target Runtime Deployability Gate

`runtime_target=production_container` 的项目必须在 code closure 前，由候选源码自身的
`04_scripts/runtime/validate_target_runtime.py` 生成 `target-runtime-evidence/1`。Completion
只消费并重新绑定这份证据，不接受项目调用方提供 Image ID、探针结果或 evidence 文件。

引擎只接受 Registry project、Registry 中的 runtime contract 路径和一个全新绝对输出路径。
它必须在 root 控制的 Linux builder 上，从 clean committed HEAD 执行 `git archive HEAD`，以
归档作为唯一 build context，并确认归档与镜像都不含 `.git`。Windows 或没有 Docker/Compose
的环境只能返回 `LINUX_BUILDER_UNAVAILABLE` 和退出码 3；镜像、Compose、身份、依赖、路径、
权限或探针错误均为 FAIL。

引擎先以 `docker compose config --no-interpolate` 读取 manifest 声明的 checked-in Compose
sources，核对 service、Dockerfile、entrypoint、working directory、环境变量名、secret 引用和
全部 mount target。实际候选 Compose 再把这些 mount target 映射到受保护的一次性 scope；
placeholder 只用于候选配置值，不能作为 production 配置值或部署证据。

候选验证只能创建一次性 candidate scope。候选容器使用不可变 Image ID、只读 rootfs、非 root
数字用户、无网络、drop all capabilities、no-new-privileges，并只挂载 candidate scope 与只读
grant 目录。生产 volume、secret、Docker socket、宿主控制目录、开发 worktree 和 ignored file
都不能成为验证输入。

宿主在容器启动前观察 Image、OCI revision、Git Tree、RELEASE、runtime manifest、marker、实际
Compose、环境、命令、mount 和 hardening，随后使用 candidate 域私钥签发一次性
`production-execution-grant/2`。私钥固定在 `/etc/market-data/runtime-identity`，不进入 Git、镜像
或 evidence。正式 entrypoint 启动后，初始化命令按 manifest argv 逐条执行；不通过 shell 拼接。

以下 13 项必须全部来自本次候选的实际观察并为 PASS：

- `entrypoint_initialization`
- `runtime_identity`
- `dependencies`
- `runtime_paths`
- `mount_permissions`
- `missing_grant_rejected`
- `wrong_commit_rejected`
- `wrong_tree_rejected`
- `wrong_image_rejected`
- `wrong_service_rejected`
- `wrong_manifest_rejected`
- `preview_write_rejected`
- `release_mismatch_rejected`

证据绑定 clean Commit、Tree、全部 source SHA256、不可变 Image ID、rendered Compose SHA256、Linux
builder identity、candidate authorization role、`.git` absence 和 production volume absence。任何
缺项、未知项、候选在验证期间变化或验证后重新 build 都会使 Completion fail closed。
