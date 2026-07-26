from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, model_validator

EventType = Literal[
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "environment_observation",
    "session_boundary",
]


class IngestEvent(BaseModel):
    type: EventType
    sequence: int = Field(ge=0)
    content: str = Field(default="", max_length=2_000_000)
    tool_name: str | None = Field(default=None, max_length=256)
    arguments: dict[str, Any] | None = None


class ProviderContext(BaseModel):
    shared_namespace: str = Field(min_length=1, max_length=256)
    source_profile: str = Field(min_length=1, max_length=128)
    source_instance: str = Field(min_length=1, max_length=128)
    external_session_id: str = Field(min_length=1, max_length=512)
    external_turn_id: str = Field(min_length=1, max_length=512)
    correlation_id: UUID


class IngestTurnRequest(BaseModel):
    context: ProviderContext
    idempotency_key: str = Field(min_length=8, max_length=512)
    occurred_at: datetime
    events: list[IngestEvent] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_sequence(self):
        values = [(item.sequence, item.type) for item in self.events]
        if len(values) != len(set(values)):
            raise ValueError("event sequence/type pairs must be unique")
        return self


class IngestTurnResponse(BaseModel):
    event_ids: list[UUID]
    job_ids: list[UUID]
    duplicate: bool
    correlation_id: UUID


class RecallBudget(BaseModel):
    max_items: int = Field(default=8, ge=1, le=50)
    max_chars: int = Field(default=4200, ge=100, le=50000)


class RecallRequest(BaseModel):
    context: ProviderContext
    query: str = Field(min_length=1, max_length=10000)
    intent: str = Field(default="conversation", max_length=128)
    budget: RecallBudget = Field(default_factory=RecallBudget)
    scopes: list[str] = Field(default_factory=lambda: ["global", "project", "phase"])
    environment_fingerprint: dict[str, Any] = Field(default_factory=dict)


class RecallItem(BaseModel):
    memory_id: UUID
    kind: str
    text: str
    source_ids: list[UUID]
    source_profile: str
    channels: list[str]
    rrf_score: float
    why_recalled: str
    permission: str = "recall"
    applicability: dict[str, Any] | None = None


class RecallResponse(BaseModel):
    items: list[RecallItem]
    truncated: bool
    correlation_id: UUID


class MemoryBrowseItem(BaseModel):
    memory_id: UUID
    statement: str
    fact_type: str
    state: str
    source_profile: str
    confidence: float
    source_ids: list[UUID]
    extraction_method: str
    updated_at: datetime


class MemoryBrowseResponse(BaseModel):
    items: list[MemoryBrowseItem]
    truncated: bool


class MemoryActionRequest(BaseModel):
    context: ProviderContext
    reason: str = Field(min_length=1, max_length=2000)


class VersionedMemoryActionRequest(MemoryActionRequest):
    expected_version: int = Field(ge=1)


class EpisodeParticipantInput(BaseModel):
    entity_id: UUID | None = None
    subject_id: UUID | None = None
    role: Literal[
        "actor",
        "experiencer",
        "participant",
        "subject",
        "location",
        "object",
        "affected",
        "artifact",
    ]

    @model_validator(mode="after")
    def exactly_one_target(self):
        if (self.entity_id is None) == (self.subject_id is None):
            raise ValueError("participant requires exactly one entity_id or subject_id")
        return self


class EpisodeUpdateRequest(VersionedMemoryActionRequest):
    title: str | None = Field(default=None, min_length=1, max_length=256)
    summary: str | None = Field(default=None, min_length=1, max_length=10000)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    time_precision: Literal["instant", "day", "range", "month", "year", "unknown"] | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=128)
    participants: list[EpisodeParticipantInput] | None = Field(
        default=None, min_length=1, max_length=100
    )

    @model_validator(mode="after")
    def has_episode_change(self):
        fields = self.model_fields_set - {"context", "reason", "expected_version"}
        if not fields:
            raise ValueError("at least one episode field must change")
        if self.started_at and self.ended_at and self.ended_at < self.started_at:
            raise ValueError("episode ended_at must not precede started_at")
        return self


