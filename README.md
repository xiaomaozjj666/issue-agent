# GitHub Issue Agent

[![CI](https://github.com/xiaomaozjj666/issue-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/xiaomaozjj666/issue-agent/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A read-only FastAPI agent that fetches a GitHub Issue, selects relevant repository source files,
and asks an OpenAI-compatible model for a structured root-cause and fix report.

Unlike a one-shot repository prompt, analysis uses a bounded tool-calling loop. The model explores a
real repository path index, reads selected source excerpts, and then produces a line-grounded report.
Every requested file is normalized and validated against the repository tree before it is fetched.

## Features

- 🔍 **Tool-calling loop** — LLM autonomously explores the repo with `read_file`, `list_directory`, `search_files`, `grep_content`
- 🛡️ **Evidence audit** — cross-checks model claims against actually-read files, forces `confidence: low` when unsupported
- 📊 **Structured reports** — JSON output with summary, root cause, code evidence, proposed changes, unified diff patches, tests, and risks
- 💬 **Interactive chat** — follow-up conversations with session persistence
- 🖥️ **Dual interface** — REST API (FastAPI) + CLI
- 🐳 **Docker support** — ready-to-use Dockerfile

## Safety model

- Only accepts `https://github.com/{owner}/{repo}/issues/{number}` URLs.
- Uses read-only GitHub REST API endpoints and never executes repository code.
- Does not modify files, create branches, or post comments.
- Treats Issue text and repository content as untrusted prompt data.
- Limits candidate files and total model context.
- Limits model output with `MAX_OUTPUT_TOKENS` (4,000 by default).
- Bounds the planning path index and planning output independently.
- Serializes requests within each in-memory session and bounds retained source and chat context.
- Supplies numbered source lines and removes evidence with unknown paths, malformed ranges, or lines
  outside the exact source excerpt given to the model.
- Returns an evidence audit and forces confidence to `low` when no valid source reference supports the
  reported root cause.

## Quick Start

### Docker

```bash
docker build -t issue-agent .
docker run -p 8000:8000 --env-file .env issue-agent
```

### Local Setup

Requires Python 3.11 or newer.

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

**Linux/macOS (bash):**

```bash
python -m venv .venv
./.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env
```

Edit `.env` and set at least:

```dotenv
OPENAI_API_KEY=your-key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4.1-mini
```

Set `GITHUB_TOKEN` for private repositories and to avoid GitHub's low anonymous rate limit. Use a
fine-grained token with read-only access to repository contents and issues.

### Configuration

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | *(required)* | API key for the LLM provider |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base URL for OpenAI-compatible API |
| `OPENAI_MODEL` | `gpt-4.1-mini` | Model name |
| `OPENAI_TIMEOUT` | `60` | Request timeout in seconds |
| `OPENAI_MAX_RETRIES` | `2` | Retry attempts for transient errors |
| `GITHUB_TOKEN` | *(optional)* | GitHub PAT for higher rate limits |
| `GITHUB_MAX_FILE_BYTES` | `512000` | Skip files larger than this |
| `MAX_CANDIDATE_FILES` | `12` | Max distinct source files per investigation |
| `MAX_PLANNING_PATHS` | `80` | Max paths shown to the model in initial prompt |
| `MAX_FILE_CHARS` | `16000` | Max retained characters from one file |
| `MAX_TOTAL_CONTEXT_CHARS` | `80000` | Max retained source + chat characters |
| `MAX_OUTPUT_TOKENS` | `4000` | Max tokens per model response |
| `MAX_AGENT_ITERATIONS` | `15` | Max tool-calling loop iterations |
| `MAX_CHAT_TOKENS` | `2000` | Max tokens per chat message |

### CLI Usage

```bash
# One-shot analysis
issue-agent analyze https://github.com/owner/repo/issues/123

# Save the generated patch
issue-agent analyze https://github.com/owner/repo/issues/123 --save-patch fix.patch

# Interactive chat mode
issue-agent chat https://github.com/owner/repo/issues/123
```

Inside chat mode:
- Type questions to discuss the issue
- `/save <file>` — save the generated patch
- `/quit` or `/exit` — end the session

## API

Start the server:

```bash
python -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for the Swagger UI.

### `POST /analyze`

```json
{
  "issue_url": "https://github.com/owner/repo/issues/123"
}
```

Returns an `AnalysisReport` with summary, root cause, confidence, evidence, proposed changes, patch, tests, and risks.

### `POST /chat`

Start a new session:
```json
{
  "issue_url": "https://github.com/owner/repo/issues/123",
  "message": "分析一下这个 bug"
}
```

Continue an existing session:
```json
{
  "session_id": "a1b2c3d4e5f6",
  "message": "问题出现在哪个函数？"
}
```

### `GET /health`

Returns `{"status": "ok"}`.

## Testing

```bash
pytest -v
```

## Current limitations

- The deterministic fallback file selection is filename-based because GitHub's code search API has stricter
  authentication and indexing constraints.
- Sessions are stored in one process. Deployments with multiple workers need shared external session
  storage (Redis, etc.) to continue a conversation reliably across workers.

## License

MIT — see [LICENSE](LICENSE) for details.
