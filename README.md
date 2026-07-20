# GitHub Issue Agent

[![CI](https://github.com/xiaomaozjj666/issue-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/xiaomaozjj666/issue-agent/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A read-only-by-default FastAPI agent that fetches a GitHub Issue, selects relevant repository source
files, and asks an OpenAI-compatible model for a structured root-cause and fix report. An opt-in write
mode can prepare pull request proposals, which require a separate explicit confirmation before creation.

Unlike a one-shot repository prompt, analysis uses a bounded tool-calling loop. The model explores a
real repository path index, reads selected source excerpts, and then produces a line-grounded report.
Every requested file is normalized and validated against the repository tree before it is fetched.

## Features

- 🔍 **Tool-calling loop** — explores paths, repository-wide code search, source files, and Git history with bounded tools
- 🛡️ **Evidence audit** — cross-checks model claims against actually-read files, forces `confidence: low` when unsupported
- 🧭 **Independent review** — a separate reviewer agent challenges the root cause, alternatives, fix status, and tests
- 📊 **Structured reports** — JSON output with summary, root cause, code evidence, proposed changes, unified diff patches, tests, and risks
- 💬 **Interactive chat** — follow-up conversations with session persistence
- 🗂️ **Session workspace** — searchable history with durable investigation events, metrics, cancellation, and recovery
- ⚡ **Concurrency safety** — optimistic session versions prevent silent cross-worker overwrite
- 🖥️ **Dual interface** — REST API (FastAPI) + CLI
- 🐳 **Docker support** — ready-to-use Dockerfile

## Safety model

- Only accepts `https://github.com/{owner}/{repo}/issues/{number}` URLs.
- Uses read-only GitHub REST API endpoints by default and never executes repository code.
- Repository writes happen only through `POST /session/{session_id}/apply-fix`, which requires `WRITE_MODE=true`, a validated
  stored proposal, and a separate explicit `confirm=true` request.
- PR proposals are revalidated before writing; incomplete write flows attempt to roll back their temporary branch.
- Treats Issue text and repository content as untrusted prompt data.
- Limits candidate files and total model context.
- Limits model output with `MAX_OUTPUT_TOKENS` (8,000 by default).
- Bounds the planning path index and planning output independently.
- Serializes requests within each session and bounds retained source and chat context.
- Supplies numbered source lines and removes evidence with unknown paths, malformed ranges, or lines
  outside the exact source excerpt given to the model.
- Returns an evidence audit and forces confidence to `low` when no valid source reference supports the
  reported root cause.
- Runs a bounded independent reviewer after deterministic evidence validation; reviewer output is validated again
  and safely degrades to the investigator report if the review provider is unavailable.

## Quick Start

### Windows one-click launcher

After completing Local Setup once, open the project folder and double-click:

```text
打开 Issue Agent.cmd
```

The launcher checks the local environment, starts the service, and opens the browser automatically.
The launcher auto-selects an available port from `8000 → 9123 → 9124 → 9125` (it verifies each
candidate is actually this app via the `/health` endpoint before reuse), so the visible URL may
differ across machines. Keep the launcher terminal window open while using the agent; press `Ctrl+C`
in that window to stop it. If the service is already running, the launcher only opens the existing
page instead of starting a duplicate process. After updating the project, the launcher compares
build identities and automatically replaces a stale local process. To force a clean restart
manually, run `./start-issue-agent.ps1 -Restart` from PowerShell.

### Docker

```bash
docker build -t issue-agent .
docker run -p 8000:8000 --env-file .env -v issue-agent-data:/app/data issue-agent
```

The container always listens on `8000` internally; map it to any host port you like
(`-p HOST:8000`).

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
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-v4-pro
```

Set `GITHUB_TOKEN` for private repositories and to avoid GitHub's low anonymous rate limit. Use a
fine-grained token with read-only access to repository contents and issues. Only grant contents and
pull-request write permissions when enabling `WRITE_MODE`.

### Configuration

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | *(required)* | API key for the LLM provider |
| `OPENAI_BASE_URL` | `https://api.deepseek.com` | Base URL for the OpenAI-compatible API |
| `OPENAI_MODEL` | `deepseek-v4-pro` | Investigator model name |
| `OPENAI_THINKING` | `enabled` | DeepSeek thinking mode (`enabled` or `disabled`) |
| `OPENAI_REASONING_EFFORT` | `high` | DeepSeek reasoning effort (`high` or `max`) |
| `OPENAI_TIMEOUT` | `180` | Request timeout in seconds (DeepSeek thinking mode often needs 60–120s) |
| `OPENAI_MAX_RETRIES` | `0` | SDK-level retry attempts; the agent owns its own multi-level retry for report generation |
| `LOG_LEVEL` | `INFO` | Application log level |
| `LOG_FORMAT` | `console` | Log format (`console` or newline-delimited `json`) |
| `GITHUB_TOKEN` | *(optional)* | GitHub PAT for higher rate limits |
| `GITHUB_MAX_FILE_BYTES` | `512000` | Skip files larger than this |
| `MAX_CANDIDATE_FILES` | `12` | Max distinct source files per investigation |
| `MAX_PLANNING_PATHS` | `80` | Max paths shown to the model in initial prompt |
| `MAX_FILE_CHARS` | `16000` | Max retained characters from one file |
| `MAX_TOTAL_CONTEXT_CHARS` | `80000` | Max retained source + chat characters |
| `MAX_OUTPUT_TOKENS` | `8000` | Max tokens per model response |
| `MAX_AGENT_ITERATIONS` | `15` | Max tool-calling loop iterations |
| `MAX_INVESTIGATION_LEDGER_CHARS` | `12000` | Bounded search, history, branch, and tool findings retained for report synthesis |
| `MAX_CHAT_TOKENS` | `2000` | Max tokens per chat message |
| `INDEPENDENT_REVIEW` | `true` | Run the independent reviewer before publishing the final report |
| `REVIEW_MODEL` | *(same as investigator)* | Optional separate model for independent review |
| `REVIEW_MAX_TOKENS` | `8000` | Maximum output tokens for the reviewer decision |
| `MAX_REVIEW_CONTEXT_CHARS` | `32000` | Maximum issue, report, and source context supplied to the reviewer |
| `MAX_REPORT_RETRIES` | `3` | Report-generation retry count (incl. first try): first attempt uses the configured thinking mode, middle attempts retry with error feedback, the final attempt degrades to thinking disabled as a fallback |
| `LANGUAGE` | `zh` | Response language (`zh` or `en`) |
| `API_KEY` | *(optional)* | Require this value in the `X-API-Key` request header |
| `WRITE_MODE` | `false` | Allow validated PR proposals and confirmed GitHub writes |
| `SESSION_DB_PATH` | `data/sessions.db` | SQLite path for persistent sessions; use `:memory:` for ephemeral tests |
| `SESSION_STALE_AFTER_SECONDS` | `1800` | Mark running sessions older than this heartbeat window as interrupted |
| `MAX_PR_FILES` | `20` | Maximum number of files allowed in one PR proposal |
| `MAX_PR_TOTAL_BYTES` | `1000000` | Maximum combined UTF-8 size of proposed file contents |

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
python -m uvicorn app.main:app --port 8000 --reload
```

Open `http://127.0.0.1:8000/docs` for the Swagger UI. On Windows the bundled launcher
(`打开 Issue Agent.cmd`) auto-selects a free port from `8000 → 9123 → 9124 → 9125`; in that
case the launcher prints the actual URL in its terminal window.

### `POST /analyze`

```json
{
  "issue_url": "https://github.com/owner/repo/issues/123"
}
```

Returns an `AnalysisReport` with summary, root cause, confidence, evidence, proposed changes, patch, tests, and risks.

### `POST /stream`

Runs the same investigation as `/analyze`, but streams progress as Server-Sent Events. Pass an
`issue_url` to start a new session, or a `session_id` to re-run an existing one:

```json
{
  "issue_url": "https://github.com/owner/repo/issues/123"
}
```

The stream begins with a `session` event carrying the session id, followed by investigation progress
events and a final `report` event; `cancelled` or `error` events are emitted when the run stops early.

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

### Session history

- `GET /sessions` lists active or archived Issue sessions and supports `q` search.
- `GET /session/{session_id}` restores messages, report, durable events, phase, metrics, and version.
- `GET /session/{session_id}/report` returns just the stored `AnalysisReport` once the investigation has produced one (404 before that).
- `PATCH /session/{session_id}` renames, archives, or restores a session.
- `POST /session/{session_id}/cancel` requests cancellation of a running investigation.
- `GET /session/{session_id}/proposal` returns a safe PR proposal preview without file contents.
- `DELETE /session/{session_id}` permanently deletes a session.

### `POST /session/{session_id}/apply-fix` (write mode)

Creates the pull request for a session's stored proposal. Disabled unless `WRITE_MODE=true`.

```
POST /session/a1b2c3d4e5f6/apply-fix
{
  "confirm": true
}
```

Requests without `confirm=true` are rejected. The stored proposal is revalidated against the
repository's default branch before any write; on failure the temporary branch is rolled back.
Returns the created `pr_url` and `branch`. See the [Safety model](#safety-model) for the full
write-path guarantees.

The legacy `POST /apply-fix?session_id=...` route remains available for compatibility but is hidden
from the OpenAPI schema; new integrations should use the session-scoped endpoint above.

## Testing

```bash
ruff check .
mypy app/
pytest -v --cov=app --cov-report=term-missing
npm install
npx playwright install chromium
npm run test:e2e
```

The Playwright suite starts an isolated local server and verifies desktop and mobile layouts,
localized accessibility labels, report navigation, source links, XSS escaping, input clearing, and
network-failure recovery. CI installs Chromium and runs the same suite on every push and pull request.

## Current limitations

- GitHub code search has stricter authentication, indexing, and rate-limit behavior than repository-tree access;
  the agent retains deterministic filename selection as a fallback.
- SQLite uses optimistic versions to reject cross-worker overwrite, but high-volume distributed deployments should
  still use a dedicated transactional database and background job system.
- Cancellation is cooperative and takes effect at the next streamed model or tool event; an in-flight provider
  request may finish before cancellation is observed.

## License

MIT — see [LICENSE](LICENSE) for details.
