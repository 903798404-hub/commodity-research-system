# Oil World 正式数据更新

## 原则

- 前端镜像不包含正式业务数据。
- 容器中的数据目录是宿主机正式数据根的只读挂载。
- release和comparison目录一经发布即视为不可变。
- `latest.json`是最后更新的正式指针。
- 任一校验失败都不得移动latest。

## 正式目录内容

只同步：

```text
latest.json
releases.json
releases/{release}/
comparisons/{previous}_to_{current}/
```

禁止同步原始Excel、HTML、Stats、JPG/GIF/PDF、导入脚本、候选工作簿、`06_outputs`、测试缓存、preview或`node_modules`。

## 季度更新顺序

1. 本地生成并审计新release与comparison。
2. 核对批准的Git提交、JSON结构、文件清单、记录数和哈希。
3. 在服务器非实时候选目录接收数据。
4. 先安装`releases/{new_release}`。
5. 再安装`comparisons/{previous}_to_{new_release}`。
6. 验证新release index、组合文件、comparison index及全部引用。
7. 备份实时`releases.json`和`latest.json`。
8. 在同一文件系统内原子替换`releases.json`。
9. 最后原子替换`latest.json`。
10. 检查HTTP、详细页、演示页和容器健康状态。

数据目录是挂载卷，因此步骤8和9完成后，容器无需重建或重启即可读取新数据。

## 热更新验收

本地验收只能操作临时数据副本：

1. 将正式数据复制到系统临时目录。
2. 使用`OIL_WORLD_DATA_ROOT`把临时目录挂载进容器。
3. 记录镜像ID、容器ID和启动时间。
4. 在临时`latest.json`增加测试字段。
5. 再次HTTP读取并确认字段出现。
6. 确认镜像ID、容器ID和启动时间未变化。
7. 恢复临时`latest.json`，确认测试字段消失。

正式目录不得用于该测试。
