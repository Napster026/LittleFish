"""应用层事务协调：组合核心与活动业务规则，依赖通用 UnitOfWork 端口。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator

from ..activity.executor import ActivityContext, ActivityExecutionResult
from ..activity.models import (
    ActivityDefinition, ActivityInstance, ActivityRecord, ActivityStatus,
    ExecutionMode, create_activity_instance,
)
from ..activity.persistence import ActivityPersistence
from ..activity.selection import ActivitySelector
from .autonomy import (
    SELECTION_EVENT_TYPE, SELECTION_SOURCE, BehaviorReservation, arbitrate_activity, selection_id,
)
from .needs import build_need_proposal, build_need_tick, parse_need_tick
from .emotion import (
    EmotionImpact, EmotionTick, build_emotion_initialization,
    build_emotion_impulse, build_emotion_interval_update, build_emotion_proposal, build_emotion_tick, parse_emotion_event,
)
from ..core.events import AgentEvent, EventProvenance
from ..core.arbiter import BehaviorArbiter, BehaviorCandidate, BehaviorDecision, BehaviorKind
from ..core.persistence import CorePersistence
from ..core.scheduler import ScheduledEvent, ScheduleRecord, ScheduleStatus, normalize_schedule_time
from ..core.settlement import StateChangeProposal
from ..core.state import AgentState
from ..persistence.ports import UnitOfWork
from ..persistence.errors import StateNotFoundError
from ..persona.needs import NeedRates, NeedDelta
from ..persona.emotion import EmotionState
from ..cognition.evaluation import EvaluationSettings
from .evaluation import evaluate_chat_event, chat_cooldown_key, evaluate_activity_event, activity_appraisal_input
from .evaluation_persistence import EvaluationPersistence
from .qq import build_message_time_adjustment
from .chat_context import read_chat_snapshot, record_feedback_head
from .appraisal_persistence import AppraisalPersistence, DUE_EVENT_TYPE
from ..cognition.evaluation import RuleEvaluation
from ..activity.feedback import ActivityFeedback, STAGE_EVENT_TYPE, FEEDBACK_SOURCE, build_feedback_event, parse_activity_feedback
from ..persistence.errors import ActivityNotFoundError, EventConflictError


class PersonaStore:
    """类型化业务提交入口；不是数据库适配器，也不自行执行 SQL。

    每次调用创建一个会话，在同一事务内组合所需业务规则。组合根显式
    注入 UnitOfWork 实现；业务消费者只依赖各自的最小存储协议。
    """

    def __init__(self, backend: UnitOfWork, *, evaluation: EvaluationSettings | None = None):
        self._backend = backend
        self._evaluation = evaluation

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
            async with self._repositories() as (_, activities):
                await activities.reconcile_status_index()
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

    async def record_rule_message(self, agent_id: str, event: AgentEvent, *, now: datetime, directed=False,
                                  platform_timestamp: datetime | None = None):
        if event.event_type != "SOCIAL.message_received" or event.provenance != EventProvenance.OBSERVED or event.scope_id is None:
            raise ValueError("聊天评价只接受有作用域的原始入站事件")
        async with self._repositories() as (core, _):
            await core.record_event(event)
            if platform_timestamp is not None:
                await core.record_event(build_message_time_adjustment(agent_id, event,
                    platform_timestamp=platform_timestamp, received_at=now))
            settings = self._evaluation or EvaluationSettings(enabled=False)
            result = evaluate_chat_event(event, settings, agent_id=agent_id, directed=directed)
            return await EvaluationPersistence(core, self._commit_emotion).evaluate_once(
                agent_id, event, settings, result, now=now, cooldown_key=chat_cooldown_key(event, result),
            )

    async def chat_snapshot(self, agent_id: str, scope_id: str, *, now, rates, labels, include_feedback=True):
        async with self._repositories() as (core, activities):
            return await read_chat_snapshot(core, activities, agent_id, scope_id, now=now,
                rates=rates, labels=labels, include_feedback=include_feedback)

    async def latest_evaluation(self, agent_id: str):
        async with self._repositories() as (core, _):
            return await EvaluationPersistence(core, self._commit_emotion).latest(agent_id)

    async def pending_appraisals(self, agent_id: str, *, limit=20):
        async with self._repositories() as (core, _):
            return await AppraisalPersistence(core, self._commit_emotion, self._evaluation).pending(agent_id, limit=limit)

    async def get_appraisal(self, agent_id: str, source_event_id: str):
        async with self._repositories() as (core, _):
            return await AppraisalPersistence(core, self._commit_emotion, self._evaluation).get(agent_id, source_event_id)

    async def complete_appraisal(self, agent_id: str, source_event_id: str, raw, *, now: datetime):
        async with self._repositories() as (core, _):
            return await AppraisalPersistence(core, self._commit_emotion, self._evaluation).complete(
                agent_id, source_event_id, raw, now=now)

    async def handle_appraisal_deadline(self, agent_id: str, event: AgentEvent, *, now: datetime):
        if event.payload.get("agent_id") != agent_id:
            raise ValueError("评价超时事件不属于当前角色")
        async with self._repositories() as (core, _):
            return await AppraisalPersistence(core, self._commit_emotion, self._evaluation).due(event, now=now)

    async def recover_appraisals(self, agent_id: str, *, now: datetime):
        """启动时重试已耗尽的超时提交；预约与最终回执仍决定一次性结算。"""
        async with self._repositories() as (core, _):
            evaluator = AppraisalPersistence(core, self._commit_emotion, self._evaluation)
            after = None
            while True:
                rows = await core.session.fetch_all("scheduled_events", where={"agent_id": agent_id, "status": "failed"},
                    order_by=("schedule_id",), after=None if after is None else {"schedule_id": after})
                if not rows: break
                for row in rows:
                    plan = core.schedule_record(row)
                    if plan.schedule.event.event_type == DUE_EVENT_TYPE:
                        await evaluator.due(plan.schedule.event, now=now)
                after = rows[-1]["schedule_id"]

    async def _evaluate_activity_events(self, core, record, events, *, now):
        settings = self._evaluation
        evaluator = EvaluationPersistence(core, self._commit_emotion)
        reservations = AppraisalPersistence(core, self._commit_emotion, settings)
        for event in events:
            if event.event_type not in (STAGE_EVENT_TYPE, "ACTIVITY.completed", "ACTIVITY.failed"):
                continue
            feedback = parse_activity_feedback(record, event)
            if feedback is not None:
                await record_feedback_head(core, event)
            if settings is None:
                continue
            fallback = evaluate_activity_event(record, event, settings, use_experience=False)
            if feedback is not None and feedback.evaluation_mode == "deferred":
                if settings.enabled and settings.activity_feedback_enabled and settings.semantic.enabled:
                    state = await core.get_state(record.instance.agent_id)
                    request = activity_appraisal_input(record, event,
                        current_emotion=None if state.emotion is None else state.emotion.current)
                    await reservations.begin(event, request, settings, fallback, now=now)
                    continue
                job = await reservations.get(record.instance.agent_id, event.event_id)
                if job is not None:
                    await reservations.fallback(job, now=now, reason="semantic_disabled")
                    continue
                fallback = RuleEvaluation(**{**fallback.model_dump(), "reason": "semantic_disabled"})
            result = evaluate_activity_event(record, event, settings)
            experience = None if feedback is None else feedback.experience
            semantic = (experience is not None and settings.enabled and settings.activity_feedback_enabled
                        and settings.semantic.enabled and not experience.uncertain)
            layer = "semantic" if semantic else "rule"
            if feedback is not None and (experience is not None or feedback.evaluation_mode == "deferred") and not semantic:
                result = fallback
                layer = "fallback"
                if experience is not None and experience.uncertain:
                    result = RuleEvaluation(**{**result.model_dump(), "reason": "semantic_uncertain"})
            await evaluator.evaluate_once(record.instance.agent_id, event, settings, result, now=now,
                layer=layer, experience=experience if semantic else None)

    async def report_activity_feedback(self, agent_id: str, activity_id: str, feedback: ActivityFeedback, *,
                                       now: datetime, observed_at: datetime | None = None,
                                       provenance: EventProvenance | None = None, scope_id: str | None = None) -> AgentEvent:
        """保存阶段内容与情绪，不更改活动状态、进度或推进基线。"""
        now = normalize_schedule_time(now)
        async with self._repositories() as (core, activities):
            record = await activities.get_activity(agent_id, activity_id)
            if record is None:
                raise ActivityNotFoundError("活动不存在")
            event = build_feedback_event(record, feedback, at=now if observed_at is None else observed_at,
                                         provenance=provenance, scope_id=scope_id)
            previous = await core.get_event(event.event_id)
            if previous is not None:
                if previous.model_dump(exclude={"timestamp"}) != event.model_dump(exclude={"timestamp"}):
                    raise EventConflictError("同一阶段标识的反馈内容不能改写")
                return previous
            if record.instance.status not in (ActivityStatus.RUNNING, ActivityStatus.PAUSED) or event.timestamp > now:
                raise ValueError("新阶段反馈要求活动仍在运行或暂停，且反馈不来自未来")
            await core.record_event(event)
            await self._evaluate_activity_events(core, record, (event,), now=now)
            return event

    async def latest_activity_feedback(self, agent_id: str, activity_id: str | None = None) -> AgentEvent | None:
        async with self._repositories() as (core, _):
            latest, after = None, None
            while True:
                rows = await core.session.fetch_all("agent_events", after=None if after is None else {"event_id": after}, order_by=("event_id",))
                if not rows:
                    return latest
                for row in rows:
                    event = AgentEvent.model_validate_json(row["event_json"])
                    if (((event.event_type == STAGE_EVENT_TYPE and event.source == FEEDBACK_SOURCE)
                             or (event.event_type in {"ACTIVITY.completed", "ACTIVITY.failed"} and "feedback" in event.payload))
                            and (activity_id is None or event.activity_id == activity_id)
                            and event.payload.get("agent_id") == agent_id
                            and (latest is None or (event.timestamp,event.event_id) > (latest.timestamp,latest.event_id))):
                        latest = event
                after = rows[-1]["event_id"]

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
        evaluation_needs: NeedDelta | None = None,
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
            if expected_plan.event != event:
                expected_plan = build_emotion_tick(spec.agent_id, from_at=spec.from_at,
                    dynamics=spec.dynamics, legacy_identity=True)
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
            if evaluation_needs is not None:
                evaluation_needs = NeedDelta.model_validate(evaluation_needs.model_dump())
                if evaluation_needs.energy != 0 or evaluation_needs.satiety != 0:
                    raise ValueError("评价不能发放精力或饱食")
                expected = StateChangeProposal(**{**expected.model_dump(), "needs_delta": evaluation_needs})
            if proposal != expected:
                raise ValueError("情绪提案与当前状态或事件规则不一致")
        result = await core.commit_proposal(proposal, settled_at=settled_at)
        candidate = proposal.emotion_change.state
        # 情绪、游标、结算回执、下一次计划共同提交；重复提交不重置计划。
        next_plan = build_emotion_tick(proposal.agent_id, from_at=candidate.updated_at, dynamics=candidate.dynamics)
        legacy = build_emotion_tick(proposal.agent_id, from_at=candidate.updated_at,
                                   dynamics=candidate.dynamics, legacy_identity=True)
        previous_plan = await core.get_schedule(proposal.agent_id, legacy.schedule_id)
        if previous_plan is not None and previous_plan.schedule == legacy:
            next_plan = legacy
        await core.create_schedule(next_plan)
        return result

    async def start_emotion_clock(
        self, agent_id: str, *, now: datetime, initial: EmotionState | None = None,
        tick_interval_seconds: float | None = None,
    ) -> ScheduleRecord:
        """初始化/恢复时钟；显式提供间隔时，仅调整采样频率并保留动力参数。"""
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
            if tick_interval_seconds is not None:
                update = build_emotion_interval_update(state, tick_interval_seconds, at=now)
                if update.payload["dynamics"]["tick_interval_seconds"] != state.emotion.dynamics.tick_interval_seconds:
                    await core.record_event(update)
                    proposal = build_emotion_proposal(update, state, to_at=now)
                    state = await self._commit_emotion(core, proposal, settled_at=now)
            plan = build_emotion_tick(
                agent_id, from_at=state.emotion.updated_at, dynamics=state.emotion.dynamics,
            )
            existing = await core.get_schedule(agent_id, plan.schedule_id)
            if existing is None:
                legacy = build_emotion_tick(agent_id, from_at=state.emotion.updated_at,
                                           dynamics=state.emotion.dynamics, legacy_identity=True)
                existing = await core.get_schedule(agent_id, legacy.schedule_id)
                if existing is not None:
                    plan = legacy
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
                activity_id = saved.schedule.event.activity_id
                previous = None if activity_id is None else await activities.get_activity(agent_id, activity_id)
                await activities.fail_exhausted_tick(saved)
                if previous is not None:
                    current = await activities.get_activity(agent_id, activity_id)
                    if current.instance.revision != previous.instance.revision:
                        events = await activities.get_committed_events(current)
                        await self._evaluate_activity_events(core, current, events, now=attempted_at)
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
                        to_at=now,
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
                to_at=now,
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
                a.candidate.definition_id: (
                    a.candidate.utility if a.candidate.score_breakdown is None
                    else a.candidate.score_breakdown.need_score
                ) for a in decision.assessments
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
                    "emotion_preference_policy": selector.emotion_policy.model_dump(mode="json"),
                    "emotion_preferences": [p.model_dump(mode="json") for p in selector.emotion_preferences],
                    "emotion_snapshot": None if state.emotion is None else state.emotion.model_dump(mode="json"),
                    "emotion_scored_at": now.isoformat(timespec="microseconds"),
                },
            ))
            return BehaviorReservation(decision=decision, activity=selected)

    async def get_activity(self, agent_id: str, activity_id: str) -> ActivityRecord | None:
        async with self._repositories() as (_, activities):
            return await activities.get_activity(agent_id, activity_id)

    async def list_active_activities(self, agent_id: str) -> tuple[ActivityRecord, ...]:
        async with self._repositories() as (_, activities):
            return await activities.list_active_activities(agent_id)

    async def get_latest_behavior_event(self, agent_id: str) -> AgentEvent | None:
        """管理查询：分页扫描日志，只返回本角色的最近一次选择回执。"""
        async with self._repositories() as (core, _):
            latest, after_id = None, None
            while True:
                rows = await core.session.fetch_all(
                    "agent_events", after=None if after_id is None else {"event_id": after_id},
                    order_by=("event_id",),
                )
                if not rows:
                    return latest
                for row in rows:
                    event = AgentEvent.model_validate_json(row["event_json"])
                    if (event.event_type == SELECTION_EVENT_TYPE and event.source == SELECTION_SOURCE
                            and event.payload.get("agent_id") == agent_id
                            and (latest is None or (event.timestamp, event.event_id) > (latest.timestamp, latest.event_id))):
                        latest = event
                after_id = rows[-1]["event_id"]

    async def commit_activity_result(
        self, operation: str, context: ActivityContext, result: ActivityExecutionResult, *,
        settled_at: datetime | None = None, next_schedule: ScheduledEvent | None = None,
        trigger_event: AgentEvent | None = None,
    ) -> ActivityRecord:
        async with self._repositories() as (core, activities):
            saved = await activities.commit_activity_result(
                operation, context, result, settled_at=settled_at,
                next_schedule=next_schedule, trigger_event=trigger_event,
            )
            candidate = ActivityRecord(definition=context.definition, instance=result.instance)
            await self._evaluate_activity_events(core, candidate, result.events,
                now=context.now if settled_at is None else settled_at)
            return saved

    async def settle_activity_completion(self, agent_id: str, activity_id: str, *, settled_at: datetime) -> AgentState:
        async with self._repositories() as (_, activities):
            return await activities.settle_activity_completion(agent_id, activity_id, settled_at=settled_at)
