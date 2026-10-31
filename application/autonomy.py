"""应用层自主活动编排；作为调度回调使用，不注册到纯提案路由器。"""

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, model_validator

from activity.models import ActivityRecord, ActivityStatus
from activity.selection import ActivitySelector
from activity.service import ActivityService
from activity.ticks import TICK_EVENT_TYPE
from application.needs import NEEDS_TICK_EVENT_TYPE, parse_need_tick
from core.events import AgentEvent
from core.arbiter import (
    ActiveBehavior, ArbitrationContext, BehaviorArbiter, BehaviorCandidate, BehaviorDecision, BehaviorKind,
)
from core.runtime import AgentRuntime
from core.scheduler import normalize_schedule_time
from core.state import AgentState


SELECTION_EVENT_TYPE = "INTERNAL.activity_selected"
SELECTION_SOURCE = "application.autonomy"


class BehaviorReservation(BaseModel):
    """仲裁结果与应用层绑定的实例；内核不依赖 ActivityRecord。"""
    model_config = ConfigDict(extra="forbid", frozen=True)
    decision: BehaviorDecision
    activity: ActivityRecord | None = None

    @model_validator(mode="after")
    def validate_binding(self):
        winner = self.decision.winner
        if winner.kind == BehaviorKind.START_ACTIVITY and self.activity is None:
            raise ValueError("开始行为必须绑定已保存的活动")
        if self.activity is not None:
            instance = self.activity.instance
            if (winner.kind == BehaviorKind.IDLE
                    or winner.definition_id != instance.definition_id or winner.mode != instance.mode.value
                    or winner.activity_id not in (None, instance.activity_id)):
                raise ValueError("仲裁结果与绑定活动不一致")
        return self


def arbitrate_activity(
    selector: ActivitySelector, arbiter: BehaviorArbiter, state: AgentState,
    active: tuple[ActivityRecord, ...], recoverable: frozenset[str],
) -> BehaviorDecision:
    """把需求候选和持久化占用快照转换为内核输入；接续使用原定义快照。"""
    context = ArbitrationContext(
        active=tuple(ActiveBehavior(
            activity_id=r.instance.activity_id, definition_id=r.instance.definition_id,
            mode=r.instance.mode.value, status=r.instance.status.value,
        ) for r in active), recoverable_activity_ids=recoverable,
    )
    candidates = tuple(BehaviorCandidate(
        candidate_id=f"start:{e.definition.definition_id}", kind=BehaviorKind.START_ACTIVITY,
        definition_id=e.definition.definition_id, mode="simulated", utility=e.score,
        unavailable_reason=e.reason,
    ) for e in selector.evaluate(state.needs))
    candidates += tuple(BehaviorCandidate(
        candidate_id=f"recover:{r.instance.activity_id}", kind=BehaviorKind.START_ACTIVITY,
        definition_id=r.instance.definition_id, activity_id=r.instance.activity_id,
        mode=r.instance.mode.value,
    ) for r in active if r.instance.activity_id in recoverable)
    return arbiter.decide(context, candidates)


def selection_id(agent_id: str, decision_key: str) -> str:
    AgentState.validate_agent_id(agent_id)
    if not isinstance(decision_key, str) or not decision_key.strip():
        raise ValueError("decision_key 必须是非空字符串，重试时沿用原值")
    identity = json.dumps([agent_id, decision_key], ensure_ascii=False, separators=(",", ":"))
    return f"autonomy:{uuid5(NAMESPACE_URL, identity)}"


class AutonomyStore(Protocol):
    async def reserve_behavior(
        self, agent_id: str, selector: ActivitySelector, *, decision_key: str, now: datetime,
        arbiter: BehaviorArbiter,
    ) -> BehaviorReservation: ...


class AutonomousActivityService:
    """需求结算后和活动 tick 后检查空闲状态，开始一个有正收益的活动。

    宿主为每个角色创建一个实例，调用一次 recover，再将 handle_event
    作为该角色唯一 TimeScheduler 的回调。暂停不自动恢复，运行活动不抢占。
    """

    def __init__(
        self, agent_id: str, storage: AutonomyStore, selector: ActivitySelector,
        activities: ActivityService, runtime: AgentRuntime, *,
        clock: Callable[[], datetime] | None = None,
        arbiter: BehaviorArbiter | None = None,
    ):
        AgentState.validate_agent_id(agent_id)
        if activities.agent_id != agent_id or runtime.agent_id != agent_id:
            raise ValueError("自主服务、活动服务和运行时必须属于同一角色")
        if clock is not None and not callable(clock):
            raise ValueError("clock 必须可调用")
        self.agent_id = agent_id
        self._storage, self._selector = storage, selector
        self._activities, self._runtime = activities, runtime
        self._arbiter = arbiter if arbiter is not None else BehaviorArbiter()
        self.last_decision: BehaviorDecision | None = None
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._lock = asyncio.Lock()

    async def consider(self, decision_key: str) -> ActivityRecord | None:
        """同一个决策键只选择一次，包括空闲/忙碌决策；启动失败可重试同一实例。"""
        async with self._lock:
            reservation = await self._storage.reserve_behavior(
                self.agent_id, self._selector, decision_key=decision_key,
                now=normalize_schedule_time(self._clock()), arbiter=self._arbiter,
            )
            self.last_decision = reservation.decision
            if reservation.decision.winner.kind != BehaviorKind.START_ACTIVITY:
                return None
            record = reservation.activity
            if record is not None and record.instance.status == ActivityStatus.PENDING:
                return await self._activities.start_activity(record.instance.activity_id)
            return record

    async def recover(self) -> ActivityRecord | None:
        """启动时重新检查：接续已选未启动的实例，或为空闲角色做一次新决策。"""
        return await self.consider(f"startup:{uuid4()}")

    async def handle_event(self, event: AgentEvent) -> None:
        event = AgentEvent.model_validate(event.model_dump())
        if event.event_type == TICK_EVENT_TYPE:
            await self._activities.handle_event(event)
        elif event.event_type == NEEDS_TICK_EVENT_TYPE:
            if parse_need_tick(event).agent_id != self.agent_id:
                raise ValueError("需求事件不属于当前角色")
            await self._runtime.submit(event)
        else:
            await self._runtime.submit(event)
            return
        # 必须等待上一步真正提交，而非仅仅入队，才能按最新需求做决定。
        await self.consider(event.event_id)
