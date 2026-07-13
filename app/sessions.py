import asyncio
import uuid
from dataclasses import dataclass, field

from app.models import AnalysisReport, IssueData


@dataclass
class Session:
    session_id: str
    issue_url: str
    issue: IssueData | None = None
    tree: list[str] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    file_cache: dict[str, str] = field(default_factory=dict)
    files_read: list[str] = field(default_factory=list)
    report: AnalysisReport | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)


class SessionManager:
    def __init__(self, max_sessions: int = 100) -> None:
        self._sessions: dict[str, Session] = {}
        self._max = max_sessions

    def create(self, issue_url: str) -> Session:
        session_id = uuid.uuid4().hex[:12]
        session = Session(session_id=session_id, issue_url=issue_url)
        self._sessions[session_id] = session
        self._evict_if_needed()
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def _evict_if_needed(self) -> None:
        while len(self._sessions) > self._max:
            oldest = next(iter(self._sessions))
            del self._sessions[oldest]
