"""Evidence-linked unified episodic, temporal, preference, and procedural memory."""

import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import Connection
from psycopg.rows import dict_row

from .config import get_settings
from .embeddings import EMBEDDING_VERSION, deterministic_embedding, vector_literal
from .ids import new_uuid, stable_uuid
from .redaction import redact_text

EXTRACTOR_VERSION = "unified-memory-v1"
USER_TIMEZONE = ZoneInfo("Asia/Shanghai")

DATE_RANGE_PATTERN = re.compile(
    r"(?:(?P<year>\d{4})年)?(?P<start_month>\d{1,2})月(?P<start_day>\d{1,2})日?"
    r"\s*(?:到|至|[-–—])\s*"
    r"(?:(?P<end_month>\d{1,2})月)?(?P<end_day>\d{1,2})日?"
)
SINGLE_DATE_PATTERN = re.compile(r"(?:(?P<year>\d{4})年)?(?P<month>\d{1,2})月(?P<day>\d{1,2})日")
TRAVEL_LOCATION_PATTERN = re.compile(
    r"(?:(?<![过失])去了|去到|前往)"
    r"(?P<name>[\u4e00-\u9fffA-Za-z0-9·_-]{2,24}?)"
    r"(?:旅游|旅行|出差|，|,|。|；|;|$)"
)
INVALID_TRAVEL_LOCATION_PATTERN = re.compile(
    r"(?:小时|分钟|秒钟|再试|试试|重试|过去|多久|多长时间|时候|现在|然后|以后|之前|之后)"
)
ENCOUNTER_PATTERN = re.compile(
    r"(?:遇到|遇见|见到)了?(?:(?P<relation>大学同学|同学|朋友|同事))?"
    r"(?P<name>小\s*[A-Za-zＡ-Ｚａ-ｚ]|[\u4e00-\u9fff]{2,8})(?:，|,|。|；|;|$)"
)
FIRST_VISIT_PATTERN = re.compile(
    r"第一次(?:去|参观)(?P<name>[\u4e00-\u9fffA-Za-z0-9·_-]{2,32}?)(?:看|参观|，|,|。|；|;|$)"
)
SAW_PATTERN = re.compile(
    r"(?:看到|看了|看见了)(?P<name>[\u4e00-\u9fffA-Za-z0-9·_-]{1,20}?)(?:，|,|。|；|;|$)"
)
PREFERENCE_PATTERN = re.compile(
    r"(?:我|本人)(?P<negation>不再|不|不要)?"
    r"(?P<verb>喜欢|偏好|更喜欢|避免|要求|必须)"
    r"(?P<topic>[^，。；;\n]{1,80})"
)
TEMPORAL_PATTERN = re.compile(
    r"(?P<label>我的生日|生日|纪念日|结婚纪念日)"
    r"(?:是|为|在|：|:)\s*(?:(?P<year>\d{4})年)?"
    r"(?P<month>\d{1,2})月(?P<day>\d{1,2})日?"
)
TECHNICAL_PATTERN = re.compile(
    r"(?:故障|异常|排查|修复|部署|迁移|无法连接|连接失败|服务不可用|数据库连接)",
    re.IGNORECASE,
)
TECH_ENTITY_PATTERN = re.compile(
    r"\b(?:n8n|mysql|mariadb|postgresql|postgres|docker|next\s*terminal|vps|"
    r"host[a-z0-9_-]+|redis|nginx|tailscale)\b",
    re.IGNORECASE,
)
REPORT_PATTERN = re.compile(
    r"(?:变更报告|知识库记录|复盘报告|处理报告)(?:为|是|：|:)?\s*([^，。；;\n]{0,120})"
)
REFERENCE_URI_PATTERN = re.compile(r"(?:https?://|kb://|knowledge://)[^\s，。；;]{1,500}", re.I)
CONTENT_HASH_PATTERN = re.compile(r"\b(?:sha256:)?[0-9a-f]{64}\b", re.I)


@dataclass(frozen=True)
class ParsedEntity:
    name: str
    entity_type: str
    role: str
    relationship_type: str | None = None


@dataclass(frozen=True)
class ParsedStep:
    kind: str
    summary: str


@dataclass(frozen=True)
class EpisodeCandidate:
    episode_type: str
    title: str
    summary: str
    started_at: datetime | None
    ended_at: datetime | None
    time_precision: str
    timezone: str
    time_resolution: dict
    entities: tuple[ParsedEntity, ...]
    steps: tuple[ParsedStep, ...]
    confidence: float
    accepted: bool


@dataclass(frozen=True)
class PreferenceCandidate:
    aspect: str
    polarity: str
    topic: str
    strength: float


@dataclass(frozen=True)
class TemporalRuleCandidate:
    rule_type: str
    label: str
    month: int
    day: int
    year: int | None


def _date_at(
    year: int,
    month: int,
    day: int,
    *,
    timezone=USER_TIMEZONE,
) -> datetime | None:
    try:
        return datetime(year, month, day, tzinfo=timezone)
    except ValueError:
        return None


def parse_date_range(
    text: str, occurred_at: datetime
) -> tuple[datetime | None, datetime | None, str, dict]:
    reference_local = occurred_at.astimezone(USER_TIMEZONE)
    match = DATE_RANGE_PATTERN.search(text)
    if match:
        explicit_year = match.group("year")
        year = int(explicit_year or reference_local.year)
        start_month = int(match.group("start_month"))
        end_month = int(match.group("end_month") or start_month)
        start = _date_at(year, start_month, int(match.group("start_day")))
        end = _date_at(year, end_month, int(match.group("end_day")))
        crossed_year = False
        if start and end and end < start and end_month < start_month:
            end = _date_at(year + 1, end_month, int(match.group("end_day")))
            crossed_year = True
        if not start or not end or end < start:
            return None, None, "unknown", {"invalid_source_text": True}
        return (
            start,
            end,
            "range",
            {
                "source": "explicit_range",
                "year_explicit": bool(explicit_year),
                "crossed_year": crossed_year,
                "reference_occurred_at": occurred_at.isoformat(),
            },
        )
    single = SINGLE_DATE_PATTERN.search(text)
    if single:
        explicit_year = single.group("year")
        value = _date_at(
            int(explicit_year or reference_local.year),
            int(single.group("month")),
            int(single.group("day")),
        )
        if value is None:
            return None, None, "unknown", {"invalid_source_text": True}
        return (
            value,
            value,
            "day",
            {
                "source": "explicit_day",
                "year_explicit": bool(explicit_year),
                "reference_occurred_at": occurred_at.isoformat(),
            },
        )
    relative_days = {"今天": 0, "昨天": -1, "前天": -2}
    for label, offset in relative_days.items():
        if label in text:
            value = datetime.combine(
                (reference_local + timedelta(days=offset)).date(),
                datetime.min.time(),
                tzinfo=USER_TIMEZONE,
            )
            return (
                value,
                value,
                "day",
                {
                    "source": "relative_day",
                    "expression": label,
                    "reference_occurred_at": occurred_at.isoformat(),
                },
            )
    last_year_month = re.search(r"去年(?P<month>\d{1,2})月", text)
    if last_year_month:
        month = int(last_year_month.group("month"))
        start = _date_at(reference_local.year - 1, month, 1)
        end = _date_at(reference_local.year - 1 + (month == 12), month % 12 + 1, 1)
        if start and end:
            return (
                start,
                end - timedelta(microseconds=1),
                "month",
                {
                    "source": "relative_month",
                    "expression": last_year_month.group(0),
                    "reference_occurred_at": occurred_at.isoformat(),
                },
            )
    if "去年" in text:
        year = reference_local.year - 1
        return (
            _date_at(year, 1, 1),
            _date_at(year + 1, 1, 1) - timedelta(microseconds=1),
            "year",
            {
                "source": "relative_year",
                "expression": "去年",
                "reference_occurred_at": occurred_at.isoformat(),
            },
        )
    fuzzy_month = re.search(
        r"(?:(?P<year>\d{4})年)?(?P<month>\d{1,2})月"
        r"(?P<period>月初|上旬|中旬|下旬|月底)",
        text,
    )
    if fuzzy_month:
        explicit_year = fuzzy_month.group("year")
        year = int(explicit_year or reference_local.year)
        month = int(fuzzy_month.group("month"))
        period = fuzzy_month.group("period")
        ranges = {
            "月初": (1, 5),
            "上旬": (1, 10),
            "中旬": (11, 20),
            "下旬": (21, None),
            "月底": (25, None),
        }
        start_day, end_day = ranges[period]
        start = _date_at(year, month, start_day)
        next_month = _date_at(year + (month == 12), month % 12 + 1, 1)
        end = (
            _date_at(year, month, end_day)
            if end_day is not None
            else next_month - timedelta(microseconds=1)
            if next_month
            else None
        )
        if start and end:
            return (
                start,
                end,
                "range",
                {
                    "source": "fuzzy_month_period",
                    "expression": fuzzy_month.group(0),
                    "year_explicit": bool(explicit_year),
                    "fuzzy": True,
                    "reference_occurred_at": occurred_at.isoformat(),
                },
            )
    return None, None, "unknown", {}


def _dedupe_entities(values: list[ParsedEntity]) -> tuple[ParsedEntity, ...]:
    seen: set[tuple[str, str]] = set()
    result: list[ParsedEntity] = []
    for value in values:
        key = (" ".join(value.name.split()).casefold(), value.role)
        if not value.name.strip() or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


