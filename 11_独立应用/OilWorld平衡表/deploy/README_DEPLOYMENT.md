# Oil World 本地生产容器

本工程把详细看板和16:9演示页构建为同一个Vite静态站点，由独立Nginx容器提供服务。

## 结构

```text
浏览器 http://127.0.0.1:8081/oil-world/
  -> oil-world-dashboard:80
      -> /usr/share/nginx/html/oil-world/                 前端静态构建
      -> /usr/share/nginx/html/oil-world/data/oil_world  只读数据挂载
```

镜像构建时使用`OIL_WORLD_BASE_PATH=/oil-world/`，不复制`public/data/oil_world`。季度数据更新不需要重新构建前端镜像。

## 前置检查

PowerShell：

```powershell
$occupied = Get-NetTCPConnection -LocalPort 8081 -State Listen -ErrorAction SilentlyContinue
if ($occupied) { throw "8081端口已占用，停止验收。" }
docker --version
docker compose version
```

8081被占用时不得改用其他端口。

## 构建与启动

从Oil World项目目录执行：

```powershell
docker compose -f deploy/compose.yml build oil-world-dashboard
docker compose -f deploy/compose.yml up -d oil-world-dashboard
```

默认数据源为项目内正式`public/data/oil_world`，以只读volume挂载。Compose不修改USDA或主工作台服务。

## 使用临时数据副本验收

季度热更新测试必须使用临时副本：

```powershell
$temporaryData = Join-Path $env:TEMP "oil-world-docker-acceptance"
Copy-Item -Recurse -Force "public/data/oil_world" $temporaryData
$env:OIL_WORLD_DATA_ROOT = $temporaryData
docker compose -f deploy/compose.yml up -d --build oil-world-dashboard
```

不要在正式`public/data/oil_world`中修改`latest.json`或添加测试字段。

## 本地地址

- `http://127.0.0.1:8081/oil-world/`
- `http://127.0.0.1:8081/oil-world/presentation`
- `http://127.0.0.1:8081/oil-world/data/oil_world/latest.json`
- `http://127.0.0.1:8081/oil-world/data/oil_world/releases.json`

直接刷新验收：

- `/oil-world/presentation`
- `/oil-world/presentation?release=2026-06&slide=palm-oil-balance`

不存在的资源必须返回真实404：

- `/oil-world/assets/does-not-exist.js`
- `/oil-world/data/oil_world/does-not-exist.json`

## 健康状态

镜像内健康检查依次验证：

1. `/oil-world/`可读取；
2. `latest.json`可读取并能解析出非空release；
3. `releases/{latest}/index.json`存在。

```powershell
docker inspect oil-world-dashboard --format '{{json .State.Health}}'
```

## 停止

```powershell
docker compose -f deploy/compose.yml down
```

停止后必须确认8081已经释放。不要使用`docker system prune`或删除用户现有容器、镜像、volume。
