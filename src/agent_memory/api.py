import logging
import os
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from uuid import UUID

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import Database
from .graph import load_graph
from .repository import (
    correct_memory,
    ingest_turn,
    recall,
    request_memory_purge,
    set_memory_state,
    trace_memory,
)
from .schemas import (
    CorrectionRequest,
    CurrentStateRequest,
    IngestTurnRequest,
    IngestTurnResponse,
    MemoryActionRequest,
    MemoryActionResponse,
    MemoryTraceResponse,
    PurgeRequest,
    PurgeResponse,
    RecallRequest,
    RecallResponse,
    StateConfigRequest,
    StateResetRequest,
    StateSimulationRequest,
    UiLoginRequest,
    UiLoginResponse,
    VaultAccessRequest,
    VaultAccessResponse,
    VaultEntryCreate,
    VaultEntryCreated,
    VaultEntrySummary,
    VaultGrantCreate,
    VaultGrantResponse,
    VaultGrantSummary,
)
from .state_views import (
    active_continuity,
    active_current_items,
    change_current_item,
    get_state_config,
    latest_state,
    list_reports,
    reset_interaction_state,
    simulate_interaction_state,
    update_state_config,
)
from .ui_auth import COOKIE_NAME, create_session, require_api_access, verify_password
from .vault import (
    VaultCrypto,
    access_entry,
    create_entry,
    create_grant,
    list_active_grants,
    list_entries,
    revoke_grant,
)

logger = logging.getLogger(__name__)


@lru_cache
def _vault_crypto() -> VaultCrypto:
    return VaultCrypto.from_file(get_settings().vault_root_key_file)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    database = Database(settings)
    database.open()
    app.state.database = database
    yield
    database.close()


app = FastAPI(
    title="Agent Memory for Hermes",
    version=os.getenv("AGENT_MEMORY_VERSION", "1.0.0-rc.1"),
    lifespan=lifespan,
)


@app.post("/api/v1/ui/login", response_model=UiLoginResponse)
def ui_login(request_body: UiLoginRequest, response: Response):
    settings = get_settings()
    if not verify_password(request_body.password.get_secret_value(), settings.ui_password_hash):
        raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS")
    token = create_session(settings.ui_session_secret.get_secret_value())
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="strict",
        secure=False,
        max_age=12 * 60 * 60,
        path="/",
    )
    return UiLoginResponse(authenticated=True)


@app.post("/api/v1/ui/logout", response_model=UiLoginResponse)
def ui_logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return UiLoginResponse(authenticated=False)


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready(request: Request):
    with request.app.state.database.connection() as connection:
        connection.execute("SELECT 1")
    return {"status": "ready"}


@app.post(
    "/api/v1/ingest/turn",
    response_model=IngestTurnResponse,
    status_code=202,
    dependencies=[Depends(require_api_access)],
)
def ingest(request_body: IngestTurnRequest, request: Request):
    settings = get_settings()
    if request_body.context.shared_namespace != settings.namespace:
        raise HTTPException(status_code=403, detail="NAMESPACE_DENIED")
    with request.app.state.database.connection() as connection:
        event_ids, job_ids, duplicate = ingest_turn(connection, request_body)
    return IngestTurnResponse(
        event_ids=event_ids,
        job_ids=job_ids,
        duplicate=duplicate,
        correlation_id=request_body.context.correlation_id,
    )


@app.post(
    "/api/v1/recall",
    response_model=RecallResponse,
    dependencies=[Depends(require_api_access)],
)
def recall_endpoint(request_body: RecallRequest, request: Request):
    settings = get_settings()
    if request_body.context.shared_namespace != settings.namespace:
        raise HTTPException(status_code=403, detail="NAMESPACE_DENIED")
    with request.app.state.database.connection() as connection:
        items, truncated = recall(connection, request_body)
    return RecallResponse(
        items=items, truncated=truncated, correlation_id=request_body.context.correlation_id
    )


@app.get(
    "/api/v1/memory/{memory_id}/trace",
    response_model=MemoryTraceResponse,
    dependencies=[Depends(require_api_access)],
)
def trace_endpoint(memory_id: str, shared_namespace: str, request: Request):
    settings = get_settings()
    if shared_namespace != settings.namespace:
        raise HTTPException(status_code=403, detail="NAMESPACE_DENIED")
    try:
        parsed_id = UUID(memory_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="VALIDATION_ERROR") from error
    with request.app.state.database.connection() as connection:
        result = trace_memory(connection, shared_namespace, parsed_id)
    if result is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return result


@app.post(
    "/api/v1/memory/{memory_id}/corrections",
    response_model=MemoryActionResponse,
    dependencies=[Depends(require_api_access)],
)
def correction_endpoint(memory_id: str, request_body: CorrectionRequest, request: Request):
    parsed_id = _governance_id(memory_id, request_body)
    with request.app.state.database.connection() as connection:
        replacement_id = correct_memory(connection, parsed_id, request_body)
    if replacement_id is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return MemoryActionResponse(
        memory_id=parsed_id,
        state="superseded",
        replacement_memory_id=replacement_id,
        correlation_id=request_body.context.correlation_id,
    )