def parse_episode(text: str, occurred_at: datetime) -> EpisodeCandidate | None:
    redacted = redact_text(text).text.strip()
    if not redacted:
        return None
    started_at, ended_at, precision, resolution = parse_date_range(redacted, occurred_at)
    location_match = TRAVEL_LOCATION_PATTERN.search(redacted)
    encounter_matches = list(ENCOUNTER_PATTERN.finditer(redacted))
    first_matches = list(FIRST_VISIT_PATTERN.finditer(redacted))
    saw_matches = list(SAW_PATTERN.finditer(redacted))
    if location_match:
        location = location_match.group("name").strip()
        if INVALID_TRAVEL_LOCATION_PATTERN.search(location):
            location_match = None
    if location_match:
        location = location_match.group("name").strip()
        entities = [ParsedEntity(location, "location", "location")]
        steps = [ParsedStep("action", location_match.group(0).rstrip("，,。；;"))]
        for match in encounter_matches:
            name = re.sub(r"\s+", "", match.group("name"))
            relationship_type = {
                "大学同学": "university_classmate",
                "同学": "classmate",
                "朋友": "friend",
                "同事": "colleague",
            }.get(match.group("relation") or "")
            entities.append(ParsedEntity(name, "person", "participant", relationship_type))
            steps.append(ParsedStep("encounter", match.group(0).rstrip("，,。；;")))
        for match in first_matches:
            name = match.group("name").strip()
            entities.append(ParsedEntity(name, "location", "object"))
            steps.append(ParsedStep("milestone", match.group(0).rstrip("，,。；;")))
        for match in saw_matches:
            name = match.group("name").strip()
            entities.append(ParsedEntity(name, "concept", "object"))
            steps.append(ParsedStep("observation", match.group(0).rstrip("，,。；;")))
        summary = redacted[:1000]
        explicit_time = resolution.get("source") in {"explicit_range", "explicit_day"}
        return EpisodeCandidate(
            episode_type="travel",
            title=f"旅行 · {location}",
            summary=summary,
            started_at=started_at,
            ended_at=ended_at,
            time_precision=precision,
            timezone="Asia/Shanghai",
            time_resolution=resolution,
            entities=_dedupe_entities(entities),
            steps=tuple(steps),
            confidence=0.92 if precision == "range" else 0.86 if explicit_time else 0.72,
            accepted=explicit_time,
        )
    if not TECHNICAL_PATTERN.search(redacted):
        return None
    entities = [
        ParsedEntity(match.group(0), "service", "affected")
        for match in TECH_ENTITY_PATTERN.finditer(redacted)
    ]
    step_kind = (
        "resolution"
        if re.search(r"(?:已修复|修复完成|恢复正常|解决)", redacted)
        else "verification"
        if re.search(r"(?:验证通过|确认正常|测试通过)", redacted)
        else "hypothesis"
        if re.search(r"(?:怀疑|可能|假设)", redacted)
        else "action"
    )
    title_entity = entities[0].name if entities else "协作任务"
    return EpisodeCandidate(
        episode_type="technical",
        title=f"技术情节 · {title_entity}",
        summary=redacted[:1000],
        started_at=occurred_at,
        ended_at=occurred_at,
        time_precision="instant",
        timezone="UTC",
        time_resolution={"source": "event_occurred_at"},
        entities=_dedupe_entities(entities),
        steps=(ParsedStep(step_kind, redacted[:1000]),),
        confidence=0.72,
        accepted=False,
    )


def parse_preference(text: str) -> PreferenceCandidate | None:
    match = PREFERENCE_PATTERN.search(redact_text(text).text)
    if not match:
        return None
    topic = match.group("topic").strip()
    if not topic or topic.endswith(("吗", "么", "呢", "?")):
        return None
    verb = match.group("verb")
    negated = bool(match.group("negation"))
    if verb in {"避免"} or negated:
        polarity = "avoid" if verb in {"避免", "要求", "必须"} else "dislike"
    elif verb in {"要求", "必须"}:
        polarity = "require"
    elif verb in {"偏好", "更喜欢"}:
        polarity = "prefer"
    else:
        polarity = "like"
    return PreferenceCandidate(
        aspect=topic,
        polarity=polarity,
        topic=topic,
        strength=0.85 if verb in {"必须", "要求"} else 0.75,
    )


def parse_temporal_rule(text: str) -> TemporalRuleCandidate | None:
    match = TEMPORAL_PATTERN.search(redact_text(text).text)
    if not match:
        return None
    month = int(match.group("month"))
    day = int(match.group("day"))
    if _date_at(int(match.group("year") or 2000), month, day) is None:
        return None
    label = match.group("label")
    return TemporalRuleCandidate(
        rule_type="birthday" if "生日" in label and "纪念" not in label else "anniversary",
        label=label,
        month=month,
        day=day,
        year=int(match.group("year")) if match.group("year") else None,
    )


def _ensure_entity(connection: Connection, namespace_id: UUID, name: str, entity_type: str) -> UUID:
    normalized = " ".join(name.split()).strip().casefold()
    entity_id = stable_uuid("unified-entity", f"{namespace_id}:{normalized}")
    row = connection.execute(
        """INSERT INTO memory.entities(
             id,namespace_id,entity_type,canonical_name,normalized_name
           ) VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT(namespace_id,normalized_name) DO UPDATE
             SET entity_type=CASE
                   WHEN memory.entities.entity_type IN ('unknown','other','concept')
                   THEN excluded.entity_type ELSE memory.entities.entity_type END,
                 updated_at=now()
           RETURNING id""",
        (entity_id, namespace_id, entity_type, name.strip(), normalized),
    ).fetchone()
    return row[0]


def _upsert_document(
    connection: Connection,
    namespace_id: UUID,
    source_kind: str,
    source_id: UUID,
    text: str,
    lifecycle_state: str,
) -> None:
    sanitized = redact_text(text).text
    connection.execute(
        """INSERT INTO retrieval.documents(
             id,namespace_id,source_kind,source_id,text_redacted,embedding,
             embedding_model_version,lifecycle_state
           ) VALUES (%s,%s,%s,%s,%s,%s::vector,%s,%s)
           ON CONFLICT(source_kind,source_id) DO UPDATE SET
             text_redacted=excluded.text_redacted,
             embedding=excluded.embedding,
             embedding_model_version=excluded.embedding_model_version,
             lifecycle_state=excluded.lifecycle_state,indexed_at=now()""",
        (
            stable_uuid("document", f"{source_kind}:{source_id}"),
            namespace_id,
            source_kind,
            source_id,
            sanitized,
            vector_literal(deterministic_embedding(sanitized)),
            EMBEDDING_VERSION,
            lifecycle_state,
        ),
    )


def _linked_fact_id(connection: Connection, event_id: UUID) -> UUID | None:
    row = connection.execute(
        """SELECT f.id FROM memory.fact_evidence link
           JOIN memory.facts f ON f.id=link.fact_id
           WHERE link.event_id=%s
           ORDER BY (f.memory_state='active') DESC,f.confidence DESC,f.created_at
           LIMIT 1""",
        (event_id,),
    ).fetchone()
    return row[0] if row else None


def _model_admitted_fact_state(
    connection: Connection, fact_id: UUID | None
) -> str | None:
    if fact_id is None:
        return None
    row = connection.execute(
        """SELECT memory_state FROM memory.facts
           WHERE id=%s AND extraction_version LIKE 'atomic-admission-%%'""",
        (fact_id,),
    ).fetchone()
    return row[0] if row else None


def _store_episode(
    connection: Connection,
    *,
    namespace_id: UUID,
    turn_id: UUID,
    event_id: UUID,
    event_index: int,
    candidate: EpisodeCandidate,
    user_subject_id: UUID,
    fact_id: UUID | None,
) -> UUID:
    episode_id = stable_uuid(
        "unified-episode", f"{namespace_id}:{turn_id}:{event_id}:{event_index}"
    )
    existing = connection.execute(
        """SELECT origin,version,review_state FROM memory.episodes WHERE id=%s""",
        (episode_id,),
    ).fetchone()
    if existing and not (
        existing[0] == "automatic"
        and int(existing[1]) == 1
        and existing[2] == "candidate"
    ):
        # Reprocessing may promote an untouched automatic candidate, but it must
        # never rewrite participants, steps, or state after user governance.
        return episode_id
    entity_ids: list[tuple[UUID, ParsedEntity]] = [
        (
            _ensure_entity(connection, namespace_id, entity.name, entity.entity_type),
            entity,
        )
        for entity in candidate.entities
    ]
    anchor_entity_id = entity_ids[0][0] if entity_ids else None
    state = "active" if candidate.accepted else "candidate"
    review_state = "accepted" if candidate.accepted else "candidate"
    connection.execute(
        """INSERT INTO memory.episodes(
             id,namespace_id,entity_id,title,summary,state,episode_type,
             started_at,ended_at,time_precision,timezone,time_resolution,
             importance,review_state,origin,extractor_version,confidence
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,
                     'automatic',%s,%s)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title,summary=excluded.summary,
             state=excluded.state,review_state=excluded.review_state,
             started_at=excluded.started_at,ended_at=excluded.ended_at,
             time_precision=excluded.time_precision,timezone=excluded.timezone,
             time_resolution=excluded.time_resolution,
             confidence=excluded.confidence,updated_at=now()
           WHERE memory.episodes.origin='automatic'
             AND memory.episodes.version=1
             AND memory.episodes.review_state='candidate'""",
        (
            episode_id,
            namespace_id,
            anchor_entity_id,
            candidate.title,
            candidate.summary,
            state,
            candidate.episode_type,
            candidate.started_at,
            candidate.ended_at,
            candidate.time_precision,
            candidate.timezone,
            json.dumps(candidate.time_resolution),
            0.65,
            review_state,
            EXTRACTOR_VERSION,
            candidate.confidence,
        ),
    )
    if fact_id:
        connection.execute(
            """INSERT INTO memory.episode_facts(episode_id,fact_id)
               VALUES (%s,%s) ON CONFLICT DO NOTHING""",
            (episode_id, fact_id),
        )
    connection.execute(
        """INSERT INTO memory.episode_entities(
             episode_id,subject_id,role,fact_id,confidence,origin
           ) VALUES (%s,%s,'experiencer',%s,%s,'automatic')
           ON CONFLICT DO NOTHING""",
        (episode_id, user_subject_id, fact_id, candidate.confidence),
    )
    for entity_id, entity in entity_ids:
        connection.execute(
            """INSERT INTO memory.episode_entities(
                 episode_id,entity_id,role,fact_id,confidence,origin
               ) VALUES (%s,%s,%s,%s,%s,'automatic')
               ON CONFLICT DO NOTHING""",
            (episode_id, entity_id, entity.role, fact_id, candidate.confidence),
        )
        if entity.relationship_type:
            relationship_state = "active" if candidate.accepted else "candidate"
            relationship_id = stable_uuid(
                "relationship",
                (
                    f"{namespace_id}:{user_subject_id}:{entity_id}:"
                    f"{entity.relationship_type}:{event_id}"
                ),
            )
            effective_relationship_state = connection.execute(
                """INSERT INTO memory.relationship_assertions(
                     id,namespace_id,subject_id,related_entity_id,relation_type,label,
                     valid_from,fact_id,episode_id,state
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(id) DO UPDATE SET
                     fact_id=excluded.fact_id,episode_id=excluded.episode_id,
                     state=CASE
                       WHEN memory.relationship_assertions.state='candidate'
                       THEN excluded.state
                       ELSE memory.relationship_assertions.state
                     END,
                     updated_at=now()
                   RETURNING state""",
                (
                    relationship_id,
                    namespace_id,
                    user_subject_id,
                    entity_id,
                    entity.relationship_type,
                    match_label(entity.relationship_type),
                    candidate.started_at,
                    fact_id,
                    episode_id,
                    relationship_state,
                ),
            ).fetchone()[0]
            _upsert_document(
                connection,
                namespace_id,
                "relationship",
                relationship_id,
                (
                    f"User 与 {entity.name} 的关系：{match_label(entity.relationship_type)}"
                    f" · {candidate.summary}"
                ),
                effective_relationship_state,
            )
    for sequence, step in enumerate(candidate.steps):
        step_id = stable_uuid("episode-step", f"{episode_id}:{sequence}")
        connection.execute(
            """INSERT INTO memory.episode_steps(
                 id,episode_id,sequence_no,step_kind,summary,occurred_at,status,
                 fact_id,evidence_event_id,confidence
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(episode_id,sequence_no) DO UPDATE SET
                 step_kind=excluded.step_kind,summary=excluded.summary,
                 occurred_at=excluded.occurred_at,fact_id=excluded.fact_id,
                 evidence_event_id=excluded.evidence_event_id,
                 status=excluded.status,confidence=excluded.confidence,updated_at=now()""",
            (
                step_id,
                episode_id,
                sequence,
                step.kind,
                step.summary,
                candidate.started_at,
                "confirmed" if candidate.accepted else "candidate",
                fact_id,
                event_id,
                candidate.confidence,
            ),
        )
    search_text = " · ".join(
        [
            candidate.title,
            candidate.summary,
            *(entity.name for entity in candidate.entities),
            *(step.summary for step in candidate.steps),
        ]
    )
    _upsert_document(connection, namespace_id, "episode", episode_id, search_text, state)
    return episode_id


