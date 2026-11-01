"""应用层自主活动编排；作为调度回调使用，不注册到纯提案路由器。"""

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, model_validator

from ..activity.models import ActivityRecord, ActivityStatus
from ..activity.selection import ActivitySelector
from ..activity.service import ActivityService
from ..activity.ticks import TICK_EVENT_TYPE
from .needs import NEEDS_TICK_EVENT_TYPE, parse_need_tick
from .emotion import EMOTION_TICK_EVENT_TYPE, EMOTION_IMPULSE_EVENT_TYPE, parse_emotion_event
from ..core.events import AgentEvent
from ..core.arbiter import (
    ActiveBehavior, ArbitrationContext, BehaviorArbiter, BehaviorCandidate, BehaviorDecision, BehaviorKind,
    UtilityBreakdown,
)
from ..core.runtime import AgentRuntime
from ..core.scheduler import normalize_schedule_time
from ..core.state import AgentState
from ..persona.emotion import EmotionEngine
from ..social.expression import ExpressionIntent


SELECTION_EVENT_TYPE = "INTERNAL.activity_selected"
SELECTION_SOURCE = "application.autonomy"


class BehaviorReservation(BaseModel):
    """仲裁结果与应用层绑定的实例；内核不依赖 ActivityRecord。"""
    model_config = ConfigDict(extra="forbid", frozen=True)
    decision: BehaviorDecision
    activity: ActivityRecord | None = None
    expression: ExpressionIntent | None = None

    @model_validator(mode="after")
    def validate_binding(self):
        winner = self.decision.winner
        if winner.kind==BehaviorKind.EXPRESS:
            if self.expression is None or self.expression.target.scope_id!=winner.target_scope_id or self.activity is not None:
                raise ValueError("主动表达必须绑定匹配的意图")
        elif self.expression is not None:
            raise ValueError("其他行为不能绑定主动表达意图")
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
    *, to_at: datetime | None = None, conversation_reason=None, expression_candidate=None, expression_pending=False,
) -> BehaviorDecision:
    """把需求候选和持久化占用快照转换为内核输入；接续使用原定义快照。"""
    context = ArbitrationContext(
        conversation_reason=conversation_reason, expression_pending=expression_pending,
        active=tuple(ActiveBehavior(
            activity_id=r.instance.activity_id, definition_id=r.instance.definition_id,
            mode=r.instance.mode.value, status=r.instance.status.value,
        ) for r in active), recoverable_activity_ids=recoverable,
    )
    effective_emotion = state.emotion
    if selector.emotion_influence_enabled and effective_emotion is not None and to_at is not None:
        advanced = EmotionEngine().advance(effective_emotion, to_at=to_at)
        if advanced is not None:
            effective_emotion = advanced.state
    candidates = tuple(BehaviorCandidate(
        candidate_id=f"start:{e.definition.definition_id}", kind=BehaviorKind.START_ACTIVITY,
        definition_id=e.definition.definition_id, mode="simulated", utility=e.final_score,
        unavailable_reason=e.reason,
        score_breakdown=UtilityBreakdown(
            need_score=e.score, emotion_adjustment=e.emotion_adjustment, final_score=e.final_score,
        ),
    ) for e in selector.evaluate(state.needs, emotion=effective_emotion))
    candidates += tuple(BehaviorCandidate(
        candidate_id=f"recover:{r.instance.activity_id}", kind=BehaviorKind.START_ACTIVITY,
        definition_id=r.instance.definition_id, activity_id=r.instance.activity_id,
        mode=r.instance.mode.value,
    ) for r in active if r.instance.activity_id in recoverable)
    if expression_candidate is not None:
        candidates+=(expression_candidate,)
    return arbiter.decide(context, candidates)


def selection_id(agent_id: str, decision_key: str) -> str:
    AgentState.validate_agent_id(agent_id)
    if not isinstance(decision_key, str) or not decision_key.strip():
        raise ValueError("decision_key 必须是非空字符串，重试时沿用原值")
    identity = json.dumps([agent_id, decision_key], ensure_ascii=False, separators=(",", ":"))
    return f"autonomy:{uuid5(NAMESPACE_URL, identity)}"


class AutonomyStore(Protocol):
    async def get_state(self, agent_id: str) -> AgentState | None: ...

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
        arbiter: BehaviorArbiter | None = None, dispatch_expression=None,
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
        self._dispatch_expression=dispatch_expression
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
            if reservation.decision.winner.kind==BehaviorKind.EXPRESS:
                if self._dispatch_expression is not None:
                    self._dispatch_expression(reservation.expression)
                return None
            if reservation.decision.winner.kind != BehaviorKind.START_ACTIVITY:
                return None
            record = reservation.activity
            if record is not None and record.instance.status == ActivityStatus.PENDING:
                return await self._activities.start_activity(record.instance.activity_id)
            return record

    async def recover(self) -> ActivityRecord | None:
        """同一状态版本复用启动回执；仍可接续已选未启动的原实例。"""
        state = await self._storage.get_state(self.agent_id)
        if state is None:
            raise ValueError("请先创建角色状态")
        return await self.consider(f"startup:{state.version}")

    async def handle_event(self, event: AgentEvent) -> None:
        event = AgentEvent.model_validate(event.model_dump())
        if event.event_type == TICK_EVENT_TYPE:
            await self._activities.handle_event(event)
        elif event.event_type == NEEDS_TICK_EVENT_TYPE:
            if parse_need_tick(event).agent_id != self.agent_id:
                raise ValueError("需求事件不属于当前角色")
            await self._runtime.submit(event)
        elif event.event_type in (EMOTION_TICK_EVENT_TYPE, EMOTION_IMPULSE_EVENT_TYPE):
            if parse_emotion_event(event).agent_id != self.agent_id:
                raise ValueError("情绪事件不属于当前角色")
            await self._runtime.submit(event)
            if not self._selector.emotion_influence_enabled:
                return
        else:
            await self._runtime.submit(event)
            return
        # 必须等待上一步真正提交，而非仅仅入队，才能按最新需求做决定。
        await self.consider(event.event_id)
