# Oil World 原始 HTML 归档方案

## 目标与边界

将每个发布期的原始 HTML、Stats 报表、图片、PDF、启动文件和历史导入脚本压缩到 Git 仓库外，避免将数千个来源文件直接纳入 Git。归档仅复制并校验原文件，不移动、不删除、不改写 `01_原始资料` 中的任何内容，也不包含已经正式纳入 Git 的 Excel 工作簿。

仓库外归档根目录：

`C:\Users\xx202\Desktop\codex自动更新\archives\OilWorld原始资料`

计划文件：

- `OilWorld原始HTML_2026-03.zip`
- `OilWorld原始HTML_2026-03.manifest.json`
- `OilWorld原始HTML_2026-06.zip`
- `OilWorld原始HTML_2026-06.manifest.json`

## 预计纳入范围

每个发布期纳入除 `.xlsx` 外的全部原始来源文件，包括：

- 唯一 `START.htm` 文件及根目录板块、国家和目录 HTML；
- `stats` / `Stats` 下的 `.htm`、`_body.htm`、`_head.htm`、JPG、GIF 和附属文件；
- `PDF`、`BILD`、`Autorun` 等原始介质目录；
- 根目录图片、`readme.txt`、`autorun.inf` 和历史 `数据导入.py`；
- 原始介质中存在的其他文件，例如 `.db` 和 `.exe`。

排除 `.xlsx`、Python/pytest 缓存、临时日志、浏览器截图、构建目录和系统临时文件。符号链接如未来出现，默认阻断归档并人工确认，不跟随到来源目录之外。

当前只读统计：

| 发布期 | 预计文件数 | 原始总大小 | START 文件 | Stats 文件 | `_body` 文件 |
| --- | ---: | ---: | --- | ---: | ---: |
| 2026-03 | 1,117 | 45,848,645 bytes | `__March 2026 START.htm` | 1,090 | 84 |
| 2026-06 | 1,147 | 47,059,456 bytes | `__June 2026 START.htm` | 1,121 | 95 |

统计以实际执行归档时重新生成的逐文件清单为准；数量或总大小发生变化时必须停止并重新核查。

## Manifest

每个 manifest 至少记录：

- `release`
- `archive_name`
- `archive_sha256`
- `file_count`
- `total_size`
- `start_file`
- `stats_file_count`
- `body_file_count`
- `created_at`
- `source_directory`

建议额外记录 `schema_version`、归档工具版本、逐文件清单 SHA-256、扩展名统计和排除规则。逐文件条目至少包括相对路径、文件大小和 SHA-256，路径统一使用 `/`，排序后再计算清单哈希。

## 安全实施流程

1. 只读扫描来源目录，确认唯一 START 文件，不允许 0 字节、打不开的文件或逃逸来源根目录的链接。
2. 生成排序后的逐文件清单，记录归档前文件数、总大小和每个文件 SHA-256。
3. 在归档目录写入临时 ZIP；目标正式 ZIP 或 manifest 已存在时默认拒绝覆盖。
4. 关闭 ZIP 后重新打开，逐项核对路径、文件数、未压缩总大小和 CRC，并从 ZIP 中逐文件计算 SHA-256，与来源清单逐项比较。
5. 校验通过后计算 ZIP SHA-256，生成临时 manifest；重新读取 manifest 验证字段和哈希。
6. 仅在全部检查通过后，将临时 ZIP 和 manifest 原子改名为正式文件。
7. 再次确认原始目录的逐文件清单哈希未变化。任何一步失败均删除仓库外临时产物，不操作来源文件。

## 后续处理原则

归档成功本身不授权删除 Git 工作区中的原始 HTML。只有在 ZIP 可读取、逐文件哈希一致、manifest 完整并另行获得用户明确确认后，才能单独评估清理方案。正式 Excel、发布快照、映射审计和业务数据不属于本归档清理范围。
