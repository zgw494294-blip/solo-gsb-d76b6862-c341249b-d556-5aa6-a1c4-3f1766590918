from fastapi import FastAPI

from . import sessions
from .analyzer import analyze, analyze_guarantee, plan_completion, plan_deactivation
from .schemas import (
    CompletionRequest,
    CompletionResponse,
    DeadlockRequest,
    DeadlockResponse,
    DeactivationRequest,
    DeactivationResponse,
    ExecutionRequest,
    GuaranteeRequest,
    GuaranteeResponse,
    ProtocolReplaceRequest,
    SessionCreateRequest,
    SessionView,
)

app = FastAPI(
    title="Offline Two-Party Message Protocol Deadlock Analyzer",
    version="1.3.0",
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/deadlocks", response_model=DeadlockResponse)
def detect_deadlocks(request: DeadlockRequest) -> dict:
    return analyze(request)


@app.post("/v1/completion-plans", response_model=CompletionResponse)
def plan_completion_endpoint(request: CompletionRequest) -> dict:
    return plan_completion(request)


@app.post("/v1/deactivation-plans", response_model=DeactivationResponse)
def plan_deactivation_endpoint(request: DeactivationRequest) -> dict:
    return plan_deactivation(request)


@app.post("/v1/completion-guarantees", response_model=GuaranteeResponse)
def analyze_completion_guarantee(request: GuaranteeRequest) -> dict:
    return analyze_guarantee(request)


# ---------------------------------------------------------------------------
# Stateful protocol-drill sessions
# ---------------------------------------------------------------------------


@app.post("/v1/sessions", response_model=SessionView, status_code=201)
def create_session(request: SessionCreateRequest) -> dict:
    session_id, session = sessions.create_session(request)
    return sessions.session_view(session_id, session)


@app.get("/v1/sessions/{session_id}", response_model=SessionView)
def read_session(session_id: str) -> dict:
    session = sessions.get_session(session_id)
    return sessions.session_view(session_id, session)


@app.post("/v1/sessions/{session_id}/transitions", response_model=SessionView)
def execute_session_transition(session_id: str, request: ExecutionRequest) -> dict:
    session = sessions.get_session(session_id)
    sessions.execute_transition(session, request.transition_id, request.expected_version)
    return sessions.session_view(session_id, session)


@app.put("/v1/sessions/{session_id}/protocol", response_model=SessionView)
def replace_session_protocol(session_id: str, request: ProtocolReplaceRequest) -> dict:
    session = sessions.get_session(session_id)
    sessions.replace_protocol(session, request.protocol, request.expected_version)
    return sessions.session_view(session_id, session)
