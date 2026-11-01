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
from .activity_memory import record_activity_memory
from .relationship import relationship_signal
from ..social.persistence import RelationshipPersistence
from ..social.clock import relationship_day_plans, parse_relationship_day, build_relationship_day
from ..social.conversation_persistence import ConversationPersistence
from ..social.conversation import conversation_occupancy
from ..social.conversation_clock import deadline_for, parse_conversation_deadline, build_conversation_deadline
from ..memory.persistence import MemoryPersistence
from ..memory.models import MemoryIndexRepairReport
from .appraisal_persistence import AppraisalPersistence, DUE_EVENT_TYPE
from .expression import ExpressionPersistence
from ..social.expression import intent_busy, gate
from ..social.expression_clock import expression_deadline_for, parse_expression_deadline, build_expression_deadline
from ..social.conversation import ConversationState
from datetime import timedelta
from ..cognition.evaluation import RuleEvaluation
from ..activity.feedback import ActivityFeedback, STAGE_EVENT_TYPE, FEEDBACK_SOURCE, build_feedback_event, parse_activity_feedback
from ..persistence.errors import ActivityNotFoundError, EventConflictError


class PersonaStore:
    """类型化业务提交入口；不是数据库适配器，也不自行执行 SQL。

    每次调用创建一个会话，在同一事务内组合所需业务规则。组合根显式
    注入 UnitOfWork 实现；业务消费者只依赖各自的最小存储协议。
    """

    def __init__(self, backend: UnitOfWork, *, evaluation: EvaluationSettings | None = None, memory=None, relationships=None,
                 conversation=None, conversation_scope_allowed=None, expression=None, expression_target_allowed=None):
        self._backend = backend
        self._evaluation = evaluation
        self._memory = memory
        self._relationships = relationships
        self._expression=expression
        self._expression_target_allowed=expression_target_allowed
        self._conversation = conversation
        self._conversation_scope_allowed = conversation_scope_allowed
        self.memory_index_report = MemoryIndexRepairReport()

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
        self.memory_index_report = MemoryIndexRepairReport()
        await self._backend.open()
        try:
            # 业务修复属于应用启动，不属于数据库连接或 schema 迁移。
            async with self._repositories() as (core, activities):
                await activities.reconcile_status_index()
                report=await MemoryPersistence(core).reconcile_indexes()
            self.memory_index_report=report
            await self.reconcile_exhausted_ticks()
        except BaseException:
            await self._backend.close()
            raise

    async def close(self) -> None:
        await self._backend.close()

    async def reconcile_exhausted_ticks(self) -> None:
        async with self._repositories() as (_, activities):
            await activities.reconcile_exhausted_ticks()

    async def get_conversation(self, agent_id, scope_id):
        async with self._repositories() as (core, _):
            return await ConversationPersistence(core.session).get(agent_id,scope_id)

    async def conversation_receive(self, agent_id, event, **kwargs):
        async with self._repositories() as (core, _):
            if await core.get_event(event.event_id) is not None:
                return False
            repository=ConversationPersistence(core.session)
            follow=await repository.receive(agent_id,event,**kwargs)
            await self._schedule_conversation(core,await repository.get(agent_id,event.scope_id),kwargs["now"])
            return follow

    async def conversation_sent(self, agent_id, event, **kwargs):
        async with self._repositories() as (core, _):
            repository=ConversationPersistence(core.session)
            await repository.sent(agent_id,event,**kwargs)
            await self._schedule_conversation(core,await repository.get(agent_id,event.scope_id),kwargs["now"])

    def _conversation_allowed(self, item):
        if self._conversation is None or not self._conversation.enabled:
            return False
        if self._conversation.private_only and ":GroupMessage:" in item.scope_id:
            return False
        return self._conversation_scope_allowed is None or self._conversation_scope_allowed(item)

    async def _schedule_conversation(self, core, item, now):
        if item is not None and self._conversation_allowed(item) and conversation_occupancy(item,now,self._conversation):
            await core.create_schedule(deadline_for(item))

    async def _conversation_reason(self, core, agent_id, now):
        if self._conversation is None or not self._conversation.enabled or not self._conversation.defer_new_activities:
            return None
        waiting=None
        async for item in ConversationPersistence(core.session).iter_states(agent_id):
            if not self._conversation_allowed(item):
                continue
            reason=conversation_occupancy(item,now,self._conversation)
            if reason=="reply_pending":
                return reason
            if reason is not None:
                waiting=reason
        return waiting

    async def conversation_arbitration_reason(self, agent_id, *, now):
        async with self._repositories() as (core, _):
            return await self._conversation_reason(core,agent_id,normalize_schedule_time(now))

    async def start_conversation_clock(self, agent_id, *, now):
        if self._conversation is None or not self._conversation.enabled or not self._conversation.defer_new_activities:
            return
        async with self._repositories() as (core, _):
            async for item in ConversationPersistence(core.session).iter_states(agent_id):
                await self._schedule_conversation(core,item,normalize_schedule_time(now))

    async def handle_conversation_deadline(self, agent_id, event, *, now):
        now=normalize_schedule_time(now)
        spec=parse_conversation_deadline(event)
        if spec.agent_id!=agent_id or spec.expires_at>now:
            raise ValueError("会话到期事件角色错误或尚未到期")
        async with self._repositories() as (core, _):
            plan=await core.get_schedule(agent_id,event.event_id)
            if plan is None or plan.schedule!=build_conversation_deadline(spec):
                raise ValueError("会话到期事件缺少匹配的持久化计划")
            item=await ConversationPersistence(core.session).get(agent_id,spec.scope_id)
            if (item is None or not self._conversation_allowed(item) or not self._conversation.defer_new_activities
                    or item.revision!=spec.revision or item.expires_at!=spec.expires_at
                    or item.phase=="closed"):
                return False
            await core.record_event(event)
            return True

    async def _expression_busy(self, core, agent_id, now):
        if self._expression is None or not self._expression.enabled or not self._expression.send_enabled:return False
        head=await ExpressionPersistence(core).get(agent_id)
        return head is not None and intent_busy(head.intent,now)

    async def expression_head(self, agent_id):
        async with self._repositories() as (core, _):
            return await ExpressionPersistence(core).get(agent_id)

    async def cancel_expression(self, agent_id, *, now, reason="manual_cancel", include_preview=False):
        now=normalize_schedule_time(now)
        async with self._repositories() as (core, _):
            repo=ExpressionPersistence(core);head=await repo.get(agent_id)
            if head is None:return False,None
            item=head.intent
            cancellable=("pending","generating","preview") if include_preview else ("pending","generating")
            if item.status not in cancellable:return False,item
            return True,await repo.transition(head,"cancelled",now,reason=reason)

    async def recover_expression(self, agent_id, *, now):
        now=normalize_schedule_time(now)
        async with self._repositories() as (core, _):
            repo=ExpressionPersistence(core);head=await repo.get(agent_id)
            if head is None:return None
            item=head.intent
            if item.status in ("generating","sending"):
                await repo.transition(head,"unknown" if item.status=="sending" else "failed",now,reason="restart_interrupted")
                return None
            if item.status!="pending":return None
            if self._expression is None or not self._expression.enabled or not self._expression.send_enabled:
                await repo.transition(head,"cancelled",now,reason="expression_disabled")
                return None
            # 兼容 v0.9.0 缺少到期计划的旧 pending；过期计划也需交调度器触发恢复仲裁。
            await core.create_schedule(expression_deadline_for(item))
            return item if now<item.expires_at else None

    async def handle_expression_deadline(self, agent_id, event, *, now):
        now=normalize_schedule_time(now)
        spec=parse_expression_deadline(event)
        if spec.agent_id!=agent_id or now<spec.expires_at:
            raise ValueError("主动表达到期事件角色错误或尚未到期")
        async with self._repositories() as (core, _):
            plan=await core.get_schedule(agent_id,event.event_id)
            if plan is None or plan.schedule!=build_expression_deadline(spec):
                raise ValueError("主动表达到期事件缺少匹配的持久化计划")
            previous=await core.get_event(event.event_id)
            if previous is not None:
                if previous!=event:raise ValueError("主动表达到期回执冲突")
                # 到期事务已提交但后续仲裁可能失败：原键继续，不重复改变意图。
                return True,None
            repo=ExpressionPersistence(core);head=await repo.get(agent_id)
            if head is None:return False,None
            item=head.intent
            if (item.intent_id!=spec.intent_id or item.expires_at!=spec.expires_at
                    or item.target.scope_id!=spec.scope_id or item.status not in ("pending","generating","sending")):
                return False,None
            changed=await repo.transition(head,"unknown" if item.status=="sending" else "cancelled",now,reason="intent_expired")
            await core.record_event(event)
            return True,changed

    async def _expression_valid(self, core, activities, item, now):
        settings=self._expression
        if (settings is None or not settings.enabled or not settings.send_enabled or now>=item.expires_at
                or gate(settings,now) is not None or item.target not in settings.targets
                or (self._expression_target_allowed is not None and not self._expression_target_allowed(item.target))):
            return False
        state=await core.get_state(item.agent_id)
        if state is None or state.needs.social_need<settings.social_threshold:return False
        if await activities.list_active_activities(item.agent_id):return False
        if await self._conversation_reason(core,item.agent_id,now):return False
        current=await ConversationPersistence(core.session).get(item.agent_id,item.target.scope_id)
        return current is not None and current.source_event_id==item.source_event_id

    async def claim_expression(self, agent_id, intent_id, *, now, autonomous_enabled, phase="generate", body=""):
        async with self._repositories() as (core, activities):
            repo=ExpressionPersistence(core);head=await repo.get(agent_id)
            if phase not in ("generate","send"):raise ValueError("非法主动表达执行阶段")
            expected="pending" if phase=="generate" else "generating"
            if head is None or head.intent.intent_id!=intent_id or head.intent.status!=expected:return None
            if not autonomous_enabled or not await self._expression_valid(core,activities,head.intent,now):
                await repo.transition(head,"cancelled",now,reason="revalidation_failed");return None
            if phase!="generate" and (not body.strip() or len(body)>self._expression.max_body_chars):
                await repo.transition(head,"failed",now,reason="invalid_body");return None
            return await repo.transition(head,"generating" if phase=="generate" else "sending",now,
                body=body if phase!="generate" else None)

    async def finish_expression(self, agent_id, intent_id, *, now, status, reason=""):
        async with self._repositories() as (core, _):
            repo=ExpressionPersistence(core);head=await repo.get(agent_id)
            if head is None or head.intent.intent_id!=intent_id or head.intent.status not in ("generating","sending"):
                return None
            if status not in ("sent","failed","unknown","cancelled"):raise ValueError("非法主动表达结束状态")
            if status=="sent" and head.intent.status!="sending":raise ValueError("发送前不能记录发送成功")
            item=await repo.transition(head,status,now,reason=reason or status)
            if status=="sent" and self._conversation is not None and self._conversation.enabled:
                previous=await ConversationPersistence(core.session).get(agent_id,item.target.scope_id)
                # 有新入站时不覆盖其对话状态，已在途的发送也不伪装成新用户消息。
                if previous is not None and previous.source_event_id==item.source_event_id:
                    state=ConversationState(agent_id=agent_id,scope_id=item.target.scope_id,
                        platform_id=item.target.platform_id,user_id=item.target.user_id,source_event_id=item.intent_id,
                        own_outbound_event_id=item.intent_id+":sent",phase="send_hook_observed",updated_at=now,expires_at=now+timedelta(seconds=self._conversation.window_seconds),
                        followups=0,revision=previous.revision+1)
                    await ConversationPersistence(core.session).save(state,previous)
                    await self._schedule_conversation(core,state,now)
                elif previous is not None:
                    # 在途发送后有新入站：只补自己的出站引用，不重开或延长用户会话。
                    state=previous.model_copy(update={"own_outbound_event_id":item.intent_id+":sent","revision":previous.revision+1})
                    await ConversationPersistence(core.session).save(state,previous)
                    await self._schedule_conversation(core,state,now)
            return item

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
            was_recorded=await core.get_event(event.event_id) is not None
            if not was_recorded and self._expression is not None:
                await ExpressionPersistence(core).cancel_inbound(agent_id,event,now)
            await core.record_event(event)
            if platform_timestamp is not None:
                await core.record_event(build_message_time_adjustment(agent_id, event,
                    platform_timestamp=platform_timestamp, received_at=now))
            if self._memory is not None:
                await MemoryPersistence(core).observe(agent_id,event,self._memory,allow_capture=not was_recorded)
            settings = self._evaluation or EvaluationSettings(enabled=False)
            result = evaluate_chat_event(event, settings, agent_id=agent_id, directed=directed)
            receipt=await EvaluationPersistence(core, self._commit_emotion).evaluate_once(
                agent_id, event, settings, result, now=now, cooldown_key=chat_cooldown_key(event, result),
            )
            if self._relationships is not None:
                await RelationshipPersistence(core).observe(agent_id,event,relationship_signal(result,self._relationships),
                    self._relationships,now=now,directed=directed,allow_capture=not was_recorded)
            return receipt

    async def chat_snapshot(self, agent_id: str, scope_id: str, *, now, rates, labels, include_feedback=True,
                            query="", speaker_id=None, include_memories=True, platform_id=None, user_id=None, include_relationship=True):
        async with self._repositories() as (core, activities):
            return await read_chat_snapshot(core, activities, agent_id, scope_id, now=now,
                rates=rates, labels=labels, include_feedback=include_feedback,
                memory_settings=self._memory, query=query, speaker_id=speaker_id, include_memories=include_memories,
                relationship_settings=self._relationships,platform_id=platform_id,user_id=user_id,include_relationship=include_relationship)

    async def start_relationship_clock(self,agent_id,*,now):
        now=normalize_schedule_time(now)
        settings=self._relationships
        if settings is None or not settings.enabled or not settings.affinity_regression_enabled:
            return None
        today,tomorrow=relationship_day_plans(agent_id,now,settings)
        async with self._repositories() as (core,_):
            if await core.get_state(agent_id) is None:
                raise StateNotFoundError("角色不存在")
            await core.record_event(today.event)
            report=await RelationshipPersistence(core).regress_all(agent_id,settings,now=now,cause=today.event)
            await core.create_schedule(tomorrow)
            return report

    async def handle_relationship_day(self,agent_id,event,*,now):
        now=normalize_schedule_time(now)
        spec=parse_relationship_day(event)
        if spec.agent_id!=agent_id or event.timestamp>now:
            raise ValueError("关系日时钟不属于当前角色或尚未到期")
        settings=self._relationships
        if settings is None:
            raise ValueError("尚未配置关系规则")
        async with self._repositories() as (core,_):
            plan=await core.get_schedule(agent_id,event.event_id)
            if plan is None or plan.schedule!=build_relationship_day(agent_id,spec.day,spec.utc_offset_minutes,spec.settlement_hour):
                raise ValueError("关系日时钟缺少匹配的持久化计划")
            await core.record_event(event)
            report=await RelationshipPersistence(core).regress_all(agent_id,settings,now=now,cause=event)
            if settings.enabled and settings.affinity_regression_enabled:
                _,tomorrow=relationship_day_plans(agent_id,now,settings)
                await core.create_schedule(tomorrow)
            return report

    async def get_relationship(self,agent_id,platform_id,user_id):
        async with self._repositories() as (core,_):
            return await RelationshipPersistence(core).get(agent_id,platform_id,user_id)

    async def relationship_view(self,agent_id,platform_id,user_id,scope_id,*,history=False):
        async with self._repositories() as (core,_):
            repository=RelationshipPersistence(core)
            return (await repository.get(agent_id,platform_id,user_id),
                    await repository.history(agent_id,platform_id,user_id,scope_id,limit=10 if history else 1))

    async def memory_operation(self, agent_id, event, action, value, settings, *, platform_timestamp=None, now):
        now=normalize_schedule_time(now)
        if event.event_type != "SOCIAL.message_received" or event.scope_id is None or event.timestamp>now:
            raise ValueError("记忆操作需要有会话归属且不来自未来的 QQ 来源消息")
        async with self._repositories() as (core, _):
            repository=MemoryPersistence(core)
            if action in ("add","delete"):
                await core.record_event(event)
                if platform_timestamp is not None:
                    await core.record_event(build_message_time_adjustment(agent_id,event,
                        platform_timestamp=platform_timestamp,received_at=now))
                if action=="add":
                    return await repository.observe(agent_id,event,settings,text=value,explicit=True)
                return await repository.delete(agent_id,event,value)
            if action=="list":
                return await repository.list(agent_id,event.scope_id,before_id=value or None)
            if action=="pending":
                return await repository.pending(agent_id,event.scope_id,after_id=value or None)
            if action=="show":
                return await repository.get(agent_id,event.scope_id,value)
            if action=="history":
                return await repository.history(agent_id,event.scope_id,value)
            if action=="search":
                from ..memory.extraction import actor_id
                return await repository.recall(agent_id,event.scope_id,settings,query=value,speaker_id=actor_id(event),now=now)
            raise ValueError("未知记忆操作")

    async def record_memory_draft(self, agent_id, event, draft, settings):
        async with self._repositories() as (core, _):
            await core.record_event(event)
            return await MemoryPersistence(core).write_draft(agent_id,event,draft,settings)

    async def list_internal_memories(self, agent_id, *, limit=10, before_id=None):
        """仅供本地维护；QQ 命令和聊天快照不调用此入口。"""
        async with self._repositories() as (core, _):
            return await MemoryPersistence(core).list_internal(agent_id,limit=limit,before_id=before_id)

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

    async def _evaluate_activity_events(self, core, record, events, *, now, new_event_ids=()):
        settings = self._evaluation
        evaluator = EvaluationPersistence(core, self._commit_emotion)
        reservations = AppraisalPersistence(core, self._commit_emotion, settings)
        for event in events:
            if event.event_type not in (STAGE_EVENT_TYPE, "ACTIVITY.completed", "ACTIVITY.failed"):
                continue
            feedback = parse_activity_feedback(record, event)
            if feedback is not None:
                await record_feedback_head(core, event)
            if self._memory is not None:
                await record_activity_memory(core,record,event,self._memory,is_new=event.event_id in new_event_ids)
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
            await self._evaluate_activity_events(core, record, (event,), now=now,new_event_ids={event.event_id})
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
                        await self._evaluate_activity_events(core, current, events, now=attempted_at,
                            new_event_ids={event.event_id for event in events})
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
                        to_at=now, conversation_reason=await self._conversation_reason(core,agent_id,now),
                        expression_pending=await self._expression_busy(core,agent_id,now),
                    )
                    if (decision.winner.kind != BehaviorKind.START_ACTIVITY
                            or decision.winner.activity_id != saved.instance.activity_id):
                        saved = None
                expression=None
                if decision.winner.kind==BehaviorKind.EXPRESS:
                    head=await ExpressionPersistence(core).get(agent_id)
                    if head is None or head.intent.intent_id!=receipt.payload.get("expression_id"):
                        return BehaviorReservation(decision=BehaviorDecision(
                            winner=BehaviorCandidate(candidate_id="idle",kind=BehaviorKind.IDLE),reason="expression_obsolete"))
                    expression=head.intent
                return BehaviorReservation(decision=decision, activity=saved,expression=expression)
            state = await core.get_state(agent_id)
            if state is None:
                raise StateNotFoundError("请先创建角色")
            if now < state.updated_at:
                raise ValueError("选择时间不能早于角色更新时间")
            active = await activities.list_active_activities(agent_id)
            social_reason=await self._conversation_reason(core,agent_id,now)
            expression_repo=ExpressionPersistence(core)
            proposal=await expression_repo.candidate(agent_id,state,self._expression,now,self._expression_target_allowed,
                occupied=social_reason is not None,active=bool(active)) if arbiter.policy.autonomous_enabled else None
            arguments=dict(to_at=now,conversation_reason=social_reason,
                expression_pending=await self._expression_busy(core,agent_id,now))
            decision = arbitrate_activity(selector,arbiter,state,active,await self._recoverable_ids(core,active),
                expression_candidate=None if proposal is None else proposal[0],**arguments)
            expression=None
            preview_id=None
            if decision.winner.kind==BehaviorKind.EXPRESS:
                item=await expression_repo.reserve(agent_id,decision_key,proposal[1],self._expression,now)
                if item.mode=="preview":
                    preview_id=item.intent_id
                    decision=arbitrate_activity(selector,arbiter,state,active,await self._recoverable_ids(core,active),**arguments)
                else:expression=item
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
            if winner.kind==BehaviorKind.EXPRESS:
                reason="expression_selected"
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
                    "expression_id":None if expression is None else expression.intent_id,
                    "expression_preview_id":preview_id,
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
            return BehaviorReservation(decision=decision, activity=selected,expression=expression)

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
            # 必须在提交前判断；升级/重载后重投旧终态事件不回填新记忆。
            new_event_ids=set()
            if self._memory is not None:
                for event in result.events:
                    if event.event_type in (STAGE_EVENT_TYPE,"ACTIVITY.completed","ACTIVITY.failed") and await core.get_event(event.event_id) is None:
                        new_event_ids.add(event.event_id)
            saved = await activities.commit_activity_result(
                operation, context, result, settled_at=settled_at,
                next_schedule=next_schedule, trigger_event=trigger_event,
            )
            candidate = ActivityRecord(definition=context.definition, instance=result.instance)
            await self._evaluate_activity_events(core, candidate, result.events,
                now=context.now if settled_at is None else settled_at,new_event_ids=new_event_ids)
            return saved

    async def settle_activity_completion(self, agent_id: str, activity_id: str, *, settled_at: datetime) -> AgentState:
        async with self._repositories() as (_, activities):
            return await activities.settle_activity_completion(agent_id, activity_id, settled_at=settled_at)
