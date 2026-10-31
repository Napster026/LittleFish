"""情绪事件协议、纯提案处理器和可持久化的单次回归计划。"""

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, StrictStr, field_validator

from core.events import AgentEvent, EventProvenance
from core.scheduler import ScheduledEvent, normalize_schedule_time
from core.settlement import EmotionTimeConflictError, StateChangeProposal
from core.state import AgentState
from persona.emotion import EmotionChange, EmotionDynamics, EmotionEngine, EmotionImpulse, EmotionState
from persona.needs import parse_need_time


EMOTION_TICK_EVENT_TYPE = "INTERNAL.emotion_tick"
EMOTION_IMPULSE_EVENT_TYPE = "INTERNAL.emotion_impulse"
EMOTION_INIT_EVENT_TYPE = "SYSTEM.emotion_initialized"
EMOTION_SOURCE = "persona.emotion"


def _identifier(kind: str, *parts: str) -> str:
    identity = json.dumps([kind, *parts], ensure_ascii=False, separators=(",", ":"))
    return f"emotion-{kind}:{uuid5(NAMESPACE_URL, identity)}"


class _EmotionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_id: StrictStr

    @field_validator("agent_id")
    @classmethod
    def agent(cls, value):
        return AgentState.validate_agent_id(value)


class EmotionTick(_EmotionSpec):
    from_at: AwareDatetime
    dynamics: EmotionDynamics

    @field_validator("from_at", mode="before")
    @classmethod
    def time(cls, value):
        return parse_need_time(value)


class EmotionImpact(_EmotionSpec):
    source_event_id: StrictStr
    impulse: EmotionImpulse

    @field_validator("source_event_id")
    @classmethod
    def identity(cls, value):
        return AgentState.validate_agent_id(value)


class EmotionInitialization(_EmotionSpec):
    state: EmotionState


def build_emotion_tick(agent_id: str, *, from_at: datetime, dynamics: EmotionDynamics) -> ScheduledEvent:
    spec = EmotionTick(agent_id=agent_id, from_at=from_at, dynamics=dynamics)
    due_at = spec.from_at + timedelta(seconds=spec.dynamics.tick_interval_seconds)
    identifier = _identifier("tick", agent_id, spec.from_at.isoformat(timespec="microseconds"))
    event = AgentEvent(
        event_id=identifier, event_type=EMOTION_TICK_EVENT_TYPE, timestamp=due_at,
        source=EMOTION_SOURCE, provenance=EventProvenance.OBSERVED,
        correlation_id=f"emotion:{agent_id}", payload=spec.model_dump(mode="json"),
    )
    return ScheduledEvent(schedule_id=identifier, agent_id=agent_id, event=event, due_at=due_at)


def build_emotion_initialization(agent_id: str, initial: EmotionState) -> AgentEvent:
    spec = EmotionInitialization(agent_id=agent_id, state=initial)
    return AgentEvent(
        event_id=_identifier("init", agent_id), event_type=EMOTION_INIT_EVENT_TYPE,
        timestamp=spec.state.updated_at, source=EMOTION_SOURCE, provenance=EventProvenance.OBSERVED,
        correlation_id=f"emotion:{agent_id}", payload=spec.model_dump(mode="json"),
    )


def _impact_event(spec: EmotionImpact, timestamp: datetime) -> AgentEvent:
    return AgentEvent(
        event_id=_identifier("impulse", spec.agent_id, spec.source_event_id),
        event_type=EMOTION_IMPULSE_EVENT_TYPE, timestamp=timestamp, source=EMOTION_SOURCE,
        provenance=EventProvenance.INFERRED, causation_id=spec.source_event_id,
        correlation_id=f"emotion:{spec.agent_id}", payload=spec.model_dump(mode="json"),
    )


def build_emotion_impulse(agent_id: str, source_event: AgentEvent, impulse: EmotionImpulse) -> AgentEvent:
    """由可信评价规则显式调用；每个角色的每个原事件最多产生一次冲击。"""
    source_event = AgentEvent.model_validate(source_event.model_dump())
    return _impact_event(EmotionImpact(
        agent_id=agent_id, source_event_id=source_event.event_id, impulse=impulse,
    ), source_event.timestamp)


def parse_emotion_event(event: AgentEvent) -> EmotionTick | EmotionImpact | EmotionInitialization:
    event = AgentEvent.model_validate(event.model_dump())
    if event.event_type == EMOTION_TICK_EVENT_TYPE:
        spec = EmotionTick.model_validate(event.payload)
        expected = build_emotion_tick(spec.agent_id, from_at=spec.from_at, dynamics=spec.dynamics).event
    elif event.event_type == EMOTION_IMPULSE_EVENT_TYPE:
        spec = EmotionImpact.model_validate(event.payload)
        expected = _impact_event(spec, event.timestamp)
    elif event.event_type == EMOTION_INIT_EVENT_TYPE:
        spec = EmotionInitialization.model_validate(event.payload)
        expected = build_emotion_initialization(spec.agent_id, spec.state)
    else:
        raise ValueError("不是情绪事件")
    if event != expected:
        raise ValueError("情绪事件与其稳定身份、来源或规则快照不一致")
    return spec


def build_emotion_proposal(
    event: AgentEvent, state: AgentState, *, to_at: datetime,
) -> StateChangeProposal | None:
    spec = parse_emotion_event(event)
    if spec.agent_id != state.agent_id:
        raise ValueError("情绪事件不属于当前角色")
    to_at = normalize_schedule_time(to_at)
    if to_at < event.timestamp or to_at < state.updated_at:
        raise ValueError("情绪处理时间不能早于原事件或角色更新时间")
    if isinstance(spec, EmotionInitialization):
        if state.emotion is not None or to_at != spec.state.updated_at:
            raise ValueError("只能在显式初始时刻初始化一次情绪")
        change = EmotionChange(from_at=None, state=spec.state)
    else:
        if state.emotion is None:
            raise ValueError("请先显式启用情绪")
        engine = EmotionEngine()
        if isinstance(spec, EmotionTick):
            if spec.dynamics != state.emotion.dynamics:
                raise ValueError("情绪计划规则与角色持久化规则不同")
            if spec.from_at < state.emotion.updated_at:
                return None  # 事件冲击已推进游标，旧时钟链自然失效。
            if spec.from_at != state.emotion.updated_at:
                raise EmotionTimeConflictError("情绪计划依据了尚未出现的游标")
            change = engine.advance(state.emotion, to_at=to_at)
        else:
            change = engine.apply_impulse(state.emotion, spec.impulse, at=to_at)
    if change is None:
        return None
    return StateChangeProposal(
        proposal_id=_identifier("proposal", event.event_id), event_id=event.event_id,
        agent_id=state.agent_id, expected_version=state.version, needs_delta=None,
        emotion_change=change,
    )


class EmotionEventHandler:
    """分别注册 tick 和 impulse 两种事件；与运行时共享时钟。"""

    def __init__(self, *, clock: Callable[[], datetime] | None = None):
        if clock is not None and not callable(clock):
            raise ValueError("clock 必须可调用")
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    async def __call__(self, event: AgentEvent, state: AgentState) -> StateChangeProposal | None:
        return build_emotion_proposal(event, state, to_at=self._clock())
