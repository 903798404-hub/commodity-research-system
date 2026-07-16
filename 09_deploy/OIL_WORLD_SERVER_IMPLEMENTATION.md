# Oil World 服务器灰度实施方案

> 本文是后续操作草案。本地部署准备任务不得执行这些服务器命令。

## 目标

- 独立服务：`oil-world-dashboard`
- 灰度端口：`8081`
- 灰度URL：`http://<服务器IP>:8081/oil-world/`
- 正式URL：`https://<正式域名>/oil-world/`
- 详细看板和演示页共用一个静态Nginx容器
- 正式数据独立只读挂载，季度更新不重建镜像

服务器IP和正式域名只能由运行环境或人工部署配置提供，不写入仓库源码。

## 建议服务器目录

沿用当前服务器项目根：

```text
/home/ubuntu/market-data/                         Git工作区
/home/ubuntu/market-data/01_data/oil_world_public 正式只读挂载源
/home/ubuntu/backups/oil-world/                   前端、指针和配置备份
```

采用`01_data/oil_world_public`前应再次确认磁盘、权限、备份策略和现有运维约定。

## 实施顺序

1. **只读确认现场**
   - 查看服务器Git HEAD、分支/ detached状态、tracked diff、远程地址。
   - 查看现有`spread-dashboard`与`usda-dashboard`状态。
   - 确认8081没有占用。
   - 记录当前Compose、`.env`键名、镜像ID和健康状态。

2. **备份**
   - 备份当前Compose和部署配置。
   - 备份`.env`但不得用仓库文件覆盖。
   - 备份正式`latest.json`和`releases.json`。
   - 保留现有主工作台与USDA镜像。

3. **取得批准版本**
   - 使用服务器只读Deploy Key执行fetch。
   - 检出并核验用户批准的完整main提交。
   - 不直接在服务器修改源码。

4. **准备正式数据目录**
   - 创建`01_data/oil_world_public`候选目录。
   - 只同步正式release、comparison、`latest.json`、`releases.json`。
   - 校验文件哈希、JSON和latest引用。
   - 确保容器以只读方式挂载。

5. **构建候选镜像**
   - 使用Oil World模块的Dockerfile。
   - build arg固定为`OIL_WORLD_BASE_PATH=/oil-world/`。
   - 镜像以批准的完整Git SHA标记。
   - 检查镜像不包含正式数据、原始资料、审计输出和源码缓存。

6. **启动灰度容器**
   - 只启动`oil-world-dashboard`，绑定8081。
   - 不重建或重启USDA、主工作台。
   - 等待容器状态变为`healthy`。

7. **灰度验收**
   - 验证详细页、演示页、latest、releases、release、comparison。
   - 验证直接刷新、404、缓存头、双时间轴和13张演示页。
   - 检查控制台、窄屏和容器重启恢复。

8. **主工作台入口**
   - 灰度阶段在服务器`.env`设置：`OIL_WORLD_DASHBOARD_URL=http://<服务器IP>:8081/oil-world/`。
   - 只重建或重启`spread-dashboard`使环境变量生效。
   - 不修改USDA服务定义、端口、volume、镜像或参数。
   - 验证主工作台卡片在新标签打开灰度URL。

9. **决定正式代理**
   - 灰度稳定后再确认域名、TLS和80/443代理。
   - 正式代理完成后把环境变量改为`https://<正式域名>/oil-world/`。
   - 只重启主工作台容器并验证入口。
   - 最后评估是否关闭8081公网访问或仅绑定127.0.0.1。

## 验收门禁

- Oil World容器健康；
- 详细页和presentation均200；
- missing JSON真实404；
- 缓存头符合路径策略；
- latest指向有效release；
- 主工作台和USDA无回归；
- 正式数据与批准哈希一致；
- 旧镜像、旧指针和代理备份仍在。

## 回滚

1. 主工作台入口异常：恢复`.env`旧值，只重启`spread-dashboard`。
2. Oil World前端异常：停止候选，切回上一SHA镜像。
3. 数据异常：恢复`releases.json`，最后恢复`latest.json`。
4. 代理异常：恢复上一配置，通过`nginx -t`后reload。
5. 不删除新release目录、失败镜像或日志，保留用于审计。
