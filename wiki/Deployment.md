# Deployment

## Docker 部署（推荐生产环境）

### 构建镜像

```bash
docker build -t issue-agent .
```

Dockerfile 使用两阶段构建：
1. **Builder**: 安装依赖到 site-packages
2. **Runtime**: 仅复制运行时必要文件，基于 `python:3.12-slim`

### 运行容器

```bash
docker run -d \
  --name issue-agent \
  -p 8000:8000 \
  --env-file .env \
  -v issue-agent-data:/app/data \
  issue-agent
```

| 参数 | 说明 |
|------|------|
| `-p 8000:8000` | 容器内固定监听 8000，映射到任意宿主机端口 |
| `--env-file .env` | 注入配置（必须包含 `OPENAI_API_KEY`） |
| `-v issue-agent-data:/app/data` | 持久化 SQLite 数据库（会话历史） |

### Docker Compose 示例

```yaml
version: "3.8"
services:
  issue-agent:
    build: .
    ports:
      - "8000:8000"
    env_file: .env
    volumes:
      - agent-data:/app/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 30s
      timeout: 5s
      retries: 3

volumes:
  agent-data:
```

---

## 本地部署

### Windows 一键启动

完成首次 Local Setup 后，双击项目根目录：

```
打开 Issue Agent.cmd
```

启动器行为：
- 自动检测环境（Python、依赖）
- 端口选择: `8000 → 9123 → 9124 → 9125`（通过 `/health` 验证）
- 自动打开浏览器
- 检测版本更新并替换旧进程
- `Ctrl+C` 停止服务

强制重启:
```powershell
./start-issue-agent.ps1 -Restart
```

### 手动启动

```bash
# 安装
python -m venv .venv
.venv/bin/pip install -e .    # Windows: .venv\Scripts\pip

# 配置
cp .env.example .env
# 编辑 .env

# 启动（开发模式，热重载）
python -m uvicorn app.main:app --port 8000 --reload

# 启动（生产模式）
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

> **注意**: 由于使用 SQLite + 进程内 asyncio Lock，多 worker 模式下 OCC 可防止数据冲突，但建议单 worker 以获得最佳性能。

---

## 生产环境建议

### 反向代理 (Nginx)

```nginx
server {
    listen 80;
    server_name agent.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # SSE 流式端点需要禁用缓冲
    location /stream {
        proxy_pass http://127.0.0.1:8000;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        proxy_set_header Connection '';
        chunked_transfer_encoding on;
    }
}
```

### 安全清单

- [ ] 设置 `API_KEY` 启用认证
- [ ] 使用 HTTPS（通过反向代理或 Caddy）
- [ ] `GITHUB_TOKEN` 使用最小权限 fine-grained token
- [ ] 不需要 PR 功能时保持 `WRITE_MODE=false`
- [ ] 限制 `GITHUB_MAX_FILE_BYTES` 防止内存溢出
- [ ] 生产环境使用 `LOG_FORMAT=json` 便于日志聚合

### 资源需求

| 资源 | 最低 | 推荐 |
|------|------|------|
| CPU | 1 core | 2 cores |
| RAM | 256 MB | 512 MB |
| Disk | 100 MB | 1 GB（含会话历史） |
| Python | 3.11+ | 3.12+ |

> 主要瓶颈在 LLM API 延迟（通常 10-60s/请求），本地资源消耗极低。

### 会话数据管理

- SQLite 数据库位于 `SESSION_DB_PATH`（默认 `data/sessions.db`）
- 启动时自动清理超过 `SESSION_RETENTION_DAYS`（默认 30 天）的已完成/失败会话
- 性能索引: `session_events.created_at`、`sessions.updated_at`、`sessions.status+updated_at`
- 备份: 直接复制 `.db` 文件即可

### 监控

健康检查端点: `GET /health`

结构化日志（`LOG_FORMAT=json`）包含:
- 每次调查的 `duration_ms`、`tool_calls`、`model_calls`
- 异常堆栈 (`exc_info`)
- 请求级别耗时

---

## CI/CD

GitHub Actions 工作流 (`.github/workflows/ci.yml`):

```
push/PR → main
    │
    ├── test (matrix: 3.11, 3.12, 3.13)
    │   ├── ruff check
    │   ├── mypy
    │   └── pytest --cov
    │
    ├── docker (needs: test)
    │   └── docker build
    │
    └── browser
        ├── npm ci
        ├── playwright install chromium
        └── npm run test:e2e
```

所有检查通过后方可合并。
