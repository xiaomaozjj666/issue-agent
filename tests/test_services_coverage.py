"""services.py 低覆盖分支的补充测试：格式化、会话终态标记、apply_fix 守卫。

覆盖 format_report_text 的 evidence/proposed_changes/patch/tests/risks/review_audit
条件段，以及 mark_session_failed / finish_cancelled_session / mark_stream_interrupted
的成功与冲突路径、apply_fix 的 404/403/409 守卫。
"""

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.events import AgentEvent
from app.models import AnalysisReport, CodeReference, ReviewAudit
from app.services import (
    apply_fix,
    finish_cancelled_session,
    format_report_text,
    mark_session_failed,
    mark_stream_interrupted,
)
from app.sessions import Session, SessionConflictError, SessionManager

# ── format_report_text 全条件段 ────────────────────────────


def _full_report() -> AnalysisReport:
    return AnalysisReport(
        summary="Summary text",
        root_cause="Root cause text",
        confidence="high",
        evidence=[
            CodeReference(path="src/a.py", lines="L10-L12", reason="why it fails"),
            CodeReference(path="src/b.py", lines=None, reason=None),
        ],
        proposed_changes=["change one", "change two"],
        patch="--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new",
        tests=["test one", "test two"],
        risks=["risk one", "risk two"],
        review_audit=ReviewAudit(status="approved", summary="review summary", findings=["finding one", "finding two"]),
    )


def test_format_report_text_renders_all_sections() -> None:
    text = format_report_text(_full_report())
    assert "Summary: Summary text" in text
    assert "Root Cause: Root cause text" in text
    assert "Confidence: high" in text
    assert "Code Evidence:" in text
    assert "src/a.py L10-L12: why it fails" in text
    assert "src/b.py : " in text  # lines/reason 为 None 时渲染空
    assert "Proposed Changes:" in text
    assert "1. change one" in text and "2. change two" in text
    assert "Patch:" in text and "old" in text and "new" in text
    assert "Suggested Tests:" in text
    assert "1. test one" in text and "2. test two" in text
    assert "Risks:" in text
    assert "- risk one" in text and "- risk two" in text
    assert "Independent Review: approved" in text
    assert "review summary" in text
    assert "- finding one" in text and "- finding two" in text


def test_format_report_text_minimal_report() -> None:
    """空列表字段的报告：不输出对应段落。"""
    report = AnalysisReport(
        summary="s",
        root_cause="r",
        confidence="low",
        evidence=[],
        proposed_changes=[],
        patch=None,
        tests=[],
        risks=[],
    )
    text = format_report_text(report)
    assert "Summary: s" in text
    assert "Root Cause: r" in text
    assert "Code Evidence:" not in text
    assert "Proposed Changes:" not in text
    assert "Patch:" not in text
    assert "Suggested Tests:" not in text
    assert "Risks:" not in text
    assert "Independent Review:" not in text


def test_format_report_text_review_unavailable_without_summary() -> None:
    """review_audit 状态非 not_run 但 summary 为空、findings 为空。"""
    report = _full_report()
    report.review_audit = ReviewAudit(status="unavailable", summary="", findings=[])
    text = format_report_text(report)
    assert "Independent Review: unavailable" in text
    assert "review summary" not in text


# ── 会话终态标记 ───────────────────────────────────────────


async def test_mark_session_failed_sets_state_and_persists(tmp_path) -> None:
    manager = SessionManager(db_path=str(tmp_path / "fail.db"))
    session = await manager.create("https://github.com/a/b/issues/1")
    await mark_session_failed(manager, session, ValueError("boom"))
    assert session.status == "failed"
    assert session.phase == "failed"
    assert session.error_message == "boom"
    restored = await manager.get(session.session_id)
    assert restored is not None
    assert restored.status == "failed"
    await manager.close()


async def test_mark_session_failed_none_session_is_noop() -> None:
    await mark_session_failed(SessionManager(), None, ValueError("boom"))