class TemporalRuleUpdateRequest(VersionedMemoryActionRequest):
    label: str | None = Field(default=None, min_length=1, max_length=256)
    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = Field(default=None, ge=1, le=31)
    year: int | None = Field(default=None, ge=1, le=9999)
    timezone: str | None = Field(default=None, min_length=1, max_length=128)
    sensitivity: Literal["normal", "personal", "protected"] | None = None

    @model_validator(mode="after")
    def has_temporal_change(self):
        if not (self.model_fields_set - {"context", "reason", "expected_version"}):
            raise ValueError("at least one temporal rule field must change")
        return self


class ReminderPolicyRequest(VersionedMemoryActionRequest):
    enabled: bool
    lead_days: int = Field(default=0, ge=0, le=365)


class ProcedureStepInput(BaseModel):
    branch_key: str | None = Field(default=None, max_length=128)
    instruction: str = Field(min_length=1, max_length=4000)
    expected_observation: str | None = Field(default=None, max_length=4000)
    success_condition: str | None = Field(default=None, max_length=4000)
    failure_condition: str | None = Field(default=None, max_length=4000)
    stop_condition: str = Field(min_length=1, max_length=4000)
    required_permission: str = Field(default="none", min_length=1, max_length=128)
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"


class ProcedureCreateRequest(MemoryActionRequest):
    title: str = Field(min_length=1, max_length=256)
    goal: str = Field(min_length=1, max_length=2000)
    scope: dict[str, Any] = Field(default_factory=dict)
    preconditions: list[dict[str, Any] | str] = Field(default_factory=list)
    environment_fingerprint: dict[str, Any] = Field(default_factory=dict)
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    episode_id: UUID
    steps: list[ProcedureStepInput] = Field(min_length=1, max_length=100)
    supersedes_procedure_id: UUID | None = None
    expected_superseded_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def complete_supersession_reference(self):
        if (self.supersedes_procedure_id is None) != (
            self.expected_superseded_version is None
        ):
            raise ValueError(
                "supersedes_procedure_id and expected_superseded_version must be provided together"
            )
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("procedure valid_to must be later than valid_from")
        return self


class ArtifactCreateRequest(MemoryActionRequest):
    artifact_type: Literal["change_report", "knowledge_record", "photo_note", "document", "other"]
    title: str = Field(min_length=1, max_length=256)
    reference_uri: str | None = Field(default=None, max_length=2000)
    content_hash: str | None = Field(default=None, max_length=256)
    summary: str = Field(default="", max_length=10000)
    sensitivity: Literal["normal", "personal", "protected"] = "normal"
    episode_id: UUID
    role: Literal["documentation", "evidence", "result", "context"] = "documentation"


class CorrectionRequest(MemoryActionRequest):
    corrected_statement: str = Field(min_length=1, max_length=100000)


class MemoryActionResponse(BaseModel):
    memory_id: UUID
    state: str
    replacement_memory_id: UUID | None = None
    correlation_id: UUID


class PurgeRequest(MemoryActionRequest):
    confirm_memory_id: UUID


class PurgeResponse(BaseModel):
    memory_id: UUID
    state: str
    job_id: UUID
    correlation_id: UUID


class EvidenceTraceItem(BaseModel):
    evidence_id: UUID
    event_type: str
    occurred_at: datetime
    source_profile: str
    source_instance: str
    source_id: UUID
    internal_session_id: UUID
    external_session_id: str
    external_turn_id: str
    support_kind: str
    weight: float
    redacted_payload: dict[str, Any]


class GovernanceTraceItem(BaseModel):
    action: str
    actor_type: str
    actor_id: str
    reason: str | None
    correlation_id: UUID
    metadata_redacted: dict[str, Any]
    created_at: datetime


class MemoryTraceResponse(BaseModel):
    memory_id: UUID
    statement: str
    state: str
    version: int
    supersedes_memory_id: UUID | None
    extraction_method: str | None = None
    extraction_version: str | None = None
    model_name: str | None = None
    evidence_span_start: int | None = None
    evidence_span_end: int | None = None
    evidence: list[EvidenceTraceItem]
    governance: list[GovernanceTraceItem]


class ReviewQueueItem(BaseModel):
    memory_id: UUID
    memory_kind: Literal[
        "fact",
        "episode",
        "preference",
        "relationship",
        "temporal_rule",
        "procedure",
    ] = "fact"
    statement: str
    fact_type: str
    state: str
    source_profile: str
    confidence: float
    evidence_count: int
    updated_at: datetime
    extraction_method: str
    version: int = Field(ge=1)
    review_reasons: list[Literal["candidate", "untrusted_tool"]]
    tool_names: list[str]


