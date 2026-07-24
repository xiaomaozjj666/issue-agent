"""Independent reviewer agent for evidence-grounded investigation reports.

重试策略通过 retry.build_attempt_plan 构造 options（thinking 降级逻辑集中管理），
messages 由 reviewer 自行构造（需额外附加 assistant 消息承认上次输出 + user 反馈）。
"""

import json
import logging

from openai import AsyncOpenAI
from pydantic import ValidationError

from app.circuit_breaker import CircuitBreaker
from app.config import Settings
from app.errors import ReviewResponseError
from app.evidence import EvidenceValidator
from app.i18n import (
    get_review_output_prompt,
    get_review_retry_prompt,
    get_review_system_prompt,
)
from app.json_utils import extract_json
from app.models import AnalysisReport, IssueData, ReviewAudit, ReviewOutcome
from app.retry import build_attempt_plan

logger = logging.getLogger(__name__)


class ReviewerAgent:
    """Review an investigator report using only independently supplied source evidence."""

    def __init__(
        self, settings: Settings, client: AsyncOpenAI, *, circuit_breaker: CircuitBreaker | None = None
    ) -> None:
        self.settings = settings
        self._client = client
        self._circuit_breaker = circuit_breaker

    async def review(
        self,
        *,
        issue: IssueData,
        report: AnalysisReport,
        file_cache: dict[str, str],
        files_read: list[str],
        line_counts: dict[str, int],
    ) -> ReviewOutcome:
        original_payload = report.model_dump(exclude={"review_audit", "files_examined", "evidence_audit"})
        context = _build_review_context(
            issue,
            report,
            file_cache,
            files_read,
            self.settings.max_review_context_chars,
        )
        base_messages = [
            {"role": "system", "content": get_review_system_prompt(self.settings.language)},
            {"role": "user", "content": f"{context}\n\n{get_review_output_prompt()}"},
        ]

        # temperature 全程统一为 0，避免保底重试反而更不确定。
        review_model = self.settings.review_model or self.settings.openai_model
        outcome: ReviewOutcome | None = None
        last_raw_content = ""
        last_failure_reason = ""
        total_attempts = max(self.settings.max_report_retries, 1)

        def _review_feedback_builder(raw: str, reason: str) -> str:
            """构造 reviewer 特有的重试反馈：assistant 承认 + user 指正。

            build_attempt_plan 只追加单条 user 消息，reviewer 需要先放 assistant
            承认上次输出。这里把两条消息合并为一条 user 消息（带上下文标记），
            避免 build_attempt_plan 的 messages 构造逻辑被污染。
            """
            return get_review_retry_prompt(raw, reason)

        for attempt in range(total_attempts):
            is_last = attempt == total_attempts - 1
            # 用 build_attempt_plan 统一构造 options（thinking 降级逻辑集中管理）
            plan = build_attempt_plan(
                self.settings,
                base_messages=base_messages,
                last_raw_content=last_raw_content,
                last_failure_reason=last_failure_reason,
                attempt=attempt,
                total_attempts=total_attempts,
                temperature=0,
                model=review_model,
                retry_feedback_builder=_review_feedback_builder if attempt > 0 else None,
            )

            # reviewer 特有：重试时在 base_messages + user 反馈之间插入 assistant 消息
            if attempt == 0:
                attempt_messages = plan.messages
            else:
                attempt_messages = [
                    *base_messages,
                    {"role": "assistant", "content": last_raw_content or "(empty response)"},
                    {"role": "user", "content": get_review_retry_prompt(last_raw_content, last_failure_reason)},
                ]

            if self._circuit_breaker is not None:
                response = await self._circuit_breaker.call(
                    self._client.chat.completions.create,
                    **plan.options,
                    messages=attempt_messages,
                    response_format={"type": "json_object"},
                    max_tokens=self.settings.review_max_tokens,
                )
            else:
                response = await self._client.chat.completions.create(  # type: ignore[call-overload]
                    **plan.options,
                    messages=attempt_messages,
                    response_format={"type": "json_object"},
                    max_tokens=self.settings.review_max_tokens,
                )
            if not response.choices:
                last_raw_content = ""
                last_failure_reason = "The reviewer returned no choices"
                logger.warning("Reviewer returned no choices on attempt %d", attempt + 1)
                if not is_last:
                    continue
                raise ReviewResponseError("The reviewer returned no choices")

            content = response.choices[0].message.content
            if not content:
                last_raw_content = ""
                last_failure_reason = "The reviewer returned an empty response"
                logger.warning("Reviewer returned empty content on attempt %d", attempt + 1)
                if not is_last:
                    continue
                raise ReviewResponseError("The reviewer returned an empty response")

            try:
                outcome = ReviewOutcome.model_validate_json(extract_json(content))
            except ValidationError as error:
                last_raw_content = content
                last_failure_reason = str(error)
                logger.warning("Reviewer validation failed on attempt %d: %s", attempt + 1, error)
                if not is_last:
                    continue
                raise ReviewResponseError("The reviewer returned an invalid decision") from error
            break

        if outcome is None:
            raise ReviewResponseError(f"The reviewer failed after {total_attempts} attempts: {last_failure_reason}")

        outcome.report = EvidenceValidator().validate(
            outcome.report,
            files_read=files_read,
            line_counts=line_counts,
        )
        reviewed_payload = outcome.report.model_dump(exclude={"review_audit", "files_examined", "evidence_audit"})
        if outcome.verdict == "approved" and reviewed_payload != original_payload:
            outcome.verdict = "revised"
            finding = (
                "审查结果修改了初版报告，因此状态已自动调整为 revised。"
                if self.settings.language == "zh"
                else "The reviewer changed the investigator report, so the verdict was normalized to revised."
            )
            if finding not in outcome.findings:
                outcome.findings.append(finding)
        outcome.report.review_audit = ReviewAudit(
            status=outcome.verdict,
            summary=outcome.summary,
            findings=outcome.findings[:10],
            reviewer_model=review_model,
        )
        logger.info("Independent review complete: verdict=%s findings=%d", outcome.verdict, len(outcome.findings))
        return outcome


def _build_review_context(
    issue: IssueData,
    report: AnalysisReport,
    file_cache: dict[str, str],
    files_read: list[str],
    max_chars: int,
) -> str:
    issue_data = {
        "repository": f"{issue.owner}/{issue.repo}",
        "number": issue.number,
        "title": issue.title,
        "body": issue.body[:4_000],
        "labels": issue.labels,
        "comments": [comment[:1_000] for comment in issue.comments[:5]],
    }
    report_data = report.model_dump(exclude={"review_audit"})
    if report_data.get("patch"):
        report_data["patch"] = str(report_data["patch"])[:4_000]
    prefix = (
        "ISSUE\n"
        + json.dumps(issue_data, ensure_ascii=False, indent=2)
        + "\n\nINVESTIGATOR REPORT\n"
        + json.dumps(report_data, ensure_ascii=False, indent=2)
        + "\n\nSOURCE EXCERPTS\n"
    )
    remaining = max(max_chars - len(prefix), 0)
    excerpts: list[str] = []
    evidence_paths = [item.path for item in report.evidence]
    ordered_paths = list(dict.fromkeys([*evidence_paths, *files_read]))
    for path in ordered_paths:
        content = file_cache.get(path)
        if not content or remaining <= 0:
            continue
        numbered = "\n".join(f"L{number}: {line}" for number, line in enumerate(content.splitlines(), 1))
        block = f"\n--- {path} ---\n{numbered}\n"
        limited = block[:remaining]
        excerpts.append(limited)
        remaining -= len(limited)
    return (prefix + "".join(excerpts))[:max_chars]