def _append_episode_step(
    connection: Connection,
    *,
    episode_id: UUID,
    event_id: UUID,
    occurred_at: datetime,
    content: str,
    fact_id: UUID | None,
) -> None:
    summary = redact_text(content).text.strip()[:1000]
    if not summary:
        return
    kind = _classify_step(summary, default="observation")
    sequence = connection.execute(
        """SELECT COALESCE(max(sequence_no),-1)+1
           FROM memory.episode_steps WHERE episode_id=%s""",
        (episode_id,),
    ).fetchone()[0]
    connection.execute(
        """INSERT INTO memory.episode_steps(
             id,episode_id,sequence_no,step_kind,summary,occurred_at,status,
             fact_id,evidence_event_id,confidence
           ) VALUES (%s,%s,%s,%s,%s,%s,'confirmed',%s,%s,0.95)
           ON CONFLICT(episode_id,sequence_no) DO NOTHING""",
        (
            stable_uuid("episode-step", f"{episode_id}:{sequence}"),
            episode_id,
            sequence,
            kind,
            summary,
            occurred_at,
            fact_id,
            event_id,
        ),
    )


def _classify_step(summary: str, *, default: str = "observation") -> str:
    if re.search(r"(?:验证通过|测试通过|恢复正常|healthy|health passed|verified)", summary, re.I):
        return "verification"
    if re.search(r"(?:根因|原因为|原因是|caused by|root cause)", summary, re.I):
        return "cause"
    if re.search(r"(?:已修复|修复完成|已解决|fixed|resolved)", summary, re.I):
        return "resolution"
    if re.search(r"(?:结果|发现|确认|result)", summary, re.I):
        return "result"
    if re.search(r"(?:怀疑|可能|假设|suspect|hypothesis)", summary, re.I):
        return "hypothesis"
    if re.search(r"(?:决定|选择|改为|decision)", summary, re.I):
        return "decision"
    return default


def _append_candidate_step(
    connection: Connection,
    *,
    episode_id: UUID,
    event_id: UUID,
    occurred_at: datetime,
    content: str,
    fact_id: UUID | None,
    default_kind: str,
    status: str,
) -> None:
    summary = redact_text(content).text.strip()[:1000]
    if not summary:
        return
    sequence = connection.execute(
        """SELECT COALESCE(max(sequence_no),-1)+1
           FROM memory.episode_steps WHERE episode_id=%s""",
        (episode_id,),
    ).fetchone()[0]
    connection.execute(
        """INSERT INTO memory.episode_steps(
             id,episode_id,sequence_no,step_kind,summary,occurred_at,status,
             fact_id,evidence_event_id,confidence
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(episode_id,sequence_no) DO NOTHING""",
        (
            stable_uuid("episode-step", f"{episode_id}:{sequence}"),
            episode_id,
            sequence,
            _classify_step(summary, default=default_kind),
            summary,
            occurred_at,
            status,
            fact_id,
            event_id,
            0.8 if status == "observed" else 0.6,
        ),
    )


def _attach_episode_subject(
    connection: Connection,
    *,
    episode_id: UUID,
    subject_id: UUID | None,
    role: str,
    fact_id: UUID | None,
    confidence: float,
) -> None:
    if subject_id is None:
        return
    connection.execute(
        """INSERT INTO memory.episode_entities(
             episode_id,subject_id,role,fact_id,confidence,origin
           ) VALUES (%s,%s,%s,%s,%s,'automatic')
           ON CONFLICT DO NOTHING""",
        (episode_id, subject_id, role, fact_id, confidence),
    )


def _store_report_artifact(
    connection: Connection,
    *,
    namespace_id: UUID,
    episode_id: UUID,
    event_id: UUID,
    content: str,
    state: str,
) -> UUID | None:
    report = REPORT_PATTERN.search(redact_text(content).text)
    if not report:
        return None
    reference = REFERENCE_URI_PATTERN.search(report.group(0))
    content_hash = CONTENT_HASH_PATTERN.search(report.group(0))
    artifact_id = stable_uuid("artifact", f"{namespace_id}:{episode_id}:{event_id}:report")
    title = report.group(1).strip() or "变更报告"
    connection.execute(
        """INSERT INTO memory.artifacts(
             id,namespace_id,artifact_type,title,reference_uri,content_hash,
             summary_redacted,sensitivity,state
           ) VALUES (%s,%s,'change_report',%s,%s,%s,%s,'normal',%s)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title,reference_uri=excluded.reference_uri,
             content_hash=excluded.content_hash,
             summary_redacted=excluded.summary_redacted,state=excluded.state,
             updated_at=now()""",
        (
            artifact_id,
            namespace_id,
            title[:256],
            reference.group(0) if reference else None,
            content_hash.group(0) if content_hash else None,
            report.group(0)[:1000],
            state,
        ),
    )
    connection.execute(
        """INSERT INTO memory.episode_artifacts(episode_id,artifact_id,role)
           VALUES (%s,%s,'documentation') ON CONFLICT DO NOTHING""",
        (episode_id, artifact_id),
    )
    _upsert_document(
        connection,
        namespace_id,
        "artifact",
        artifact_id,
        report.group(0),
        state,
    )
    return artifact_id


def match_label(relationship_type: str) -> str:
    return {
        "university_classmate": "大学同学",
        "classmate": "同学",
        "friend": "朋友",
        "colleague": "同事",
    }.get(relationship_type, relationship_type)


def _store_preference(
    connection: Connection,
    *,
    namespace_id: UUID,
    user_subject_id: UUID,
    event_id: UUID,
    occurred_at: datetime,
    candidate: PreferenceCandidate,
    fact_id: UUID | None,
) -> UUID:
    topic_entity_id = _ensure_entity(connection, namespace_id, candidate.topic, "concept")
    preference_id = stable_uuid(
        "preference", f"{namespace_id}:{user_subject_id}:{event_id}:{candidate.aspect.casefold()}"
    )
    prior = connection.execute(
        """SELECT id,version FROM memory.preference_assertions
           WHERE namespace_id=%s AND subject_id=%s AND lower(aspect)=lower(%s)
             AND state='active' AND id<>%s
           ORDER BY updated_at DESC LIMIT 1""",
        (namespace_id, user_subject_id, candidate.aspect, preference_id),
    ).fetchone()
    if prior:
        connection.execute(
            """UPDATE memory.preference_assertions
               SET state='superseded',valid_to=%s,updated_at=now() WHERE id=%s""",
            (occurred_at, prior[0]),
        )
    connection.execute(
        """INSERT INTO memory.preference_assertions(
             id,namespace_id,subject_id,topic_entity_id,aspect,polarity,strength,
             explicitness,valid_from,fact_id,state,supersedes_id,version
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,'explicit',%s,%s,'active',%s,%s)
           ON CONFLICT(id) DO UPDATE SET
             aspect=excluded.aspect,polarity=excluded.polarity,
             strength=excluded.strength,fact_id=excluded.fact_id,updated_at=now()""",
        (
            preference_id,
            namespace_id,
            user_subject_id,
            topic_entity_id,
            candidate.aspect,
            candidate.polarity,
            candidate.strength,
            occurred_at,
            fact_id,
            prior[0] if prior else None,
            int(prior[1]) + 1 if prior else 1,
        ),
    )
    text = f"用户偏好 {candidate.polarity}: {candidate.aspect}"
    _upsert_document(connection, namespace_id, "preference", preference_id, text, "active")
    return preference_id


def _store_temporal_rule(
    connection: Connection,
    *,
    namespace_id: UUID,
    user_subject_id: UUID,
    event_id: UUID,
    candidate: TemporalRuleCandidate,
    fact_id: UUID | None,
) -> UUID:
    rule_id = stable_uuid(
        "temporal-rule", f"{namespace_id}:{user_subject_id}:{event_id}:{candidate.label}"
    )
    prior = connection.execute(
        """SELECT id,version FROM memory.temporal_rules
           WHERE namespace_id=%s AND owner_subject_id=%s
             AND rule_type=%s AND label=%s AND state='active' AND id<>%s
           ORDER BY updated_at DESC LIMIT 1""",
        (
            namespace_id,
            user_subject_id,
            candidate.rule_type,
            candidate.label,
            rule_id,
        ),
    ).fetchone()
    if prior:
        connection.execute(
            "UPDATE memory.temporal_rules SET state='superseded',updated_at=now() WHERE id=%s",
            (prior[0],),
        )
    connection.execute(
        """INSERT INTO memory.temporal_rules(
             id,namespace_id,owner_subject_id,rule_type,label,month,day,year,
             timezone,recurrence,sensitivity,reminder_policy,fact_id,
             review_state,state,supersedes_id,version
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'Asia/Shanghai','yearly','personal',
                     '{"enabled":false}'::jsonb,%s,'accepted','active',%s,%s)
           ON CONFLICT(id) DO UPDATE SET
             month=excluded.month,day=excluded.day,year=excluded.year,
             fact_id=excluded.fact_id,updated_at=now()""",
        (
            rule_id,
            namespace_id,
            user_subject_id,
            candidate.rule_type,
            candidate.label,
            candidate.month,
            candidate.day,
            candidate.year,
            fact_id,
            prior[0] if prior else None,
            int(prior[1]) + 1 if prior else 1,
        ),
    )
    text = f"{candidate.label} 每年 {candidate.month}月{candidate.day}日"
    _upsert_document(connection, namespace_id, "temporal_rule", rule_id, text, "active")
    return rule_id


