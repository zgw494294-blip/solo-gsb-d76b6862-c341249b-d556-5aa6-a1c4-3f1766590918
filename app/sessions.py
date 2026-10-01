"""In-process stateful protocol-drill sessions.

A session binds one validated protocol to an execution trace: the ordered list
of transition ids already fired, replayed from the protocol's initial
configuration. Replacing the protocol keeps the same trace ids and replays
them against the new transition definitions, which lets a drill continue after
the protocol is edited.

All state lives in a single process and is cleared on restart. Each session
carries its own lock; every mutation and snapshot read is atomic, so a failed
execution or replacement leaves the previous session snapshot intact and a
query always observes one version (protocol, configuration, trace and version
change together).
"""

from dataclasses import dataclass, field
from threading import Lock
from uuid import uuid4

from fastapi import HTTPException

from .analyzer import (
    apply_transition,
    build_model,
    enabled_transitions,
    is_completion,
    _configuration_view,
)


@dataclass
class Session:
    protocol: object
    version: int
    trace_ids: list[str]
    lock: Lock = field(default_factory=Lock)


_sessions: dict[str, Session] = {}


def create_session(protocol) -> tuple[str, Session]:
    """Validate the protocol by building its model, then store it."""
    build_model(protocol)
    session_id = uuid4().hex
    session = Session(protocol=protocol, version=0, trace_ids=[])
    _sessions[session_id] = session
    return session_id, session


def get_session(session_id: str) -> Session:
    session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return session


def execute_transition(session: Session, transition_id: str, expected_version: int) -> None:
    with session.lock:
        if session.version != expected_version:
            raise _version_conflict(session.version)

        model = build_model(session.protocol)
        configuration = _replay(model, session.trace_ids)

        if is_completion(configuration, model.finals):
            raise HTTPException(
                status_code=409,
                detail={"error": "session_completed", "transition_id": transition_id},
            )

        enabled = enabled_transitions(configuration, model.outgoing)
        if not any(transition.id == transition_id for transition in enabled):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "transition_not_enabled",
                    "transition_id": transition_id,
                    "enabled_transition_ids": [transition.id for transition in enabled],
                },
            )

        session.trace_ids.append(transition_id)
        session.version += 1


def replace_protocol(session: Session, protocol, expected_version: int) -> None:
    with session.lock:
        if session.version != expected_version:
            raise _version_conflict(session.version)

        # Replay the already executed ids from the new initial configuration.
        # Every step must be enabled and the trace must not reach a completion
        # configuration before its last step; otherwise the old session stands.
        model = build_model(protocol)
        by_id = {transition.id: transition for transition in model.transitions}

        configuration = model.initial
        for index, transition_id in enumerate(session.trace_ids, start=1):
            if is_completion(configuration, model.finals):
                raise _replay_failure(index, "configuration already completed")
            transition = by_id.get(transition_id)
            if transition is None:
                raise _replay_failure(index, f"unknown transition id {transition_id!r}")
            if transition not in enabled_transitions(configuration, model.outgoing):
                raise _replay_failure(index, f"transition {transition_id!r} is not enabled")
            configuration = apply_transition(configuration, transition)

        session.protocol = protocol
        session.version += 1


def _version_conflict(current_version: int) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"error": "version_conflict", "current_version": current_version},
    )


def _replay_failure(index: int, reason: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": "replay_failed",
            "first_failed_step": index,
            "reason": reason,
        },
    )


def _replay(model, trace_ids: list[str]):
    """Apply the trace ids; the trace is assumed valid for this model."""
    by_id = {transition.id: transition for transition in model.transitions}
    configuration = model.initial
    for transition_id in trace_ids:
        configuration = apply_transition(configuration, by_id[transition_id])
    return configuration


def session_view(session_id: str, session: Session) -> dict:
    # Copy the session's mutable references under its lock so protocol, trace
    # and version always come from the same revision.
    with session.lock:
        protocol = session.protocol
        version = session.version
        trace_ids = list(session.trace_ids)

    model = build_model(protocol)
    by_id = {transition.id: transition for transition in model.transitions}

    configuration = model.initial
    trace = []
    for index, transition_id in enumerate(trace_ids, start=1):
        transition = by_id[transition_id]
        configuration = apply_transition(configuration, transition)
        trace.append(
            {
                "index": index,
                "transition_id": transition.id,
                "from": transition.sender,
                "to": transition.receiver,
                "action": transition.action,
                "message": transition.message,
                **_configuration_view(configuration),
            }
        )

    return {
        "id": session_id,
        "version": version,
        "protocol": protocol,
        "configuration": _configuration_view(configuration),
        "trace": trace,
        "enabled_transition_ids": [
            transition.id
            for transition in enabled_transitions(configuration, model.outgoing)
        ],
    }