class ReviewQueueResponse(BaseModel):
    items: list[ReviewQueueItem]
    total: int
    limit: int
    offset: int
    profiles: list[str]


class BulkMemoryGovernanceTarget(BaseModel):
    memory_id: UUID
    memory_kind: Literal[
        "fact",
        "episode",
        "preference",
        "relationship",
        "temporal_rule",
        "procedure",
    ]
    expected_version: int = Field(ge=1)


class BulkMemoryGovernanceRequest(MemoryActionRequest):
    memory_ids: list[UUID] = Field(default_factory=list, max_length=200)
    targets: list[BulkMemoryGovernanceTarget] = Field(default_factory=list, max_length=200)
    action: Literal["confirm", "forget", "isolate"]
    preview_only: bool = True

    @model_validator(mode="after")
    def unique_memory_ids(self):
        if bool(self.memory_ids) == bool(self.targets):
            raise ValueError("provide exactly one of memory_ids or targets")
        ids = self.memory_ids or [target.memory_id for target in self.targets]
        if len(ids) != len(set(ids)):
            raise ValueError("bulk governance memory ids must be unique")
        return self


class EntityMergeRequest(BaseModel):
    context: ProviderContext
    target_entity_id: UUID
    reason: str = Field(min_length=1, max_length=2000)


class EntitySplitRequest(BaseModel):
    context: ProviderContext
    canonical_name: str = Field(min_length=1, max_length=256)
    entity_type: str = Field(default="other", min_length=1, max_length=64)
    fact_ids: list[UUID] = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=2000)


class EntityGovernanceRequest(BaseModel):
    context: ProviderContext
    reason: str = Field(min_length=1, max_length=2000)


class EntityGovernanceResponse(BaseModel):
    entity_id: UUID
    state: Literal["active", "merged"]
    canonical_entity_id: UUID | None = None
    created_entity_id: UUID | None = None
    affected_fact_count: int = 0
    correlation_id: UUID


class EntityRelationResponse(BaseModel):
    entity_id: UUID
    fact_id: UUID
    state: Literal["attached", "detached"]
    correlation_id: UUID


class SubjectSourceSummary(BaseModel):
    source_id: UUID
    source_profile: str
    source_instance: str
    mapping_origin: Literal["automatic", "manual"]


class SubjectSummary(BaseModel):
    id: UUID
    entity_id: UUID
    kind: Literal["user", "profile_persona"]
    stable_key: str
    display_name: str
    display_name_origin: Literal["default", "source", "manual"]
    color: str
    status: Literal["active", "hidden"]
    created_at: datetime
    updated_at: datetime
    sources: list[SubjectSourceSummary]


class SubjectUpdateRequest(BaseModel):
    context: ProviderContext
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    status: Literal["active", "hidden"] | None = None
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def has_change(self):
        if self.display_name is None and self.color is None and self.status is None:
            raise ValueError("at least one subject field must change")
        return self


class SubjectSourceMappingRequest(BaseModel):
    context: ProviderContext
    reason: str = Field(min_length=1, max_length=2000)


class GalaxyCreateRequest(BaseModel):
    context: ProviderContext
    display_name: str = Field(min_length=1, max_length=128)
    family: str = Field(default="manual", min_length=1, max_length=64)
    entity_ids: list[UUID] = Field(min_length=3, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def unique_entities(self):
        if len(self.entity_ids) != len(set(self.entity_ids)):
            raise ValueError("galaxy entity ids must be unique")
        return self


class GalaxyUpdateRequest(BaseModel):
    context: ProviderContext
    expected_version: int = Field(ge=1)
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    visibility: Literal["visible", "hidden"] | None = None
    manual_locked: bool | None = None
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def has_change(self):
        if self.display_name is None and self.visibility is None and self.manual_locked is None:
            raise ValueError("at least one galaxy field must change")
        return self


class GalaxyMembershipRequest(BaseModel):
    context: ProviderContext
    expected_version: int = Field(ge=1)
    action: Literal["fixed", "excluded", "automatic"]
    role: Literal["core", "bridge", "satellite", "member"] = "member"
    membership_kind: Literal["primary", "secondary"] = "secondary"
    reason: str = Field(min_length=1, max_length=2000)


class GalaxyRebuildRequest(BaseModel):
    context: ProviderContext
    reason: str = Field(min_length=1, max_length=2000)


class GalaxyUndoRequest(BaseModel):
    context: ProviderContext
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)


