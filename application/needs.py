"""需求时钟接线：严格事件、纯提案处理器与单次后续计划。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictStr, field_validator

from ..core.events import AgentEvent, EventProvenance
from ..core.scheduler import ScheduledEvent, normalize_schedule_time
from ..core.settlement import NeedTimeConflictError, StateChangeProposal
from ..core.state import AgentState
from ..persona.needs import NeedEngine, NeedRates, parse_need_time


NEEDS_TICK_EVENT_TYPE = "INTERNAL.needs_tick"
NEEDS_TICK_SOURCE = "persona.needs"


# noinspection PyNestedDecorators
class NeedTick(BaseModel):
    """计划保存前一游标、规则快照及间隔，重试不能偷偷切换速率。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: StrictStr
    from_at: AwareDatetime | None
    rates_per_hour: NeedRates
    interval_seconds: Annotated[float, Field(ge=0.001, le=86400, allow_inf_nan=False)]

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, value: str) -> str:
        return AgentState.validate_agent_id(value)

    @field_validator("from_at", mode="before")
    @classmethod
    def validate_from_at(cls, value: Any) -> datetime | None:
        return parse_need_time(value)

    @field_validator("interval_seconds", mode="before")
    @classmethod
    def validate_interval(cls, value: Any) -> Any:
        if type(value) not in (int, float):
            raise ValueError("需求检查间隔必须是数值")
        return value


def build_need_tick(
    agent_id: str, *, from_at: datetime | None, rates: NeedRates,
    interval_seconds: float = 60.0, baseline_at: datetime | None = None,
) -> ScheduledEvent:
    spec = NeedTick(
        agent_id=agent_id, from_at=from_at, rates_per_hour=rates, interval_seconds=interval_seconds,
    )
    if spec.from_at is None:
        if baseline_at is None:
            raise ValueError("首次需求计划必须提供基线检查时间")
        due_at = normalize_schedule_time(baseline_at)
    else:
        due_at = spec.from_at + timedelta(seconds=spec.interval_seconds)
    identity = json.dumps(
        ["persona.needs.tick", spec.agent_id,
         None if spec.from_at is None else spec.from_at.isoformat(timespec="microseconds")],
        ensure_ascii=False, separators=(",", ":"),
    )
    identifier = f"needs-tick:{uuid5(NAMESPACE_URL, identity)}"
    event = AgentEvent(
        event_id=identifier, event_type=NEEDS_TICK_EVENT_TYPE, timestamp=due_at,
        source=NEEDS_TICK_SOURCE, provenance=EventProvenance.OBSERVED,
        correlation_id=f"needs:{spec.agent_id}", payload=spec.model_dump(mode="json"),
    )
    return ScheduledEvent(schedule_id=identifier, agent_id=spec.agent_id, event=event, due_at=due_at)


def parse_need_tick(event: AgentEvent) -> NeedTick:
    if (
        event.event_type != NEEDS_TICK_EVENT_TYPE or event.source != NEEDS_TICK_SOURCE
        or event.provenance != EventProvenance.OBSERVED
    ):
        raise ValueError("不是需求时钟事件")
    spec = NeedTick.model_validate(event.payload)
    expected = build_need_tick(
        spec.agent_id, from_at=spec.from_at, rates=spec.rates_per_hour,
        interval_seconds=spec.interval_seconds, baseline_at=event.timestamp,
    )
    if event != expected.event:
        raise ValueError("需求事件与其规则快照或稳定标识不一致")
    return spec


def build_need_proposal(event: AgentEvent, state: AgentState, *, to_at: datetime) -> StateChangeProposal | None:
    """计算时点显式传入，供路由处理器与事务内提交校验复用。"""
    spec = parse_need_tick(event)
    if spec.agent_id != state.agent_id:
        raise ValueError("需求事件不属于当前角色")
    to_at = normalize_schedule_time(to_at)
    if to_at < event.timestamp or to_at < state.updated_at:
        raise ValueError("需求处理时间不能早于计划或角色更新时间")
    if state.needs_updated_at != spec.from_at:
        if state.needs_updated_at is not None and state.needs_updated_at >= event.timestamp:
            return None  # 已有较新的时钟链，不重算旧时间，也不再延长旧链。
        raise NeedTimeConflictError("需求事件依据的时间游标不匹配")
    update = NeedEngine(spec.rates_per_hour).advance(state.needs, from_at=spec.from_at, to_at=to_at)
    if update is None:
        return None
    identifier = str(uuid5(NAMESPACE_URL, f"persona.needs.proposal:{event.event_id}"))
    return StateChangeProposal(
        proposal_id=identifier, event_id=event.event_id, agent_id=state.agent_id,
        expected_version=state.version, needs_delta=update.delta, needs_time=update.time,
    )


class NeedTickHandler:
    """可注册进 EventRouter，只计算提案；clock 与 AgentRuntime 使用同一时钟。"""

    def __init__(self, *, clock: Callable[[], datetime] | None = None):
        if clock is not None and not callable(clock):
            raise ValueError("clock 必须可调用")
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    async def __call__(self, event: AgentEvent, state: AgentState) -> StateChangeProposal | None:
        return build_need_proposal(event, state, to_at=normalize_schedule_time(self._clock()))
