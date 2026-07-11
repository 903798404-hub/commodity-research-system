# 腾讯云轻量应用服务器部署说明

服务器信息：

```text
公网 IP：124.222.106.25
系统镜像：Ubuntu22.04-Docker26
Docker version 26.1.3
Docker Compose version v2.27.1
服务器目标目录：/home/ubuntu/market-data
```

## 1. 上传项目到服务器

在本地项目根目录（即本文件所在项目目录）打包或同步代码到服务器。

示例做法一：使用 `scp` 上传压缩包。

```bash
scp market-data.zip ubuntu@124.222.106.25:/home/ubuntu/
ssh ubuntu@124.222.106.25
cd /home/ubuntu
unzip market-data.zip -d market-data
```

示例做法二：如果代码已放在私有 Git 仓库，可在服务器拉取。

```bash
ssh ubuntu@124.222.106.25
cd /home/ubuntu
git clone <your-private-repo-url> market-data
```

不要把敏感数据、Wind 原始数据、API 密钥提交到 GitHub。`01_data`、`06_outputs`、`10_logs` 建议通过安全文件传输或服务器备份恢复。

## 2. 进入项目目录

```bash
cd /home/ubuntu/market-data
```

确认核心数据库存在：

```bash
ls -lh 01_data/historical_spread_database.xlsx
```

## 3. 启动看板

```bash
docker compose up -d --build
```

如果 Docker build 卡在 pip 下载，或出现 `files.pythonhosted.org` 超时，应确认 `Dockerfile` 使用腾讯云 PyPI 镜像源：

```text
PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
PIP_DEFAULT_TIMEOUT=120
```

并使用如下方式安装依赖：

```text
python -m pip install --no-cache-dir --default-timeout=120 -r requirements.txt
```

服务器已验证该方式可用。

容器服务名：

```text
spread-dashboard
```

容器内 Streamlit 监听 `8501`，服务器端口映射为：

```text
8501:8501
```

## 4. 查看日志

```bash
docker compose logs -f
```

只看看板服务日志：

```bash
docker compose logs -f spread-dashboard
```

## 5. 停止服务

```bash
docker compose down
```

## 6. 更新代码后重启

```bash
cd /home/ubuntu/market-data
docker compose down
docker compose up -d --build
```

如果只更新了挂载目录中的 `01_data`、`06_outputs`、`10_logs`，通常不需要重新 build，可直接重启：

```bash
docker compose restart spread-dashboard
```

## 7. 备份 01_data、06_outputs、10_logs

`01_data`、`06_outputs`、`10_logs` 是可持久化目录，应定期备份。

```bash
cd /home/ubuntu/market-data
tar -czf market-data-backup-$(date +%Y%m%d_%H%M).tar.gz 01_data 06_outputs 10_logs
```

恢复时：

```bash
tar -xzf market-data-backup-YYYYMMDD_HHMM.tar.gz -C /home/ubuntu/market-data
```

## 8. 访问测试

在浏览器访问：

```text
http://124.222.106.25:8501
```

如果无法访问，请检查：

```bash
docker compose ps
docker compose logs -f spread-dashboard
```

并确认腾讯云轻量应用服务器防火墙/安全组已放行 TCP `8501`。

## 9. 安全提醒

不要长期裸露 `8501` 端口。正式公开前应增加至少一种保护：

- 域名
- HTTPS
- 账号密码
- Nginx/Caddy 反向代理
- 服务器防火墙白名单

不要把 Wind 原始数据、API 密钥、`.env`、`01_data`、`06_outputs`、`10_logs` 上传到公开 GitHub 仓库。

