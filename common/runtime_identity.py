"""Ambient runtime identity: who the current work is being done for.

Everything downstream of message routing needs to know which Agent, which end
user, which session and which task it is running under. Threading that through
every call site is what makes multi-agent refactors expensive, so it lives in a
ContextVar instead: routing resolves it once at the entry point, leaf code
reads it and never re-derives it.

ContextVars do not cross thread boundaries on their own. Work handed to another
thread must go through ``submit`` or ``wrap`` below, which copy the calling
context into the worker.
"""

from __future__ import annotations

import contextvars
import functools
from concurrent.futures import Executor, Future
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterator, Optional

_FIELDS = ("agent_id", "user_id", "tenant_id", "session_id", "run_id",
           "web_auth_session_id", "execution_target", "execution_cwd",
           "local_execution_refusal")


@dataclass(frozen=True)
class RuntimeIdentity:
    """Every field is optional.

    ``agent_id`` is None on single-Agent installs and before routing has run;
    ``user_id`` stays None until tenancy lands; ``tenant_id`` is set when the
    request selected a tenant so path resolution can scope to the tenant's
    shared root; ``run_id`` is set per task once sub agents exist.
    Consumers must treat ``user_id``/``tenant_id``/``agent_id`` as None meaning
    "no verified subject yet".
    """

    agent_id: Optional[str] = None
    user_id: Optional[str] = None
    tenant_id: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    # Set only by the authenticated Web entry point. This is the non-secret
    # database session row id, never the bearer/cookie token. Tools revalidate
    # its current owner, expiry and revocation when they act for the user.
    web_auth_session_id: Optional[str] = None

    # Where this run's tools may act, resolved once at message entry (change
    # ``align-desktop-project-execution-with-master``). It is a *value*, not a
    # reference to live session state, so a concurrent turn that re-points the
    # Agent cannot move this run's boundary. None means the pre-existing
    # server-side behaviour; readers must treat None and ``BACKEND_TARGET``
    # identically.
    execution_target: Optional[Any] = None

    # The directory that target resolved to *on this machine*, frozen with the
    # target at message entry (task 3.5). Also a value: a concurrent turn that
    # re-points the shared Agent's tools cannot move this run's working
    # directory, which is what "目录切换只作用于下一轮" means. None means either
    # "no desktop target" or "the target could not be resolved here" -- the two
    # are told apart by ``execution_target``, and only the second is a refusal.
    execution_cwd: Optional[str] = None

    # Why this run may *not* act in the project, when the target itself is
    # fine (change task 3.7). The re-authorization the run boundary performs
    # (``agent.desktop_local.run_context``) answers "is the grant still live";
    # this answers the other half -- "is the Agent that will execute eligible,
    # and is this an interactive turn at all". Kept separate from
    # ``execution_cwd`` so the reason survives: a caller that only saw a missing
    # cwd would report "the project is unavailable" for a teammate that simply
    # may not use it.
    local_execution_refusal: Optional[str] = None

    def execution_location(self) -> str:
        """``"backend"`` or ``"desktop"``; the safe default is the backend."""
        target = self.execution_target
        return getattr(target, "location", "backend") or "backend"

    def allows_project_execution(self) -> bool:
        return bool(getattr(self.execution_target, "allows_project_execution", False))

    def derive(self, **overrides: Any) -> "RuntimeIdentity":
        unknown = set(overrides) - set(_FIELDS)
        if unknown:
            raise TypeError(f"unknown identity fields: {sorted(unknown)}")
        return replace(self, **overrides)


EMPTY_IDENTITY = RuntimeIdentity()

_current: contextvars.ContextVar[RuntimeIdentity] = contextvars.ContextVar(
    "cow_runtime_identity", default=EMPTY_IDENTITY
)


def current_identity() -> RuntimeIdentity:
    return _current.get()


def current_agent_id() -> Optional[str]:
    return _current.get().agent_id


def current_user_id() -> Optional[str]:
    """The verified end user this work belongs to, or None.

    The single source for user-scoped memory ownership. Callers must not accept
    a user id from their arguments: whoever supplies it decides whose private
    memory is written or read, so it has to come from the verified runtime
    identity. None (legacy, or a machine-initiated run) means "no user
    dimension", which the memory layer treats as the pre-existing behaviour.
    """
    return _current.get().user_id or None


@contextmanager
def identity_scope(**overrides: Any) -> Iterator[RuntimeIdentity]:
    """Derive an identity from the ambient one for the duration of a block.

    Sub agents use this: they inherit agent_id/user_id/session_id from the
    parent and take a fresh run_id.
    """
    identity = _current.get().derive(**overrides)
    token = _current.set(identity)
    try:
        yield identity
    finally:
        _current.reset(token)


@contextmanager
def use_identity(identity: RuntimeIdentity) -> Iterator[RuntimeIdentity]:
    """Replace the ambient identity wholesale, for entry points that resolved
    it from scratch rather than deriving it."""
    token = _current.set(identity)
    try:
        yield identity
    finally:
        _current.reset(token)


def override_identity(identity: RuntimeIdentity):
    """Install ``identity`` as the ambient one, returning a restore token.

    For the one shape ``use_identity`` cannot express: a run that learns *part*
    of its authorization later than the entry scope did. A turn is scoped at
    message entry, but the Agent that will actually execute is only known once
    routing has picked the speaker (change task 3.7) -- and by then the scope
    context manager is already open around the whole turn, so the narrowed
    identity has to be installed in place and restored by the run's own cleanup
    (``restore_identity``).
    """
    return _current.set(identity)


def restore_identity(token) -> None:
    """Undo :func:`override_identity`. A ``None`` token is a no-op."""
    if token is not None:
        _current.reset(token)


def submit(executor: Executor, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
    """``executor.submit`` that carries the caller's identity into the worker."""
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, functools.partial(fn, *args, **kwargs))


def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Bind the current context to a callable, for ``threading.Thread(target=)``."""
    ctx = contextvars.copy_context()

    @functools.wraps(fn)
    def _run(*args: Any, **kwargs: Any) -> Any:
        return ctx.run(functools.partial(fn, *args, **kwargs))

    return _run
