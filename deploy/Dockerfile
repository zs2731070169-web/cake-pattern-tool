# pattern-tool 生产镜像（Docker Compose 部署用，文档见 deploy/部署文档.md）
#
# 硬前提（技术方案 6 节）：
# - uvicorn worker=1：SQLite 单写者 + 进程内单例（配置/连接池/线程池），
#   绝不加 --workers 多进程；
# - 容器内端口固定 8200：对外入口由 nginx 网关决定（勿改 PT_PORT——
#   改动需同步 EXPOSE / healthcheck / nginx proxy_pass 三处）；
# - 不使用 start.sh（它每次启动清空 data/，是本地调试口径——容器内
#   靠 TTL 每小时清理 + 重启恢复兜底）。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# opencv-python-headless 在 slim 上的运行库（headless 无 GUI 依赖，仅 glib）
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖层（单独前置，requirements 变更才重建）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# rembg u2netp 模型预下载（4.5MB 烘进镜像，技术方案 7.1"离线部署预下载"口径：
# 运行时不再依赖外网；下载落 HOME 下模型目录，随镜像层分发——容器以 root
# 运行时 HOME 一致即可命中）
RUN python -c "from rembg import new_session; new_session('u2netp')"

# 应用层（.dockerignore 已排除 .env/data/.venv/tests/docs——密钥绝不进镜像）
COPY src/ ./src/
COPY web/ ./web/
COPY db/  ./db/

EXPOSE 8200

# worker=1 是硬前提——不要加 --workers
CMD ["python", "-m", "uvicorn", "src.app.main:app", "--host", "0.0.0.0", "--port", "8200"]
