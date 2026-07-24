# ---- build stage ----
FROM python:3.12-slim AS builder
WORKDIR /app
COPY pyproject.toml ./
COPY app/ app/
RUN pip install --no-cache-dir -e .

# ---- runtime stage ----
FROM python:3.12-slim
WORKDIR /app
# 非 root 用户运行：降低容器逃逸后的攻击面
RUN useradd --create-home --shell /bin/bash appuser
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --chown=appuser:appuser app/ app/
COPY --chown=appuser:appuser pyproject.toml ./

USER appuser

EXPOSE 8000
ENV PYTHONUNBUFFERED=1
# 健康检查：容器编排层自动探活，失败自动重启
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
