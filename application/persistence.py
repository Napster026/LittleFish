"""应用层事务协调：组合核心与活动业务规则，依赖通用 UnitOfWork 端口。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator

from activity.executor import ActivityContext, ActivityExecutionResult
from activity.models import (
    ActivityDefinition, ActivityInstance, ActivityRecord, ActivityStatus,
    ExecutionMode, create_activity_instance,
)
from activity.persistence import ActivityPersistence
from activity.selection import ActivitySelector
from application.autonomy import (
    SELECTION_EVENT_TYPE, SELECTION_SOURCE, BehaviorReservation, arbitrate_activity, selection_id,
)
from application.needs import build_need_proposal, build_need_tick, parse_need_tick
from application.emotion import (
    EmotionImpact, EmotionTick, build_emotion_initialization,
    build_emotion_impulse, build_emotion_proposal, build_emotion_tick, parse_emotion_event,
)
from core.events import AgentEvent, EventProvenance
from core.arbiter import BehaviorArbiter, BehaviorCandidate, BehaviorDecision, BehaviorKind
from core.persistence import CorePersistence
from core.scheduler import ScheduledEvent, ScheduleRecord, ScheduleStatus, normalize_schedule_time
from core.settlement import StateChangeProposal
from core.state import AgentState
from persistence.ports import UnitOfWork
from persistence.errors import StateNotFoundError
from persona.needs import NeedRates
from persona.emotion import EmotionState


class PersonaStore:
    """类型化业务提交入口；不是数据库适配器，也不自行执行 SQL。

    每次调用创建一个会话，在同一事务内组合所需业务规则。组合根显式
    注入 UnitOfWork 实现；业务消费者只依赖各自的最小存储协议。
    """

    def __init__(self, backend: UnitOfWork):
        self._backend = backend

    async def __aenter__(self) -> PersonaStore:
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.close()

    @asynccontextmanager
    async def _repositories(self) -> AsyncIterator[tuple[CorePersistence, ActivityPersistence]]:
        async with self._backend.transaction() as session:
            core = CorePersistence(session)
            yield core, ActivityPersistence(session, core)

    async def open(self) -> None:
        await self._backend.open()
        try:
            # 业务修复属于应用启动，不属于数据库连接或 schema 迁移。
            await self.reconcile_exhausted_ticks()
        except BaseException:
            await self._backend.close()
            raise

    async def close(self) -> None:
        await self._backend.close()

    async def reconcile_exhausted_ticks(self) -> None:
        async with self._repositories() as (_, activities):
            await activities.reconcile_exhausted_ticks()

    async def create_state(self, state: AgentState) -> None:
        async with self._repositories() as (core, _):
            await core.create_state(state)

    async def get_state(self, agent_id: str) -> AgentState | None:
        async with self._repositories() as (core, _):
            return await core.get_state(agent_id)

    async def record_event(self, event: AgentEvent) -> None:
        async with self._repositories() as (core, _):
            await core.record_event(event)

    async def get_event(self, event_id: str) -> AgentEvent | None:
        async with self._repositories() as (core, _):
            return await core.get_event(event_id)

    async def get_committed_proposal(self, agent_id: str, event_id: str) -> StateChangeProposal | None:
        async with self._repositories() as (core, _):
            return await core.get_committed_proposal(agent_id, event_id)

    async def commit_proposal(self, proposal: StateChangeProposal, *, settled_at: datetime) -> AgentState:
        async with self._repositories() as (core, _):
            if proposal.emotion_change is not None:
                return await self._commit_emotion(core, proposal, settled_at=settled_at)
            if proposal.needs_time is None:
                return await core.commit_proposal(proposal, settled_at=settled_at)
            event = await core.get_event(proposal.event_id)
            if event is None:
                raise ValueError("需求时间提案缺少原始事件")
            spec = parse_need_tick(event)
            plan = await core.get_schedule(proposal.agent_id, event.event_id)
            expected_plan = build_need_tick(
                spec.agent_id, from_at=spec.from_at, rates=spec.rates_per_hour,
                interval_seconds=spec.interval_seconds, baseline_at=event.timestamp,
            )
            if plan is None or plan.schedule != expected_plan or spec.agent_id != proposal.agent_id:
                raise ValueError("需求时间提案缺少匹配的持久化计划")
            committed = await core.get_committed_proposal(proposal.agent_id, proposal.event_id)
            if committed is None:
                if plan.status == ScheduleStatus.FAILED:
                    raise ValueError("需求计划已经耗尽重试次数")
                current = await core.get_state(proposal.agent_id)
                if current is None:
                    raise StateNotFoundError("需求角色不存在")
                expected = build_need_proposal(event, current, to_at=proposal.needs_time.to_at)
                if expected != proposal:
                    raise ValueError("需求时间提案与当前状态或计划规则不一致")
            result = await core.commit_proposal(proposal, settled_at=settled_at)
            # 与需求值、时间游标和结算回执原子保存；重放不重置原计划状态。
            await core.create_schedule(build_need_tick(
                proposal.agent_id, from_at=proposal.needs_time.to_at, rates=spec.rates_per_hour,
                interval_seconds=spec.interval_seconds,
            ))
            return result

    @staticmethod
    async def _commit_emotion(
        core: CorePersistence, proposal: StateChangeProposal, *, settled_at: datetime,
    ) -> AgentState:
        event = await core.get_event(proposal.event_id)
        if event is None:
            raise ValueError("情绪提案缺少原始事件")
        spec = parse_emotion_event(event)
        if spec.agent_id != proposal.agent_id:
            raise ValueError("情绪提案与事件角色不同")
        committed = await core.get_committed_proposal(proposal.agent_id, proposal.event_id)
        if isinstance(spec, EmotionTick):
            plan = await core.get_schedule(proposal.agent_id, event.event_id)
            expected_plan = build_emotion_tick(spec.agent_id, from_at=spec.from_at, dynamics=spec.dynamics)
            if plan is None or plan.schedule != expected_plan:
                raise ValueError("情绪提案缺少匹配的持久化计划")
            if committed is None and plan.status == ScheduleStatus.FAILED:
                raise ValueError("情绪计划已经耗尽重试次数")
        elif isinstance(spec, EmotionImpact):
            original = await core.get_event(spec.source_event_id)
            if original is None or build_emotion_impulse(spec.agent_id, original, spec.impulse) != event:
                raise ValueError("情绪冲击缺少匹配的原事件")
        if committed is None:
            current = await core.get_state(proposal.agent_id)
            if current is None:
                raise StateNotFoundError("情绪角色不存在")
            expected = build_emotion_proposal(
                event, current, to_at=proposal.emotion_change.state.updated_at,
            )
            if proposal != expected:
                raise ValueError("情绪提案与当前状态或事件规则不一致")
        result = await core.commit_proposal(proposal, settled_at=settled_at)
        candidate = proposal.emotion_change.state
        # 情绪、游标、结算回执、下一次计划共同提交；重复提交不重置计划。
        await core.create_schedule(build_emotion_tick(
            proposal.agent_id, from_at=candidate.updated_at, dynamics=candidate.dynamics,
        ))
        return result

    async def start_emotion_clock(
        self, agent_id: str, *, now: datetime, initial: EmotionState | None = None,
    ) -> ScheduleRecord:
        """显式初始化并保存第一条计划，重启时省略 initial 找回当前计划。"""
        now = normalize_schedule_time(now)
        async with self._repositories() as (core, _):
            state = await core.get_state(agent_id)
            if state is None:
                raise StateNotFoundError("请先创建角色")
            if now < state.updated_at:
                raise ValueError("启动时间不能早于角色更新时间")
            if state.emotion is None:
                if initial is None or initial.updated_at != now:
                    raise ValueError("首次启用必须显式提供当前时刻的初始情绪")
                event = build_emotion_initialization(agent_id, initial)
                await core.record_event(event)
                proposal = build_emotion_proposal(event, state, to_at=now)
                if proposal is None:
                    raise ValueError("初始化必须产生情绪提案")
                state = await self._commit_emotion(core, proposal, settled_at=now)
            elif initial is not None and initial != state.emotion:
                raise ValueError("已有情绪不能重新初始化；重启请省略 initial")
            plan = build_emotion_tick(
                agent_id, from_at=state.emotion.updated_at, dynamics=state.emotion.dynamics,
            )
            existing = await core.get_schedule(agent_id, plan.schedule_id)
            if existing is not None:
                if existing.schedule != plan or existing.status != ScheduleStatus.PENDING:
                    raise ValueError("情绪计划已结束或规则不同，请检查处理器或失败原因")
                return existing
            return await core.create_schedule(plan)

    async def start_needs_clock(
        self, agent_id: str, *, rates: NeedRates, now: datetime, interval_seconds: float = 60.0,
    ) -> ScheduleRecord:
        """建立或找回单条需求时钟链；不热改已有规则或重置失败计划。"""
        now = normalize_schedule_time(now)
        async with self._repositories() as (core, _):
            state = await core.get_state(agent_id)
            if state is None:
                raise StateNotFoundError("请先创建角色")
            if now < state.updated_at:
                raise ValueError("启动需求时钟的时间不能早于角色更新时间")
            plan = build_need_tick(
                agent_id, from_at=state.needs_updated_at, rates=rates,
                interval_seconds=interval_seconds, baseline_at=now,
            )
            existing = await core.get_schedule(agent_id, plan.schedule_id)
            if existing is not None:
                spec = parse_need_tick(existing.schedule.event)
                requested = parse_need_tick(plan.event)
                if spec != requested:
                    raise ValueError("已有需求计划规则不同，当前不支持热切换")
                if existing.status != ScheduleStatus.PENDING:
                    raise ValueError("需求计划已经结束但游标未继续，请检查处理器或失败原因")
                return existing
            return await core.create_schedule(plan)

    async def create_schedule(self, schedule: ScheduledEvent) -> ScheduleRecord:
        async with self._repositories() as (core, _):
            return await core.create_schedule(schedule)

    async def get_schedule(self, agent_id: str, schedule_id: str) -> ScheduleRecord | None:
        async with self._repositories() as (core, _):
            return await core.get_schedule(agent_id, schedule_id)

    async def list_due_schedules(self, agent_id: str, *, now: datetime, limit: int = 100) -> tuple[ScheduleRecord, ...]:
        async with self._repositories() as (core, _):
            return await core.list_due_schedules(agent_id, now=now, limit=limit)

    async def record_schedule_delivery(
        self, agent_id: str, schedule_id: str, *, expected_attempts: int,
        attempted_at: datetime, succeeded: bool, error: str | None = None,
    ) -> ScheduleRecord:
        async with self._repositories() as (core, activities):
            saved = await core.record_schedule_delivery(
                agent_id, schedule_id, expected_attempts=expected_attempts,
                attempted_at=attempted_at, succeeded=succeeded, error=error,
            )
            if saved.status == ScheduleStatus.FAILED:
                await activities.fail_exhausted_tick(saved)
            return saved

    async def create_activity(self, definition: ActivityDefinition, instance: ActivityInstance) -> ActivityRecord:
        async with self._repositories() as (_, activities):
            return await activities.create_activity(definition, instance)

    @staticmethod
    async def _recoverable_ids(core: CorePersistence, active: tuple[ActivityRecord, ...]) -> frozenset[str]:
        recoverable = set()
        for record in active:
            if record.instance.status != ActivityStatus.PENDING:
                continue
            origin = await core.get_event(record.instance.activity_id)
            if (origin is not None and origin.event_type == SELECTION_EVENT_TYPE
                    and origin.source == SELECTION_SOURCE
                    and origin.activity_id == record.instance.activity_id
                    and origin.payload.get("agent_id") == record.instance.agent_id):
                recoverable.add(record.instance.activity_id)
        return frozenset(recoverable)

    async def reserve_next_activity(
        self, agent_id: str, selector: ActivitySelector, *, decision_key: str, now: datetime,
    ) -> ActivityRecord | None:
        """兼容旧接口；内部仍必须通过默认行为仲裁。"""
        reserved = await self.reserve_behavior(
            agent_id, selector, decision_key=decision_key, now=now, arbiter=BehaviorArbiter(),
        )
        return reserved.activity if reserved.decision.winner.kind == BehaviorKind.START_ACTIVITY else None

    async def reserve_behavior(
        self, agent_id: str, selector: ActivitySelector, *, decision_key: str, now: datetime,
        arbiter: BehaviorArbiter,
    ) -> BehaviorReservation:
        """在同一事务内读取快照、仲裁、创建实例并保存结果；不调用执行器。"""
        identifier = selection_id(agent_id, decision_key)
        now = normalize_schedule_time(now)
        async with self._repositories() as (core, activities):
            receipt = await core.get_event(identifier)
            if receipt is not None:
                if (receipt.event_type != SELECTION_EVENT_TYPE or receipt.source != SELECTION_SOURCE
                        or receipt.payload.get("agent_id") != agent_id
                        or receipt.payload.get("decision_key") != decision_key):
                    raise ValueError("活动选择回执标识冲突")
                saved = None
                if receipt.activity_id is not None:
                    saved = await activities.get_activity(agent_id, receipt.activity_id)
                    if saved is None:
                        raise ValueError("选择回执引用的活动丢失")
                if "arbitration" in receipt.payload:
                    decision = BehaviorDecision.model_validate(receipt.payload["arbitration"])
                else:
                    # 兼容仲裁层引入之前的选择回执，无需重写旧事件。
                    winner = BehaviorCandidate(candidate_id="idle", kind=BehaviorKind.IDLE)
                    if saved is not None:
                        winner = BehaviorCandidate(
                            candidate_id=f"legacy:{saved.instance.activity_id}",
                            kind=BehaviorKind.START_ACTIVITY, activity_id=saved.instance.activity_id,
                            definition_id=saved.instance.definition_id, mode=saved.instance.mode.value,
                        )
                    decision = BehaviorDecision(winner=winner, reason="legacy_receipt")
                if (decision.winner.kind == BehaviorKind.START_ACTIVITY and saved is not None
                        and saved.instance.status == ActivityStatus.PENDING):
                    # 不重新选择新活动；仅授权接续原 pending，当前禁用策略立即生效。
                    state = await core.get_state(agent_id)
                    if state is None:
                        raise StateNotFoundError("角色不存在")
                    if now < state.updated_at:
                        raise ValueError("接续时间不能早于角色更新时间")
                    active = await activities.list_active_activities(agent_id)
                    decision = arbitrate_activity(
                        selector, arbiter, state, active, await self._recoverable_ids(core, active),
                    )
                    if (decision.winner.kind != BehaviorKind.START_ACTIVITY
                            or decision.winner.activity_id != saved.instance.activity_id):
                        saved = None
                return BehaviorReservation(decision=decision, activity=saved)
            state = await core.get_state(agent_id)
            if state is None:
                raise StateNotFoundError("请先创建角色")
            if now < state.updated_at:
                raise ValueError("选择时间不能早于角色更新时间")
            active = await activities.list_active_activities(agent_id)
            decision = arbitrate_activity(
                selector, arbiter, state, active, await self._recoverable_ids(core, active),
            )
            selected = None
            winner = decision.winner
            if winner.kind == BehaviorKind.START_ACTIVITY:
                if winner.activity_id is not None:
                    selected = next(r for r in active if r.instance.activity_id == winner.activity_id)
                else:
                    definition = next(d for d in selector.definitions if d.definition_id == winner.definition_id)
                    selected = await activities.create_activity(definition, create_activity_instance(
                        definition, activity_id=identifier, agent_id=agent_id,
                        mode=ExecutionMode.SIMULATED, created_at=now,
                    ))
            elif winner.kind == BehaviorKind.CONTINUE_ACTIVITY:
                selected = next(r for r in active if r.instance.activity_id == winner.activity_id)
            # 保留旧 reason/scores 字段语义；完整解释保存在 arbitration 中。
            reason = "busy" if active else "no_candidate"
            if winner.kind == BehaviorKind.START_ACTIVITY:
                reason = "resume_pending" if winner.activity_id is not None else "selected"
            scores = {} if active else {
                a.candidate.definition_id: a.candidate.utility for a in decision.assessments
                if a.candidate.kind == BehaviorKind.START_ACTIVITY
                and a.candidate.activity_id is None and a.candidate.unavailable_reason is None
            }
            await core.record_event(AgentEvent(
                event_id=identifier, event_type=SELECTION_EVENT_TYPE, source=SELECTION_SOURCE,
                timestamp=now, provenance=EventProvenance.INFERRED,
                activity_id=(selected.instance.activity_id
                             if selected is not None and winner.kind == BehaviorKind.START_ACTIVITY else None),
                payload={
                    "agent_id": agent_id, "decision_key": decision_key,
                    "state_version": state.version, "reason": reason, "scores": scores,
                    "policy": selector.policy.model_dump(mode="json"),
                    "arbitration_policy": arbiter.policy.model_dump(mode="json"),
                    "arbitration": decision.model_dump(mode="json"),
                },
            ))
            return BehaviorReservation(decision=decision, activity=selected)

    async def get_activity(self, agent_id: str, activity_id: str) -> ActivityRecord | None:
        async with self._repositories() as (_, activities):
            return await activities.get_activity(agent_id, activity_id)

    async def commit_activity_result(
        self, operation: str, context: ActivityContext, result: ActivityExecutionResult, *,
        settled_at: datetime | None = None, next_schedule: ScheduledEvent | None = None,
        trigger_event: AgentEvent | None = None,
    ) -> ActivityRecord:
        async with self._repositories() as (_, activities):
            return await activities.commit_activity_result(
                operation, context, result, settled_at=settled_at,
                next_schedule=next_schedule, trigger_event=trigger_event,
            )

    async def settle_activity_completion(self, agent_id: str, activity_id: str, *, settled_at: datetime) -> AgentState:
        async with self._repositories() as (_, activities):
            return await activities.settle_activity_completion(agent_id, activity_id, settled_at=settled_at)
