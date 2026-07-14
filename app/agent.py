import json
import logging
import re
from collections.abc import AsyncGenerator

from openai import AsyncOpenAI
from pydantic import ValidationError

from app.config import Settings
from app.events import (
    AgentEvent,
    done_event,
    report_event,
    start_event,
    thinking_event,
    tool_call_event,
    tool_result_event,
)
from app.github import GitHubClient, parse_issue_url, select_candidate_paths
from app.i18n import get_chat_system_prompt, get_final_output_prompt, get_system_prompt, t
from app.models import AnalysisReport, ChatResponse, IssueData
from app.sessions import Session
from app.tools import ToolExecutor, get_tool_definitions, parse_tool_call

logger = logging.getLogger(__name__)

LINE_RANGE = re.compile(r"^L(\d+)(?:-L?(\d+))?$")


class ModelResponseError(RuntimeError):
    pass


class IssueAgent:
    def __init__(
        self,
        settings: Settings,
        *,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.settings = settings
        self._client: AsyncOpenAI | None = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.close()
        self._client = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.settings.openai_api_key,
                base_url=self.settings.openai_base_url,
                timeout=self.settings.openai_timeout,
                max_retries=self.settings.openai_max_retries,
            )
            self._owns_client = True
        return self._client

    # ── streaming investigation ─────────────────────────────────────

    async def investigate_stream(
        self, issue_url: str, *, session: Session | None = None
    ) -> AsyncGenerator[AgentEvent, None]:
        """Investigate an issue, yielding AgentEvent objects for real-time streaming."""
        owner, repo, number = parse_issue_url(issue_url)
        logger.info("Investigating issue %s/%s#%d", owner, repo, number)

        async with GitHubClient(
            self.settings.github_token,
            max_file_bytes=self.settings.github_max_file_bytes,
        ) as github:
            issue = await github.get_issue(owner, repo, number)
            tree = await github.get_tree(issue)
            if session is not None:
                session.issue = issue
                session.tree = tree
            yield start_event(issue.title, len(tree))
            logger.info("Fetched issue: %r (%d comments, %d files)", issue.title, len(issue.comments), len(tree))

            executor = self._build_executor(github, issue, tree)
            messages = self._build_initial_messages(issue, tree)

            tools = get_tool_definitions(self.settings)
            client = self._get_client()
            for iteration in range(self.settings.max_agent_iterations):
                response = await client.chat.completions.create(
                    model=self.settings.openai_model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=0.1,
                    max_tokens=self.settings.max_output_tokens,
                )

                if not response.choices:
                    raise ModelResponseError("The model returned no choices")

                msg = response.choices[0].message
                content = msg.content or ""
                messages.append(_serialize_message(msg))

                if content and not msg.tool_calls:
                    yield thinking_event(content)

                if not msg.tool_calls:
                    logger.info("Agent finished exploration after %d iterations", iteration + 1)
                    break

                for tc in msg.tool_calls:
                    name, args = parse_tool_call(tc)
                    logger.info("Tool call %d: %s(%s)", iteration + 1, name, args)
                    yield tool_call_event(name, args, iteration + 1)
                    result = await executor.execute(name, args)
                    if session is not None and executor.pr_proposal is not None:
                        session.pending_pr = executor.pr_proposal
                    yield tool_result_event(name, result)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            else:
                logger.warning("Agent reached max iterations (%d)", self.settings.max_agent_iterations)

            report = await self._generate_report(messages, executor)
            yield report_event(report.model_dump())

            if session is not None:
                session.file_cache = executor.file_cache
                session.files_read = executor.files_read
                session.report = report

            yield done_event()

    # ── non‑streaming wrappers (backward‑compatible) ─────────────────

    async def investigate(self, issue_url: str, *, session: Session | None = None) -> AnalysisReport:
        if session is not None:
            async with session.lock:
                return await self._investigate_from_stream(issue_url, session=session)
        return await self._investigate_from_stream(issue_url)

    async def _investigate_from_stream(self, issue_url: str, *, session: Session | None = None) -> AnalysisReport:
        report: AnalysisReport | None = None
        async for event in self.investigate_stream(issue_url, session=session):
            if event.type == "report" and event.data:
                report = AnalysisReport(**event.data)
        if report is None:
            raise ModelResponseError("Investigation did not produce a report")
        return report

    # ── chat ─────────────────────────────────────────────────────────

    async def chat(self, session: Session, message: str) -> ChatResponse:
        async with session.lock:
            return await self._chat(session, message)

    async def _chat(self, session: Session, message: str) -> ChatResponse:
        if session.issue is None:
            raise ValueError("Session not initialized")

        tools = get_tool_definitions(self.settings)
        async with GitHubClient(
            self.settings.github_token,
            max_file_bytes=self.settings.github_max_file_bytes,
        ) as github:
            executor = self._build_executor(
                github,
                session.issue,
                session.tree,
                file_cache=session.file_cache,
                files_read=session.files_read,
            )

            investigation_context = _build_investigation_context(session)
            session.messages.append({"role": "user", "content": message})
            messages = [
                {"role": "system", "content": get_chat_system_prompt()},
                {"role": "system", "content": investigation_context},
                *session.messages,
            ]

            for _ in range(self.settings.max_agent_iterations):
                response = await self._call_llm(messages, tools=tools, max_tokens=self.settings.max_chat_tokens)
                if not response.choices:
                    raise ModelResponseError("The model returned no choices")
                msg = response.choices[0].message
                serialized = _serialize_message(msg)
                messages.append(serialized)
                session.messages.append(serialized)

                if not msg.tool_calls:
                    session.file_cache = executor.file_cache
                    session.files_read = executor.files_read
                    history_budget = self.settings.max_total_context_chars - sum(
                        len(content) for content in session.file_cache.values()
                    )
                    _trim_session_messages(session.messages, max(history_budget, 0))
                    return ChatResponse(
                        session_id=session.session_id,
                        reply=msg.content or "",
                        tools_used=executor.tools_used,
                    )

                for tc in msg.tool_calls:
                    name, args = parse_tool_call(tc)
                    result = await executor.execute(name, args)
                    if executor.pr_proposal is not None:
                        session.pending_pr = executor.pr_proposal
                    tool_msg = {"role": "tool", "tool_call_id": tc.id, "content": result}
                    messages.append(tool_msg)
                    session.messages.append(tool_msg)

        session.file_cache = executor.file_cache
        session.files_read = executor.files_read
        history_budget = self.settings.max_total_context_chars - sum(
            len(content) for content in session.file_cache.values()
        )
        _trim_session_messages(session.messages, max(history_budget, 0))
        return ChatResponse(
            session_id=session.session_id,
            reply=t("depth_limit"),
            tools_used=executor.tools_used,
        )

    # ── report generation ────────────────────────────────────────────

    async def _generate_report(self, messages: list[dict], executor: ToolExecutor) -> AnalysisReport:
        client = self._get_client()
        final_messages = [*messages, {"role": "user", "content": get_final_output_prompt()}]

        response = await client.chat.completions.create(
            model=self.settings.openai_model,
            messages=final_messages,
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=self.settings.max_output_tokens,
        )

        if not response.choices:
            raise ModelResponseError("The model returned no choices")

        content = response.choices[0].message.content
        if not content:
            raise ModelResponseError("The model returned an empty response")

        json_text = _extract_json(content)
        try:
            report = AnalysisReport.model_validate_json(json_text)
        except ValidationError as error:
            logger.warning("Report validation failed: %s", error)
            raise ModelResponseError("The model returned an invalid analysis report") from error

        line_counts = executor.line_counts
        read_paths = set(executor.files_read)
        report.evidence = [
            item
            for item in report.evidence
            if item.path in read_paths and self._has_valid_lines(item.lines, line_counts.get(item.path, 0))
        ]
        report.files_examined = executor.files_read
        report.evidence_audit.valid_references = len(report.evidence)
        report.evidence_audit.root_cause_supported = bool(report.evidence) and all(
            bool(item.reason and item.reason.strip()) for item in report.evidence
        )

        reference_count = len(report.evidence)
        confidence_rank = {"low": 0, "medium": 1, "high": 2}
        maximum_confidence = "low" if reference_count == 0 else "medium" if reference_count < 3 else "high"
        if confidence_rank[report.confidence] > confidence_rank[maximum_confidence]:
            report.confidence = maximum_confidence

        if not report.evidence_audit.root_cause_supported:
            report.confidence = "low"
            warning = t("evidence_unsupported")
            if warning not in report.risks:
                report.risks.append(warning)

        logger.info(
            "Analysis complete: confidence=%s, evidence=%d, files=%d",
            report.confidence,
            report.evidence_audit.valid_references,
            len(report.files_examined),
        )
        return report

    # ── helpers ──────────────────────────────────────────────────────

    def _build_initial_messages(self, issue: IssueData, tree: list[str]) -> list[dict]:
        candidate_paths = select_candidate_paths(tree, issue, self.settings.max_planning_paths)
        tree_preview = candidate_paths if candidate_paths else tree[: self.settings.max_planning_paths]
        issue_context = json.dumps(
            {"title": issue.title, "body": issue.body[:5000], "labels": issue.labels, "comments": issue.comments[:10]},
            ensure_ascii=False,
            indent=2,
        )
        return [
            {"role": "system", "content": get_system_prompt()},
            {
                "role": "user",
                "content": (
                    f"Investigate this GitHub issue:\n{issue_context}\n\n"
                    f"Repository file tree ({len(tree)} files total, showing {len(tree_preview)} most relevant):\n"
                    + "\n".join(tree_preview)
                ),
            },
        ]

    @staticmethod
    def _has_valid_lines(lines: str | None, line_count: int) -> bool:
        if lines is None:
            return True
        match = LINE_RANGE.fullmatch(lines)
        if not match:
            return False
        start = int(match.group(1))
        end = int(match.group(2) or start)
        return 1 <= start <= end <= line_count

    async def _call_llm(self, messages: list[dict], *, tools: list | None = None, max_tokens: int | None = None):
        client = self._get_client()
        kwargs: dict = {
            "model": self.settings.openai_model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": max_tokens or self.settings.max_output_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return await client.chat.completions.create(**kwargs)

    def _build_executor(self, github: GitHubClient, issue: IssueData, tree: list[str], **kwargs) -> ToolExecutor:
        return ToolExecutor(
            github,
            self.settings,
            issue,
            tree,
            max_files=self.settings.max_candidate_files,
            max_file_chars=self.settings.max_file_chars,
            max_total_context_chars=self.settings.max_total_context_chars,
            **kwargs,
        )


# ── module‑level helpers ────────────────────────────────────────────


def _serialize_message(message) -> dict:
    msg: dict = {"role": "assistant", "content": message.content or ""}
    if message.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in message.tool_calls
        ]
    return msg


