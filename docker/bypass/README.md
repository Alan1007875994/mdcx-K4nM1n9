# Cloudflare Bypass 派生镜像操作说明（#183）

本说明指导在 NAS（如 QNAP Container Station / Linux NAS）上基于仓库 `docker/bypass/Dockerfile` 构建派生镜像，完成隔离验收、生产切换以及离线回退。

> [!NOTE]
> - 本方案由 GitHub Issue #182 推荐并由 J 拍板执行。
> - 镜像固化了 `cloakbrowser[geoip]==0.4.4` 与 `geoip2==5.3.0`，并在镜像层烘焙了 GeoIP 数据库（`/home/ubuntu/.cloakbrowser/geoip/GeoLite2-City.mmdb`），容器启动无需再次联网下载依赖或数据库。
> - 命令中含 `<...>` 的部分为占位符，请由 J 根据实际 NAS 路径填入。每段命令可整段复制粘贴执行。

---

## 1. 在 NAS 上构建

在电脑终端程序（如 macOS 终端、Windows PowerShell 或 SSH 客户端）中，通过 SSH 登录 NAS 命令行。

### 1.1 准备构建目录并放置 Dockerfile
将本仓库的 `docker/bypass/Dockerfile` 复制到 NAS 本地任意构建目录（例如 `/share/Container/cloudflare-bypass-build/`）。

```bash
# 进入存放 Dockerfile 的目录（请将 <BUILD_DIR> 替换为实际存放 Dockerfile 的路径）
cd <BUILD_DIR>

# 确认当前目录下有 Dockerfile
ls -l Dockerfile
```

**看到什么算成功**：
- 终端输出显示当前目录下存在 `Dockerfile`。

### 1.2 本机构建派生镜像
在构建目录下执行以下命令（锁定 `linux/amd64` 架构，同架构本机构建，无需推送任何镜像仓库）：

```bash
docker build --platform linux/amd64 -t cloudflare-bypass:geoip-20261111 -f Dockerfile .
```

**看到什么算成功**：
- 看到 `pip install --no-cache-dir ...` 成功安装了 `cloakbrowser[geoip]==0.4.4`、`geoip2==5.3.0` 及其传递依赖；
- 看到 Python 脚本执行下载 `Downloading GeoIP database (~70 MB)...` 并顺利通过断言；
- 末尾输出 `naming to docker.io/library/cloudflare-bypass:geoip-20261111` 或 `Successfully tagged cloudflare-bypass:geoip-20261111`；
- 执行 `docker image inspect cloudflare-bypass:geoip-20261111` 能够正常输出镜像信息。

---

## 2. 隔离端口验收（不碰真实服务）

在切换生产服务前，真实容器（端口 8000）全程保持运行。所有验收在独立的测试容器和隔离端口上完成。

### 2.1 断网启动验收（验证完全不依赖网络启动）
在 SSH 终端中粘贴执行以下命令：

```bash
# 1. 启动一个完全断网（--network none）的临时测试容器
docker run -d --name cfb-offline-test --network none cloudflare-bypass:geoip-20261111

# 2. 等待 3 秒后查看容器启动日志
sleep 3
docker logs cfb-offline-test
```

**看到什么算成功**：
- 日志中直接显示 `Uvicorn running on http://0.0.0.0:8000`（或 Application startup complete）；
- 日志中**绝对没有** `Collecting ...`、`pip install` 或 `Downloading GeoIP database` 等在线下载行。

### 2.2 确认 GeoIP 数据库文件在且大小正确
在 SSH 终端中粘贴执行以下命令：

```bash
# 检查断网容器内的 GeoIP 文件路径及字节数
docker exec cfb-offline-test python3 -c "from pathlib import Path; p=Path('/home/ubuntu/.cloakbrowser/geoip/GeoLite2-City.mmdb'); print(f'GeoIP exists: {p.exists()}, size: {p.stat().st_size} bytes')"

# 停止并删除断网测试容器
docker stop cfb-offline-test && docker rm cfb-offline-test
```

**看到什么算成功**：
- 终端打印 `GeoIP exists: True, size: ... bytes`，文件大小约为 70MB（通常大于 60,000,000 字节）；
- 容器清理命令执行完成。