def process_unified_turn(connection: Connection, job) -> None:
    _job_id, namespace_id, _kind, turn_id, _input_version = job
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT e.id,e.event_type,e.redacted_payload->>'content' AS content,
                      e.occurred_at,s.subject_id,
                      COALESCE(e.redacted_payload->>'tool_name','') AS tool_name
               FROM evidence.events e
               JOIN core.turns t ON t.id=e.turn_id
               JOIN core.sessions session ON session.id=t.session_id
               JOIN core.sources s ON s.id=session.source_id
               WHERE e.namespace_id=%s AND e.turn_id=%s
               ORDER BY e.sequence_no""",
            (namespace_id, turn_id),
        )
        rows = cursor.fetchall()
    if not rows:
        raise ValueError("INPUT_NOT_FOUND")
    user_subject = connection.execute(
        """SELECT id FROM core.subjects
           WHERE namespace_id=%s AND kind='user' AND status='active'""",
        (namespace_id,),
    ).fetchone()
    if not user_subject:
        raise ValueError("USER_SUBJECT_NOT_FOUND")
    created: list[tuple[str, UUID]] = []
    current_technical_episode_id: UUID | None = None
    trusted_tools = get_settings().trusted_observation_tools
    has_trusted_observation = any(
        row["event_type"] == "tool_result"
        and row["tool_name"].casefold() in trusted_tools
        for row in rows
    )
    for index, row in enumerate(rows):
        if current_technical_episode_id:
            fact_id = _linked_fact_id(connection, row["id"])
            if (
                row["event_type"] == "tool_result"
                and row["tool_name"].casefold() in trusted_tools
            ):
                _attach_episode_subject(
                    connection,
                    episode_id=current_technical_episode_id,
                    subject_id=row["subject_id"],
                    role="actor",
                    fact_id=fact_id,
                    confidence=0.95,
                )
                _append_episode_step(
                    connection,
                    episode_id=current_technical_episode_id,
                    event_id=row["id"],
                    occurred_at=row["occurred_at"],
                    content=row["content"] or "",
                    fact_id=fact_id,
                )
                artifact_id = _store_report_artifact(
                    connection,
                    namespace_id=namespace_id,
                    episode_id=current_technical_episode_id,
                    event_id=row["id"],
                    content=row["content"] or "",
                    state="active",
                )
                if artifact_id:
                    created.append(("artifact", artifact_id))
                continue
            if (
                row["event_type"] == "tool_call"
                and row["tool_name"].casefold() in trusted_tools
            ):
                _attach_episode_subject(
                    connection,
                    episode_id=current_technical_episode_id,
                    subject_id=row["subject_id"],
                    role="actor",
                    fact_id=fact_id,
                    confidence=0.9,
                )
                _append_candidate_step(
                    connection,
                    episode_id=current_technical_episode_id,
                    event_id=row["id"],
                    occurred_at=row["occurred_at"],
                    content=f"调用工具 {row['tool_name']}",
                    fact_id=fact_id,
                    default_kind="action",
                    status="observed",
                )
                continue
            if row["event_type"] == "assistant_message" and row["content"]:
                _append_candidate_step(
                    connection,
                    episode_id=current_technical_episode_id,
                    event_id=row["id"],
                    occurred_at=row["occurred_at"],
                    content=row["content"],
                    fact_id=fact_id,
                    default_kind="observation",
                    status="candidate",
                )
                artifact_id = _store_report_artifact(
                    connection,
                    namespace_id=namespace_id,
                    episode_id=current_technical_episode_id,
                    event_id=row["id"],
                    content=row["content"],
                    state="candidate",
                )
                if artifact_id:
                    created.append(("artifact", artifact_id))
                continue
        if row["event_type"] != "user_message" or not row["content"]:
            continue
        fact_id = _linked_fact_id(connection, row["id"])
        episode = parse_episode(row["content"], row["occurred_at"])
        if episode and episode.episode_type == "technical":
            admission_state = _model_admitted_fact_state(connection, fact_id)
            if admission_state == "active":
                episode = replace(
                    episode,
                    accepted=True,
                    confidence=max(episode.confidence, 0.82),
                )
            elif (
                admission_state is None
                and fact_id is None
                and not has_trusted_observation
            ):
                # Wait for model admission instead of materializing every technical
                # keyword hit as a manual governance task.
                episode = None
        if episode:
            episode_id = _store_episode(
                connection,
                namespace_id=namespace_id,
                turn_id=turn_id,
                event_id=row["id"],
                event_index=index,
                candidate=episode,
                user_subject_id=user_subject[0],
                fact_id=fact_id,
            )
            created.append(
                (
                    "episode",
                    episode_id,
                )
            )
            current_technical_episode_id = (
                episode_id if episode.episode_type == "technical" else None
            )
            artifact_id = _store_report_artifact(
                connection,
                namespace_id=namespace_id,
                episode_id=episode_id,
                event_id=row["id"],
                content=row["content"],
                state="active",
            )
            if artifact_id:
                created.append(("artifact", artifact_id))
        preference = parse_preference(row["content"])
        if preference:
            created.append(
                (
                    "preference",
                    _store_preference(
                        connection,
                        namespace_id=namespace_id,
                        user_subject_id=user_subject[0],
                        event_id=row["id"],
                        occurred_at=row["occurred_at"],
                        candidate=preference,
                        fact_id=fact_id,
                    ),
                )
            )
        temporal = parse_temporal_rule(row["content"])
        if temporal:
            created.append(
                (
                    "temporal_rule",
                    _store_temporal_rule(
                        connection,
                        namespace_id=namespace_id,
                        user_subject_id=user_subject[0],
                        event_id=row["id"],
                        candidate=temporal,
                        fact_id=fact_id,
                    ),
                )
            )
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,
             correlation_id,metadata_redacted
           ) VALUES (%s,%s,'worker','unified-memory-worker','memory.unified.build',
                     'turn',%s,%s,%s::jsonb)""",
        (
            new_uuid(),
            namespace_id,
            turn_id,
            new_uuid(),
            json.dumps({"extractor_version": EXTRACTOR_VERSION, "created": len(created)}),
        ),
    )


def invalidate_unified_dependents(
    connection: Connection, namespace_id: UUID, fact_id: UUID
) -> None:
    affected_episodes = connection.execute(
        """UPDATE memory.episodes episode
           SET state='candidate',review_state='candidate',version=version+1,updated_at=now()
           WHERE episode.namespace_id=%s AND episode.origin='automatic'
             AND EXISTS (
               SELECT 1 FROM memory.episode_facts link
               WHERE link.episode_id=episode.id AND link.fact_id=%s
             )
             AND NOT EXISTS (
               SELECT 1 FROM memory.episode_facts link
               JOIN memory.facts fact ON fact.id=link.fact_id
               WHERE link.episode_id=episode.id AND link.fact_id<>%s
                 AND fact.memory_state='active'
             )
           RETURNING episode.id""",
        (namespace_id, fact_id, fact_id),
    ).fetchall()
    episode_ids = [row[0] for row in affected_episodes]
    if episode_ids:
        connection.execute(
            """UPDATE retrieval.documents SET lifecycle_state='candidate',indexed_at=now()
               WHERE source_kind='episode' AND source_id=ANY(%s)""",
            (episode_ids,),
        )
        _invalidate_procedures_for_episodes(
            connection,
            namespace_id=namespace_id,
            episode_ids=episode_ids,
            reason="supporting fact invalidated",
        )
    for table, source_kind in (
        ("relationship_assertions", "relationship"),
        ("preference_assertions", "preference"),
    ):
        ids = [
            row[0]
            for row in connection.execute(
                f"""UPDATE memory.{table}
                    SET state='candidate',version=version+1,updated_at=now()
                    WHERE namespace_id=%s AND fact_id=%s AND state='active'
                    RETURNING id""",
                (namespace_id, fact_id),
            ).fetchall()
        ]
        if ids:
            connection.execute(
                """UPDATE retrieval.documents SET lifecycle_state='candidate',indexed_at=now()
                   WHERE source_kind=%s AND source_id=ANY(%s)""",
                (source_kind, ids),
            )
    temporal_ids = [
        row[0]
        for row in connection.execute(
            """UPDATE memory.temporal_rules
               SET review_state='candidate',version=version+1,updated_at=now()
               WHERE namespace_id=%s AND fact_id=%s AND state='active'
               RETURNING id""",
            (namespace_id, fact_id),
        ).fetchall()
    ]
    if temporal_ids:
        connection.execute(
            """UPDATE retrieval.documents SET lifecycle_state='candidate',indexed_at=now()
               WHERE source_kind='temporal_rule' AND source_id=ANY(%s)""",
            (temporal_ids,),
        )


def list_episodes(
    connection: Connection,
    *,
    namespace_key: str,
    episode_type: str | None = None,
    state: str | None = None,
    entity_id: UUID | None = None,
    subject_id: UUID | None = None,
    started_after: datetime | None = None,
    ended_before: datetime | None = None,
    limit: int = 50,
) -> list[dict]:
    namespace_id = stable_uuid("namespace", namespace_key)
    filters = ["episode.namespace_id=%s"]
    parameters: list[object] = [namespace_id]
    if episode_type:
        filters.append("episode.episode_type=%s")
        parameters.append(episode_type)
    if state:
        filters.append("episode.state=%s")
        parameters.append(state)
    if entity_id:
        filters.append(
            "EXISTS (SELECT 1 FROM memory.episode_entities ee "
            "WHERE ee.episode_id=episode.id AND ee.entity_id=%s)"
        )
        parameters.append(entity_id)
    if subject_id:
        filters.append(
            "EXISTS (SELECT 1 FROM memory.episode_entities ee "
            "WHERE ee.episode_id=episode.id AND ee.subject_id=%s)"
        )
        parameters.append(subject_id)
    if started_after:
        filters.append("episode.started_at >= %s")
        parameters.append(started_after)
    if ended_before:
        filters.append("COALESCE(episode.ended_at,episode.started_at) <= %s")
        parameters.append(ended_before)
    parameters.append(limit)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            f"""SELECT episode.id,episode.episode_type,episode.title,episode.summary,
                       episode.started_at,episode.ended_at,episode.time_precision,
                       episode.timezone,episode.importance,episode.review_state,
                       episode.state,episode.origin,episode.confidence,episode.version,
                       episode.created_at,episode.updated_at
                FROM memory.episodes episode
                WHERE {" AND ".join(filters)}
                ORDER BY COALESCE(episode.started_at,episode.created_at) DESC
                LIMIT %s""",
            parameters,
        )
        rows = cursor.fetchall()
    for row in rows:
        _localize_episode_dates(row)
    return rows


def _localize_episode_dates(row: dict) -> None:
    timezone_name = row.get("timezone")
    if not timezone_name:
        return
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return
    for field in ("started_at", "ended_at", "occurred_at"):
        if row.get(field) is not None:
            row[field] = row[field].astimezone(timezone)


