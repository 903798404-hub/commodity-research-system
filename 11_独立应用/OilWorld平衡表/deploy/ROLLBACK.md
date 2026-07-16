# Oil World 部署回滚

## 前端回滚

1. 每个生产镜像以完整Git SHA标记。
2. 新镜像上线时保留上一稳定镜像和Compose配置。
3. 页面、资源路径或健康检查失败时，只把`oil-world-dashboard`切回上一镜像。
4. 不重建或重启USDA、主工作台和其他容器。

## 数据回滚

1. 季度更新前备份`releases.json`与`latest.json`。
2. 回滚时先恢复匹配的`releases.json`。
3. 最后原子恢复`latest.json`到上一正式发布期。
4. 新release/comparison目录保持不动，待故障审计后处理。
5. 不原地修改或删除正式release目录。

## Nginx回滚

1. 修改主机代理前保留上一配置。
2. 候选配置必须先通过`nginx -t`。
3. 失败时恢复上一配置，再次通过`nginx -t`后才允许reload。
4. 验证主工作台、USDA和Oil World旧入口。

## 触发条件

以下任一情况立即回滚：

- `/oil-world/`或`/oil-world/presentation`不是HTTP 200；
- JSON请求返回HTML；
- `latest.json`或`releases.json`无法解析；
- latest引用的release index不存在；
- 13张演示页任一页无法读取正式数据；
- 主工作台或USDA出现回归；
- 健康状态持续为`unhealthy`。

不得留下前端、数据指针或代理配置只更新一部分的状态。
