# 生产 Runtime Manifest 合同

本文属于独立 `shared-runtime-manifest` 项目，定义源码中的通用清单。它不是某台服务器的配置，不签发生产授权，也不是实际容器可部署性证明。项目边界以 [Project Registry](../../02_configs/project_registry.json) 为准；上位规范见 [标准开发与生产发布规范](../03_标准开发与生产发布规范.md)。

## 版本与入口

结构 Schema 位于 [runtime_manifest.schema.json](../../02_configs/runtime_manifest.schema.json)，完整解析器位于 [runtime_manifest.py](../../03_src/agri_research_agent/shared/runtime_manifest.py)。

- `runtime-manifest/1` 保留现有清单的 18 个顶层字段和结构语义，包括显式空 roots/mounts。没有新增字段时不能推导 v2 身份根或初始化语义。
- `runtime-manifest/2` 使用这 18 个字段并要求 `identity_root_role`、`initialization_commands`、`source_inputs`，共 21 个字段。未知版本、未知字段和缺失字段均拒绝。
- `parse_runtime_manifest(raw)` 接受已经解析的普通 JSON 对象，返回不可变对象；`to_dict()` 返回独立的可变副本，不改变已解析合同。
- `load_runtime_manifest(path)` 严格读取 UTF-8 JSON，两个版本都拒绝重复键和非有限数值。这是新 API 的明确要求；旧 A 消费者原有的 JSON 读取行为尚未被替换，不得以此宣称无差别替换旧读取器。

JSON Schema 负责结构、类型和常量；Python 解析器补充跨字段的角色、路径、权限及重复输入约束。仅通过 JSON Schema 不等于完整合同验证。解析器不读取 Git、Docker、宿主挂载或生产数据。

## 基本源码合同

`project_id`、`module_id`、`service_id` 标识项目、模块与服务；目标必须为 `production_container`，执行身份必须显式为 `oci_container`。清单解析成功不证明 project 已获 Registry 授权，实际消费者仍须与所选 Registry record 核对。

`build` 精确声明 Dockerfile、dockerignore、依赖合同列表及有序 Compose source 列表。清单中的路径是仓库相对路径，不得替代部署时通过实际 Compose 元数据识别的配置来源。`entrypoint` 是正式入口 argv；`working_directory` 是容器内逻辑工作目录。

`required_environment` 只声明必需的环境变量键，`secret_references` 只声明既有 secret-file 合同的引用名称，不保存值。`required_executables` 和 `required_python_modules` 声明运行依赖。这里的列表不等于依赖已安装；后续验证器必须在目标镜像内验证实际可用性。不得在 argv 中嵌入 secret 值。

清单不包含宿主服务器绝对路径、当前 Image ID、Container ID、授权 grant、私钥、实际环境变量值、rendered Compose 或候选实例临时目录。这些实例事实属于受保护的 deployment evidence。容器内 `/app/...` 一类逻辑路径可以固定，宿主存储来源不能由源码清单冒充。

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

两个清单版本均保持现有完整的 13 个 `validation_probes` 名称：入口初始化、执行身份、依赖、运行路径、挂载权限，以及 missing grant、wrong Commit、wrong Tree、wrong Image ID、wrong service、wrong manifest、Preview write 和 release mismatch 的拒绝验证。源码声明 probes 不意味着它们已经执行。

## 消费者适配和交付边界

本项目只交付 Schema、共享解析器、测试和本文四个文件。现有 `target_runtime_gate.py` 与 host authorization 仍按各自现有版本处理；它们在完成独立 owner 适配前必须拒绝 v2。不能因为 C 已进入 main 就声称 A/B 消费链支持 v2。

后续独立阶段必须完成：治理侧版本分派及 source hash 绑定；host 侧按 identity role 验证真实挂载并证明候选临时存储隔离；Linux 引擎侧精确镜像构建、无 `.git` 的实际初始化与正反向验证；release 前再次核对同一 Image ID、Commit/Tree、实际 Compose、挂载和清单。

本合同不修改 FULL DAILY/Wrapper，不接入业务 capture、调度或 Notification，不写旧 SEALED Snapshot，不变更 Approved identity，不部署。真实 AM/PM temporal acceptance 仍属于后续实际时点的独立证据。