def get_episode(connection: Connection, *, namespace_key: str, episode_id: UUID) -> dict | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT id,episode_type,title,summary,started_at,ended_at,time_precision,
                      timezone,time_resolution,importance,review_state,state,origin,
                      confidence,version,created_at,updated_at
               FROM memory.episodes WHERE namespace_id=%s AND id=%s""",
            (namespace_id, episode_id),
        )
        episode = cursor.fetchone()
        if episode is None:
            return None
        _localize_episode_dates(episode)
        cursor.execute(
            """SELECT link.role,link.confidence,link.origin,
                      entity.id AS entity_id,entity.canonical_name AS entity_name,
                      entity.entity_type,subject.id AS subject_id,
                      subject.display_name AS subject_name,subject.kind AS subject_kind
               FROM memory.episode_entities link
               LEFT JOIN memory.entities entity ON entity.id=link.entity_id
               LEFT JOIN core.subjects subject ON subject.id=link.subject_id
               WHERE link.episode_id=%s
               ORDER BY link.role,COALESCE(entity.canonical_name,subject.display_name)""",
            (episode_id,),
        )
        entities = cursor.fetchall()
        cursor.execute(
            """SELECT id,sequence_no,parent_step_id,branch_key,step_kind,summary,
                      occurred_at,status,fact_id,evidence_event_id,confidence
               FROM memory.episode_steps WHERE episode_id=%s ORDER BY sequence_no""",
            (episode_id,),
        )
        steps = cursor.fetchall()
        for step in steps:
            step["timezone"] = episode["timezone"]
            _localize_episode_dates(step)
            step.pop("timezone", None)
        cursor.execute(
            """SELECT artifact.id,artifact.artifact_type,artifact.title,
                      artifact.reference_uri,artifact.content_hash,
                      artifact.summary_redacted,artifact.sensitivity,link.role
               FROM memory.episode_artifacts link
               JOIN memory.artifacts artifact ON artifact.id=link.artifact_id
               WHERE link.episode_id=%s ORDER BY artifact.created_at""",
            (episode_id,),
        )
        artifacts = cursor.fetchall()
    return {**episode, "participants": entities, "steps": steps, "artifacts": artifacts}


def set_episode_review(
    connection: Connection,
    *,
    namespace_key: str,
    episode_id: UUID,
    expected_version: int,
    action: str,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    states = {
        "confirm": ("accepted", "active"),
        "forget": ("accepted", "forgotten"),
        "isolate": ("accepted", "isolated"),
    }
    if action not in states:
        raise ValueError("EPISODE_ACTION_INVALID")
    review_state, state = states[action]
    row = connection.execute(
        """UPDATE memory.episodes SET review_state=%s,state=%s,version=version+1,
                                      updated_at=now()
           WHERE namespace_id=%s AND id=%s AND version=%s
           RETURNING id,version,title,summary,state,review_state""",
        (review_state, state, namespace_id, episode_id, expected_version),
    ).fetchone()
    if row is None:
        exists = connection.execute(
            "SELECT 1 FROM memory.episodes WHERE namespace_id=%s AND id=%s",
            (namespace_id, episode_id),
        ).fetchone()
        if exists:
            raise ValueError("VERSION_CONFLICT")
        return None
    connection.execute(
        "UPDATE retrieval.documents SET lifecycle_state=%s,indexed_at=now() "
        "WHERE source_kind='episode' AND source_id=%s",
        (state, episode_id),
    )
    if action in {"forget", "isolate"}:
        _invalidate_procedures_for_episodes(
            connection,
            namespace_id=namespace_id,
            episode_ids=[episode_id],
            reason=f"supporting episode {action}",
        )
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,
             reason,correlation_id
           ) VALUES (%s,%s,'user',%s,%s,'episode',%s,%s,%s)""",
        (
            new_uuid(),
            namespace_id,
            actor_id,
            f"episode.{action}",
            episode_id,
            reason,
            correlation_id,
        ),
    )
    return {
        "id": row[0],
        "version": row[1],
        "title": row[2],
        "summary": row[3],
        "state": row[4],
        "review_state": row[5],
    }


def update_episode(
    connection: Connection,
    *,
    namespace_key: str,
    episode_id: UUID,
    expected_version: int,
    changes: dict,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    participants = changes.pop("participants", None)
    allowed = {
        "title",
        "summary",
        "started_at",
        "ended_at",
        "time_precision",
        "timezone",
    }
    if set(changes) - allowed:
        raise ValueError("EPISODE_FIELD_INVALID")
    current = connection.execute(
        """SELECT title,summary,started_at,ended_at,time_precision,timezone,version
           FROM memory.episodes WHERE namespace_id=%s AND id=%s FOR UPDATE""",
        (namespace_id, episode_id),
    ).fetchone()
    if current is None:
        return None
    if current[6] != expected_version:
        raise ValueError("VERSION_CONFLICT")
    next_started_at = changes.get("started_at", current[2])
    next_ended_at = changes.get("ended_at", current[3])
    if next_started_at and next_ended_at and next_ended_at < next_started_at:
        raise ValueError("EPISODE_TIME_ORDER_INVALID")
    assignments: list[str] = []
    parameters: list[object] = []
    for field in (
        "title",
        "summary",
        "started_at",
        "ended_at",
        "time_precision",
        "timezone",
    ):
        if field in changes:
            assignments.append(f"{field}=%s")
            parameters.append(changes[field])
    if assignments:
        connection.execute(
            f"""UPDATE memory.episodes SET {",".join(assignments)},
                        version=version+1,updated_at=now()
                 WHERE namespace_id=%s AND id=%s""",
            (*parameters, namespace_id, episode_id),
        )
    else:
        connection.execute(
            """UPDATE memory.episodes SET version=version+1,updated_at=now()
               WHERE namespace_id=%s AND id=%s""",
            (namespace_id, episode_id),
        )
    if participants is not None:
        for participant in participants:
            entity_id = participant.get("entity_id")
            subject_id = participant.get("subject_id")
            if entity_id:
                valid = connection.execute(
                    "SELECT 1 FROM memory.entities WHERE namespace_id=%s AND id=%s",
                    (namespace_id, entity_id),
                ).fetchone()
            else:
                valid = connection.execute(
                    "SELECT 1 FROM core.subjects WHERE namespace_id=%s AND id=%s",
                    (namespace_id, subject_id),
                ).fetchone()
            if valid is None:
                raise ValueError("EPISODE_PARTICIPANT_NOT_FOUND")
        connection.execute(
            "DELETE FROM memory.episode_entities WHERE episode_id=%s",
            (episode_id,),
        )
        for participant in participants:
            connection.execute(
                """INSERT INTO memory.episode_entities(
                     episode_id,entity_id,subject_id,role,confidence,origin
                   ) VALUES (%s,%s,%s,%s,1,'manual')""",
                (
                    episode_id,
                    participant.get("entity_id"),
                    participant.get("subject_id"),
                    participant["role"],
                ),
            )
    updated = get_episode(connection, namespace_key=namespace_key, episode_id=episode_id)
    if updated is None:
        return None
    _upsert_document(
        connection,
        namespace_id,
        "episode",
        episode_id,
        f"{updated['title']} · {updated['summary']}",
        updated["state"],
    )
    _invalidate_procedures_for_episodes(
        connection,
        namespace_id=namespace_id,
        episode_ids=[episode_id],
        reason="supporting episode corrected",
    )
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,
             reason,correlation_id,metadata_redacted
           ) VALUES (%s,%s,'user',%s,'episode.correct','episode',%s,%s,%s,%s::jsonb)""",
        (
            new_uuid(),
            namespace_id,
            actor_id,
            episode_id,
            redact_text(reason).text,
            correlation_id,
            json.dumps(
                {
                    "changed_fields": sorted(
                        {*changes, *(["participants"] if participants is not None else [])}
                    )
                }
            ),
        ),
    )
    return updated


def _invalidate_procedures_for_episodes(
    connection: Connection,
    *,
    namespace_id: UUID,
    episode_ids: list[UUID],
    reason: str,
) -> list[UUID]:
    if not episode_ids:
        return []
    procedure_ids = [
        row[0]
        for row in connection.execute(
            """UPDATE memory.procedures procedure
               SET state='dormant',review_state='candidate',
                   version=version+1,updated_at=now()
               WHERE procedure.namespace_id=%s AND procedure.state='active'
                 AND EXISTS (
                   SELECT 1 FROM memory.procedure_support support
                   WHERE support.procedure_id=procedure.id
                     AND support.episode_id=ANY(%s)
                 )
               RETURNING procedure.id""",
            (namespace_id, episode_ids),
        ).fetchall()
    ]
    if not procedure_ids:
        return []
    connection.execute(
        """UPDATE retrieval.documents SET lifecycle_state='dormant',indexed_at=now()
           WHERE source_kind='procedure' AND source_id=ANY(%s)""",
        (procedure_ids,),
    )
    for procedure_id in procedure_ids:
        connection.execute(
            """INSERT INTO audit.events(
                 id,namespace_id,actor_type,actor_id,action,target_type,target_id,
                 reason,correlation_id,metadata_redacted
               ) VALUES (
                 %s,%s,'system','unified-memory-invalidation',
                 'procedure.invalidate','procedure',%s,%s,%s,%s::jsonb
               )""",
            (
                new_uuid(),
                namespace_id,
                procedure_id,
                reason,
                new_uuid(),
                json.dumps({"episode_ids": [str(item) for item in episode_ids]}),
            ),
        )
    return procedure_ids


def list_temporal_rules(connection: Connection, namespace_key: str) -> list[dict]:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT id,owner_subject_id,owner_entity_id,rule_type,label,month,day,year,
                      timezone,recurrence,sensitivity,reminder_policy,review_state,state,
                      supersedes_id,version,created_at,updated_at
               FROM memory.temporal_rules WHERE namespace_id=%s
               ORDER BY month,day,label""",
            (namespace_id,),
        )
        return cursor.fetchall()


