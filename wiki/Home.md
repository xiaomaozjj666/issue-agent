# GitHub Issue Agent — Wiki

[![CI](https://github.com/xiaomaozjj666/issue-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/xiaomaozjj666/issue-agent/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](../LICENSE)

## 项目简介

GitHub Issue Agent 是一个**企业级 AI 代码分析服务**。它接收 GitHub Issue URL，通过有界工具调用循环（tool-calling loop）自主探索仓库源码，最终输出带有行级证据的结构化根因分析报告。

核心特性：

| 特性 | 说明 |
|------|------|
| 工具调用循环 | 模型自主探索目录树、代码搜索、文件读取、Git 历史 |
| 证据审计 | 交叉验证模型声明与实际读取的源码，无证据则强制 `confidence: low` |
| 独立评审 | 独立 Reviewer Agent 挑战根因、替代假设、修复状态 |
| SSE 流式 | 实时推送调查进度（phase / tool_call / thinking / report） |
| 会话持久化 | SQLite 存储，支持搜索、归档、恢复、取消 |
| 安全写模式 | 默认只读；PR 创建需 `WRITE_MODE=true` + 显式 `confirm=true` |
| 双接口 | REST API (FastAPI) + CLI |

## 文档导航

| 页面 | 内容 |
|------|------|
| [Architecture](Architecture.md) | 系统架构、模块职责、数据流、设计决策 |
| [API Reference](API-Reference.md) | 完整 REST API 接口文档（请求/响应/错误码） |
| [Configuration](Configuration.md) | 所有环境变量与配置项详解 |
| [Deployment](Deployment.md) | Docker / 本地 / 生产环境部署指南 |
| [Development Guide](Development-Guide.md) | 开发环境搭建、测试、代码规范、CI |

## 技术栈

- **语言**: Python 3.11+
- **Web 框架**: FastAPI + Uvicorn
- **LLM SDK**: OpenAI Python SDK（兼容 DeepSeek / 任意 OpenAI-compatible 端点）
- **HTTP 客户端**: httpx（连接池 + 指数退避重试）
- **持久化**: aiosqlite（异步 SQLite）
- **配置管理**: pydantic-settings（frozen，环境变量 / .env 驱动）
- **前端**: 原生 JS + Primer CSS（无构建步骤）
- **测试**: pytest + pytest-asyncio + Playwright (E2E)
- **代码质量**: ruff (lint) + mypy (type-check)

## 快速开始

```bash
# 1. 克隆并安装
git clone https://github.com/xiaomaozjj666/issue-agent.git
cd issue-agent
python -m venv .venv
.venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip

# 2. 配置
cp .env.example .env
# 编辑 .env 设置 OPENAI_API_KEY

# 3. 启动
python -m uvicorn app.main:app --port 8000 --reload

# 4. 使用
# 浏览器打开 http://127.0.0.1:8000
# 或 CLI:
issue-agent analyze https://github.com/owner/repo/issues/123
```

## 版本

当前版本: **0.6.0** — 见 [pyproject.toml](../pyproject.toml)
