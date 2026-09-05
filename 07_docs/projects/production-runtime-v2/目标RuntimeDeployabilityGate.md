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
全部 mount target。每个 v2 source Compose 必须把 host 控制的 grant 目录只读挂载到
`/run/market-data-grants`，并把 `MARKET_DATA_EXECUTION_GRANT` 精确设为其中的 `grant.json`；缺失、
可写或改道均 FAIL。实际候选 Compose 再把 runtime mount target 映射到受保护的一次性 scope；
source Compose 的 rendered build 只允许 `context` 与 `dockerfile`，context 必须精确等于候选仓库根，
Dockerfile 必须精确等于 manifest 声明；任何额外 build option、父/子目录或路径别名均 FAIL。
placeholder 只用于候选配置值，不能作为 production 配置值或部署证据。
源码检查同时核对 Dockerfile 的上下文输入：COPY 的每个源必须是已绑定的精确常规文件，
支持无选项的简单参数或 JSON 数组以及续行；拒绝目录、通配符、变量、别名和未声明文件。
Dockerfile、dockerignore、Compose 和验证引擎仅作为审计元数据绑定，不能因此成为 COPY
输入。声明源码、runtime manifest 及其 parser/schema、依赖合同必须全部复制。COPY 覆盖
不证明 RUN 实际使用了依赖合同；锁定安装语义仍须独立审核，并由实际依赖和初始化探针验证。
当前解析合同仅支持单个 digest 固定的基础镜像，构建前检查该镜像没有继承的 ONBUILD。
拒绝 ADD、ONBUILD、自定义 frontend/escape、COPY 选项、RUN 挂载/选项和 heredoc；未知
输入语义不能静默通过。该检查不分析任意 RUN 程序；网络依赖仍须由声明的依赖合同控制，
最终执行字节身份继续由实际 Image ID 与宿主授权绑定。
`secret_references` 只在 checked-in Compose 上精确核对 file-backed secret 名称、绝对容器挂载
目标与顶层 file 引用；target 只能是 `/run/secrets/` 的直接安全子文件，且顶层定义与 service 引用
集合必须精确相等；拒绝额外或重复 secret mount、external secret 和 secret driver。静态检查不
展开或读取宿主 secret file。candidate
Compose 不注入 production secret、空白替代 secret 或调用方临时 secret；初始化命令必须在不读取
secret 的情况下验证 runtime wiring。真实 secret source、文件权限与实际 Compose identity 留在
pre-release/deployment host revalidation 中验证。

候选验证只能创建一次性 candidate scope。候选容器使用不可变 Image ID、只读 rootfs、非 root
数字用户、无网络、drop all capabilities、no-new-privileges，并只挂载 candidate scope 与只读
grant 目录。生产 volume、secret、Docker socket、宿主控制目录、开发 worktree 和 ignored file
都不能成为验证输入。

宿主在容器启动前观察 Image、OCI revision、Git Tree、RELEASE、runtime manifest、marker、实际
Compose、环境、命令、mount 和 hardening，随后使用 candidate 域私钥签发一次性
`production-execution-grant/2`。私钥固定在 `/etc/market-data/runtime-identity`，不进入 Git、镜像
或 evidence。正式 entrypoint 启动后，初始化命令按 manifest argv 逐条执行；不通过 shell 拼接。
Python 依赖探针在实际候选容器内逐模块执行 import，模块可发现但导入失败、缺少传递依赖
或动态库加载失败均为 FAIL；仅 find_spec 成功不能计为 dependencies PASS。

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

其中错误 manifest 与错误 RELEASE 各使用独立的 candidate scope 和 stopped container，实际调用
host grant issuer；只有分别在 manifest hash 与 RELEASE hash 检查点拒绝才可记 PASS。

证据绑定 clean Commit、Tree、全部 source SHA256、不可变 Image ID、rendered Compose SHA256、Linux
builder identity、candidate authorization role、`.git` absence 和 production volume absence。任何
缺项、未知项、候选在验证期间变化或验证后重新 build 都会使 Completion fail closed。