def _set_versioned_state(
    connection: Connection,
    *,
    namespace_id: UUID,
    table: str,
    target_kind: str,
    source_kind: str,
    target_id: UUID,
    expected_version: int,
    state: str,
    review_state: str | None,
    action: str,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict | None:
    review_sql = ",review_state=%s" if review_state is not None else ""
    parameters: list[object] = [state]
    if review_state is not None:
        parameters.append(review_state)
    parameters.extend([namespace_id, target_id, expected_version])
    row = connection.execute(
        f"""UPDATE memory.{table}
            SET state=%s{review_sql},version=version+1,updated_at=now()
            WHERE namespace_id=%s AND id=%s AND version=%s
            RETURNING id,state,version""",
        parameters,
    ).fetchone()
    if row is None:
        exists = connection.execute(
            f"SELECT 1 FROM memory.{table} WHERE namespace_id=%s AND id=%s",
            (namespace_id, target_id),
        ).fetchone()
        if exists:
            raise ValueError("VERSION_CONFLICT")
        return None
    connection.execute(
        """UPDATE retrieval.documents SET lifecycle_state=%s,indexed_at=now()
           WHERE source_kind=%s AND source_id=%s""",
        (state, source_kind, target_id),
    )
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,
             reason,correlation_id
           ) VALUES (%s,%s,'user',%s,%s,%s,%s,%s,%s)""",
        (
            new_uuid(),
            namespace_id,
            actor_id,
            f"{target_kind}.{action}",
            target_kind,
            target_id,
            redact_text(reason).text,
            correlation_id,
        ),
    )
    return {"id": row[0], "state": row[1], "version": row[2]}


def set_temporal_rule_state(
    connection: Connection,
    *,
    namespace_key: str,
    rule_id: UUID,
    expected_version: int,
    action: str,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict | None:
    state_map = {
        "confirm": ("active", "accepted"),
        "disable": ("disabled", "accepted"),
        "forget": ("forgotten", "accepted"),
        "isolate": ("isolated", "accepted"),
    }
    if action not in state_map:
        raise ValueError("TEMPORAL_RULE_ACTION_INVALID")
    state, review = state_map[action]
    return _set_versioned_state(
        connection,
        namespace_id=stable_uuid("namespace", namespace_key),
        table="temporal_rules",
        target_kind="temporal_rule",
        source_kind="temporal_rule",
        target_id=rule_id,
        expected_version=expected_version,
        state=state,
        review_state=review,
        action=action,
        actor_id=actor_id,
        reason=reason,
        correlation_id=correlation_id,
    )


def correct_temporal_rule(
    connection: Connection,
    *,
    namespace_key: str,
    rule_id: UUID,
    expected_version: int,
    changes: dict,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT * FROM memory.temporal_rules
               WHERE namespace_id=%s AND id=%s FOR UPDATE""",
            (namespace_id, rule_id),
        )
        current = cursor.fetchone()
    if current is None:
        return None
    if current["version"] != expected_version:
        raise ValueError("VERSION_CONFLICT")
    values = {
        field: changes.get(field, current[field])
        for field in ("label", "month", "day", "year", "timezone", "sensitivity")
    }
    validation_year = int(values["year"] or 2000)
    if _date_at(validation_year, int(values["month"]), int(values["day"])) is None:
        raise ValueError("TEMPORAL_RULE_DATE_INVALID")
    replacement_id = stable_uuid(
        "temporal-rule-correction", f"{rule_id}:{correlation_id}:{expected_version}"
    )
    connection.execute(
        """UPDATE memory.temporal_rules SET state='superseded',updated_at=now()
           WHERE id=%s""",
        (rule_id,),
    )
    connection.execute(
        """INSERT INTO memory.temporal_rules(
             id,namespace_id,owner_subject_id,owner_entity_id,rule_type,label,
             month,day,year,timezone,recurrence,sensitivity,reminder_policy,
             fact_id,review_state,state,supersedes_id,version
           ) VALUES (
             %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,
             'accepted','active',%s,%s
           )""",
        (
            replacement_id,
            namespace_id,
            current["owner_subject_id"],
            current["owner_entity_id"],
            current["rule_type"],
            values["label"],
            values["month"],
            values["day"],
            values["year"],
            values["timezone"],
            current["recurrence"],
            values["sensitivity"],
            json.dumps(current["reminder_policy"]),
            current["fact_id"],
            rule_id,
            expected_version + 1,
        ),
    )
    connection.execute(
        """UPDATE retrieval.documents SET lifecycle_state='superseded',indexed_at=now()
           WHERE source_kind='temporal_rule' AND source_id=%s""",
        (rule_id,),
    )
    _upsert_document(
        connection,
        namespace_id,
        "temporal_rule",
        replacement_id,
        f"{values['label']} 每年 {values['month']}月{values['day']}日",
        "active",
    )
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,
             reason,correlation_id,metadata_redacted
           ) VALUES (
             %s,%s,'user',%s,'temporal_rule.correct','temporal_rule',%s,%s,%s,%s::jsonb
           )""",
        (
            new_uuid(),
            namespace_id,
            actor_id,
            replacement_id,
            redact_text(reason).text,
            correlation_id,
            json.dumps({"supersedes_id": str(rule_id), "changed_fields": sorted(changes)}),
        ),
    )
    return next(
        (
            row
            for row in list_temporal_rules(connection, namespace_key)
            if row["id"] == replacement_id
        ),
        None,
    )


def update_reminder_policy(
    connection: Connection,
    *,
    namespace_key: str,
    rule_id: UUID,
    expected_version: int,
    enabled: bool,
    lead_days: int,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    policy = {"enabled": enabled, "lead_days": lead_days}
    row = connection.execute(
        """UPDATE memory.temporal_rules
           SET reminder_policy=%s::jsonb,version=version+1,updated_at=now()
           WHERE namespace_id=%s AND id=%s AND version=%s
           RETURNING id,state,version,reminder_policy""",
        (json.dumps(policy), namespace_id, rule_id, expected_version),
    ).fetchone()
    if row is None:
        exists = connection.execute(
            "SELECT 1 FROM memory.temporal_rules WHERE namespace_id=%s AND id=%s",
            (namespace_id, rule_id),
        ).fetchone()
        if exists:
            raise ValueError("VERSION_CONFLICT")
        return None
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,
             reason,correlation_id,metadata_redacted
           ) VALUES (
             %s,%s,'user',%s,'temporal_rule.reminder_policy','temporal_rule',
             %s,%s,%s,%s::jsonb
           )""",
        (
            new_uuid(),
            namespace_id,
            actor_id,
            rule_id,
            redact_text(reason).text,
            correlation_id,
            json.dumps(policy),
        ),
    )
    return {
        "id": row[0],
        "state": row[1],
        "version": row[2],
        "reminder_policy": row[3],
    }


def list_preferences(connection: Connection, namespace_key: str) -> list[dict]:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT preference.id,preference.subject_id,subject.display_name AS subject_name,
                      preference.topic_entity_id,entity.canonical_name AS topic,
                      preference.aspect,preference.polarity,preference.strength,
                      preference.explicitness,preference.valid_from,preference.valid_to,
                      preference.state,preference.supersedes_id,preference.version,
                      preference.created_at,preference.updated_at
               FROM memory.preference_assertions preference
               JOIN core.subjects subject ON subject.id=preference.subject_id
               LEFT JOIN memory.entities entity ON entity.id=preference.topic_entity_id
               WHERE preference.namespace_id=%s
               ORDER BY preference.updated_at DESC""",
            (namespace_id,),
        )
        return cursor.fetchall()


def set_preference_state(
    connection: Connection,
    *,
    namespace_key: str,
    preference_id: UUID,
    expected_version: int,
    action: str,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict | None:
    state_map = {
        "confirm": "active",
        "dormant": "dormant",
        "forget": "forgotten",
        "isolate": "isolated",
    }
    if action not in state_map:
        raise ValueError("PREFERENCE_ACTION_INVALID")
    return _set_versioned_state(
        connection,
        namespace_id=stable_uuid("namespace", namespace_key),
        table="preference_assertions",
        target_kind="preference",
        source_kind="preference",
        target_id=preference_id,
        expected_version=expected_version,
        state=state_map[action],
        review_state=None,
        action=action,
        actor_id=actor_id,
        reason=reason,
        correlation_id=correlation_id,
    )


def list_relationships(connection: Connection, namespace_key: str) -> list[dict]:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT relationship.id,relationship.subject_id,
                      subject.display_name AS subject_name,
                      relationship.related_entity_id,
                      entity.canonical_name AS related_entity_name,
                      relationship.relation_type,relationship.label,
                      relationship.valid_from,relationship.valid_to,
                      relationship.fact_id,relationship.episode_id,
                      relationship.state,relationship.supersedes_id,
                      relationship.version,relationship.created_at,
                      relationship.updated_at
               FROM memory.relationship_assertions relationship
               JOIN core.subjects subject ON subject.id=relationship.subject_id
               JOIN memory.entities entity ON entity.id=relationship.related_entity_id
               WHERE relationship.namespace_id=%s
               ORDER BY relationship.updated_at DESC""",
            (namespace_id,),
        )
        return cursor.fetchall()


def set_relationship_state(
    connection: Connection,
    *,
    namespace_key: str,
    relationship_id: UUID,
    expected_version: int,
    action: str,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict | None:
    state_map = {
        "confirm": "active",
        "dormant": "dormant",
        "forget": "forgotten",
        "isolate": "isolated",
    }
    if action not in state_map:
        raise ValueError("RELATIONSHIP_ACTION_INVALID")
    return _set_versioned_state(
        connection,
        namespace_id=stable_uuid("namespace", namespace_key),
        table="relationship_assertions",
        target_kind="relationship",
        source_kind="relationship",
        target_id=relationship_id,
        expected_version=expected_version,
        state=state_map[action],
        review_state=None,
        action=action,
        actor_id=actor_id,
        reason=reason,
        correlation_id=correlation_id,
    )


def list_artifacts(connection: Connection, namespace_key: str) -> list[dict]:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT artifact.id,artifact.artifact_type,artifact.title,
                      artifact.reference_uri,artifact.content_hash,
                      artifact.summary_redacted,artifact.sensitivity,artifact.state,
                      artifact.version,artifact.created_at,artifact.updated_at,
                      array_remove(array_agg(link.episode_id),NULL) AS episode_ids
               FROM memory.artifacts artifact
               LEFT JOIN memory.episode_artifacts link ON link.artifact_id=artifact.id
               WHERE artifact.namespace_id=%s
               GROUP BY artifact.id ORDER BY artifact.updated_at DESC""",
            (namespace_id,),
        )
        return cursor.fetchall()


def get_artifact(connection: Connection, namespace_key: str, artifact_id: UUID) -> dict | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT artifact.id,artifact.artifact_type,artifact.title,
                      artifact.reference_uri,artifact.content_hash,
                      artifact.summary_redacted,artifact.sensitivity,artifact.state,
                      artifact.version,artifact.created_at,artifact.updated_at,
                      array_remove(array_agg(link.episode_id),NULL) AS episode_ids
               FROM memory.artifacts artifact
               LEFT JOIN memory.episode_artifacts link ON link.artifact_id=artifact.id
               WHERE artifact.namespace_id=%s AND artifact.id=%s
               GROUP BY artifact.id""",
            (namespace_id, artifact_id),
        )
        return cursor.fetchone()


