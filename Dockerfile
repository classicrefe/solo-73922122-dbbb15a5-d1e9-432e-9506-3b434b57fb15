# 轻量运行镜像：slim 基础镜像 + 纯 Python 依赖，无需编译工具链
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RECEIPT_DATA_DIR=/data

WORKDIR /srv

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码 + 本地演示银行源（同一镜像可运行两种角色）
COPY app ./app
COPY bank_source ./bank_source

# 非 root 用户运行；/data 用于共享 SQLite 索引与本地原件
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /srv /data
USER appuser

# 回单数据目录（Compose 中挂载命名卷，重启后保留）
VOLUME ["/data"]

EXPOSE 8000

# 容器内健康检查：uvicorn 起来后探测 /health
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