class LayoutPreferenceRequest(BaseModel):
    context: ProviderContext
    scope_kind: Literal["universe", "galaxy"]
    scope_id: UUID
    target_kind: Literal["camera", "entity", "galaxy", "subject"]
    target_id: UUID
    position: dict[str, float] = Field(default_factory=dict)
    zoom: float | None = Field(default=None, ge=0.05, le=20)
    motion_enabled: bool | None = None
    pinned: bool = False
    expected_version: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_position(self):
        if set(self.position) - {"x", "y"}:
            raise ValueError("layout position only accepts x and y")
        if any(abs(value) > 1_000_000 for value in self.position.values()):
            raise ValueError("layout position is outside supported bounds")
        return self


class QualityReportResponse(BaseModel):
    namespace: str
    generated_at: datetime
    automatic_ready: bool
    promotion_ready: bool
    manual_review_required: bool
    gates: dict[str, bool]
    metrics: dict[str, int | float | None]
    classifications: dict[str, int]
    decision: Literal["AUTOMATIC_GATES_FAILED", "MANUAL_REVIEW_REQUIRED"]


class VaultEntryCreate(BaseModel):
    context: ProviderContext
    kind: str = Field(min_length=1, max_length=128)
    display_label: str = Field(min_length=1, max_length=256)
    redacted_hint: str = Field(min_length=1, max_length=512)
    secret_value: SecretStr = Field(min_length=1, max_length=100000)
    linked_memory_id: UUID | None = None


class VaultEntrySummary(BaseModel):
    id: UUID
    kind: str
    display_label: str
    redacted_hint: str
    status: str
    created_at: datetime
    updated_at: datetime


class VaultEntryCreated(BaseModel):
    entry_id: UUID
    correlation_id: UUID


class VaultEntryMetadataUpdate(BaseModel):
    context: ProviderContext
    display_label: str = Field(min_length=1, max_length=256)
    redacted_hint: str = Field(min_length=1, max_length=512)
    password: SecretStr = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=2000)


class VaultEntryRevealRequest(BaseModel):
    context: ProviderContext
    password: SecretStr = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=2000)


class VaultEntryRevealResponse(BaseModel):
    entry_id: UUID
    secret_value: str
    correlation_id: UUID


class VaultEntryReplaceRequest(BaseModel):
    context: ProviderContext
    secret_value: SecretStr = Field(min_length=1, max_length=100000)
    password: SecretStr = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=2000)


class VaultEntryStatusRequest(BaseModel):
    context: ProviderContext
    status: Literal["active", "disabled"]
    password: SecretStr = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=2000)


class VaultEntryDeleteRequest(BaseModel):
    context: ProviderContext
    confirm_entry_id: UUID
    password: SecretStr = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=2000)


class VaultEntryActionResponse(BaseModel):
    entry_id: UUID
    state: str
    correlation_id: UUID


class VaultGrantCreate(BaseModel):
    context: ProviderContext
    operation: Literal["reveal_to_model"]
    target_profile: str = Field(min_length=1, max_length=128)
    expires_at: datetime
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_expiry(self):
        now = datetime.now(UTC)
        expires_at = self.expires_at.astimezone(UTC)
        if expires_at <= now:
            raise ValueError("grant expiry must be in the future")
        if expires_at > now + timedelta(hours=24):
            raise ValueError("V1 Vault grants may not exceed 24 hours")
        return self


class VaultGrantResponse(BaseModel):
    grant_id: UUID
    entry_id: UUID
    operation: str
    target_profile: str
    expires_at: datetime
    correlation_id: UUID


class VaultGrantSummary(BaseModel):
    id: UUID
    entry_id: UUID
    display_label: str
    operation: str
    target_profile: str
    expires_at: datetime
    created_at: datetime


class VaultAccessRequest(BaseModel):
    context: ProviderContext
    entry_id: UUID
    operation: Literal["reveal_to_model"]


class VaultAccessResponse(BaseModel):
    authorized: bool
    entry_id: UUID
    grant_id: UUID
    secret_value: str
    correlation_id: UUID


class UiLoginRequest(BaseModel):
    password: SecretStr = Field(min_length=1, max_length=1000)


class UiLoginResponse(BaseModel):
    authenticated: bool