def _governance_id(memory_id: str, request_body: MemoryActionRequest):
    settings = get_settings()
    if request_body.context.shared_namespace != settings.namespace:
        raise HTTPException(status_code=403, detail="NAMESPACE_DENIED")
    try:
        return UUID(memory_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="VALIDATION_ERROR") from error


def _change_state(
    memory_id: str,
    request_body: MemoryActionRequest,
    request: Request,
    state: str,
) -> MemoryActionResponse:
    parsed_id = _governance_id(memory_id, request_body)
    with request.app.state.database.connection() as connection:
        changed = set_memory_state(
            connection,
            namespace_key=request_body.context.shared_namespace,
            memory_id=parsed_id,
            state=state,
            actor_id=request_body.context.source_profile,
            reason=request_body.reason,
            correlation_id=request_body.context.correlation_id,
        )
    if not changed:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return MemoryActionResponse(
        memory_id=parsed_id,
        state=state,
        correlation_id=request_body.context.correlation_id,
    )


@app.post(
    "/api/v1/memory/{memory_id}/isolate",
    response_model=MemoryActionResponse,
    dependencies=[Depends(require_api_access)],
)
def isolate_endpoint(memory_id: str, request_body: MemoryActionRequest, request: Request):
    return _change_state(memory_id, request_body, request, "isolated")


@app.post(
    "/api/v1/memory/{memory_id}/forget",
    response_model=MemoryActionResponse,
    dependencies=[Depends(require_api_access)],
)
def forget_endpoint(memory_id: str, request_body: MemoryActionRequest, request: Request):
    return _change_state(memory_id, request_body, request, "forgotten")


@app.post(
    "/api/v1/memory/{memory_id}/purge",
    response_model=PurgeResponse,
    status_code=202,
    dependencies=[Depends(require_api_access)],
)
def purge_endpoint(memory_id: str, request_body: PurgeRequest, request: Request):
    parsed_id = _governance_id(memory_id, request_body)
    if request_body.confirm_memory_id != parsed_id:
        raise HTTPException(status_code=409, detail="PURGE_CONFIRMATION_MISMATCH")
    with request.app.state.database.connection() as connection:
        job_id = request_memory_purge(
            connection,
            namespace_key=request_body.context.shared_namespace,
            memory_id=parsed_id,
            actor_id=request_body.context.source_profile,
            reason=request_body.reason,
            correlation_id=request_body.context.correlation_id,
        )
    if job_id is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return PurgeResponse(
        memory_id=parsed_id,
        state="purge_requested",
        job_id=job_id,
        correlation_id=request_body.context.correlation_id,
    )


def _check_namespace(namespace: str) -> None:
    if namespace != get_settings().namespace:
        raise HTTPException(status_code=403, detail="NAMESPACE_DENIED")


@app.post(
    "/api/v1/vault/entries",
    response_model=VaultEntryCreated,
    status_code=201,
    dependencies=[Depends(require_api_access)],
)
def create_vault_entry(request_body: VaultEntryCreate, request: Request):
    _check_namespace(request_body.context.shared_namespace)
    with request.app.state.database.connection() as connection:
        entry_id = create_entry(
            connection,
            _vault_crypto(),
            namespace_key=request_body.context.shared_namespace,
            kind=request_body.kind,
            display_label=request_body.display_label,
            redacted_hint=request_body.redacted_hint,
            secret_value=request_body.secret_value.get_secret_value(),
            actor_id=request_body.context.source_profile,
            correlation_id=request_body.context.correlation_id,
            linked_memory_id=request_body.linked_memory_id,
        )
    if entry_id is None:
        raise HTTPException(status_code=404, detail="LINKED_MEMORY_NOT_FOUND")
    return VaultEntryCreated(entry_id=entry_id, correlation_id=request_body.context.correlation_id)


@app.get(
    "/api/v1/vault/entries",
    response_model=list[VaultEntrySummary],
    dependencies=[Depends(require_api_access)],
)
def list_vault_entries(shared_namespace: str, request: Request):
    _check_namespace(shared_namespace)
    with request.app.state.database.connection() as connection:
        return list_entries(connection, shared_namespace)


@app.post(
    "/api/v1/vault/entries/{entry_id}/grants",
    response_model=VaultGrantResponse,
    status_code=201,
    dependencies=[Depends(require_api_access)],
)
def create_vault_grant(entry_id: UUID, request_body: VaultGrantCreate, request: Request):
    _check_namespace(request_body.context.shared_namespace)
    with request.app.state.database.connection() as connection:
        grant_id = create_grant(
            connection,
            namespace_key=request_body.context.shared_namespace,
            entry_id=entry_id,
            operation=request_body.operation,
            target_profile=request_body.target_profile,
            expires_at=request_body.expires_at,
            actor_id=request_body.context.source_profile,
            reason=request_body.reason,
            correlation_id=request_body.context.correlation_id,
        )
    if grant_id is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return VaultGrantResponse(
        grant_id=grant_id,
        entry_id=entry_id,
        operation=request_body.operation,
        target_profile=request_body.target_profile,
        expires_at=request_body.expires_at,
        correlation_id=request_body.context.correlation_id,
    )


