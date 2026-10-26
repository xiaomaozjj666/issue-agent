"""Opt-in local red-green patch validation using isolated Git worktrees.

This module is deliberately not registered as an LLM tool. Commands come only
from an explicit maintainer request and run against temporary detached
worktrees, never against the maintainer's working directory.
"""

import asyncio
import hashlib
import os
import signal
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path
from time import monotonic

from app.models import CommandExecution, PatchValidationEvidence, PatchValidationRequest

_GIT_TIMEOUT_SECONDS = 60.0


class PatchValidationError(RuntimeError):
    """A repository, patch, or worktree setup error."""


def _pending_command(
    command: str,
    phase: str,
    timeout_seconds: float,
) -> CommandExecution:
    return CommandExecution(
        command=command,
        phase=phase,  # type: ignore[arg-type]
        timeout_seconds=timeout_seconds,
    )


async def _read_limited(stream: asyncio.StreamReader | None, limit: int) -> tuple[str, bool]:
    """Drain a pipe without unbounded buffering, retaining only its prefix."""
    if stream is None:
        return "", False
    retained = bytearray()
    truncated = False
    while chunk := await stream.read(8192):
        remaining = limit - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            truncated = True
    return retained.decode("utf-8", errors="replace"), truncated


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate the shell and its children before awaiting pipe closure."""
    if process.returncode is not None:
        return
    if os.name == "nt":
        await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()


async def _run_command(
    command: str,
    *,
    cwd: Path,
    phase: str,
    timeout_seconds: float,
    max_output_bytes: int,
) -> CommandExecution:
    started = monotonic()
    # CREATE_NEW_PROCESS_GROUP 仅存在于 Windows 运行时与 typeshed；getattr 让
    # Linux CI 的 mypy 与 Linux 运行时都安全回落到 0。
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name != "nt",
        creationflags=creationflags,
    )
    stdout_task = asyncio.create_task(_read_limited(process.stdout, max_output_bytes))
    stderr_task = asyncio.create_task(_read_limited(process.stderr, max_output_bytes))
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        await _terminate_process(process)
    finally:
        stdout, stdout_truncated = await stdout_task
        stderr, stderr_truncated = await stderr_task

    return CommandExecution(
        command=command,
        phase=phase,  # type: ignore[arg-type]
        status="timeout" if timed_out else ("passed" if process.returncode == 0 else "failed"),
        exit_code=None if timed_out else process.returncode,
        duration_ms=round((monotonic() - started) * 1000),
        timeout_seconds=timeout_seconds,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


async def _git(repository: Path, *args: str, input_text: str | None = None) -> str:
    """Run a local Git operation with no shell and no network-fetch behavior."""

    def invoke() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *args],
            cwd=repository,
            input=input_text.encode("utf-8") if input_text is not None else None,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )

    try:
        result = await asyncio.to_thread(invoke)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PatchValidationError(f"Git command failed: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[:2000]
        raise PatchValidationError(f"git {' '.join(args[:2])} failed: {detail or 'unknown error'}")
    return result.stdout.decode("utf-8", errors="replace").strip()


async def _resolve_repository(repository_path: str, base_sha: str | None) -> tuple[Path, str]:
    repository = Path(repository_path).expanduser().resolve()
    if not repository.is_dir():
        raise PatchValidationError("repository_path is not an existing directory")
    top_level = await _git(repository, "rev-parse", "--show-toplevel")
    repository = Path(top_level).resolve()
    revision = base_sha or "HEAD"
    resolved_sha = await _git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if len(resolved_sha) != 40:
        raise PatchValidationError("Could not resolve a full base commit SHA")
    return repository, resolved_sha


async def _remove_worktree(repository: Path, path: Path) -> bool:
    try:
        await _git(repository, "worktree", "remove", "--force", str(path))
        return True
    except PatchValidationError:
        return False


def _overall_status(
    before: CommandExecution,
    after: CommandExecution,
    regressions: list[CommandExecution],
) -> str:
    executions = [before, after, *regressions]
    if any(execution.status == "timeout" for execution in executions):
        return "timeout"
    if before.status == "passed":
        return "not_reproduced"
    if before.status != "failed" or after.status != "passed":
        return "failed"
    if any(execution.status != "passed" for execution in regressions):
        return "failed"
    return "passed"


def _summary(status: str, regression_count: int) -> str:
    if status == "passed":
        return (
            "Red-green verified: the test failed before the patch and passed after it; "
            f"{regression_count} regression command(s) passed."
        )
    if status == "not_reproduced":
        return "The test already passed before the patch; this is not red-green evidence."
    if status == "timeout":
        return "At least one validation command timed out; the patch is not verified."
    if status == "error":
        return "Validation setup or patch application failed; see the recorded error."
    return "The patch did not produce a complete red-green and regression result."


async def validate_patch(request: PatchValidationRequest) -> PatchValidationEvidence:
    """Execute maintainer-approved commands and return only observed evidence.

    The source checkout is never modified. A fixed ``base_sha`` selects that
    commit; otherwise the source repository's current ``HEAD`` is used. Both
    before/after worktrees are removed on success and failure.
    """
    if not request.confirm:
        raise PermissionError("Set confirm=true (or --confirm) to execute trusted local commands")
    if not request.test_command.strip() or any(not command.strip() for command in request.regression_commands):
        raise ValueError("Validation commands cannot be blank")

    repository, resolved_sha = await _resolve_repository(request.repository_path, request.base_sha)
    before = _pending_command(request.test_command, "before_patch", request.timeout_seconds)
    after = _pending_command(request.test_command, "after_patch", request.timeout_seconds)
    regressions = [
        _pending_command(command, "regression", request.timeout_seconds) for command in request.regression_commands
    ]
    patch_digest = hashlib.sha256(request.patch.encode("utf-8")).hexdigest()
    evidence = PatchValidationEvidence(
        status="error",
        summary=_summary("error", len(regressions)),
        repository_path=str(repository),
        base_sha=resolved_sha,
        patch_sha256=patch_digest,
        before_patch=before,
        after_patch=after,
        regressions=regressions,
    )
    added_worktrees: list[Path] = []
    temporary = tempfile.TemporaryDirectory(prefix="issue-agent-validation-")
    temporary_path = Path(temporary.name)
    try:
        before_path = temporary_path / "before"
        after_path = temporary_path / "after"
        for path in (before_path, after_path):
            await _git(repository, "worktree", "add", "--detach", str(path), resolved_sha)
            added_worktrees.append(path)

        evidence.before_patch = before = await _run_command(
            request.test_command,
            cwd=before_path,
            phase="before_patch",
            timeout_seconds=request.timeout_seconds,
            max_output_bytes=request.max_output_bytes,
        )
        await _git(after_path, "apply", "--whitespace=nowarn", "-", input_text=request.patch)
        evidence.after_patch = after = await _run_command(
            request.test_command,
            cwd=after_path,
            phase="after_patch",
            timeout_seconds=request.timeout_seconds,
            max_output_bytes=request.max_output_bytes,
        )
        for index, command in enumerate(request.regression_commands):
            regressions[index] = await _run_command(
                command,
                cwd=after_path,
                phase="regression",
                timeout_seconds=request.timeout_seconds,
                max_output_bytes=request.max_output_bytes,
            )
        evidence.regressions = regressions
        status = _overall_status(before, after, regressions)
        evidence.status = status  # type: ignore[assignment]
        evidence.summary = _summary(status, len(regressions))
    except PatchValidationError as error:
        evidence.error = str(error)
    finally:
        cleanup_results: list[bool] = []
        for path in reversed(added_worktrees):
            cleanup_results.append(await _remove_worktree(repository, path))
        try:
            temporary.cleanup()
            temporary_removed = not temporary_path.exists()
        except OSError:
            temporary_removed = False
        evidence.workspace_cleaned = all(cleanup_results) and temporary_removed
        try:
            await _git(repository, "worktree", "prune")
        except PatchValidationError:
            evidence.workspace_cleaned = False
    return evidence
