# 生产 Runtime Manifest 合同

本文属于独立 `shared-runtime-manifest` 项目，定义源码中的通用清单。它不是某台服务器的配置，不签发生产授权，也不是实际容器可部署性证明。项目边界以 [Project Registry](../../02_configs/project_registry.json) 为准；上位规范见 [标准开发与生产发布规范](../03_标准开发与生产发布规范.md)。

## 版本与入口

结构 Schema 位于 [runtime_manifest.schema.json](../../02_configs/runtime_manifest.schema.json)，完整解析器位于 [runtime_manifest.py](../../03_src/agri_research_agent/shared/runtime_manifest.py)。

- `runtime-manifest/1` 保留现有清单的 18 个顶层字段和结构语义，包括显式空 roots/mounts。没有新增字段时不能推导 v2 身份根或初始化语义。
- `runtime-manifest/2` 使用这 18 个字段并要求 `identity_root_role`、`initialization_commands`、`source_inputs`，共 21 个字段。未知版本、未知字段和缺失字段均拒绝。
- `runtime-manifest/3` 在 v2 上增加 `environment_bindings`、`forbidden_environment`、`candidate_runtime_inputs`，共 24 个字段。v1/v2 不增加默认值、不隐式升级；新字段必须使用 v3。
- `parse_runtime_manifest(raw)` 接受已经解析的普通 JSON 对象，返回不可变对象；`to_dict()` 返回独立的可变副本，不改变已解析合同。
- `load_runtime_manifest(path)` 严格读取 UTF-8 JSON，三个版本都拒绝重复键和非有限数值。这是新 API 的明确要求；旧 A 消费者原有的 JSON 读取行为尚未被替换，不得以此宣称无差别替换旧读取器。

JSON Schema 负责结构、类型和常量；Python 解析器补充跨字段的角色、路径、权限及重复输入约束。仅通过 JSON Schema 不等于完整合同验证。解析器不读取 Git、Docker、宿主挂载或生产数据。

## 基本源码合同

`project_id`、`module_id`、`service_id` 标识项目、模块与服务；目标必须为 `production_container`，执行身份必须显式为 `oci_container`。清单解析成功不证明 project 已获 Registry 授权，实际消费者仍须与所选 Registry record 核对。

`build` 精确声明 Dockerfile、dockerignore、依赖合同列表及有序 Compose source 列表。清单中的路径是仓库相对路径，不得替代部署时通过实际 Compose 元数据识别的配置来源。`entrypoint` 是正式入口 argv；`working_directory` 是容器内逻辑工作目录。

`required_environment` 只声明必需的环境变量键，`secret_references` 只声明既有 secret-file 合同的引用名称，不保存值。`required_executables` 和 `required_python_modules` 声明运行依赖。这里的列表不等于依赖已安装；后续验证器必须在目标镜像内验证实际可用性。不得在 argv 中嵌入 secret 值。

清单不包含宿主服务器绝对路径、当前 Image ID、Container ID、授权 grant、私钥、生产实例参数值、rendered Compose 或候选实例临时目录。这些实例事实属于受保护的 deployment evidence。容器内 `/app/...` 一类逻辑路径可以固定，宿主存储来源不能由源码清单冒充。v3 可声明非密钥的固定应用配置及候选专用参数，具体限制见下文。

## 路径、权限与身份根

`runtime_roots` 每项包含 `role`、`container_path`、`access`；`required_mounts` 每项包含同一 `role`、同一 `container_path` 和对应的 `read_only`。角色、路径必须唯一，两个列表必须逐角色一致。

v2 的 `identity_root_role` 必须指向只读根；它用于定位 `.market-data-runtime.json`。所有可写根必须是该根的严格子路径，不能等于身份根，也不能通过 `..`、反斜杠或路径别名越界。其他只读输入根可以位于身份根之外。

例如，逻辑身份根可以是 `/app/runtime/state`，可写结果根是其下的 `/app/runtime/state/results`。候选与生产可以使用相同容器目标路径，但必须由不同、受验证的宿主存储实例提供。候选只允许独立临时 backing volumes；生产不能指向开发 worktree、Preview 或 fixture。物理隔离必须由宿主观察证明，清单只负责逻辑合同。

Runtime marker 表示数据根身份，不能独立授予生产写权限。实际 `PRODUCTION_WRITE` 仍须同时满足受信任的执行身份、授权角色、marker 和写入路径边界。

## 初始化与受控源码输入

v2 的 `initialization_commands` 是非空、有序列表，每项精确包含唯一的 `name` 和非空 `argv` 字符串数组。命令共用全局 `working_directory`，执行器必须使用 `shell=False`。清单不提供每条命令的宿主 cwd、环境值或跳过验证开关。

