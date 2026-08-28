"""Shared FastAPI dependencies and request-scoped helpers used by the route modules."""

from typing import Annotated, NamedTuple

from fastapi import Depends, Request
from openai import AsyncOpenAI
from pydantic import BaseModel as PydanticBaseModel

from app.agent import IssueAgent
from app.circuit_breaker import CircuitBreaker
from app.config import Settings, get_settings
from app.github import GitHubClient
from app.models import ChatRequest
from app.sessions import Session, SessionManager
from app.task_queue import TaskQueue


def get_session_manager(request: Request) -> SessionManager:
    """FastAPI dependency: retrieve the SessionManager from app state."""
    manager: SessionManager = request.app.state.session_manager
    return manager


def get_circuit_breaker(request: Request) -> CircuitBreaker:
    """FastAPI dependency: retrieve the CircuitBreaker from app state."""
    breaker: CircuitBreaker = request.app.state.circuit_breaker
    return breaker


def get_task_queue(request: Request) -> TaskQueue:
    """FastAPI dependency: retrieve the TaskQueue from app state."""
    queue: TaskQueue = request.app.state.task_queue
    return queue


# Annotated dependency alias — avoids B008 lint warnings and reduces line length.
SessionMgr = Annotated[SessionManager, Depends(get_session_manager)]
CircuitBreakerDep = Annotated[CircuitBreaker, Depends(get_circuit_breaker)]
TaskQueueDep = Annotated[TaskQueue, Depends(get_task_queue)]


class ProviderClients(NamedTuple):
    """Process-scoped provider clients shared across requests (wired in lifespan)."""

    openai_client: AsyncOpenAI | None
    github_client: GitHubClient | None


def get_provider_clients(request: Request) -> ProviderClients:
    state = request.app.state
    return ProviderClients(
        openai_client=getattr(state, "openai_client", None),
        github_client=getattr(state, "github_client", None),
    )


ProviderClientsDep = Annotated[ProviderClients, Depends(get_provider_clients)]


def build_issue_agent(clients: ProviderClients, settings: Settings, breaker: CircuitBreaker) -> IssueAgent:
    """Build a request coordinator backed by process-scoped provider clients."""
    shared_github = clients.github_client
    return IssueAgent(
        settings,
        client=clients.openai_client,
        github_client=shared_github.fork() if shared_github is not None else None,
        circuit_breaker=breaker,
    )


def resolve_override_settings(req: PydanticBaseModel) -> Settings:
    """Fork server settings, applying any per-request overrides from the request body.

    The global cached Settings object is never mutated; a deep copy is returned
    with language/model/thinking/reasoning_effort/review overridden when the
    request provides them. Falls back to the original settings when nothing is set.
    """
    settings = get_settings()
    overrides: dict[str, object] = {}
    mapping = (
        ("language", "language"),
        ("model", "openai_model"),
        ("thinking", "openai_thinking"),
        ("reasoning_effort", "openai_reasoning_effort"),
        ("review", "independent_review"),
    )
    for attr, field in mapping:
        value = getattr(req, attr, None)
        if value is not None:
            overrides[field] = value
    if not overrides:
        return settings
    return settings.model_copy(update=overrides, deep=True)


def apply_regenerate(session: Session, request: ChatRequest) -> None:
    """Regeneration: drop trailing assistant message(s) and point the message at the last user turn.

    Mutates the in-memory session so the agent re-answers the most recent user
    question without the previous (discarded) assistant reply in its history.
    """
    messages = getattr(session, "messages", None) or []
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], dict) and messages[i].get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        return
    last_user_content = messages[last_user_idx].get("content", "")
    if last_user_content:
        request.message = last_user_content
    session.messages = messages[:last_user_idx]
