# Target Runtime Deployability Gate

## 当前适用范围

`02_configs/runtime_contracts/spread-production-runtime.json` 是 spread dashboard 的源码合同；其实际 source_inputs、环境绑定、挂载及版本为核验依据。现行大豆页面使用已发布只读行情与独立 SQLite CNF 状态，正式保存由 application-service credential 和运行上下文控制；不得将初期只读 CNF 验证方案当作现行页面禁写规则。

Registry ownership 提供责任和影响映射，不是开发 hard authorization，也不要求先做独立治理 main transition。修改 manifest 与相关接线可在同一明确 candidate 审查；不能取得生产批准或绕过真实 Linux 镜像/权限验证。初期空 candidate inputs、旧 COPY/grant/用户权限不足描述仅是历史开发阶段，不代表当前运行事实。

开发与 main 接纳以根规范的 required hosted CI 为准；Completion 是可选入口，调用时仍消费完整目标验证证据。生产另行核验 Approved Commit/Tree/Image、Manifest、实际挂载和 fresh grant。source_inputs 中保留未激活脚本不授予执行权限；归档和文档不成为运行输入。

## 验证接口

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
manifest/2 的 placeholder 只用于候选配置值，不能作为 production 配置值或部署证据。
manifest/3 使用显式 environment bindings：literal 与 runtime_path 必须和源码 Compose
精确一致；deployment 使用声明的 candidate_value，生产实际值按声明类型验证；
execution_grant 使用受信宿主确定的 grant 路径。源码 Compose 环境名集合必须与声明相等，
实际容器合并后的环境还必须拒绝 forbidden_environment 中的任何键，包括空值。

manifest/3 的 candidate_runtime_inputs 只从绑定 Commit 的精确 Git blob 读取，必须匹配
声明 SHA256 与完整候选 source binding；总大小上限 64 MiB。引擎从 build context 删除这些
文件，Dockerfile 也不得 COPY 它们。宿主仅将其独占创建到一次性 scope 的受保护只读子挂载，
文件权限为 0444；不得覆盖文件、identity marker 或写入其他 scope。签发前及初始化后均核对
宿主与容器可见字节。生产复验不注入 fixture、不拿 fixture 冒充正式数据，正式数据身份仍需
单独验收。
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
`production-execution-grant/2`（manifest/3 对应 grant/3）。私钥固定在 `/etc/market-data/runtime-identity`，不进入 Git、镜像
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

## 跨阶段候选记录与生产复验（当前独立候选，尚未发布闭包）

`04_scripts/runtime/pre_release_runtime.py` 的候选记录入口只接受 Registry project、全新
record 输出路径、受保护的 candidate-domain key 和有限有效期。入口自行执行隔离验证器，
不接受调用方提交 evidence、Image ID 或 probe 结论。执行前后必须确认自身源码是 root
控制的 clean detached 普通 Git clone，全部 tracked 字节与 Git archive 一致；不能使用
linked feature worktree、隐藏 index flags、replacement refs 或忽略的 bytecode。

`candidate-validation-record/1` 以现有 candidate-validation Ed25519 key 签名。签名覆盖
完整 envelope metadata 与 payload（除 signature 本身）；payload 包含目的、角色、record ID、
签发/到期时间、完整 `target-runtime-evidence/1` 与其 canonical SHA256。最长有效期七天。
严格拒绝未知字段、重复 JSON key、非有限数、错误签名/签名域、过期、撤销、缺少探针和
候选身份漂移。现有 trust 的 `revoked_grant_ids` 同时作为此签名记录的 record ID 撤销列表；
记录只证明候选验证事实，不授予 production write。输出使用受保护目录、0600 和 exclusive
创建，不能覆盖已有记录。

生产 host policy 新增 `/3`，必须消费该记录并重新绑定 Approved source Commit/Tree、
完整 source binding、runtime manifest 和同一不可变 Image ID。`policy/2` 只允许 candidate
validation；旧 `policy/1` 与 manifest/1 兼容；容器执行授权继续使用 grant/2，不引入第三类
审批密钥。签发前后必须重新读取 policy、record、trust 和实际实例。

manifest/3 显式使用 `target-runtime-validator/2`，candidate policy/4、production policy/5
以及 grant/3；旧 manifest/2 保持 validator/1、policy/2 与 /3、grant/2。
candidate-validation-record/1 的签名载荷只接受这两个明确的 validator 版本，消费时仍须
与当前源码生成的完整 binding 精确一致。版本不能自动 fallback，旧 validator 证据不能
证明 manifest/3 的环境和候选输入已验证。

候选 render 和生产 render 分别记录，不能要求临时路径与生产路径相等。生产复验用同一
受保护 environment file 重新渲染 Approved 源 Compose，再与真正使用的生产 Compose 比较；
只允许移除 build metadata 和加入 host nonce，其余配置差异拒绝。生产 storage allocation
与开发 clone、候选临时 scope 物理隔离。file-secret 必须依照其实际 bind mount 观察验证
source、target 和只读权限；secret 引用校验不能代替容器实际观察。源码 Compose 字节由候选
record 绑定，实际部署 Compose 字节由 protected policy 绑定；两者路径不必相同，必须通过
上述 render 比较证明只有明确允许的部署差异。本机 Docker/Compose/socket 身份检查同样
用于生产复验，但不会执行镜像构建。

生产复验入口使用 `--production-policy --container-id --report-output`，container ID 必须是完整
64 位小写 hex。此模式只能观察 fresh unstarted 实例，不加载生产私钥、不签 grant、不启动容器；
结果独立记录候选/生产 render、实际 Image ID、manifest、mount、config 和 policy 身份。
secret 文件必须 root 控制、无 world access，并允许容器显式 numeric UID:GID 读取；root 的
owner-write 位不等于业务可写。检查 inode、权限、大小、时间及内容摘要在前后观察间不漂移，
报告只保存整体 identity hash，不暴露凭据内容。

本次 candidate 须完成适用 required / impact / platform CI 和实际 Linux 候选验证；main 与生产分别审查，独立 integration 不是通用前置。
上述候选实现与专项测试不代表 Production Approval，
不允许因此启动 production grant、自动 capture、FULL DAILY 接入或 Notification activation。