语法解析不能证明命令完成了真正初始化或没有副作用。即使 argv 语法有效，仅运行 `--help` 或单纯 import 也不能作为完整 deployability PASS；后续引擎必须在隔离候选容器内执行明确的初始化路径，并观察入口、依赖、RuntimeContext 和挂载行为。此项目不执行命令，也不通过字符串黑名单伪造此结论。

`source_inputs` 为非空的精确文件清单，每项仅包含 `path`、`role`，角色限定为：

| role | 含义 |
| --- | --- |
| `entrypoint` | 正式入口的源码，至少声明一个 |
| `initialization` | 初始化使用的源码、静态资源或非 secret 配置 |
| `runtime_configuration` | 启动读取的受版本控制、非 secret 静态配置 |

这些文件不是整个镜像的 SBOM。路径必须规范、相对仓库、无 glob；重复路径、大小写别名以及与 build 输入重复声明均拒绝。清单自身路径与 source 输入是否重合需要持有该路径的实际消费者验证。

后续 A/D 消费者必须确认输入确为 HEAD 跟踪的 regular file，核对链接、大小写与提交字节，并将 SHA 纳入精确候选绑定。文件存在不等于受控，Git clean 也不能证明不存在 ignored-file 依赖。不得使用开发目录中的未跟踪文件补足目标镜像缺失内容。

## 生产与 Preview

`production_policy` 必须明确 `deployment_role=production`、`write_grant_required=true`。`preview_policy` 必须明确禁止 production write 和 production rw mounts，不能用缺省值推导安全状态。

artifact origin、实际 deployment role 和 write grant 是不同事实。候选镜像经过批准后可以使用同一个 Image ID 晋升生产，无须为了改变 origin label 重建镜像。候选授权只允许临时隔离验证，不等于 Production Approval，不能用来通过生产写入检查。

三个清单版本均保持现有完整的 13 个 `validation_probes` 名称：入口初始化、执行身份、依赖、运行路径、挂载权限，以及 missing grant、wrong Commit、wrong Tree、wrong Image ID、wrong service、wrong manifest、Preview write 和 release mismatch 的拒绝验证。源码声明 probes 不意味着它们已经执行。

## v3 环境值与候选输入

v3 解决候选引擎用通用占位值代替全部环境变量、只创建空运行挂载时无法验证真实初始化的问题。它不改变生产授权角色，不提供新的 secret 环境变量合同，也不允许候选访问生产数据。以下是消费者适配必须实现的合同；本阶段只有纯解析器和 Schema，不执行这些动作。

`environment_bindings` 是显式列表，名称必须唯一并且恰好覆盖 `required_environment`。变量名使用大写 ASCII 字母、数字和下划线，首位不能是数字。每项只接受对应类型的精确字段：

| kind | 额外字段 | 消费者语义 |
| --- | --- | --- |
| `literal` | `value` | 非密钥、非宿主实例的固定应用配置，候选和生产必须相等，例如运行模式；不能用它藏入密钥或主机路径 |
| `runtime_path` | `role`, `relative_path` | 根据声明的运行根角色解析容器路径；空相对路径表示该根，其他值必须是规范相对路径 |
| `deployment` | `value_type`, `candidate_value` | 候选使用显式测试值；生产从受保护的实际 Compose 取得值并校验同一类型，不要求生产值等于候选值 |
| `execution_grant` | 无 | 唯一绑定 `MARKET_DATA_EXECUTION_GRANT`，路径由受信任消费者按现有授权注入合同提供；源码不能指定授权内容或私钥 |

`MARKET_DATA_EXECUTION_GRANT` 必须出现，不能使用其他 kind；其他键也不能使用 `execution_grant`。`forbidden_environment` 是显式、唯一的变量名列表，与必需键不重叠。适配后的引擎及宿主必须核验合并 image ENV 后的实际环境；禁止项即使值为空也必须拒绝。不能只检查 Compose 声明，也不能用初始化命令临时覆盖环境后声称正式入口已验证。

`literal` 解析器只校验非空、首尾无空白、无控制字符以及最多 2048 字符，不能从字符串证明它不是密钥或宿主实例路径。表中的非敏感及实例分离要求属于必须完成的源码审查；不能以 parser PASS 替代审查。

`deployment.value_type` 只支持 `nonempty`、`http_url`、`https_url`。候选 `nonempty` 值限定 ASCII 字母、数字及 `_./:+-`；URL 必须有 host，`http_url` 接受 HTTP/HTTPS，`https_url` 仅 HTTPS，拒绝凭据、查询、fragment、反斜杠、空白和非法端口。候选值最多 2048 字符；这些限制不能自动判断值是否属于密钥，代码审查仍须确认只提交非敏感测试值。生产值不得写回源码或日志；宿主验证后绑定真实 rendered Compose 和实际容器环境。

既有 `secret_references` 继续只引用原 secret-file 合同。v3 不新增 `secret_file` 环境类型，不将 secret 内容放入 literal、deployment、argv 或 fixture。需要超出现有 secret-file 能力时必须先完成独立消费者合同设计。