def create_artifact(
    connection: Connection,
    *,
    namespace_key: str,
    artifact_type: str,
    title: str,
    reference_uri: str | None,
    content_hash: str | None,
    summary: str,
    sensitivity: str,
    episode_id: UUID,
    role: str,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict:
    namespace_id = stable_uuid("namespace", namespace_key)
    episode = connection.execute(
        "SELECT 1 FROM memory.episodes WHERE namespace_id=%s AND id=%s",
        (namespace_id, episode_id),
    ).fetchone()
    if episode is None:
        raise ValueError("EPISODE_NOT_FOUND")
    for value in (reference_uri or "", content_hash or ""):
        if redact_text(value).findings:
            raise ValueError("ARTIFACT_REFERENCE_SECRET_FORBIDDEN")
    artifact_id = stable_uuid(
        "artifact",
        f"{namespace_id}:{episode_id}:{artifact_type}:{title.casefold()}:{reference_uri or ''}",
    )
    redacted_summary = redact_text(summary).text
    row = connection.execute(
        """INSERT INTO memory.artifacts(
             id,namespace_id,artifact_type,title,reference_uri,content_hash,
             summary_redacted,sensitivity,state
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active')
           ON CONFLICT(id) DO UPDATE SET
             reference_uri=excluded.reference_uri,content_hash=excluded.content_hash,
             summary_redacted=excluded.summary_redacted,
             sensitivity=excluded.sensitivity,version=memory.artifacts.version+1,
             updated_at=now()
           RETURNING id,artifact_type,title,reference_uri,content_hash,
                     summary_redacted,sensitivity,state,version,created_at,updated_at""",
        (
            artifact_id,
            namespace_id,
            artifact_type,
            redact_text(title).text,
            reference_uri,
            content_hash,
            redacted_summary,
            sensitivity,
        ),
    ).fetchone()
    connection.execute(
        """INSERT INTO memory.episode_artifacts(episode_id,artifact_id,role)
           VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
        (episode_id, artifact_id, role),
    )
    _upsert_document(
        connection,
        namespace_id,
        "artifact",
        artifact_id,
        f"{title} · {redacted_summary}",
        "active",
    )
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,
             reason,correlation_id
           ) VALUES (%s,%s,'user',%s,'artifact.create','artifact',%s,%s,%s)""",
        (
            new_uuid(),
            namespace_id,
            actor_id,
            artifact_id,
            redact_text(reason).text,
            correlation_id,
        ),
    )
    return {
        "id": row[0],
        "artifact_type": row[1],
        "title": row[2],
        "reference_uri": row[3],
        "content_hash": row[4],
        "summary_redacted": row[5],
        "sensitivity": row[6],
        "state": row[7],
        "version": row[8],
        "created_at": row[9],
        "updated_at": row[10],
        "episode_ids": [episode_id],
    }


def procedure_applicability(
    procedure_fingerprint: dict,
    current_fingerprint: dict | None,
    *,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    now: datetime | None = None,
) -> dict:
    current_time = now or datetime.now(UTC)
    if valid_to is not None and valid_to <= current_time:
        return {
            "status": "expired",
            "matched": [],
            "mismatched": [],
            "missing": [],
            "auto_apply": False,
        }
    if valid_from is not None and valid_from > current_time:
        return {
            "status": "not_yet_valid",
            "matched": [],
            "mismatched": [],
            "missing": [],
            "auto_apply": False,
        }
    if not procedure_fingerprint:
        return {
            "status": "unknown",
            "matched": [],
            "mismatched": [],
            "missing": [],
            "auto_apply": False,
        }
    current = current_fingerprint or {}
    matched: list[str] = []
    mismatched: list[str] = []
    missing: list[str] = []
    for key, expected in procedure_fingerprint.items():
        if key not in current:
            missing.append(key)
        elif current[key] == expected:
            matched.append(key)
        else:
            mismatched.append(key)
    status = "incompatible" if mismatched else "unknown" if missing else "applicable"
    return {
        "status": status,
        "matched": matched,
        "mismatched": mismatched,
        "missing": missing,
        "auto_apply": False,
    }


def list_procedures(
    connection: Connection,
    namespace_key: str,
    *,
    include_candidates: bool = False,
    current_fingerprint: dict | None = None,
) -> list[dict]:
    namespace_id = stable_uuid("namespace", namespace_key)
    states = ("candidate", "active", "dormant") if include_candidates else ("active",)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT procedure.id,procedure.title,procedure.goal,procedure.scope,
                      procedure.preconditions,procedure.environment_fingerprint,
                      procedure.risk_level,procedure.state,procedure.review_state,
                      procedure.valid_from,procedure.valid_to,procedure.supersedes_id,
                      procedure.version,procedure.created_at,procedure.updated_at,
                      count(DISTINCT support.episode_id)
                        FILTER (WHERE support.support_kind='success') AS success_episodes
               FROM memory.procedures procedure
               LEFT JOIN memory.procedure_support support
                 ON support.procedure_id=procedure.id
               WHERE procedure.namespace_id=%s AND procedure.state=ANY(%s)
               GROUP BY procedure.id ORDER BY procedure.updated_at DESC""",
            (namespace_id, list(states)),
        )
        rows = cursor.fetchall()
    results: list[dict] = []
    for row in rows:
        detail = get_procedure(connection, namespace_key, row["id"])
        results.append(
            {
                **row,
                "steps": detail["steps"] if detail else [],
                "support": detail["support"] if detail else [],
                "applicability": procedure_applicability(
                    row["environment_fingerprint"],
                    current_fingerprint,
                    valid_from=row["valid_from"],
                    valid_to=row["valid_to"],
                ),
            }
        )
    return results


def get_procedure(connection: Connection, namespace_key: str, procedure_id: UUID) -> dict | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """SELECT id,title,goal,scope,preconditions,environment_fingerprint,
                      risk_level,state,review_state,valid_from,valid_to,supersedes_id,
                      version,created_at,updated_at
               FROM memory.procedures WHERE namespace_id=%s AND id=%s""",
            (namespace_id, procedure_id),
        )
        procedure = cursor.fetchone()
        if procedure is None:
            return None
        cursor.execute(
            """SELECT id,sequence_no,parent_step_id,branch_key,instruction,
                      expected_observation,success_condition,failure_condition,
                      stop_condition,required_permission,risk_level
               FROM memory.procedure_steps WHERE procedure_id=%s ORDER BY sequence_no""",
            (procedure_id,),
        )
        steps = cursor.fetchall()
        cursor.execute(
            """SELECT episode_id,fact_id,artifact_id,support_kind,weight
               FROM memory.procedure_support WHERE procedure_id=%s""",
            (procedure_id,),
        )
        support = cursor.fetchall()
    return {**procedure, "steps": steps, "support": support}


def create_procedure(
    connection: Connection,
    *,
    namespace_key: str,
    title: str,
    goal: str,
    scope: dict,
    preconditions: list,
    environment_fingerprint: dict,
    risk_level: str,
    valid_from: datetime | None,
    valid_to: datetime | None,
    episode_id: UUID,
    steps: list[dict],
    supersedes_procedure_id: UUID | None,
    expected_superseded_version: int | None,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict:
    namespace_id = stable_uuid("namespace", namespace_key)
    episode = connection.execute(
        """SELECT id,state FROM memory.episodes
           WHERE namespace_id=%s AND id=%s""",
        (namespace_id, episode_id),
    ).fetchone()
    if episode is None:
        raise ValueError("EPISODE_NOT_FOUND")
    if not steps:
        raise ValueError("PROCEDURE_STEPS_REQUIRED")
    if valid_from and valid_to and valid_to <= valid_from:
        raise ValueError("PROCEDURE_VALIDITY_INVALID")
    if not any(step.get("stop_condition") for step in steps):
        raise ValueError("PROCEDURE_STOP_CONDITION_REQUIRED")
    procedure_texts = [
        title,
        goal,
        json.dumps(scope, ensure_ascii=False),
        json.dumps(preconditions, ensure_ascii=False),
        json.dumps(environment_fingerprint, ensure_ascii=False),
        *(json.dumps(step, ensure_ascii=False) for step in steps),
    ]
    if any(redact_text(text).findings for text in procedure_texts):
        raise ValueError("PROCEDURE_SECRET_FORBIDDEN")
    replacement_version = 1
    if supersedes_procedure_id is not None:
        previous = connection.execute(
            """SELECT version FROM memory.procedures
               WHERE namespace_id=%s AND id=%s FOR UPDATE""",
            (namespace_id, supersedes_procedure_id),
        ).fetchone()
        if previous is None:
            raise ValueError("PROCEDURE_SUPERSEDED_NOT_FOUND")
        if expected_superseded_version is None or previous[0] != expected_superseded_version:
            raise ValueError("VERSION_CONFLICT")
        replacement_version = int(previous[0]) + 1
    procedure_id = stable_uuid(
        "procedure",
        (
            f"{namespace_id}:{episode_id}:{title.casefold()}:{goal.casefold()}:"
            f"{supersedes_procedure_id or 'root'}:{replacement_version}"
        ),
    )
    connection.execute(
        """INSERT INTO memory.procedures(
             id,namespace_id,title,goal,scope,preconditions,environment_fingerprint,
             risk_level,valid_from,valid_to,state,review_state,supersedes_id,version
           ) VALUES (
             %s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,
             'candidate','candidate',%s,%s
           )
           ON CONFLICT(id) DO UPDATE SET
             scope=excluded.scope,preconditions=excluded.preconditions,
             environment_fingerprint=excluded.environment_fingerprint,
             risk_level=excluded.risk_level,valid_from=excluded.valid_from,
             valid_to=excluded.valid_to,
             version=memory.procedures.version+1,updated_at=now()
           WHERE memory.procedures.state='candidate'""",
        (
            procedure_id,
            namespace_id,
            title,
            goal,
            json.dumps(scope),
            json.dumps(preconditions),
            json.dumps(environment_fingerprint),
            risk_level,
            valid_from,
            valid_to,
            supersedes_procedure_id,
            replacement_version,
        ),
    )
    existing = connection.execute(
        "SELECT state FROM memory.procedures WHERE namespace_id=%s AND id=%s",
        (namespace_id, procedure_id),
    ).fetchone()
    if existing is None:
        raise ValueError("PROCEDURE_CREATE_FAILED")
    if existing[0] != "candidate":
        raise ValueError("PROCEDURE_IMMUTABLE_AFTER_CONFIRMATION")
    connection.execute(
        """INSERT INTO memory.procedure_support(
             procedure_id,episode_id,support_kind,weight
           ) VALUES (%s,%s,'success',1) ON CONFLICT DO NOTHING""",
        (procedure_id, episode_id),
    )
    for sequence, step in enumerate(steps):
        connection.execute(
            """INSERT INTO memory.procedure_steps(
                 id,procedure_id,sequence_no,parent_step_id,branch_key,instruction,
                 expected_observation,success_condition,failure_condition,
                 stop_condition,required_permission,risk_level
               ) VALUES (%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(procedure_id,sequence_no) DO UPDATE SET
                 branch_key=excluded.branch_key,instruction=excluded.instruction,
                 expected_observation=excluded.expected_observation,
                 success_condition=excluded.success_condition,
                 failure_condition=excluded.failure_condition,
                 stop_condition=excluded.stop_condition,
                 required_permission=excluded.required_permission,
                 risk_level=excluded.risk_level,updated_at=now()""",
            (
                stable_uuid("procedure-step", f"{procedure_id}:{sequence}"),
                procedure_id,
                sequence,
                step.get("branch_key"),
                redact_text(str(step["instruction"])).text,
                redact_text(str(step.get("expected_observation") or "")).text or None,
                redact_text(str(step.get("success_condition") or "")).text or None,
                redact_text(str(step.get("failure_condition") or "")).text or None,
                redact_text(str(step["stop_condition"])).text,
                str(step.get("required_permission") or "none"),
                str(step.get("risk_level") or risk_level),
            ),
        )
    if supersedes_procedure_id is not None:
        connection.execute(
            """UPDATE memory.procedures
               SET state='superseded',review_state='accepted',updated_at=now()
               WHERE namespace_id=%s AND id=%s""",
            (namespace_id, supersedes_procedure_id),
        )
        connection.execute(
            """UPDATE retrieval.documents
               SET lifecycle_state='superseded',indexed_at=now()
               WHERE source_kind='procedure' AND source_id=%s""",
            (supersedes_procedure_id,),
        )
    search_text = " · ".join([title, goal, *(str(step["instruction"]) for step in steps)])
    _upsert_document(connection, namespace_id, "procedure", procedure_id, search_text, "candidate")
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,
             reason,correlation_id,metadata_redacted
           ) VALUES (
             %s,%s,'user',%s,'procedure.create','procedure',%s,%s,%s,%s::jsonb
           )""",
        (
            new_uuid(),
            namespace_id,
            actor_id,
            procedure_id,
            reason,
            correlation_id,
            json.dumps(
                {
                    "supersedes_procedure_id": (
                        str(supersedes_procedure_id) if supersedes_procedure_id else None
                    ),
                    "version": replacement_version,
                }
            ),
        ),
    )
    return get_procedure(connection, namespace_key, procedure_id)  # type: ignore[return-value]


def set_procedure_state(
    connection: Connection,
    *,
    namespace_key: str,
    procedure_id: UUID,
    expected_version: int,
    action: str,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict | None:
    namespace_id = stable_uuid("namespace", namespace_key)
    state_map = {
        "confirm": ("active", "accepted"),
        "disable": ("disabled", "accepted"),
        "supersede": ("superseded", "accepted"),
    }
    if action not in state_map:
        raise ValueError("PROCEDURE_ACTION_INVALID")
    if action == "confirm":
        support = connection.execute(
            """SELECT 1 FROM memory.procedure_support support
               JOIN memory.episodes episode ON episode.id=support.episode_id
               JOIN memory.episode_steps step ON step.episode_id=episode.id
               WHERE support.procedure_id=%s AND support.support_kind='success'
                 AND episode.state='active'
                 AND episode.review_state='accepted'
                 AND step.status='confirmed'
                 AND step.step_kind IN ('result','resolution','verification')
               LIMIT 1""",
            (procedure_id,),
        ).fetchone()
        if not support:
            raise ValueError("PROCEDURE_VERIFIED_EPISODE_REQUIRED")
        stop = connection.execute(
            """SELECT 1 FROM memory.procedure_steps
               WHERE procedure_id=%s AND length(trim(stop_condition))>0 LIMIT 1""",
            (procedure_id,),
        ).fetchone()
        if not stop:
            raise ValueError("PROCEDURE_STOP_CONDITION_REQUIRED")
    state, review = state_map[action]
    row = connection.execute(
        """UPDATE memory.procedures SET state=%s,review_state=%s,
                                        version=version+1,updated_at=now()
           WHERE namespace_id=%s AND id=%s AND version=%s
           RETURNING id""",
        (state, review, namespace_id, procedure_id, expected_version),
    ).fetchone()
    if row is None:
        exists = connection.execute(
            "SELECT 1 FROM memory.procedures WHERE namespace_id=%s AND id=%s",
            (namespace_id, procedure_id),
        ).fetchone()
        if exists:
            raise ValueError("VERSION_CONFLICT")
        return None
    connection.execute(
        "UPDATE retrieval.documents SET lifecycle_state=%s,indexed_at=now() "
        "WHERE source_kind='procedure' AND source_id=%s",
        (state, procedure_id),
    )
    connection.execute(
        """INSERT INTO audit.events(
             id,namespace_id,actor_type,actor_id,action,target_type,target_id,
             reason,correlation_id
           ) VALUES (%s,%s,'user',%s,%s,'procedure',%s,%s,%s)""",
        (
            new_uuid(),
            namespace_id,
            actor_id,
            f"procedure.{action}",
            procedure_id,
            reason,
            correlation_id,
        ),
    )
    return get_procedure(connection, namespace_key, procedure_id)


def bulk_govern_unified_targets(
    connection: Connection,
    *,
    namespace_key: str,
    targets: list[dict],
    action: str,
    preview_only: bool,
    actor_id: str,
    reason: str,
    correlation_id: UUID,
) -> dict:
    """Preview or atomically govern typed memory objects with optimistic versions."""
    if action not in {"confirm", "forget", "isolate"}:
        raise ValueError("BULK_ACTION_INVALID")
    namespace_id = stable_uuid("namespace", namespace_key)
    lookup = {
        "fact": (
            "memory.facts",
            "memory_state",
            "statement",
        ),
        "episode": (
            "memory.episodes",
            "state",
            "title || ' · ' || summary",
        ),
        "preference": (
            "memory.preference_assertions",
            "state",
            "'偏好 ' || polarity || ' · ' || aspect",
        ),
        "relationship": (
            "memory.relationship_assertions",
            "state",
            "label",
        ),
        "temporal_rule": (
            "memory.temporal_rules",
            "state",
            "label || ' · ' || month || '月' || day || '日'",
        ),
        "procedure": (
            "memory.procedures",
            "state",
            "title || ' · ' || goal",
        ),
    }
    state_targets = {
        "fact": {"confirm": "active", "forget": "forgotten", "isolate": "isolated"},
        "episode": {"confirm": "active", "forget": "forgotten", "isolate": "isolated"},
        "preference": {"confirm": "active", "forget": "forgotten", "isolate": "isolated"},
        "relationship": {"confirm": "active", "forget": "forgotten", "isolate": "isolated"},
        "temporal_rule": {
            "confirm": "active",
            "forget": "forgotten",
            "isolate": "isolated",
        },
        "procedure": {"confirm": "active", "forget": "disabled", "isolate": "disabled"},
    }
    preview_items: list[dict] = []
    for target in targets:
        kind = str(target["memory_kind"])
        if kind not in lookup:
            raise ValueError("BULK_MEMORY_KIND_INVALID")
        table, state_column, statement_sql = lookup[kind]
        row = connection.execute(
            f"""SELECT id,{state_column} AS state,version,{statement_sql} AS statement
                FROM {table} WHERE namespace_id=%s AND id=%s FOR UPDATE""",
            (namespace_id, target["memory_id"]),
        ).fetchone()
        if row is None:
            raise ValueError("BULK_MEMORY_NOT_FOUND")
        if int(row[2]) != int(target["expected_version"]):
            raise ValueError("VERSION_CONFLICT")
        preview_items.append(
            {
                "memory_id": row[0],
                "memory_kind": kind,
                "statement": redact_text(row[3]).text,
                "state": row[1],
                "version": row[2],
                "target_state": state_targets[kind][action],
            }
        )
    if preview_only:
        return {
            "preview_only": True,
            "action": action,
            "count": len(preview_items),
            "items": preview_items,
            "correlation_id": correlation_id,
        }

    results: list[dict] = []
    for target, item in zip(targets, preview_items, strict=True):
        kind = item["memory_kind"]
        memory_id = target["memory_id"]
        version = target["expected_version"]
        if kind == "fact":
            row = connection.execute(
                """UPDATE memory.facts
                   SET memory_state=%s,version=version+1,updated_at=now()
                   WHERE namespace_id=%s AND id=%s AND version=%s
                     AND memory_state<>'purge_requested'
                   RETURNING version""",
                (item["target_state"], namespace_id, memory_id, version),
            ).fetchone()
            if row is None:
                raise ValueError("VERSION_CONFLICT")
            connection.execute(
                """UPDATE retrieval.documents SET lifecycle_state=%s,indexed_at=now()
                   WHERE source_kind='fact' AND source_id=%s""",
                (item["target_state"], memory_id),
            )
            if action in {"forget", "isolate"}:
                invalidate_unified_dependents(connection, namespace_id, memory_id)
            connection.execute(
                """INSERT INTO audit.events(
                     id,namespace_id,actor_type,actor_id,action,target_type,target_id,
                     reason,correlation_id,metadata_redacted
                   ) VALUES (
                     %s,%s,'user',%s,%s,'fact',%s,%s,%s,'{"bulk":true}'::jsonb
                   )""",
                (
                    new_uuid(),
                    namespace_id,
                    actor_id,
                    f"memory.{action}",
                    memory_id,
                    redact_text(reason).text,
                    correlation_id,
                ),
            )
            result_version = int(row[0])
        elif kind == "episode":
            result = set_episode_review(
                connection,
                namespace_key=namespace_key,
                episode_id=memory_id,
                expected_version=version,
                action=action,
                actor_id=actor_id,
                reason=reason,
                correlation_id=correlation_id,
            )
            result_version = int(result["version"])  # type: ignore[index]
        elif kind == "preference":
            result = set_preference_state(
                connection,
                namespace_key=namespace_key,
                preference_id=memory_id,
                expected_version=version,
                action=action,
                actor_id=actor_id,
                reason=reason,
                correlation_id=correlation_id,
            )
            result_version = int(result["version"])  # type: ignore[index]
        elif kind == "relationship":
            result = set_relationship_state(
                connection,
                namespace_key=namespace_key,
                relationship_id=memory_id,
                expected_version=version,
                action=action,
                actor_id=actor_id,
                reason=reason,
                correlation_id=correlation_id,
            )
            result_version = int(result["version"])  # type: ignore[index]
        elif kind == "temporal_rule":
            result = set_temporal_rule_state(
                connection,
                namespace_key=namespace_key,
                rule_id=memory_id,
                expected_version=version,
                action=action,
                actor_id=actor_id,
                reason=reason,
                correlation_id=correlation_id,
            )
            result_version = int(result["version"])  # type: ignore[index]
        else:
            result = set_procedure_state(
                connection,
                namespace_key=namespace_key,
                procedure_id=memory_id,
                expected_version=version,
                action="confirm" if action == "confirm" else "disable",
                actor_id=actor_id,
                reason=reason,
                correlation_id=correlation_id,
            )
            result_version = int(result["version"])  # type: ignore[index]
        results.append({**item, "state": item["target_state"], "version": result_version})
    return {
        "preview_only": False,
        "action": action,
        "count": len(results),
        "items": results,
        "correlation_id": correlation_id,
    }