def _trim_session_messages(messages: list[dict], max_chars: int, max_messages: int = 50) -> None:
    def message_size(message: dict) -> int:
        size = len(str(message.get("content", "")))
        for tool_call in message.get("tool_calls", []):
            function = tool_call.get("function", {})
            size += len(str(function.get("arguments", "")))
        return size

    turns: list[list[dict]] = []
    for message in messages:
        if message.get("role") == "user" or not turns:
            turns.append([])
        turns[-1].append(message)

    retained: list[list[dict]] = []
    used = 0
    message_count = 0
    for turn in reversed(turns):
        turn_size = sum(message_size(message) for message in turn)
        if used + turn_size > max_chars or message_count + len(turn) > max_messages:
            if retained:
                break
            turn = [
                message
                for message in turn
                if message.get("role") == "user"
                or (message.get("role") == "assistant" and not message.get("tool_calls"))
            ][-max_messages:]
            turn_size = sum(message_size(message) for message in turn)
        if not turn:
            break
        retained.append(turn)
        used += turn_size
        message_count += len(turn)
        if used >= max_chars:
            break

    selected = [message for turn in reversed(retained) for message in turn]
    total = sum(message_size(message) for message in selected)
    for message in selected:
        overflow = total - max_chars
        if overflow <= 0:
            break
        content = str(message.get("content", ""))
        removed = min(len(content), overflow)
        message["content"] = content[removed:]
        total -= removed
    messages[:] = selected


_JSON_BLOCK = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def _build_investigation_context(session: Session) -> str:
    if session.report is None:
        return t("no_investigation")
    report = session.report
    parts = [t("investigation_context_header"), ""]
    parts.append(f"Summary: {report.summary}")
    parts.append(f"Root Cause: {report.root_cause}")
    parts.append(f"Confidence: {report.confidence}")
    if report.evidence:
        parts.append("Evidence:")
        for ev in report.evidence:
            parts.append(f"  - {ev.path} {ev.lines or ''}: {ev.reason or ''}")
    if report.proposed_changes:
        parts.append("Proposed Changes:")
        for i, change in enumerate(report.proposed_changes, 1):
            parts.append(f"  {i}. {change}")
    if report.files_examined:
        parts.append(f"Files Examined: {', '.join(report.files_examined)}")
    parts.append("")
    parts.append(t("investigation_context_footer"))
    return "\n".join(parts)


def _extract_json(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    block_match = _JSON_BLOCK.search(content)
    if block_match:
        return block_match.group(1).strip()
    brace_start = stripped.find("{")
    brace_end = stripped.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        return stripped[brace_start : brace_end + 1]
    return stripped
