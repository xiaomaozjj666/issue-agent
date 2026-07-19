from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_windows_one_click_launcher_is_wired_to_powershell() -> None:
    command_launcher = (PROJECT_ROOT / "打开 Issue Agent.cmd").read_text(encoding="utf-8")
    powershell_launcher = (PROJECT_ROOT / "start-issue-agent.ps1").read_text(encoding="utf-8")

    assert 'start-issue-agent.ps1" %*' in command_launcher
    assert "Get-IssueAgentHealth" in powershell_launcher
    assert "Stop-LocalIssueAgent" in powershell_launcher
    assert "calculate_build_id" in powershell_launcher
    assert "-m uvicorn app.main:app --host 127.0.0.1 --port 8000" in powershell_launcher
    assert "Start-Process $appUrl" in powershell_launcher