async def test_mark_session_failed_swallows_conflict(tmp_path, monkeypatch) -> None:
    manager = SessionManager(db_path=str(tmp_path / "fail2.db"))
    session = await manager.create("https://github.com/a/b/issues/1")
    original_save = manager.save

    async def conflict_save(_session: Session) -> None:
        raise SessionConflictError("conflict")

    monkeypatch.setattr(manager, "save", conflict_save)
    # 不抛出：冲突时记录警告并保留原始异常语义
    await mark_session_failed(manager, session, ValueError("boom"))
    await manager.close()
    assert original_save is not None


async def test_finish_cancelled_session_sets_state_and_persists_event(tmp_path) -> None:
    manager = SessionManager(db_path=str(tmp_path / "cancel.db"))
    session = await manager.create("https://github.com/a/b/issues/1")
    event = AgentEvent(type="cancelled", message="cancelled")
    await finish_cancelled_session(manager, session.session_id, event, started_at=1_000.0)
    restored = await manager.get(session.session_id)
    assert restored is not None
    assert restored.status == "cancelled"
    assert restored.phase == "cancelled"
    assert restored.metrics["duration_ms"] >= 0  # monotonic 差值 >= 0 即可
    events = await manager.list_events(session.session_id)
    assert events[-1]["type"] == "cancelled"
    await manager.close()


async def test_finish_cancelled_session_unknown_session_is_noop(tmp_path) -> None:
    manager = SessionManager(db_path=str(tmp_path / "cancel2.db"))
    await finish_cancelled_session(manager, "missing", AgentEvent(type="cancelled"), started_at=0.0)
    await manager.close()


async def test_finish_cancelled_session_swallows_conflict(tmp_path, monkeypatch) -> None:
    manager = SessionManager(db_path=str(tmp_path / "cancel3.db"))
    session = await manager.create("https://github.com/a/b/issues/1")

    async def conflict_save(_session: Session) -> None:
        raise SessionConflictError("conflict")

    monkeypatch.setattr(manager, "save", conflict_save)
    await finish_cancelled_session(manager, session.session_id, AgentEvent(type="cancelled"), started_at=0.0)
    await manager.close()


async def test_mark_stream_interrupted_marks_running_session(tmp_path) -> None:
    manager = SessionManager(db_path=str(tmp_path / "interrupt.db"))
    session = await manager.create("https://github.com/a/b/issues/1")
    session.status = "running"
    await manager.save(session)
    await mark_stream_interrupted(manager, session.session_id, started_at=0.0)
    restored = await manager.get(session.session_id)
    assert restored is not None
    assert restored.status == "failed"
    assert restored.phase == "interrupted"
    assert "Connection closed" in (restored.error_message or "")
    events = await manager.list_events(session.session_id)
    assert events[-1]["type"] == "interrupted"
    await manager.close()


async def test_mark_stream_interrupted_skips_non_running(tmp_path) -> None:
    manager = SessionManager(db_path=str(tmp_path / "interrupt2.db"))
    session = await manager.create("https://github.com/a/b/issues/1")
    session.status = "completed"
    await manager.save(session)
    await mark_stream_interrupted(manager, session.session_id, started_at=0.0)
    restored = await manager.get(session.session_id)
    assert restored is not None
    assert restored.status == "completed"  # 不覆盖已完成状态
    await manager.close()


async def test_mark_stream_interrupted_swallows_conflict(tmp_path, monkeypatch) -> None:
    manager = SessionManager(db_path=str(tmp_path / "interrupt3.db"))
    session = await manager.create("https://github.com/a/b/issues/1")
    session.status = "running"
    await manager.save(session)

    async def conflict_save(_session: Session) -> None:
        raise SessionConflictError("conflict")

    monkeypatch.setattr(manager, "save", conflict_save)
    await mark_stream_interrupted(manager, session.session_id, started_at=0.0)
    await manager.close()


# ── apply_fix 守卫路径 ────────────────────────────────────

_SETTINGS = Settings(openai_api_key="test-key", write_mode=True)
_SETTINGS_RO = Settings(openai_api_key="test-key", write_mode=False)