@app.get(
    "/api/v1/vault/grants",
    response_model=list[VaultGrantSummary],
    dependencies=[Depends(require_api_access)],
)
def list_vault_grants(shared_namespace: str, request: Request):
    _check_namespace(shared_namespace)
    with request.app.state.database.connection() as connection:
        return list_active_grants(connection, shared_namespace)


@app.post(
    "/api/v1/vault/grants/{grant_id}/revoke",
    response_model=MemoryActionResponse,
    dependencies=[Depends(require_api_access)],
)
def revoke_vault_grant(grant_id: UUID, request_body: MemoryActionRequest, request: Request):
    _check_namespace(request_body.context.shared_namespace)
    with request.app.state.database.connection() as connection:
        revoked = revoke_grant(
            connection,
            namespace_key=request_body.context.shared_namespace,
            grant_id=grant_id,
            actor_id=request_body.context.source_profile,
            reason=request_body.reason,
            correlation_id=request_body.context.correlation_id,
        )
    if not revoked:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return MemoryActionResponse(
        memory_id=grant_id,
        state="revoked",
        correlation_id=request_body.context.correlation_id,
    )


@app.post(
    "/api/v1/vault/requests",
    response_model=VaultAccessResponse,
    dependencies=[Depends(require_api_access)],
)
def request_vault_access(request_body: VaultAccessRequest, request: Request):
    _check_namespace(request_body.context.shared_namespace)
    with request.app.state.database.connection() as connection:
        result = access_entry(
            connection,
            _vault_crypto(),
            namespace_key=request_body.context.shared_namespace,
            entry_id=request_body.entry_id,
            operation=request_body.operation,
            source_profile=request_body.context.source_profile,
            correlation_id=request_body.context.correlation_id,
        )
    if result is None:
        raise HTTPException(status_code=403, detail="VAULT_GRANT_REQUIRED")
    secret_value, grant_id = result
    return VaultAccessResponse(
        authorized=True,
        entry_id=request_body.entry_id,
        grant_id=grant_id,
        secret_value=secret_value,
        correlation_id=request_body.context.correlation_id,
    )


@app.get(
    "/api/v1/graph/subgraph",
    dependencies=[Depends(require_api_access)],
)
def graph_subgraph(shared_namespace: str, request: Request):
    _check_namespace(shared_namespace)
    with request.app.state.database.connection() as connection:
        return load_graph(connection, shared_namespace)


@app.get("/api/v1/state", dependencies=[Depends(require_api_access)])
def state_status(shared_namespace: str, request: Request):
    _check_namespace(shared_namespace)
    with request.app.state.database.connection() as connection:
        return {
            "interaction": latest_state(connection, shared_namespace),
            "current_items": active_current_items(connection, shared_namespace),
            "continuities": active_continuity(connection, shared_namespace),
            "config": get_state_config(connection, shared_namespace),
        }


@app.post("/api/v1/state/items", dependencies=[Depends(require_api_access)])
def update_current_state(request_body: CurrentStateRequest, request: Request):
    _check_namespace(request_body.context.shared_namespace)
    with request.app.state.database.connection() as connection:
        result = change_current_item(connection, request_body)
    if result is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return result


@app.put("/api/v1/state/config", dependencies=[Depends(require_api_access)])
def configure_state(request_body: StateConfigRequest, request: Request):
    _check_namespace(request_body.context.shared_namespace)
    with request.app.state.database.connection() as connection:
        return update_state_config(connection, request_body)


@app.post("/api/v1/state/reset", dependencies=[Depends(require_api_access)])
def reset_state(request_body: StateResetRequest, request: Request):
    _check_namespace(request_body.context.shared_namespace)
    with request.app.state.database.connection() as connection:
        return reset_interaction_state(connection, request_body)


@app.post("/api/v1/state/simulate", dependencies=[Depends(require_api_access)])
def simulate_state(request_body: StateSimulationRequest, request: Request):
    _check_namespace(request_body.context.shared_namespace)
    with request.app.state.database.connection() as connection:
        return simulate_interaction_state(connection, request_body)


@app.get("/api/v1/reports/consolidation", dependencies=[Depends(require_api_access)])
def consolidation_reports(shared_namespace: str, request: Request, limit: int = 12):
    _check_namespace(shared_namespace)
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="VALIDATION_ERROR")
    with request.app.state.database.connection() as connection:
        return list_reports(connection, shared_namespace, limit)


static_directory = Path(__file__).with_name("static")
if static_directory.is_dir():
    app.mount("/", StaticFiles(directory=static_directory, html=True), name="star-map")


def main() -> None:
    uvicorn.run("agent_memory.api:app", host="0.0.0.0", port=8080, log_level="info")
