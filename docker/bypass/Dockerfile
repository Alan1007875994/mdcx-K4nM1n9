# Cloudflare Bypass for Scraping - 派生镜像
# 固化运行依赖（cloakbrowser[geoip] 与 geoip2）并在构建期预下载 GeoIP 数据库
# 对应 Marcus515J/mdcx #182 方案与 #183 任务；操作说明见同目录下 README.md

FROM ghcr.io/sarperavci/cloudflarebypassforscraping@sha256:0e679c1c0f6ac3209d233c2b7e99737f39a137987171b5bf30b15ef53a01ce33

USER ubuntu

# 1. 固化直接运行依赖（钉住版本，避免容器启动时动态联网 pip 安装）
RUN pip install --no-cache-dir \
    "cloakbrowser[geoip]==0.4.4" \
    "geoip2==5.3.0"

# 2. 构建期预下载 GeoIP 数据库至缓存路径 /home/ubuntu/.cloakbrowser/geoip/GeoLite2-City.mmdb
#    使用 cloakbrowser 原生函数下载，并在构建期严格断言路径一致且文件有效（>1MB）
RUN python3 -c "\
from pathlib import Path; \
from cloakbrowser.geoip import _ensure_geoip_db; \
p = _ensure_geoip_db(); \
expected = Path('/home/ubuntu/.cloakbrowser/geoip/GeoLite2-City.mmdb'); \
assert p and p.exists() and p.resolve() == expected.resolve() and p.stat().st_size > 1000000, f'GeoIP download failed or invalid: {p}'"

# 3. 容器启动直接执行上游 entrypoint，无需启动时再次 pip 安装或在线下载 GeoIP
CMD ["/app/docker-entrypoint.sh"]