async def _session_with_proposal(tmp_path) -> tuple[SessionManager, str]:
    from app.models import IssueData

    manager = SessionManager(db_path=str(tmp_path / "apply_guard.db"))
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="t",
        body="",
        labels=[],
        comments=[],
        default_branch="main",
    )
    session.status = "completed"
    await manager.save(session)
    await manager.save_pr_proposal(
        session.session_id,
        {
            "branch": "fix/issue-1",
            "title": "Fix",
            "body": "Fix it",
            "changes": [{"path": "src/a.py", "content": "x = 1", "message": "fix"}],
        },
    )
    return manager, session.session_id


async def test_apply_fix_requires_write_mode(tmp_path) -> None:
    manager, session_id = await _session_with_proposal(tmp_path)
    with pytest.raises(HTTPException) as exc_info:
        await apply_fix(session_id, SimpleRequest(True), settings=_SETTINGS_RO, session_mgr=manager)
    assert exc_info.value.status_code == 403
    await manager.close()


async def test_apply_fix_unknown_session_returns_404(tmp_path) -> None:
    manager, _ = await _session_with_proposal(tmp_path)
    with pytest.raises(HTTPException) as exc_info:
        await apply_fix("missing", SimpleRequest(True), settings=_SETTINGS, session_mgr=manager)
    assert exc_info.value.status_code == 404
    await manager.close()


async def test_apply_fix_without_proposal_returns_404(tmp_path) -> None:
    manager = SessionManager(db_path=str(tmp_path / "apply_guard2.db"))
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.status = "completed"
    await manager.save(session)
    with pytest.raises(HTTPException) as exc_info:
        await apply_fix(session.session_id, SimpleRequest(True), settings=_SETTINGS, session_mgr=manager)
    assert exc_info.value.status_code == 404
    await manager.close()


async def test_apply_fix_without_confirm_returns_400(tmp_path) -> None:
    manager, session_id = await _session_with_proposal(tmp_path)
    with pytest.raises(HTTPException) as exc_info:
        await apply_fix(session_id, SimpleRequest(False), settings=_SETTINGS, session_mgr=manager)
    assert exc_info.value.status_code == 400
    await manager.close()


async def test_apply_fix_running_session_returns_409(tmp_path) -> None:
    manager, session_id = await _session_with_proposal(tmp_path)
    session = await manager.get(session_id)
    assert session is not None
    session.status = "running"
    await manager.save(session)
    with pytest.raises(HTTPException) as exc_info:
        await apply_fix(session_id, SimpleRequest(True), settings=_SETTINGS, session_mgr=manager)
    assert exc_info.value.status_code == 409
    await manager.close()


async def test_apply_fix_invalid_stored_proposal_returns_409(tmp_path, monkeypatch) -> None:

    manager, session_id = await _session_with_proposal(tmp_path)

    def reject(*args, **kwargs):
        raise ValueError("invalid")

    monkeypatch.setattr("app.services.validate_pr_proposal", reject)
    with pytest.raises(HTTPException) as exc_info:
        await apply_fix(session_id, SimpleRequest(True), settings=_SETTINGS, session_mgr=manager)
    assert exc_info.value.status_code == 409
    assert "invalid" in exc_info.value.detail
    await manager.close()


async def test_apply_fix_incomplete_session_returns_409(tmp_path) -> None:
    manager = SessionManager(db_path=str(tmp_path / "apply_guard3.db"))
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.status = "completed"
    await manager.save(session)
    await manager.save_pr_proposal(
        session.session_id,
        {
            "branch": "fix/issue-1",
            "title": "Fix",
            "body": "Fix it",
            "changes": [{"path": "src/a.py", "content": "x = 1", "message": "fix"}],
        },
    )
    # session.issue 为 None → 409
    with pytest.raises(HTTPException) as exc_info:
        await apply_fix(session.session_id, SimpleRequest(True), settings=_SETTINGS, session_mgr=manager)
    assert exc_info.value.status_code == 409
    await manager.close()


class SimpleRequest:
    def __init__(self, confirm: bool) -> None:
        self.confirm = confirm
