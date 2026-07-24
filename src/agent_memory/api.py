import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles

from .community_projection import PROJECTION_VERSION as COMMUNITY_PROJECTION_VERSION
from .config import get_settings
from .db import Database
from .galaxies import (
    create_manual_galaxy,
    get_galaxy,
    list_galaxies,
    list_layout_preferences,
    request_rebuild,
    save_layout_preference,
    undo_last_galaxy_change,
    update_galaxy,
    update_membership,
)
from .graph import GraphLens, load_graph
from .ids import stable_uuid
from .quality import build_quality_report
from .repository import (
    change_entity_fact_relation,
    correct_memory,
    ingest_turn,
    list_review_queue,
    merge_entity,
    recall,
    request_memory_purge,
    set_memory_state,
    split_entity,
    trace_memory,
    unmerge_entity,
)
from .schemas import (
    CorrectionRequest,
    CurrentStateRequest,
    EntityGovernanceRequest,
    EntityGovernanceResponse,
    EntityMergeRequest,
    EntityRelationResponse,
    EntitySplitRequest,
    GalaxyCreateRequest,
    GalaxyMembershipRequest,
    GalaxyRebuildRequest,
    GalaxyUndoRequest,
    GalaxyUpdateRequest,
    IngestTurnRequest,
    IngestTurnResponse,
    LayoutPreferenceRequest,
    MemoryActionRequest,
    MemoryActionResponse,
    MemoryTraceResponse,
    PurgeRequest,
    PurgeResponse,
    QualityReportResponse,
    RecallRequest,
    RecallResponse,
    ReviewQueueResponse,
    StateConfigRequest,
    StateResetRequest,
    StateSimulationRequest,
    SubjectSourceMappingRequest,
    SubjectSummary,
    SubjectUpdateRequest,
    UiConfigResponse,
    UiLoginRequest,
    UiLoginResponse,
    VaultAccessRequest,
    VaultAccessResponse,
    VaultEntryActionResponse,
    VaultEntryCreate,
    VaultEntryCreated,
    VaultEntryDeleteRequest,
    VaultEntryMetadataUpdate,
    VaultEntryReplaceRequest,
    VaultEntryRevealRequest,
    VaultEntryRevealResponse,
    VaultEntryStatusRequest,
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
from .subjects import (
    assign_source_to_subject,
    ensure_user_subject,
    list_subjects,
    reset_source_subject_mapping,
    update_subject,
)
from .ui_auth import (
    COOKIE_NAME,
    create_session,
    require_api_access,
    require_service_access,
    require_ui_session,
    verify_password,
)
from .vault import (
    VaultCrypto,
    access_entry,
    create_entry,
    create_grant,
    delete_entry,
    list_active_grants,
    list_entries,
    replace_entry_secret,
    reveal_entry,
    revoke_grant,
    set_entry_status,
    update_entry_metadata,
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
    with database.connection() as connection:
        namespace_id = stable_uuid("namespace", settings.namespace)
        connection.execute(
            """INSERT INTO core.namespaces(id,stable_key) VALUES (%s,%s)
               ON CONFLICT DO NOTHING""",
            (namespace_id, settings.namespace),
        )
        ensure_user_subject(connection, namespace_id)
    app.state.database = database
    yield
    database.close()


app = FastAPI(
    title="Agent Memory for Hermes",
    version=os.getenv("AGENT_MEMORY_VERSION", "1.0.0-rc.8"),
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


@app.get(
    "/api/v1/ui/config",
    response_model=UiConfigResponse,
    dependencies=[Depends(require_api_access)],
)
def ui_config():
    settings = get_settings()
    return UiConfigResponse(
        namespace=settings.namespace,
        namespace_id=stable_uuid("namespace", settings.namespace),
        version=os.getenv("AGENT_MEMORY_VERSION", "1.0.0-rc.8"),
    )


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
    dependencies=[Depends(require_service_access)],
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


@app.get(
    "/api/v1/memories/review",
    response_model=ReviewQueueResponse,
    dependencies=[Depends(require_api_access)],
)
def review_queue_endpoint(
    request: Request,
    shared_namespace: str,
    reason: Literal["all", "candidate", "untrusted_tool"] = "all",
    source_profile: str | None = Query(default=None, min_length=1, max_length=128),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    _check_namespace(shared_namespace)
    settings = get_settings()
    with request.app.state.database.connection() as connection:
        return list_review_queue(
            connection,
            namespace_key=shared_namespace,
            trusted_tools=settings.trusted_observation_tools,
            reason=reason,
            source_profile=source_profile,
            limit=limit,
            offset=offset,
        )


def _entity_governance_error(error: ValueError) -> HTTPException:
    detail = str(error)
    status = 409 if detail in {
        "ENTITY_ALREADY_MERGED",
        "ENTITY_MERGE_CYCLE",
        "ENTITY_NAME_CONFLICT",
        "ENTITY_RELATION_ALREADY_ATTACHED",
        "ENTITY_RELATION_NOT_ATTACHED",
    } else 422
    return HTTPException(status_code=status, detail=detail)


@app.post(
    "/api/v1/entities/{entity_id}/merge",
    response_model=EntityGovernanceResponse,
    dependencies=[Depends(require_api_access)],
)
def merge_entity_endpoint(entity_id: UUID, request_body: EntityMergeRequest, request: Request):
    _check_namespace(request_body.context.shared_namespace)
    try:
        with request.app.state.database.connection() as connection:
            result = merge_entity(connection, entity_id, request_body)
    except ValueError as error:
        raise _entity_governance_error(error) from error
    if result is None:
        raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
    return result


@app.post(
    "/api/v1/entities/{entity_id}/unmerge",
    response_model=EntityGovernanceResponse,
    dependencies=[Depends(require_api_access)],
)
def unmerge_entity_endpoint(
    entity_id: UUID, request_body: EntityGovernanceRequest, request: Request
):
    _check_namespace(request_body.context.shared_namespace)
    with request.app.state.database.connection() as connection:
        result = unmerge_entity(
            connection,
            namespace_key=request_body.context.shared_namespace,
            entity_id=entity_id,
            actor_id=request_body.context.source_profile,
            reason=request_body.reason,
            correlation_id=request_body.context.correlation_id,
        )
    if result is None:
        raise HTTPException(status_code=404, detail="ENTITY_NOT_MERGED")
    return result


@app.post(
    "/api/v1/entities/{entity_id}/split",
    response_model=EntityGovernanceResponse,
    dependencies=[Depends(require_api_access)],
)
def split_entity_endpoint(entity_id: UUID, request_body: EntitySplitRequest, request: Request):
    _check_namespace(request_body.context.shared_namespace)
    try:
        with request.app.state.database.connection() as connection:
            result = split_entity(connection, entity_id, request_body)
    except ValueError as error:
        raise _entity_governance_error(error) from error
    if result is None:
        raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
    return result


@app.post(
    "/api/v1/entities/{entity_id}/facts/{fact_id}/{action}",
    response_model=EntityRelationResponse,
    dependencies=[Depends(require_api_access)],
)
def change_entity_fact_relation_endpoint(
    entity_id: UUID,
    fact_id: UUID,
    action: Literal["attach", "detach"],
    request_body: EntityGovernanceRequest,
    request: Request,
):
    _check_namespace(request_body.context.shared_namespace)
    try:
        with request.app.state.database.connection() as connection:
            result = change_entity_fact_relation(
                connection,
                namespace_key=request_body.context.shared_namespace,
                entity_id=entity_id,
                fact_id=fact_id,
                action=action,
                actor_id=request_body.context.source_profile,
                reason=request_body.reason,
                correlation_id=request_body.context.correlation_id,
            )
    except ValueError as error:
        raise _entity_governance_error(error) from error
    if result is None:
        raise HTTPException(status_code=404, detail="ENTITY_OR_FACT_NOT_FOUND")
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


def _verify_vault_reauthentication(password: str) -> None:
    if not verify_password(password, get_settings().ui_password_hash):
        raise HTTPException(status_code=401, detail="VAULT_REAUTHENTICATION_REQUIRED")


@app.post(
    "/api/v1/vault/entries",
    response_model=VaultEntryCreated,
    status_code=201,
    dependencies=[Depends(require_ui_session)],
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
    dependencies=[Depends(require_ui_session)],
)
def list_vault_entries(shared_namespace: str, request: Request):
    _check_namespace(shared_namespace)
    with request.app.state.database.connection() as connection:
        return list_entries(connection, shared_namespace)


@app.post(
    "/api/v1/vault/entries/{entry_id}/reveal",
    response_model=VaultEntryRevealResponse,
    dependencies=[Depends(require_ui_session)],
)
def reveal_vault_entry(
    entry_id: UUID,
    request_body: VaultEntryRevealRequest,
    request: Request,
    response: Response,
):
    _check_namespace(request_body.context.shared_namespace)
    _verify_vault_reauthentication(request_body.password.get_secret_value())
    with request.app.state.database.connection() as connection:
        secret = reveal_entry(
            connection,
            _vault_crypto(),
            namespace_key=request_body.context.shared_namespace,
            entry_id=entry_id,
            actor_id=request_body.context.source_profile,
            reason=request_body.reason,
            correlation_id=request_body.context.correlation_id,
        )
    if secret is None:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return VaultEntryRevealResponse(
        entry_id=entry_id,
        secret_value=secret,
        correlation_id=request_body.context.correlation_id,
    )


@app.patch(
    "/api/v1/vault/entries/{entry_id}",
    response_model=VaultEntryActionResponse,
    dependencies=[Depends(require_ui_session)],
)
def update_vault_entry_metadata(
    entry_id: UUID, request_body: VaultEntryMetadataUpdate, request: Request
):
    _check_namespace(request_body.context.shared_namespace)
    _verify_vault_reauthentication(request_body.password.get_secret_value())
    with request.app.state.database.connection() as connection:
        updated = update_entry_metadata(
            connection,
            namespace_key=request_body.context.shared_namespace,
            entry_id=entry_id,
            display_label=request_body.display_label,
            redacted_hint=request_body.redacted_hint,
            actor_id=request_body.context.source_profile,
            reason=request_body.reason,
            correlation_id=request_body.context.correlation_id,
        )
    if not updated:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return VaultEntryActionResponse(
        entry_id=entry_id,
        state="updated",
        correlation_id=request_body.context.correlation_id,
    )


@app.post(
    "/api/v1/vault/entries/{entry_id}/replace",
    response_model=VaultEntryActionResponse,
    dependencies=[Depends(require_ui_session)],
)
def replace_vault_entry_secret(
    entry_id: UUID, request_body: VaultEntryReplaceRequest, request: Request
):
    _check_namespace(request_body.context.shared_namespace)
    _verify_vault_reauthentication(request_body.password.get_secret_value())
    with request.app.state.database.connection() as connection:
        updated = replace_entry_secret(
            connection,
            _vault_crypto(),
            namespace_key=request_body.context.shared_namespace,
            entry_id=entry_id,
            secret_value=request_body.secret_value.get_secret_value(),
            actor_id=request_body.context.source_profile,
            reason=request_body.reason,
            correlation_id=request_body.context.correlation_id,
        )
    if not updated:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return VaultEntryActionResponse(
        entry_id=entry_id,
        state="replaced",
        correlation_id=request_body.context.correlation_id,
    )


@app.post(
    "/api/v1/vault/entries/{entry_id}/status",
    response_model=VaultEntryActionResponse,
    dependencies=[Depends(require_ui_session)],
)
def change_vault_entry_status(
    entry_id: UUID, request_body: VaultEntryStatusRequest, request: Request
):
    _check_namespace(request_body.context.shared_namespace)
    _verify_vault_reauthentication(request_body.password.get_secret_value())
    with request.app.state.database.connection() as connection:
        updated = set_entry_status(
            connection,
            namespace_key=request_body.context.shared_namespace,
            entry_id=entry_id,
            new_status=request_body.status,
            actor_id=request_body.context.source_profile,
            reason=request_body.reason,
            correlation_id=request_body.context.correlation_id,
        )
    if not updated:
        raise HTTPException(status_code=404, detail="NOT_FOUND_OR_UNCHANGED")
    return VaultEntryActionResponse(
        entry_id=entry_id,
        state=request_body.status,
        correlation_id=request_body.context.correlation_id,
    )


@app.post(
    "/api/v1/vault/entries/{entry_id}/delete",
    response_model=VaultEntryActionResponse,
    dependencies=[Depends(require_ui_session)],
)
def delete_vault_entry(
    entry_id: UUID, request_body: VaultEntryDeleteRequest, request: Request
):
    _check_namespace(request_body.context.shared_namespace)
    if request_body.confirm_entry_id != entry_id:
        raise HTTPException(status_code=409, detail="DELETE_CONFIRMATION_MISMATCH")
    _verify_vault_reauthentication(request_body.password.get_secret_value())
    with request.app.state.database.connection() as connection:
        deleted = delete_entry(
            connection,
            namespace_key=request_body.context.shared_namespace,
            entry_id=entry_id,
            actor_id=request_body.context.source_profile,
            reason=request_body.reason,
            correlation_id=request_body.context.correlation_id,
        )
    if not deleted:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    return VaultEntryActionResponse(
        entry_id=entry_id,
        state="deleted",
        correlation_id=request_body.context.correlation_id,
    )


@app.post(
    "/api/v1/vault/entries/{entry_id}/grants",
    response_model=VaultGrantResponse,
    status_code=201,
    dependencies=[Depends(require_ui_session)],
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
    dependencies=[Depends(require_ui_session)],
)
def list_vault_grants(shared_namespace: str, request: Request):
    _check_namespace(shared_namespace)
    with request.app.state.database.connection() as connection:
        return list_active_grants(connection, shared_namespace)


@app.post(
    "/api/v1/vault/grants/{grant_id}/revoke",
    response_model=MemoryActionResponse,
    dependencies=[Depends(require_ui_session)],
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
    dependencies=[Depends(require_service_access)],
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
    "/api/v1/graph/subjects",
    response_model=list[SubjectSummary],
    dependencies=[Depends(require_api_access)],
)
def graph_subjects(shared_namespace: str, request: Request):
    _check_namespace(shared_namespace)
    with request.app.state.database.connection() as connection:
        return list_subjects(connection, shared_namespace)


@app.put(
    "/api/v1/graph/subjects/{subject_id}",
    response_model=SubjectSummary,
    dependencies=[Depends(require_ui_session)],
)
def update_graph_subject(
    subject_id: UUID, request_body: SubjectUpdateRequest, request: Request
):
    _check_namespace(request_body.context.shared_namespace)
    try:
        with request.app.state.database.connection() as connection:
            result = update_subject(
                connection,
                namespace_key=request_body.context.shared_namespace,
                subject_id=subject_id,
                display_name=request_body.display_name,
                color=request_body.color,
                status=request_body.status,
                actor_id=request_body.context.source_profile,
                reason=request_body.reason,
                correlation_id=request_body.context.correlation_id,
            )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="SUBJECT_NOT_FOUND")
    return result


@app.post(
    "/api/v1/graph/subjects/{subject_id}/sources/{source_id}",
    response_model=SubjectSummary,
    dependencies=[Depends(require_ui_session)],
)
def assign_graph_subject_source(
    subject_id: UUID,
    source_id: UUID,
    request_body: SubjectSourceMappingRequest,
    request: Request,
):
    _check_namespace(request_body.context.shared_namespace)
    try:
        with request.app.state.database.connection() as connection:
            result = assign_source_to_subject(
                connection,
                namespace_key=request_body.context.shared_namespace,
                subject_id=subject_id,
                source_id=source_id,
                actor_id=request_body.context.source_profile,
                reason=request_body.reason,
                correlation_id=request_body.context.correlation_id,
            )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="SUBJECT_OR_SOURCE_NOT_FOUND")
    return result


@app.delete(
    "/api/v1/graph/subjects/{subject_id}/sources/{source_id}",
    response_model=SubjectSummary,
    dependencies=[Depends(require_ui_session)],
)
def reset_graph_subject_source(
    subject_id: UUID,
    source_id: UUID,
    request_body: SubjectSourceMappingRequest,
    request: Request,
):
    _check_namespace(request_body.context.shared_namespace)
    with request.app.state.database.connection() as connection:
        result = reset_source_subject_mapping(
            connection,
            namespace_key=request_body.context.shared_namespace,
            subject_id=subject_id,
            source_id=source_id,
            actor_id=request_body.context.source_profile,
            reason=request_body.reason,
            correlation_id=request_body.context.correlation_id,
        )
    if result is None:
        raise HTTPException(status_code=404, detail="SUBJECT_SOURCE_MAPPING_NOT_FOUND")
    return result


def _raise_galaxy_error(error: ValueError) -> None:
    detail = str(error)
    status_code = 404 if "NOT_FOUND" in detail else 409 if "CONFLICT" in detail else 422
    raise HTTPException(status_code=status_code, detail=detail) from error


@app.get(
    "/api/v1/graph/galaxies",
    dependencies=[Depends(require_api_access)],
)
def graph_galaxies(
    shared_namespace: str,
    request: Request,
    include_inactive: bool = False,
):
    _check_namespace(shared_namespace)
    with request.app.state.database.connection() as connection:
        return list_galaxies(
            connection,
            shared_namespace,
            include_inactive=include_inactive,
        )


@app.post(
    "/api/v1/graph/galaxies",
    dependencies=[Depends(require_ui_session)],
)
def create_graph_galaxy(request_body: GalaxyCreateRequest, request: Request):
    _check_namespace(request_body.context.shared_namespace)
    try:
        with request.app.state.database.connection() as connection:
            return create_manual_galaxy(connection, request_body)
    except ValueError as error:
        _raise_galaxy_error(error)


@app.post(
    "/api/v1/graph/galaxies/rebuild",
    status_code=202,
    dependencies=[Depends(require_ui_session)],
)
def rebuild_graph_galaxies(request_body: GalaxyRebuildRequest, request: Request):
    _check_namespace(request_body.context.shared_namespace)
    with request.app.state.database.connection() as connection:
        job_id = request_rebuild(
            connection,
            namespace_key=request_body.context.shared_namespace,
            actor_id=request_body.context.source_profile,
            reason=request_body.reason,
            correlation_id=request_body.context.correlation_id,
        )
    return {"job_id": job_id, "correlation_id": request_body.context.correlation_id}


@app.patch(
    "/api/v1/graph/galaxies/{galaxy_id}",
    dependencies=[Depends(require_ui_session)],
)
def update_graph_galaxy(
    galaxy_id: UUID, request_body: GalaxyUpdateRequest, request: Request
):
    _check_namespace(request_body.context.shared_namespace)
    try:
        with request.app.state.database.connection() as connection:
            return update_galaxy(connection, galaxy_id, request_body)
    except ValueError as error:
        _raise_galaxy_error(error)


@app.put(
    "/api/v1/graph/galaxies/{galaxy_id}/members/{entity_id}",
    dependencies=[Depends(require_ui_session)],
)
def govern_graph_galaxy_member(
    galaxy_id: UUID,
    entity_id: UUID,
    request_body: GalaxyMembershipRequest,
    request: Request,
):
    _check_namespace(request_body.context.shared_namespace)
    try:
        with request.app.state.database.connection() as connection:
            return update_membership(connection, galaxy_id, entity_id, request_body)
    except ValueError as error:
        _raise_galaxy_error(error)


@app.post(
    "/api/v1/graph/galaxies/{galaxy_id}/undo",
    dependencies=[Depends(require_ui_session)],
)
def undo_graph_galaxy(
    galaxy_id: UUID, request_body: GalaxyUndoRequest, request: Request
):
    _check_namespace(request_body.context.shared_namespace)
    try:
        with request.app.state.database.connection() as connection:
            return undo_last_galaxy_change(connection, galaxy_id, request_body)
    except ValueError as error:
        _raise_galaxy_error(error)


@app.get(
    "/api/v1/graph/layout",
    dependencies=[Depends(require_api_access)],
)
def graph_layout(shared_namespace: str, request: Request):
    _check_namespace(shared_namespace)
    with request.app.state.database.connection() as connection:
        return list_layout_preferences(connection, shared_namespace)


@app.put(
    "/api/v1/graph/layout",
    dependencies=[Depends(require_ui_session)],
)
def save_graph_layout(request_body: LayoutPreferenceRequest, request: Request):
    _check_namespace(request_body.context.shared_namespace)
    try:
        with request.app.state.database.connection() as connection:
            return save_layout_preference(connection, request_body)
    except ValueError as error:
        _raise_galaxy_error(error)


def _galaxy_graph_view(graph: dict, galaxy: dict) -> dict:
    member_node_ids = {
        f"entity:{member['entity_id']}"
        for member in galaxy["members"]
        if member["governance_state"] != "excluded"
    }
    graph["nodes"] = [
        node for node in graph["nodes"] if node["data"]["id"] in member_node_ids
    ]
    graph["edges"] = [
        {
            "data": {
                "id": f"typed-relation:{relation['id']}",
                "record_id": str(relation["id"]),
                "source": f"entity:{relation['source_entity_id']}",
                "target": f"entity:{relation['target_entity_id']}",
                "kind": "typed_relation",
                "relation_type": relation["relation_type"],
                "transport": relation["transport"],
                "strength": f"{relation['confidence']:.2f}",
                "fact_ids": "|".join(f"fact:{item}" for item in relation["fact_ids"]),
                "evidence_ids": "|".join(str(item) for item in relation["evidence_ids"]),
            }
        }
        for relation in galaxy["relations"]
        if f"entity:{relation['source_entity_id']}" in member_node_ids
        and f"entity:{relation['target_entity_id']}" in member_node_ids
    ]
    for collection in ("facts", "episodes", "arcs"):
        graph[collection] = [
            item
            for item in graph[collection]
            if member_node_ids.intersection(
                value for value in item["data"].get("entity_ids", "").split("|") if value
            )
        ]
    graph["vault_markers"] = [
        marker
        for marker in graph["vault_markers"]
        if member_node_ids.intersection(
            value for value in marker["data"].get("target_ids", "").split("|") if value
        )
    ]
    graph["galaxies"] = [galaxy]
    graph["projection"].update(
        {
            "community_projection": COMMUNITY_PROJECTION_VERSION,
            "view": "galaxy",
            "galaxy_id": str(galaxy["id"]),
        }
    )
    return graph


def _universe_graph_view(graph: dict, galaxies: list[dict]) -> dict:
    visible = [
        galaxy
        for galaxy in galaxies
        if galaxy["lifecycle_state"] == "active" and galaxy["visibility"] == "visible"
    ]
    relations = {
        str(relation["id"]): relation
        for galaxy in visible
        for relation in galaxy["relations"]
    }
    typed_pairs = {
        frozenset(
            (
                f"entity:{relation['source_entity_id']}",
                f"entity:{relation['target_entity_id']}",
            )
        )
        for relation in relations.values()
    }
    graph["edges"] = [
        edge
        for edge in graph["edges"]
        if edge["data"].get("kind") != "relation"
        or frozenset((edge["data"]["source"], edge["data"]["target"])) not in typed_pairs
    ]
    graph["edges"].extend(
        {
            "data": {
                "id": f"typed-relation:{relation['id']}",
                "record_id": str(relation["id"]),
                "source": f"entity:{relation['source_entity_id']}",
                "target": f"entity:{relation['target_entity_id']}",
                "kind": "typed_relation",
                "relation_type": relation["relation_type"],
                "transport": relation["transport"],
                "strength": f"{relation['confidence']:.2f}",
                "fact_ids": "|".join(f"fact:{item}" for item in relation["fact_ids"]),
                "evidence_ids": "|".join(str(item) for item in relation["evidence_ids"]),
            }
        }
        for relation in sorted(relations.values(), key=lambda item: str(item["id"]))
    )
    graph["galaxies"] = galaxies
    graph["projection"].update(
        {"community_projection": COMMUNITY_PROJECTION_VERSION, "view": "universe"}
    )
    return graph


@app.get(
    "/api/v1/graph/subgraph",
    dependencies=[Depends(require_api_access)],
)
def graph_subgraph(
    shared_namespace: str,
    request: Request,
    profile: Annotated[list[str] | None, Query()] = None,
    fact_type: Annotated[list[str] | None, Query()] = None,
    lifecycle: Annotated[list[str] | None, Query()] = None,
    activity: Annotated[list[str] | None, Query()] = None,
    sensitivity: Annotated[list[str] | None, Query()] = None,
    updated_after: datetime | None = None,
    view: Literal["universe", "galaxy"] = "universe",
    galaxy_id: UUID | None = None,
):
    _check_namespace(shared_namespace)
    if view == "galaxy" and galaxy_id is None:
        raise HTTPException(status_code=422, detail="GALAXY_ID_REQUIRED")
    if view == "universe" and galaxy_id is not None:
        raise HTTPException(status_code=422, detail="GALAXY_ID_ONLY_VALID_FOR_GALAXY_VIEW")
    with request.app.state.database.connection() as connection:
        graph = load_graph(
            connection,
            shared_namespace,
            lens=GraphLens(
                profiles=tuple(profile or ()),
                fact_types=tuple(fact_type or ()),
                lifecycle_states=tuple(lifecycle or ()),
                activities=tuple(activity or ()),
                sensitivities=tuple(sensitivity or ()),
                updated_after=updated_after,
            ),
        )
        if view == "galaxy":
            galaxy = get_galaxy(connection, shared_namespace, galaxy_id)
            if galaxy is None:
                raise HTTPException(status_code=404, detail="GALAXY_NOT_FOUND")
            return _galaxy_graph_view(graph, galaxy)
        galaxies = list_galaxies(connection, shared_namespace)
        return _universe_graph_view(graph, galaxies)


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


@app.get(
    "/api/v1/reports/quality",
    response_model=QualityReportResponse,
    dependencies=[Depends(require_api_access)],
)
def quality_report(shared_namespace: str, request: Request):
    _check_namespace(shared_namespace)
    settings = get_settings()
    with request.app.state.database.connection() as connection:
        return build_quality_report(
            connection,
            namespace_key=shared_namespace,
            trusted_tools=settings.trusted_observation_tools,
        )


static_directory = Path(__file__).with_name("static")
if static_directory.is_dir():
    app.mount("/", StaticFiles(directory=static_directory, html=True), name="star-map")


def main() -> None:
    uvicorn.run("agent_memory.api:app", host="0.0.0.0", port=8080, log_level="info")
