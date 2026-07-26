# Oil World 数据更新与本地验证契约

> 文档状态：当前项目专项契约。本文件只具体化 Oil World 的数据更新与本地验证；代码发布仍以 [`../03_标准开发与生产发布规范.md`](../03_标准开发与生产发布规范.md) 为准。

## 1. 适用范围和边界

本契约适用于：

- `11_独立应用/OilWorld平衡表/` 下的 Oil World 数据处理和前端；
- 仓库外授权原始资料的读取；
- 新季度 Release 的本地校验、生成、比较、测试和预览；
- Oil World 生产工具尚未补齐期间的停止条件。

授权原始资料、密钥和生产数据不得提交 Git。数据 Release 与代码镜像 Release
是两类对象：只读挂载的数据更新不得隐式触发镜像重建；代码、依赖、前端或镜像
变化则必须进入标准代码发布链。

## 2. 输入契约

先进入项目目录并为本次会话指定仓库外原始资料根目录：

```powershell
cd '11_独立应用\OilWorld平衡表'
$env:OILWORLD_RAW_DATA_ROOT = '<仓库外授权原始资料绝对路径>'
```

本地个人路径不得写入长期规范。历史手册曾使用过具体 Windows 目录作为示例，
该实例不是当前配置，使用前必须重新核验。

目标季度的输入目录固定为：

```text
${OILWORLD_RAW_DATA_ROOT}\YYYY-MM\
```

目录中必须恰好有一个主 Excel。文件名可以包含英文月份和年份，例如
`Oil World-September 2026.xlsx`；实际选择仍以更新脚本的输入校验为准。

以下任一情况必须停止：

- 未设置或无法读取 `OILWORLD_RAW_DATA_ROOT`；
- 目标季度目录不存在；
- 主 Excel 为 0 个或多于 1 个；
- 发布期已存在；
- 映射冲突、引用缺失、时间轴冲突或自然年校验失败；
- 原始资料授权、来源或目标月份无法确认。

## 3. 当前可执行的本地数据流程

以下命令从 Oil World 项目目录执行。先只校验，不写正式 Release：

```powershell
..\..\.venv\Scripts\python.exe 04_scripts\update_oil_world.py --release 2026-09 --validate-only
```

只有 `--validate-only` 通过并确认目标月份后，才生成不可变季度 Release：

```powershell
..\..\.venv\Scripts\python.exe 04_scripts\update_oil_world.py --release 2026-09
```

`2026-09` 只是命令格式示例，不是“当前季度”；每次任务必须替换为已核验的
`YYYY-MM`。不得覆盖既有 Release，也不得跳过预校验直接生成。

生成后至少检查：

```powershell
Get-Content public\data\oil_world\latest.json
Get-Content public\data\oil_world\releases.json
Get-ChildItem public\data\oil_world\comparisons
```

输出边界：

- `public/data/oil_world/latest.json` 指向已发布的最新季度；
- `public/data/oil_world/releases.json` 登记可用 Release；
- `public/data/oil_world/comparisons/` 保存跨期比较；
- 已发布季度内容按不可变对象理解，不得就地修补；
- source hash、mapping、comparison 和时间轴必须相互一致。

自然年校验至少覆盖 Brazil/Soybeans 和 Argentina/Sunflowerseed 的年度归属。

## 4. 定向测试和本地预览

数据 Release 定向测试：

```powershell
..\..\.venv\Scripts\python.exe -m unittest discover -s 08_tests -p 'test_release_pipeline.py' -v
```

前端测试：

```powershell
pnpm --dir 05_apps/oil_world_dashboard test
```

本地预览：

```powershell
pnpm --dir 05_apps/oil_world_dashboard dev
```

开发服务器实际端口以命令输出为准。历史手册曾登记端口 `5175`，对应页面为
`/oil-world/` 和 `/oil-world/presentation`；该端口是历史实例，若本次输出不同，
以本次只读核验结果为准。

## 5. 生产边界

只更新季度数据且生产采用只读数据挂载时，不应重建前端镜像。正式数据挂载宿主
路径和统一原子季度发布入口仍待在实际生产任务中确认；在确认前不得根据本地目录
或历史命令猜测生产路径，不得直接部署。

如果前端代码、依赖、Dockerfile、Compose 或镜像内容变化，三个应用统一适用：

1. main-first；
2. 独立只读浅克隆完整 Git SHA 和 Tree SHA；
3. 在隔离目录构建和验收候选；
4. 密封候选结果和证据后删除候选容器；
5. 计划生成前确认候选容器不存在；
6. 正式部署复用候选验收的同一 Image ID；
7. 正式切换阶段禁止 build；
8. 最后生成并验证部署结果及其 Manifest。

Oil World 的受控发布入口位于 `09_deploy/oil_world_release/`。它只管理
`oil-world-dashboard`，不读取 Spread 或 USDA 的环境变量，也不启动其他服务：

1. `compose.production.yml` 是唯一 Git 管理的生产 Compose 契约，project name
   固定为 `market-data-oil-world`；它只接收 Oil World 环境变量，并把正式数据
   挂载为只读。
2. `compose.candidate.yml` 只绑定 `127.0.0.1` 的 18081–18499 端口范围，restart
   固定为 `no`，不能使用正式容器名、端口或 project name。
3. `prepare_oil_world_candidate.py` 默认 dry-run，只有显式 `--execute` 才允许
   构建和启动候选。真实顺序固定为：正式容器快照、构建、release bundle、候选
   验证、`candidate_result`、候选容器删除、`deployment_plan`。候选验证后保留
   同一 Image ID，正式阶段不得重新 build。
4. `deploy_oil_world_release.py` 只接受已密封的 release、candidate result 和
   deployment plan；它为同一 Image ID 增加正式不可变标签，并且只使用
   `--no-build --pull never --no-deps --force-recreate oil-world-dashboard` 切换。
   计划固定上一正式镜像作为回滚对象；任何身份、数据挂载或正式容器变化不符时停止。

这些工具的本地假 Docker/HTTP 契约测试不能替代服务器真实 Compose 候选门槛。
在服务器门槛通过前，不得使用它们切换正式 Oil World 服务。

## 6. 历史生产命令登记

旧手册记录过以下服务器命令：

```bash
cd /home/ubuntu/market-data/11_独立应用/OilWorld平衡表
docker compose -f deploy/compose.yml build oil-world-dashboard
docker compose -f deploy/compose.yml up -d --no-deps oil-world-dashboard
docker inspect oil-world-dashboard
```

这些命令仅用于保留历史事实，**不是当前授权的生产发布入口，不得直接复用**。
其中在正式项目目录 build 的做法已经被当前标准替代。未来 Oil World 自动化必须
从隔离只读浅克隆构建候选，并以 `--no-build` 复用同一候选 Image ID 正式切换。

## 7. 完成标准

一次本地季度更新只有在以下条件全部满足时才可交付：

- 原始资料根目录和唯一主 Excel 已核验；
- `--validate-only` 成功；
- 正式生成命令成功且未覆盖旧 Release；
- source hash、mapping、comparison、latest 和 releases 一致；
- 数据定向测试和前端测试通过；
- 详情页和 presentation 已本地预览；
- 没有执行服务器连接、生产部署或未经批准的数据传输；
- 若涉及代码发布，已单独进入标准开发与生产发布流程。
