"""Persistent, cross-process protocol-drill sessions stored in local SQLite.

A session binds one validated protocol to an execution trace: the ordered list
of transition ids already fired, replayed from the protocol's initial
configuration. Replacing the protocol keeps the same trace ids and replays
them against the new transition definitions, which lets a drill continue after
the protocol is edited.

The protocol payload, the trace ids and the version live in a single SQLite
database file shared by every worker process. Each mutation opens its own
connection, takes a ``BEGIN IMMEDIATE`` write transaction and performs the
version check, the trace replay and the row update against the same committed
row. Two processes submitting the same ``expected_version`` are therefore
serialized: one commits and the other observes the new version and fails with
409; a failed replay rolls back and leaves the previous revision untouched.

A query is one ``SELECT`` and can only observe a fully committed row (the
protocol, trace and version change together in one update). Successful
mutations build their response snapshot from the exact values they committed
instead of re-reading the row, so a response never mixes in a later commit.
Versions are persisted and never regress across restarts.
"""

import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from .analyzer import (
    apply_transition,
    build_model,
    enabled_transitions,
    is_completion,
    _configuration_view,
)
from .schemas import DeadlockRequest

# In the container this lives on the named volume mounted at /data; override
# with SESSION_DB_PATH for local development and tests.
DB_PATH = os.environ.get("SESSION_DB_PATH", "/data/sessions.db")
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    protocol_json TEXT NOT NULL,
    trace_json TEXT NOT NULL
)
"""


def init_db(path: str = DB_PATH) -> None:
    """Create the database directory, enable WAL and create the table.

    Runs at import time, so every worker process self-provisions the database
    on startup and concurrent first starts simply serialize on SQLite's locks.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(_SCHEMA)
    finally:
        connection.close()


def _connect() -> sqlite3.Connection:
    # isolation_level=None puts the driver in autocommit mode so that the
    # transaction boundaries below are exactly the ones we issue.
    connection = sqlite3.connect(DB_PATH, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def _dump_protocol(protocol: DeadlockRequest) -> str:
    return protocol.model_dump_json(by_alias=True)


def _load_protocol(blob: str) -> DeadlockRequest:
    return DeadlockRequest.model_validate_json(blob)


def _dump_trace(trace_ids: list[str]) -> str:
    return json.dumps(trace_ids, ensure_ascii=False)


def _load_trace(blob: str) -> list[str]:
    return json.loads(blob)


def create_session(protocol) -> dict:
    """Validate the protocol by building its model, then persist it."""
    build_model(protocol)
    protocol_json = _dump_protocol(protocol)
    trace_json = _dump_trace([])

    connection = _connect()
    try:
        # uuid4 collisions are effectively impossible, but retry rather than
        # surfacing a 500 if the generated id is somehow already taken.
        for _ in range(5):
            session_id = uuid4().hex
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO sessions (id, version, protocol_json, trace_json)"
                    " VALUES (?, 0, ?, ?)",
                    (session_id, protocol_json, trace_json),
                )
                connection.commit()
                break
            except sqlite3.IntegrityError:
                connection.rollback()
        else:
            raise HTTPException(status_code=500, detail="could not allocate session id")
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    # Build from the values just inserted rather than re-reading the row.
    return _build_view(session_id, protocol, 0, [])


def view_session(session_id: str) -> dict:
    connection = _connect()
    try:
        # A single SELECT in WAL mode observes one fully committed revision.
        row = connection.execute(
            "SELECT version, protocol_json, trace_json FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        raise HTTPException(status_code=404, detail="unknown session")

    version, protocol_json, trace_json = row
    return _build_view(session_id, _load_protocol(protocol_json), version, _load_trace(trace_json))


def execute_transition(session_id: str, transition_id: str, expected_version: int) -> dict:
    connection = _connect()
    try:
        # Reserve the writer lock first: version check, replay validation and
        # the write then all run against the same committed revision.
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT version, protocol_json, trace_json FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown session")

        version, protocol_json, trace_json = row
        if version != expected_version:
            raise _version_conflict(version)

        protocol = _load_protocol(protocol_json)
        trace_ids = _load_trace(trace_json)

        model = build_model(protocol)
        configuration = _replay(model, trace_ids)

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

        new_trace_ids = [*trace_ids, transition_id]
        new_version = version + 1
        connection.execute(
            "UPDATE sessions SET version = ?, trace_json = ? WHERE id = ?",
            (new_version, _dump_trace(new_trace_ids), session_id),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    return _build_view(session_id, protocol, new_version, new_trace_ids)


def replace_protocol(session_id: str, protocol, expected_version: int) -> dict:
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT version, protocol_json, trace_json FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown session")

        version, _protocol_json, trace_json = row
        if version != expected_version:
            raise _version_conflict(version)

        trace_ids = _load_trace(trace_json)

        # Replay the already executed ids from the new initial configuration.
        # Every step must be enabled and the trace must not reach a completion
        # configuration before its last step; otherwise the transaction rolls
        # back and the old session stands.
        model = build_model(protocol)
        by_id = {transition.id: transition for transition in model.transitions}

        configuration = model.initial
        for index, transition_id in enumerate(trace_ids, start=1):
            if is_completion(configuration, model.finals):
                raise _replay_failure(index, "configuration already completed")
            transition = by_id.get(transition_id)
            if transition is None:
                raise _replay_failure(index, f"unknown transition id {transition_id!r}")
            if transition not in enabled_transitions(configuration, model.outgoing):
                raise _replay_failure(index, f"transition {transition_id!r} is not enabled")
            configuration = apply_transition(configuration, transition)

        new_version = version + 1
        connection.execute(
            "UPDATE sessions SET version = ?, protocol_json = ? WHERE id = ?",
            (new_version, _dump_protocol(protocol), session_id),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    return _build_view(session_id, protocol, new_version, trace_ids)


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


def _build_view(session_id: str, protocol, version: int, trace_ids: list[str]) -> dict:
    """Replay the trace against the protocol to assemble one revision's view.

    Called only with values already committed (or about to be returned from a
    successful create), so the snapshot can never contain data from a revision
    committed later by another process.
    """
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


init_db()