### 2.3 隔离端口启动与冷启动耗时对比
在 SSH 终端中粘贴执行以下命令（使用临时端口 `18000` 映射，不占用生产端口 `8000`）：

```bash
# 在隔离端口 18000 启动测试容器，并记录启动耗时
time docker run -d --name cfb-isolated-test -p 18000:8000 cloudflare-bypass:geoip-20261111

# 等待 3 秒后查看日志
sleep 3
docker logs cfb-isolated-test

# 测试隔离端口 HTTP 连通性
curl -s -o /dev/null -w "HTTP Response Code: %{http_code}\n" http://127.0.0.1:18000/

# 清理隔离测试容器
docker stop cfb-isolated-test && docker rm cfb-isolated-test
```

**看到什么算成功**：
- 容器启动耗时极短（从 `docker run` 到日志显示 `Uvicorn running` 仅需约 2~3 秒，相比旧镜像首次运行需要的 44~45 秒显著减少）；
- `curl` 命令返回 HTTP 状态码（通常为 200 或 404，表明服务已正常监听处理请求）；
- 测试容器顺利停止并清理。

---

## 3. 换上真实服务

确认隔离验收通过后，将真实运行的容器切换到新镜像。

### 3.1 备份旧镜像与原 compose 配置（防线）
在 SSH 终端中，进入真实服务所在目录（即包含当前 `docker-compose.yml` 的目录）：

```bash
# 进入真实 compose 目录（请将 <COMPOSE_DIR> 替换为真实 compose 文件所在路径）
cd <COMPOSE_DIR>

# 1. 给现用旧镜像打上稳定备份标签 pre-182
docker tag ghcr.io/sarperavci/cloudflarebypassforscraping:latest cloudflare-bypass:pre-182

# 2. 备份原 compose 配置文件
cp docker-compose.yml docker-compose.yml.bak-pre-182
```

**看到什么算成功**：
- 执行 `docker images | grep pre-182` 可见 `cloudflare-bypass:pre-182`；
- 当前目录下生成了 `docker-compose.yml.bak-pre-182`。

### 3.2 修改真实服务的 docker-compose.yml
使用文本编辑器（如 `nano docker-compose.yml`）编辑真实配置：

1. **修改镜像字段 `image`**：
   ```yaml
   image: cloudflare-bypass:geoip-20261111
   ```
2. **修改或删除启动命令 `command`**：
   将原先用于在线探测与 pip 安装的命令改为直接调用入口点：
   ```yaml
   command: ["/app/docker-entrypoint.sh"]
   ```
   （或者直接删除 `command` 配置行，容器将自动采用镜像内置的 `CMD ["/app/docker-entrypoint.sh"]`）。

### 3.3 切换并重建真实服务
在 compose 目录执行重建启动命令：

```bash
docker compose up -d --force-recreate
```

**看到什么算成功**：
- 终端输出 `Recreating ... Done`；
- 执行 `docker compose ps` 显示状态为 `Up`；
- 执行 `docker compose logs --tail=50` 查看日志，服务秒级就绪，无 pip 安装或 GeoIP 下载行，真实刮削业务正常访问。

---

## 4. 一步回退（完全不依赖网络）

如果在上线后发现任何异常或需要撤回，在 SSH 终端中进入真实 compose 目录执行以下命令。该回退完全使用本地已固化的 `pre-182` 镜像和配置文件备份，**100% 离线执行，不依赖外网**：

```bash
# 进入真实 compose 目录（请将 <COMPOSE_DIR> 替换为真实 compose 目录路径）
cd <COMPOSE_DIR>

# 1. 还原备份的 compose 配置文件
cp docker-compose.yml.bak-pre-182 docker-compose.yml

# 2. 确保原标签指向本地 pre-182 镜像（避免 tag 漂移）
docker tag cloudflare-bypass:pre-182 ghcr.io/sarperavci/cloudflarebypassforscraping:latest

# 3. 强制重建切回原旧版服务
docker compose up -d --force-recreate
```

**看到什么算成功**：
- 输出显示容器重新创建完成；
- 执行 `docker compose ps` 查看容器状态为 `Up`；
- 真实端口（8000）服务恢复原状，全部切回操作在数秒内完成。
