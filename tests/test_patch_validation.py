import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models import (
    AnalysisReport,
    CommandExecution,
    PatchValidationEvidence,
    PatchValidationRequest,
)
from app.patch_validation import validate_patch

FIX_PATCH = """--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(left, right):
-    return left - right
+    return left + right
"""

STILL_FAILING_PATCH = FIX_PATCH.replace("left + right", "left * right")


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _command(*args: str) -> str:
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def _python(script: str) -> str:
    return _command(sys.executable, "-c", script)


@pytest.fixture
def local_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "Issue Agent Tests")
    (repository / "calculator.py").write_text(
        "def add(left, right):\n    return left - right\n",
        encoding="utf-8",
    )
    (repository / "test_bug.py").write_text(
        "import unittest\nfrom calculator import add\n\n"
        "class BugTest(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    (repository / "test_regression.py").write_text(
        "import unittest\nfrom calculator import add\n\n"
        "class RegressionTest(unittest.TestCase):\n"
        "    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "initial")
    return repository


def _request(repository: Path, **overrides) -> PatchValidationRequest:
    values = {
        "repository_path": str(repository),
        "patch": FIX_PATCH,
        "test_command": _command(sys.executable, "-m", "unittest", "-v", "test_bug"),
        "regression_commands": [_command(sys.executable, "-m", "unittest", "-v", "test_regression")],
        "timeout_seconds": 10,
        "confirm": True,
    }
    values.update(overrides)
    return PatchValidationRequest(**values)


def _assert_worktrees_removed(repository: Path) -> None:
    worktrees = _git(repository, "worktree", "list", "--porcelain")
    assert "issue-agent-validation-" not in worktrees
    assert worktrees.count("worktree ") == 1


async def test_red_green_and_regression_evidence_is_real_and_workspace_is_untouched(
    local_repository: Path,
) -> None:
    original = (local_repository / "calculator.py").read_bytes()
    (local_repository / "maintainer-notes.txt").write_text("dirty user data", encoding="utf-8")

    evidence = await validate_patch(_request(local_repository))

    assert evidence.status == "passed"
    assert evidence.before_patch.status == "failed"
    assert evidence.before_patch.exit_code != 0
    assert evidence.after_patch.status == "passed"
    assert evidence.after_patch.exit_code == 0
    assert [result.status for result in evidence.regressions] == ["passed"]
    assert evidence.base_sha == _git(local_repository, "rev-parse", "HEAD")
    assert len(evidence.patch_sha256) == 64
    assert evidence.workspace_cleaned is True
    assert evidence.error is None
    assert (local_repository / "calculator.py").read_bytes() == original
    assert (local_repository / "maintainer-notes.txt").read_text(encoding="utf-8") == "dirty user data"
    _assert_worktrees_removed(local_repository)


async def test_patch_that_still_fails_is_not_marked_as_passed(local_repository: Path) -> None:
    evidence = await validate_patch(_request(local_repository, patch=STILL_FAILING_PATCH))

    assert evidence.status == "failed"
    assert evidence.before_patch.status == "failed"
    assert evidence.after_patch.status == "failed"
    assert evidence.after_patch.exit_code != 0
    assert evidence.regressions[0].status == "passed"
    assert evidence.workspace_cleaned is True
    _assert_worktrees_removed(local_repository)


async def test_regression_failure_prevents_overall_pass(local_repository: Path) -> None:
    evidence = await validate_patch(
        _request(
            local_repository,
            regression_commands=[_python("import sys; print('regression failed'); sys.exit(9)")],
        )
    )

    assert evidence.status == "failed"
    assert evidence.before_patch.status == "failed"
    assert evidence.after_patch.status == "passed"
    assert evidence.regressions[0].status == "failed"
    assert evidence.regressions[0].exit_code == 9
    assert evidence.workspace_cleaned is True


async def test_test_passing_before_patch_is_not_reproduced(local_repository: Path) -> None:
    evidence = await validate_patch(
        _request(
            local_repository,
            test_command=_python("from calculator import add; assert add(4, 0) == 4"),
            regression_commands=[],
        )
    )

    assert evidence.status == "not_reproduced"
    assert evidence.before_patch.status == "passed"
    assert evidence.after_patch.status == "passed"
    assert evidence.workspace_cleaned is True


async def test_timeout_is_recorded_and_worktrees_are_cleaned(local_repository: Path) -> None:
    evidence = await validate_patch(
        _request(
            local_repository,
            test_command=_python("import time; time.sleep(30)"),
            regression_commands=[],
            timeout_seconds=0.15,
        )
    )

    assert evidence.status == "timeout"
    assert evidence.before_patch.status == "timeout"
    assert evidence.after_patch.status == "timeout"
    assert evidence.before_patch.exit_code is None
    assert evidence.after_patch.exit_code is None
    assert evidence.workspace_cleaned is True
    _assert_worktrees_removed(local_repository)


@pytest.mark.parametrize("field", ["test_command", "regression_commands"])
def test_blank_commands_are_rejected_by_request_schema(local_repository: Path, field: str) -> None:
    value = "  " if field == "test_command" else ["ok", " \t"]
    with pytest.raises(ValidationError, match="cannot be blank|cannot contain blank"):
        _request(local_repository, **{field: value})


async def test_direct_blank_command_is_rejected_before_repository_access(
    local_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(local_repository).model_copy(update={"test_command": " "})

    async def unexpected_resolve(*args):
        raise AssertionError("blank command reached repository setup")

    monkeypatch.setattr("app.patch_validation._resolve_repository", unexpected_resolve)
    with pytest.raises(ValueError, match="cannot be blank"):
        await validate_patch(request)


async def test_confirmation_is_mandatory_before_any_execution(local_repository: Path) -> None:
    request = _request(local_repository, confirm=False, repository_path="does-not-exist")

    with pytest.raises(PermissionError, match="confirm=true"):
        await validate_patch(request)


async def test_missing_repository_fails_before_command_execution(tmp_path: Path) -> None:
    from app.patch_validation import PatchValidationError

    request = _request(tmp_path, repository_path=str(tmp_path / "missing"))
    with pytest.raises(PatchValidationError, match="not an existing directory"):
        await validate_patch(request)


async def test_output_is_bounded_and_truncation_is_explicit(local_repository: Path) -> None:
    command = _python(
        "from calculator import add; import sys; "
        "ok = add(2, 2) == 4; "
        "print('x' * 20000 if not ok else 'ok'); "
        "sys.exit(0 if ok else 1)"
    )
    evidence = await validate_patch(
        _request(
            local_repository,
            test_command=command,
            regression_commands=[],
            max_output_bytes=100,
        )
    )

    assert evidence.status == "passed"
    assert len(evidence.before_patch.stdout.encode("utf-8")) <= 100
    assert evidence.before_patch.stdout_truncated is True
    assert evidence.after_patch.stdout.splitlines() == ["ok"]
    assert evidence.after_patch.stdout_truncated is False


async def test_fixed_base_sha_and_invalid_patch_cleanup(local_repository: Path) -> None:
    initial_sha = _git(local_repository, "rev-parse", "HEAD")
    (local_repository / "later.txt").write_text("later commit\n", encoding="utf-8")
    _git(local_repository, "add", "later.txt")
    _git(local_repository, "commit", "-qm", "later")

    evidence = await validate_patch(_request(local_repository, base_sha=initial_sha))
    invalid = await validate_patch(_request(local_repository, base_sha=initial_sha, patch="not a patch"))

    assert evidence.status == "passed"
    assert evidence.base_sha == initial_sha
    assert invalid.status == "error"
    assert invalid.after_patch.status == "not_run"
    assert invalid.regressions[0].status == "not_run"
    assert invalid.error is not None
    assert invalid.workspace_cleaned is True
    _assert_worktrees_removed(local_repository)


def _evidence(repository: Path) -> PatchValidationEvidence:
    before = CommandExecution(
        command="trusted-test",
        phase="before_patch",
        status="failed",
        exit_code=1,
        timeout_seconds=1,
    )
    after = CommandExecution(
        command="trusted-test",
        phase="after_patch",
        status="passed",
        exit_code=0,
        timeout_seconds=1,
    )
    return PatchValidationEvidence(
        status="passed",
        summary="red-green verified",
        repository_path=str(repository),
        base_sha="a" * 40,
        patch_sha256="b" * 64,
        before_patch=before,
        after_patch=after,
        workspace_cleaned=True,
    )


def _report() -> AnalysisReport:
    return AnalysisReport(
        summary="summary",
        root_cause="root cause",
        confidence="high",
        evidence=[],
        proposed_changes=[],
        tests=[],
        risks=[],
    )


async def test_session_api_attaches_and_persists_validation_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    import app.routes.sessions as session_routes
    from app.config import Settings
    from app.deps import get_session_manager
    from app.main import app
    from app.sessions import SessionManager

    manager = SessionManager()
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.report = _report()
    session.status = "completed"
    await manager.save(session)
    expected = _evidence(tmp_path)

    async def fake_validate(request: PatchValidationRequest) -> PatchValidationEvidence:
        assert request.confirm is True
        return expected

    monkeypatch.setattr("app.services.validate_patch", fake_validate)
    monkeypatch.setattr(
        session_routes,
        "get_settings",
        lambda: Settings(openai_api_key="test-key", patch_validation_enabled=True, api_key="secret"),
    )
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/session/{session.session_id}/validate-patch",
                json=_request(tmp_path).model_dump(),
            )
            persisted = await client.get(f"/session/{session.session_id}/report")
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert response.status_code == 200
    assert response.json()["patch_validation"]["status"] == "passed"
    assert response.json()["patch_validation"]["before_patch"]["status"] == "failed"
    assert persisted.json()["patch_validation"]["patch_sha256"] == "b" * 64
    exported = json.dumps(response.json())
    assert "workspace_cleaned" in exported


async def test_session_api_is_disabled_and_requires_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    import app.routes.sessions as session_routes
    from app.config import Settings
    from app.deps import get_session_manager
    from app.main import app
    from app.sessions import SessionManager

    manager = SessionManager()
    session = await manager.create("https://github.com/acme/widget/issues/1")
    app.dependency_overrides[get_session_manager] = lambda: manager
    enabled = Settings(openai_api_key="test-key", patch_validation_enabled=True, api_key="secret")
    monkeypatch.setattr(session_routes, "get_settings", lambda: enabled)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            unconfirmed = await client.post(
                f"/session/{session.session_id}/validate-patch",
                json=_request(tmp_path, confirm=False).model_dump(),
            )
            monkeypatch.setattr(
                session_routes,
                "get_settings",
                lambda: Settings(openai_api_key="test-key", patch_validation_enabled=False),
            )
            disabled = await client.post(
                f"/session/{session.session_id}/validate-patch",
                json=_request(tmp_path).model_dump(),
            )
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert unconfirmed.status_code == 400
    assert disabled.status_code == 403


async def test_patch_validation_without_api_key_refuses_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main as main_module
    from app.config import Settings

    settings = Settings(
        openai_api_key="test-key",
        patch_validation_enabled=True,
        api_key=None,
        session_db_path=":memory:",
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    with pytest.raises(RuntimeError, match="PATCH_VALIDATION_ENABLED"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_cli_emits_structured_evidence_and_honors_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rich.console import Console

    import app.cli as cli

    output = Console(record=True, width=200)
    monkeypatch.setattr(cli, "console", output)
    patch_file = tmp_path / "fix.patch"
    patch_file.write_text(FIX_PATCH, encoding="utf-8")

    async def fake_validate(request: PatchValidationRequest) -> PatchValidationEvidence:
        assert request.confirm is True
        assert request.patch == FIX_PATCH
        return _evidence(tmp_path)

    monkeypatch.setattr(cli, "validate_patch", fake_validate)
    result = await cli.cmd_validate_patch(
        str(tmp_path),
        str(patch_file),
        "trusted-test",
        regression_commands=[],
        base_sha=None,
        timeout_seconds=1,
        max_output_bytes=1000,
        confirm=True,
    )

    assert result == 0
    assert '"status": "passed"' in output.export_text()


async def test_cli_without_confirmation_does_not_resolve_repository(tmp_path: Path) -> None:
    import app.cli as cli

    patch_file = tmp_path / "fix.patch"
    patch_file.write_text(FIX_PATCH, encoding="utf-8")
    result = await cli.cmd_validate_patch(
        "repository-does-not-exist",
        str(patch_file),
        "trusted-test",
        regression_commands=[],
        base_sha=None,
        timeout_seconds=1,
        max_output_bytes=1000,
        confirm=False,
    )
    assert result == 2


def test_main_dispatches_validate_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.cli as cli

    called = {}

    async def fake_validate(*args, **kwargs):
        called.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "cmd_validate_patch", fake_validate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "issue-agent",
            "validate-patch",
            ".",
            "--patch-file",
            "fix.patch",
            "--test-command",
            "pytest",
            "--regression-command",
            "ruff check",
            "--confirm",
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert called["regression_commands"] == ["ruff check"]
    assert called["confirm"] is True


def test_text_report_renders_only_observed_validation_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rich.console import Console

    import app.cli as cli
    from app.services import format_report_text

    report = _report()
    report.patch_validation = _evidence(tmp_path)
    rendered = format_report_text(report)
    output = Console(record=True, width=200)
    monkeypatch.setattr(cli, "console", output)
    cli.print_report(report)

    assert "Patch Validation: passed" in rendered
    assert "before_patch: failed (exit=1)" in rendered
    assert "after_patch: passed (exit=0)" in rendered
    assert "Workspace cleaned: True" in rendered
    assert "Patch Validation: passed" in output.export_text()


def test_patch_validation_request_bounds() -> None:
    with pytest.raises(ValidationError):
        PatchValidationRequest(
            repository_path=".",
            patch="patch",
            test_command="test",
            timeout_seconds=0,
        )
    with pytest.raises(ValidationError):
        PatchValidationRequest(
            repository_path=".",
            patch="patch",
            test_command="test",
            base_sha="not-a-sha; rm -rf",
        )
