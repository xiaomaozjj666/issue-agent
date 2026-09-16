# ---- build stage ----
FROM python:3.12-slim AS builder
WORKDIR /app
COPY pyproject.toml README.md ./
COPY app/ app/
# 非 editable 安装：镜像里不应保留指向构建阶段源码树的 .pth 链接，
# 运行阶段只保留已安装的包与下面显式 COPY 的 app/。
RUN pip install --no-cache-dir --no-compile .

# ---- runtime stage ----
FROM python:3.12-slim
WORKDIR /app
# 非 root 用户运行：降低容器逃逸后的攻击面
RUN useradd --create-home --shell /bin/bash appuser
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --chown=appuser:appuser app/ app/
COPY --chown=appuser:appuser pyproject.toml ./

# 数据目录必须在 USER 之前创建并 chown：
# app/config.py 默认 session_db_path="data/sessions.db"，app/db.py 会 mkdir 并落库。
# /app 由 root 创建，若不显式授权，非 root 的 appuser 首次落库会 PermissionError；
# 挂载具名卷（-v issue-agent-data:/app/data）时也是以 root 创建的挂载点。
RUN mkdir -p /app/data && chown -R appuser:appuser /app/data

USER appuser

EXPOSE 8000
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# 健康检查：容器编排层自动探活，失败自动重启
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