`candidate_runtime_inputs` 是显式列表，允许为空。每项精确包含：

| 字段 | 约束 |
| --- | --- |
| `source_path` | `08_tests/fixtures/` 下的精确仓库相对文件；无 glob、目录、别名；不得重复或同时列为 image/source/build input |
| `sha256` | 64 位小写十六进制内容 SHA-256；解析器只检查格式 |
| `role` | 已声明的只读子运行根；不能为身份根或可写根 |
| `relative_path` | 非空规范相对文件路径；禁止 marker、`.git`、跨挂载、大小写别名以及文件/目录目标重叠 |

v3 的全部运行根必须位于单个只读身份根之下，且不得等于、包含或位于保留授权注入目录 `/run/market-data-grants` 内；因此候选 seed 不能进入真正的授权目录。普通数据目录中的文件即使名为 `grant.json` 也不是授权来源，消费者不得按文件名探测或切换来源。v2 允许其他只读根在身份根外的语义保留。受信任候选引擎必须从精确提交读取 regular fixture 文件并验证字节 SHA、Git 跟踪状态、链接及来源，将 fixture 身份纳入候选 evidence。fixture 不复制进可提升的业务镜像；Docker 构建不能读取未声明的 fixture。源 fixture 与目标路径不可交给业务进程或任意宿主初始化脚本解释。

引擎只在受保护的候选临时目录中、容器启动之前填充这些文件；先验证目标 role 与真实临时 backing mount 的对应关系，再以不可覆盖方式写入，保持文件及父目录不能被容器用户修改，最后按正式合同只读挂载。不得覆盖身份 marker、grant、已有文件或其他挂载。应在运行后复核只读输入未变，并随候选结果记录真实输入哈希和路径角色。

生产 revalidation/部署不得执行 seed，也不得以 fixture 作为生产存储、旧 SEALED 或真实时点证据。生产运行数据须另有独立来源和身份链。候选 fixture 可证明初始化与拒绝行为，不能证明数据已发布、Production Approval 或 AM/PM temporal acceptance。

## 消费者适配和交付边界

本项目只交付 Schema、共享解析器、测试和本文四个文件。现有 v1/v2 消费链保持原有能力；治理、Linux 引擎、host 和容器 grant 消费者在完成独立 owner 适配前必须拒绝 v3。不能因为纯解析器支持 v3 就声称新合同已完成 deployability 或生产授权。

后续独立阶段必须完成：治理侧版本分派及 source hash 绑定；host 侧按 identity role 验证真实挂载并证明候选临时存储隔离；Linux 引擎侧精确镜像构建、无 `.git` 的实际初始化与正反向验证；release 前再次核对同一 Image ID、Commit/Tree、实际 Compose、挂载和清单。

本合同不修改 FULL DAILY/Wrapper，不接入业务 capture、调度或 Notification，不写旧 SEALED Snapshot，不变更 Approved identity，不部署。真实 AM/PM temporal acceptance 仍属于后续实际时点的独立证据。

## Manual CNF 运行期写入

历史 release、历史 CNF cache、snapshot 和原 intraday results 均保持 immutable / RO。
当前 manual CNF 使用 `/runtime/import-profit/operational/cnf/manual_cnf_quotes.parquet`，
保存产生的 AM result 使用 `/runtime/import-profit/operational/am-results`；只把这两个子目录声明为 scoped RW，
不开放整个 import-profit 根。历史与当前 CNF 按完整 BusinessKey 组合，当前 AM 优先读取 operational store，
缺少对应日期/session 时读取原只读结果；历史 PM 图保持原读取路径，不跨日期补值，不重写历史。

正式 Compose 的 `SPREAD_MANUAL_CNF_ROOT`、`SPREAD_AM_RESULT_ROOT` 必须指向独立、已分配的运行目录，
不可与历史来源重叠。`IMPORT_PROFIT_INTRADAY_ALLOW_CNF_SAVE=1` 仅配合完整 Manifest 和 fresh OCI grant 使用；
缺目录、RO、路径别名或 grant 未授权均在 preflight 失败。关闭开关的只读配置不提供 save handler。
Candidate 的两个 RW 来源只能位于既有验证的 isolated root，不能等于、包含、位于或链接到生产来源；
宿主仍观察实际 mounts 并绑定实例 grant。测试保存只写隔离数据。

人工保存沿用既有 CNF → 已有 SEALED AM → result 流程及 NULL/0、日期、产地、船期、AM/PM、12月合同。
没有该日 AM snapshot 时仍按既有业务检查拒绝保存，不自动 capture。
应用运行后的合法人工保存是业务操作；release/preflight 只配置和验证 capability，不自动写 CNF/result，
不初始化历史数据到新 store，不部署时迁移历史。Runtime/mount 变化仍由现有 risk classifier 分类。