class UiConfigResponse(BaseModel):
    namespace: str
    namespace_id: UUID
    version: str


class CurrentStateRequest(BaseModel):
    context: ProviderContext
    action: Literal["set", "update", "resolve"]
    topic_key: str = Field(min_length=1, max_length=256)
    summary: str | None = Field(default=None, max_length=4000)
    expires_at: datetime | None = None
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_state_action(self):
        if self.action in {"set", "update"}:
            if not self.summary or self.expires_at is None:
                raise ValueError("set/update require summary and expires_at")
            now = datetime.now(UTC)
            expiry = self.expires_at.astimezone(UTC)
            if not now < expiry <= now + timedelta(days=365):
                raise ValueError("state expiry must be within 365 days")
        return self


class StateConfigRequest(BaseModel):
    context: ProviderContext
    enabled: bool = True
    drift_hours: int = Field(default=72, ge=1, le=720)
    axes_initial: dict[str, float]
    axis_labels: dict[str, str]
    axis_ranges: dict[str, dict[str, float]]
    axis_enabled: dict[str, bool]
    thresholds: dict[str, float]
    profile_overrides: dict[str, dict] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_state_config(self):
        axes = {"interaction_need", "restraint", "valence", "arousal", "immersion"}
        thresholds = {"immersion_focus", "arousal_risk", "interaction_prompt"}
        axis_fields = (
            self.axes_initial,
            self.axis_labels,
            self.axis_ranges,
            self.axis_enabled,
        )
        if any(set(field) != axes for field in axis_fields) or set(self.thresholds) != thresholds:
            raise ValueError("state axes or thresholds are incomplete")
        if any(not label.strip() or len(label) > 64 for label in self.axis_labels.values()):
            raise ValueError("state axis labels must be 1 to 64 characters")
        for key, axis_range in self.axis_ranges.items():
            if set(axis_range) != {"min", "max"}:
                raise ValueError("state axis ranges require min and max")
            minimum, maximum = axis_range["min"], axis_range["max"]
            if not 0 <= minimum < maximum <= 1:
                raise ValueError("state axis ranges must be ordered within 0 and 1")
            if not minimum <= self.axes_initial[key] <= maximum:
                raise ValueError("state initial values must be within axis ranges")
        values = list(self.axes_initial.values()) + list(self.thresholds.values())
        if any(not 0 <= value <= 1 for value in values):
            raise ValueError("state values must be between 0 and 1")
        for override in self.profile_overrides.values():
            allowed = {
                "enabled",
                "drift_hours",
                "axes_initial",
                "axis_labels",
                "axis_ranges",
                "axis_enabled",
                "thresholds",
            }
            if not set(override) <= allowed:
                raise ValueError("unsupported profile state override")
            override_axes = override.get("axes_initial", self.axes_initial)
            override_labels = override.get("axis_labels", self.axis_labels)
            override_ranges = override.get("axis_ranges", self.axis_ranges)
            override_enabled = override.get("axis_enabled", self.axis_enabled)
            override_thresholds = override.get("thresholds", self.thresholds)
            profile_axis_fields = (
                override_axes,
                override_labels,
                override_ranges,
                override_enabled,
            )
            if (
                any(set(field) != axes for field in profile_axis_fields)
                or set(override_thresholds) != thresholds
            ):
                raise ValueError("profile state override is incomplete")
            override_values = list(override_axes.values()) + list(override_thresholds.values())
            if any(not 0 <= value <= 1 for value in override_values):
                raise ValueError("profile state values must be between 0 and 1")
            for key, axis_range in override_ranges.items():
                minimum, maximum = axis_range.get("min"), axis_range.get("max")
                if minimum is None or maximum is None or not 0 <= minimum < maximum <= 1:
                    raise ValueError("profile state ranges are invalid")
                if not minimum <= override_axes[key] <= maximum:
                    raise ValueError("profile state initial value is outside its range")
            if not 1 <= override.get("drift_hours", self.drift_hours) <= 720:
                raise ValueError("profile state drift must be between 1 and 720 hours")
        return self


class StateResetRequest(BaseModel):
    context: ProviderContext
    reason: str = Field(min_length=1, max_length=2000)


class StateSimulationRequest(BaseModel):
    context: ProviderContext
    event_type: Literal["user_message", "tool_result", "environment_observation"]
    content: str = Field(min_length=1, max_length=10000)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
