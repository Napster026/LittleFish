"""单角色插件组装与生命周期；AstrBot API 保留在 main.py。"""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from ..activity.executors.simulated import SimulatedActivityExecutor
from ..activity.manager import ActivityManager
from ..activity.models import ExecutionMode
from ..activity.registry import ExecutorRegistry
from ..activity.service import ActivityService
from ..core.arbiter import ArbitrationPolicy, BehaviorArbiter, BehaviorDecision
from ..core.events import AgentEvent
from ..core.router import EventRouter
from ..core.runtime import AgentRuntime
from ..core.scheduler import TimeScheduler, normalize_schedule_time
from ..core.state import AgentState
from ..infrastructure.storage import SQLiteStorage
from ..persistence.errors import EventConflictError
from .autonomy import AutonomousActivityService
from .appraisal_persistence import DUE_EVENT_TYPE, REQUEST_EVENT_TYPE, AppraisalJob
from .evaluation import evaluation_id
from .emotion import EMOTION_IMPULSE_EVENT_TYPE, EMOTION_TICK_EVENT_TYPE, EmotionEventHandler
from .needs import NEEDS_TICK_EVENT_TYPE, NeedTickHandler
from .persistence import PersonaStore
from .settings import HostSettings
from ..cognition.evaluation import RuleEvaluation
from ..activity.feedback import ActivityFeedback
from ..cognition.prompt_builder import build_prompt_context, ContextOutbound
from .memory import format_memory_result
from .relationship import format_relationship
from ..social.clock import DAY_EVENT_TYPE
from ..social.conversation_clock import CONVERSATION_DUE_EVENT
from .conversation import conversation_scope_allowed
from ..social.expression import gate, ExpressionIntent, local_day
from ..social.expression_clock import EXPRESSION_DUE_EVENT


class PersonaHost:
    def __init__(self, settings: HostSettings, data_dir: Path, *, clock=None,
                 persist_controls: Callable[[ArbitrationPolicy], None] | None = None,
                 generate_expression=None, send_expression=None, persist_expression=None):
        self.settings = HostSettings.model_validate(settings.model_dump())
        self.data_dir = Path(data_dir)
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self.store = PersonaStore(SQLiteStorage(self.data_dir / "persona.sqlite3"), evaluation=self.settings.evaluation,
            memory=self.settings.memory, relationships=self.settings.relationships, conversation=self.settings.conversation,
            conversation_scope_allowed=lambda item: conversation_scope_allowed(item,self.settings),
            expression=self.settings.expression,expression_target_allowed=self._expression_target_allowed)
        self.selector = self.settings.simulation.selector()
        self.policy = self.settings.arbitration()
        self._persist_controls = persist_controls
        self._lifecycle = asyncio.Lock()
        self._operations = asyncio.Lock()
        self._shutdown_task = None
        self._generate_expression=generate_expression
        self._send_expression=send_expression
        self._expression_task=None
        self._expression_task_id=None
        self._expression_waiting=None
        self._persist_expression=persist_expression
        self.ready = False
        self.last_message_id = None
        self._last_contexts = OrderedDict()
        self.runtime = self.activities = self.autonomy = self.scheduler = None

    def _autonomy(self, policy):
        return AutonomousActivityService(self.settings.agent_id, self.store, self.selector,
                                         self.activities, self.runtime, clock=self.clock,
                                         arbiter=BehaviorArbiter(policy),dispatch_expression=self._queue_expression)

    async def start(self):
        async with self._lifecycle:
            if self.ready:
                return
            if self._shutdown_task is not None and not self._shutdown_task.done():
                raise RuntimeError("角色正在关闭，请稍后重试")
            self.data_dir.mkdir(parents=True, exist_ok=True)
            await self.store.open()
            try:
                agent_id = self.settings.agent_id
                cfg = self.settings.simulation
                now = normalize_schedule_time(self.clock())
                state = await self.store.get_state(agent_id)
                if state is None:
                    await self.store.create_state(AgentState(agent_id=agent_id, needs=cfg.initial_needs, updated_at=now))
                    state = await self.store.get_state(agent_id)
                registry = ExecutorRegistry()
                ids = {d.definition_id for d in cfg.activities}
                ids.update(r.instance.definition_id for r in await self.store.list_active_activities(agent_id)
                           if r.instance.mode == ExecutionMode.SIMULATED)
                for identifier in sorted(ids):
                    registry.register(identifier, ExecutionMode.SIMULATED, SimulatedActivityExecutor())
                self.activities = ActivityService(agent_id, self.store, ActivityManager(registry), clock=self.clock,
                                                   tick_interval_seconds=cfg.activity_interval_seconds)
                router = EventRouter()
                router.register(NEEDS_TICK_EVENT_TYPE, NeedTickHandler(clock=self.clock))
                emotional = EmotionEventHandler(clock=self.clock)
                router.register(EMOTION_TICK_EVENT_TYPE, emotional)
                router.register(EMOTION_IMPULSE_EVENT_TYPE, emotional)
                await self.store.start_needs_clock(agent_id, rates=cfg.need_rates, now=now,
                                                   interval_seconds=cfg.needs_interval_seconds)
                if state.emotion is not None:
                    if cfg.initial_emotion is not None and (
                        state.emotion.baseline != cfg.initial_emotion.baseline
                        or state.emotion.dynamics.model_dump(exclude={"tick_interval_seconds"})
                        != cfg.initial_emotion.dynamics.model_dump(exclude={"tick_interval_seconds"})
                    ):
                        raise ValueError("已保存的情绪基线或参数与配置不同，不能自动重置")
                    await self.store.start_emotion_clock(agent_id, now=now,
                        tick_interval_seconds=None if cfg.initial_emotion is None
                        else cfg.initial_emotion.dynamics.tick_interval_seconds)
                elif cfg.initial_emotion is not None:
                    await self.store.start_emotion_clock(agent_id, now=now, initial=cfg.initial_emotion.at(now))
                await self.store.start_relationship_clock(agent_id,now=now)
                await self.store.start_conversation_clock(agent_id,now=now)
                self.runtime = AgentRuntime(agent_id, self.store, router, clock=self.clock)
                await self.runtime.start()
                self.autonomy = self._autonomy(self.policy)
                self.scheduler = TimeScheduler(agent_id, self.store, self._deliver, clock=self.clock)
                recovery_key=None
                try:
                    before_expression=await self.store.expression_head(agent_id)
                    pending_expression=await self.store.recover_expression(agent_id,now=now)
                    after_expression=await self.store.expression_head(agent_id)
                    if after_expression is not None and before_expression!=after_expression:
                        recovery_key="expression-recovery:"+after_expression.intent.intent_id+":"+after_expression.intent.status
                except (ValueError,RuntimeError):
                    logging.getLogger(__name__).exception("主动表达游标异常，关闭本次加载的主动表达")
                    self.store._expression=None
                    pending_expression=None
                # 先处理一批离线期间到期计划，再做启动仲裁；后台继续处理余下计划。
                await self.scheduler.run_due()
                await self.store.recover_appraisals(agent_id, now=self.clock())
                await self.autonomy.recover()
                if recovery_key is not None:await self.autonomy.consider(recovery_key)
                await self.scheduler.start()
                self.ready = True
                if self.store._expression is not None:
                    head=await self.store.expression_head(agent_id)
                    if head is not None and head.intent.status=="pending":pending_expression=head.intent
                if pending_expression is not None:self._queue_expression(pending_expression)
                self._shutdown_task = None
            except BaseException:
                self.ready = False
                await self._shutdown()
                raise

    async def _shutdown(self):
        task=self._expression_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        self._expression_task=None
        self._expression_waiting=None
        try:
            if self.scheduler is not None:
                await self.scheduler.stop()
        finally:
            async with self._operations:
                try:
                    if self.runtime is not None:
                        await self.runtime.stop()
                finally:
                    await self.store.close()
        self.runtime = self.activities = self.autonomy = self.scheduler = None
        self._last_contexts.clear()

    async def stop(self):
        async with self._lifecycle:
            self.ready = False
            if self._shutdown_task is None:
                self._shutdown_task = asyncio.create_task(self._shutdown())
            task = self._shutdown_task
        await asyncio.shield(task)

    def _check(self):
        if not self.ready:
            raise RuntimeError("角色尚未就绪或正在关闭")

    async def _deliver(self, event):
        async with self._operations:
            if event.event_type==EXPRESSION_DUE_EVENT:
                resume,item=await self.store.handle_expression_deadline(self.settings.agent_id,event,now=self.clock())
                self._cancel_generation(item)
                if resume:await self.autonomy.consider(event.event_id)
            elif event.event_type==CONVERSATION_DUE_EVENT:
                if await self.store.handle_conversation_deadline(self.settings.agent_id,event,now=self.clock()):
                    await self.autonomy.consider(event.event_id)
            elif event.event_type==DAY_EVENT_TYPE:
                await self.store.handle_relationship_day(self.settings.agent_id,event,now=self.clock())
            elif event.event_type == DUE_EVENT_TYPE:
                receipt = await self.store.handle_appraisal_deadline(self.settings.agent_id, event, now=self.clock())
                await self._consider_appraisal(receipt)
            else:
                await self.autonomy.handle_event(event)

    def _expression_target_allowed(self,target):
        return (target.user_id in self.settings.qq_private_ids
            and (not self.settings.qq_platform_ids or target.platform_id in self.settings.qq_platform_ids))

    def _queue_expression(self,intent):
        if not self.ready or intent is None or intent.status!="pending":return
        if self._expression_task is not None and not self._expression_task.done():
            if self._expression_task_id!=intent.intent_id:self._expression_waiting=intent
            return
        self._expression_task_id=intent.intent_id
        self._expression_task=asyncio.create_task(self._run_expression(intent))
        self._expression_task.add_done_callback(self._expression_done)

    def _cancel_generation(self,item):
        if item is not None and self._expression_waiting is not None and self._expression_waiting.intent_id==item.intent_id:
            self._expression_waiting=None
        # 不等待任务退出：调用处持有操作锁，任务退出时也需要该锁保存结果。
        if (item is not None and item.status=="cancelled" and self._expression_task_id==item.intent_id
                and self._expression_task is not None and not self._expression_task.done()):
            self._expression_task.cancel()

    async def cancel_expression(self):
        async with self._operations:
            self._check()
            changed,item=await self.store.cancel_expression(self.settings.agent_id,now=self.clock(),include_preview=True)
            self._cancel_generation(item)
            if changed:
                await self.autonomy.consider("expression-control:"+item.intent_id+":manual_cancel")
                return "已取消未发送意图；原冷却和预算保留。"
            if item is None:return "尚无可取消的主动表达意图"
            if item.status=="sending":return "该意图已进入发送阶段，不能确认撤回；将保留发送结果，不重发。"
            return f"该意图已为 {item.status}，无需取消；不提供重发。"

    async def set_expression_enabled(self,enabled):
        if type(enabled) is not bool:raise ValueError("主动表达开关必须是布尔值")
        async with self._operations:
            self._check()
            cfg=self.settings.expression.model_copy(update={"enabled":enabled})
            if self._persist_expression is None:raise RuntimeError("未配置主动表达设置持久化")
            self._persist_expression(cfg)
            self.settings=self.settings.model_copy(update={"expression":cfg})
            self.store._expression=cfg
            if not enabled:
                changed,item=await self.store.cancel_expression(self.settings.agent_id,now=self.clock(),reason="expression_paused")
                self._cancel_generation(item)
                if changed:await self.autonomy.consider("expression-control:"+item.intent_id+":expression_paused")
            # 恢复不复活旧意图，不重置冷却/预算，不改实际发送开关，也不立即强制表达。

    def _expression_done(self,task):
        if not task.cancelled():
            error=task.exception()
            if error is not None:
                logging.getLogger(__name__).error("主动表达后台任务未完成：%s",type(error).__name__)
        if task is self._expression_task:
            pending,self._expression_waiting=self._expression_waiting,None
            if self.ready and pending is not None:self._queue_expression(pending)

    async def _run_expression(self,intent):
        sending=False
        try:
            async with self._operations:
                if not self.ready:return
                item=await self.store.claim_expression(self.settings.agent_id,intent.intent_id,now=self.clock(),
                    autonomous_enabled=self.policy.autonomous_enabled)
            if item is None:return
            if self._generate_expression is None or self._send_expression is None:
                raise RuntimeError("未配置主动表达适配器")
            # 模型和网络等待不持有角色操作锁，正常入站可取消旧意图。
            context=await self.chat_context(item.target.scope_id,is_private=True,
                speaker_id=f"{item.target.platform_id}:user:{item.target.user_id}",
                platform_id=item.target.platform_id,user_id=item.target.user_id)
            prompt=("请沿用既有人设，主动发起一句自然、简短且不施压的私聊。"
                f"正文不超过{self.settings.expression.max_body_chars}字。不假装用户刚说过话，不声称现实观察、工具执行或已完成模拟活动。只输出发给用户的正文。\n"
                +("" if context is None else context.system_text+"\n"+context.user_text))
            body=await asyncio.wait_for(self._generate_expression(item,prompt),
                timeout=self.settings.expression.generation_timeout_seconds)
            if not isinstance(body,str) or not body.strip() or len(body)>self.settings.expression.max_body_chars:
                raise ValueError("模型正文为空或超长")
            async with self._operations:
                item=await self.store.claim_expression(self.settings.agent_id,item.intent_id,now=self.clock(),
                    autonomous_enabled=self.policy.autonomous_enabled,phase="send",body=body.strip())
            if item is None:return
            sending=True
            result=await asyncio.wait_for(self._send_expression(item),timeout=self.settings.expression.send_timeout_seconds)
            async with self._operations:
                await self.store.finish_expression(self.settings.agent_id,item.intent_id,now=self.clock(),
                    status="sent" if result is True else "failed",reason="adapter_returned" if result is True else "no_platform")
        except asyncio.CancelledError:
            async with self._operations:
                await self.store.finish_expression(self.settings.agent_id,intent.intent_id,now=self.clock(),
                    status="unknown" if sending else "cancelled",reason="shutdown")
            raise
        except asyncio.TimeoutError:
            async with self._operations:
                await self.store.finish_expression(self.settings.agent_id,intent.intent_id,now=self.clock(),
                    status="unknown" if sending else "failed",reason="send_timeout" if sending else "generation_timeout")
        except Exception:
            logging.getLogger(__name__).exception("主动表达执行未完成；不自动重发")
            async with self._operations:
                await self.store.finish_expression(self.settings.agent_id,intent.intent_id,now=self.clock(),
                    status="unknown" if sending else "failed",reason="send_uncertain" if sending else "generation_failed")
        finally:
            if self.ready:
                async with self._operations:
                    if self.ready:
                        head=await self.store.expression_head(self.settings.agent_id)
                        if (head is not None and head.intent.intent_id==intent.intent_id
                                and head.intent.status in ("sent","failed","cancelled","unknown")):
                            await self.autonomy.consider("expression-finished:"+intent.intent_id+":"+head.intent.status)

    async def expression_status(self):
        async with self._operations:
            self._check()
            cfg=self.settings.expression
            head=await self.store.expression_head(self.settings.agent_id)
            condition=gate(cfg,self.clock(),head)
            if condition is None and not self.policy.autonomous_enabled:condition="autonomy_disabled"
            state=await self.store.get_state(self.settings.agent_id)
            if condition is None and state.needs.social_need<cfg.social_threshold:condition="social_need_below_threshold"
            if condition is None and await self.store.list_active_activities(self.settings.agent_id):condition="activity_occupied"
            if condition is None:condition=await self.store.conversation_arbitration_reason(self.settings.agent_id,now=self.clock())
            lines=[f"主动表达：{'开启' if cfg.enabled else '关闭'}；模式：{'实际发送' if cfg.send_enabled else '仅预览（不调用模型）'}",
                f"配置目标：{len(cfg.targets)}；社交阈值：{cfg.social_threshold}；每日上限：{cfg.daily_limit}",
                "当前条件："+(condition or ("等待正常仲裁及目标最近互动检查" if cfg.targets else "no_targets"))]
            if head is None:return "\n".join(lines+["尚无表达意图；只在正常自主检查中选择，不由查询强制触发。"])
            item=head.intent
            effective="expired" if item.status in ("pending","generating") and self.clock()>=item.expires_at else item.status
            today=local_day(self.clock(),cfg)
            used_today=head.used if head.budget_day==today else 0
            cooldown=max(0,cfg.cooldown_seconds-int((self.clock()-head.last_attempt_at).total_seconds()))
            lines.extend((f"意图：{item.intent_id}；状态：{item.status}；有效状态：{effective}；原因：{item.reason}",
                f"目标：{item.target.scope_id}；截止：{item.expires_at.isoformat()}",
                f"预算日：{head.budget_day}；已用：{head.used}（预览与失败也保守计入）",
                f"今日（{today}）已用：{used_today}/{cfg.daily_limit}；剩余冷却：{cooldown} 秒",
                "sent 仅表示平台适配器正常返回，不代表用户收到或阅读。"))
            if item.body:lines.append("正文："+item.body)
            return "\n".join(lines)

    async def _consider_appraisal(self, receipt):
        result = RuleEvaluation.model_validate(receipt.payload["evaluation"])
        if result.needs_delta is not None or (result.impulse is not None and self.selector.emotion_influence_enabled):
            await self.autonomy.consider(receipt.event_id)

    async def pending_appraisals(self, *, limit=20):
        async with self._operations:
            self._check()
            return await self.store.pending_appraisals(self.settings.agent_id, limit=limit)

    async def complete_appraisal(self, source_event_id, raw):
        async with self._operations:
            self._check()
            receipt = await self.store.complete_appraisal(self.settings.agent_id, source_event_id, raw, now=self.clock())
            await self._consider_appraisal(receipt)
            return receipt

    async def _canonical_message(self, event, now):
        platform_timestamp = None
        existing = await self.store.get_event(event.event_id)
        if existing is not None:
            # 重投先复用首条规范事件；不重新钳制或改写时间审计记录。
            if existing.model_dump(exclude={"timestamp"}) != event.model_dump(exclude={"timestamp"}):
                raise EventConflictError("同一 QQ 消息 ID 的内容或作用域发生变化")
            event = existing
        elif event.timestamp > now:
            # 接收时间用于结算；平台原始时间另存关联审计，与原消息原子提交。
            platform_timestamp = event.timestamp
            event = AgentEvent(**{**event.model_dump(), "timestamp": now})
        return event, platform_timestamp

    async def ingest(self, event: AgentEvent, *, directed_to_agent=False):
        async with self._operations:
            self._check()
            if event.event_type != "SOCIAL.message_received":
                raise ValueError("消息入口只接收 SOCIAL.message_received")
            now = normalize_schedule_time(self.clock())
            event, platform_timestamp = await self._canonical_message(event,now)
            receipt = await self.store.record_rule_message(self.settings.agent_id, event, now=now,
                directed=directed_to_agent, platform_timestamp=platform_timestamp)
            result = await self.runtime.submit(event)
            self.last_message_id = event.event_id
            await self._consider_appraisal(receipt)
            return result

    async def conversation_receive(self, event, *, directed, blocked=False):
        async with self._operations:
            self._check()
            now=normalize_schedule_time(self.clock())
            before=await self.store.get_conversation(self.settings.agent_id,event.scope_id)
            follow=await self.store.conversation_receive(self.settings.agent_id,event,now=now,
                settings=self.settings.conversation,directed=directed,blocked=blocked)
            after=await self.store.get_conversation(self.settings.agent_id,event.scope_id)
            if after!=before and self.settings.conversation.defer_new_activities:
                await self.autonomy.consider(f"conversation:{event.event_id}:received:{after.revision}")
            return follow

    async def conversation_sent(self, event):
        async with self._operations:
            self._check()
            before=await self.store.get_conversation(self.settings.agent_id,event.scope_id)
            await self.store.conversation_sent(self.settings.agent_id,event,
                now=normalize_schedule_time(self.clock()),settings=self.settings.conversation)
            after=await self.store.get_conversation(self.settings.agent_id,event.scope_id)
            if after!=before and self.settings.conversation.defer_new_activities:
                await self.autonomy.consider(f"conversation:{event.event_id}:sent:{after.revision}")

    async def conversation_status(self, scope_id):
        async with self._operations:
            self._check()
            item=await self.store.get_conversation(self.settings.agent_id,scope_id)
            if item is None:
                return "当前会话尚无续聊状态"
            now=normalize_schedule_time(self.clock())
            phase=("disabled" if not self.settings.conversation.enabled else
                "expired" if item.phase!="closed" and now>=item.expires_at else item.phase)
            return (f"对话对象：{item.user_id}；状态：{phase}\n"
                f"自动续聊：{item.followups}/{self.settings.conversation.max_followups}；截止：{item.expires_at.isoformat()}\n"
                "send_hook_observed 表示发送流程观察（回复钩子或主动发送适配器返回），不代表平台送达确认。")

    async def report_feedback(self, activity_id: str, feedback: ActivityFeedback, *, observed_at=None, provenance=None, scope_id=None):
        async with self._operations:
            self._check()
            event = await self.store.report_activity_feedback(self.settings.agent_id, activity_id, feedback,
                now=self.clock(), observed_at=observed_at, provenance=provenance, scope_id=scope_id)
            receipt = await self.store.get_event(evaluation_id(self.settings.agent_id, event.event_id))
            if receipt is not None:
                await self._consider_appraisal(receipt)
            return event

    async def chat_context(self, scope_id, *, query="", speaker_id=None, is_private=True, platform_id=None, user_id=None):
        async with self._operations:
            self._check()
            cfg = self.settings.chat_context
            if not cfg.enabled:
                return None
            snapshot = await self.store.chat_snapshot(self.settings.agent_id, scope_id, now=self.clock(),
                rates=self.settings.simulation.need_rates,
                labels={item.definition_id: item.name for item in self.settings.simulation.activities},
                include_feedback=cfg.include_feedback,query=query,speaker_id=speaker_id,
                include_memories=self.settings.memory.enabled and (not self.settings.memory.private_only or is_private),
                platform_id=platform_id,user_id=user_id,
                include_relationship=self.settings.relationships.enabled and (not self.settings.relationships.private_only or is_private))
            try:
                conversation=await self.store.get_conversation(self.settings.agent_id,scope_id)
                event=None if conversation is None or conversation.own_outbound_event_id is None else await self.store.get_event(conversation.own_outbound_event_id)
                if event is not None and event.event_type=="INTERNAL.expression_updated" and event.source=="application.expression":
                    item=ExpressionIntent.model_validate(event.payload["intent"])
                    if (item.agent_id==self.settings.agent_id and item.status=="sent" and item.target.scope_id==scope_id
                            and event.scope_id==scope_id and event.event_id==item.intent_id+":sent"
                            and speaker_id==f"{item.target.platform_id}:user:{item.target.user_id}"):
                        snapshot=snapshot.model_copy(update={"outbound":ContextOutbound(scope_id=scope_id,actor_id=speaker_id,
                            text=item.body,at=item.updated_at)})
            except (ValueError,RuntimeError,KeyError):
                pass  # 可选出站引用损坏时仍保留原聊天状态。
            return build_prompt_context(snapshot, cfg, max_memory_chars=self.settings.memory.max_context_chars)

    async def relationship_command(self,event,action="show"):
        async with self._operations:
            self._check()
            cfg=self.settings.relationships
            if not cfg.enabled:
                return "关系功能已关闭"
            if event.event_type!="SOCIAL.message_received" or event.provenance.value!="observed" or event.scope_id is None:
                raise ValueError("关系查询需要当前 QQ 入站身份")
            if cfg.private_only and (event.payload.get("group_id") or ":GroupMessage:" in event.scope_id):
                return "当前关系功能仅在绑定私聊中使用"
            if action not in ("show","history"):
                return "用法：/persona_relationship [show|history]（仅当前用户）"
            profile,changes=await self.store.relationship_view(self.settings.agent_id,event.payload.get("platform_id"),
                event.payload.get("sender_id"),event.scope_id,history=action=="history")
            return format_relationship(profile,changes,cfg,history=action=="history")

    async def memory_command(self, event, action, value=""):
        async with self._operations:
            self._check()
            cfg=self.settings.memory
            if not cfg.enabled:
                return "记忆功能已关闭"
            if cfg.private_only and event.payload.get("group_id"):
                return "当前记忆功能仅在绑定私聊中使用"
            if action not in ("add","list","pending","show","history","delete","search"):
                return "用法：/persona_memory add 内容 | list [上一页最后ID] | pending [上页最后ID] | show ID | history ID | delete ID | search 问题"
            if action in ("add","delete","show","history","search") and not value.strip():
                return "此操作需要正文、问题或记忆 ID"
            now=normalize_schedule_time(self.clock())
            event,platform_timestamp=await self._canonical_message(event,now)
            result=await self.store.memory_operation(self.settings.agent_id,event,action,value.strip(),cfg,
                platform_timestamp=platform_timestamp,now=now)
            if action=="delete":
                self._last_contexts.pop(event.scope_id,None)
            return format_memory_result(action,result,cfg)

    def remember_context(self, context):
        self._last_contexts[context.scope_id] = context
        self._last_contexts.move_to_end(context.scope_id)
        while len(self._last_contexts) > 64:
            self._last_contexts.popitem(last=False)

    def last_context(self, scope_id):
        return self._last_contexts.get(scope_id)

    async def set_controls(self, *, enabled=None, activity_id=None, disabled=None):
        async with self._operations:
            self._check()
            ids = set(self.policy.disabled_definition_ids)
            if activity_id is not None:
                known = {d.definition_id for d in self.selector.definitions}
                known.update(r.instance.definition_id for r in await self.store.list_active_activities(self.settings.agent_id))
                if activity_id not in known:
                    raise ValueError("活动 ID 不存在，请先查看活动目录")
                if disabled:
                    ids.add(activity_id)
                else:
                    ids.discard(activity_id)
            policy = ArbitrationPolicy(autonomous_enabled=self.policy.autonomous_enabled if enabled is None else enabled,
                                       disabled_definition_ids=ids)
            replacement = self._autonomy(policy)
            replacement.last_decision = self.autonomy.last_decision
            if self._persist_controls is None:
                raise RuntimeError("未配置控制项持久化，拒绝仅在内存修改")
            self._persist_controls(policy)
            self.policy, self.autonomy = policy, replacement
            if not policy.autonomous_enabled:
                changed,item=await self.store.cancel_expression(self.settings.agent_id,now=self.clock(),reason="autonomy_paused")
                self._cancel_generation(item)
                if changed:await self.autonomy.consider("expression-control:"+item.intent_id+":autonomy_paused")

    async def control_activity(self, action, activity_id=""):
        async with self._operations:
            self._check()
            methods = {"pause": self.activities.pause_activity, "resume": self.activities.resume_activity,
                       "stop": self.activities.stop_activity}
            if action not in methods:
                raise ValueError("活动操作只能为 pause、resume 或 stop")
            if not activity_id:
                active = await self.store.list_active_activities(self.settings.agent_id)
                if len(active) != 1:
                    raise ValueError("当前不是唯一活动，请显式提供活动实例 ID")
                activity_id = active[0].instance.activity_id
            return await methods[action](activity_id)

    async def status(self):
        async with self._operations:
            self._check()
            state = await self.store.get_state(self.settings.agent_id)
            active = await self.store.list_active_activities(self.settings.agent_id)
            needs = state.needs
            lines = [f"角色：{state.agent_id}（状态版本 {state.version}）",
                     f"自主启动：{'开启' if self.policy.autonomous_enabled else '暂停'}",
                     f"需求：精力 {needs.energy:.1f}，饱食 {needs.satiety:.1f}，无聊 {needs.boredom:.1f}，社交 {needs.social_need:.1f}"]
            if state.emotion is not None:
                vad = state.emotion.current
                lines.append(f"已结算情绪 VAD：{vad.valence:.3f} / {vad.arousal:.3f} / {vad.dominance:.3f}")
            for record in active:
                item = record.instance
                progress = "未知" if item.progress is None else f"{item.progress:.0%}"
                lines.append(f"活动：{record.definition.name} [{item.status.value}] {progress}\n实例：{item.activity_id}")
            if not active:
                lines.append("活动：空闲")
            lines.append("目录：" + ", ".join(d.definition_id for d in self.selector.definitions))
            lines.append("禁用：" + (", ".join(sorted(self.policy.disabled_definition_ids)) or "无"))
            lines.append("调度：" + (self.scheduler.last_error or "正常"))
            lines.append("本次加载最近入站：" + (self.last_message_id or "尚未收到绑定范围内的 QQ 消息"))
            lines.append("规则评价：" + ("开启" if self.settings.evaluation.enabled else "关闭"))
            lines.append("附带语义评价：" + ("开启" if self.settings.evaluation.semantic.enabled else "关闭"))
            if self.store.memory_index_report.skipped:
                lines.append(f"记忆索引：本次加载跳过 {self.store.memory_index_report.skipped} 条异常记录；用 /persona_memory pending 查看当前会话")
            return "\n".join(lines)

    async def evaluation_status(self, source_event_id=""):
        async with self._operations:
            self._check()
            agent_id = self.settings.agent_id
            if source_event_id:
                receipt = await self.store.get_event(evaluation_id(agent_id, source_event_id))
                if receipt is None:
                    job = await self.store.get_appraisal(agent_id, source_event_id)
                    receipt = None if job is None else job.event()
            else:
                receipt = await self.store.latest_evaluation(agent_id)
            if receipt is None:
                return "暂无评价"
            if receipt.event_type == REQUEST_EVENT_TYPE:
                job = AppraisalJob.model_validate(receipt.payload["job"])
                return "\n".join(("评价方式：等待已有模型调用的体验结果", f"原事件：{job.request.source_event_id}",
                    f"截止时间：{job.deadline.isoformat()}", "情绪尚未结算；超时将采用规则兜底"))
            result = RuleEvaluation.model_validate(receipt.payload["evaluation"])
            return "\n".join(("评价方式："+receipt.payload.get("layer", "rule"), f"原事件：{receipt.causation_id}",
                f"规则：{result.rule_id or '无'}，原因：{result.reason}",
                "有效冲击：" + (str(result.impulse.model_dump()) if result.impulse is not None else "无"),
                "需求变化：" + (str(result.needs_delta.model_dump()) if result.needs_delta is not None else "无"),
                f"需求判定：{result.needs_reason or '无'}"))

    async def feedback_status(self, activity_id=""):
        async with self._operations:
            self._check()
            event = await self.store.latest_activity_feedback(self.settings.agent_id, activity_id or None)
            if event is None:
                return "暂无阶段反馈"
            feedback = ActivityFeedback.model_validate(event.payload["feedback"])
            return "\n".join((f"活动：{event.activity_id}", f"反馈事件：{event.event_id}", f"阶段标识：{feedback.checkpoint_id}",
                f"范围：{feedback.scope}，任务：{feedback.task or feedback.outcome}，来源：{event.provenance.value}", f"内容：{feedback.summary}",
                f"内容引用：{feedback.content_ref or '无'}"))

    async def decision(self):
        async with self._operations:
            self._check()
            decision = self.autonomy.last_decision
            if decision is None:
                receipt = await self.store.get_latest_behavior_event(self.settings.agent_id)
                if receipt is not None and "arbitration" in receipt.payload:
                    decision = BehaviorDecision.model_validate(receipt.payload["arbitration"])
            if decision is None:
                return "暂无可读取的仲裁结果"
            live=await self.store.conversation_arbitration_reason(self.settings.agent_id,now=self.clock())
            labels={"reply_pending":"正在准备回复，暂缓新活动", "waiting_followup":"等待用户续聊，暂缓新活动"}
            lines = [f"行为：{decision.winner.kind.value}，原因：{decision.reason}",
                "本次仲裁会话约束："+labels.get(decision.conversation_reason,"无"),
                "当前会话约束："+labels.get(live,"无")]
            if decision.conversation_reason is not None and decision.winner.kind.value=="continue_activity":
                lines.append("已有活动继续运行；会话约束只限制新启动与 pending 接续。")
            for item in decision.assessments[:10]:
                score = item.candidate.score_breakdown
                detail = "" if score is None else f"，需求 {score.need_score:.4f} + 情绪 {score.emotion_adjustment:.4f} = {score.final_score:.4f}"
                lines.append(f"{item.candidate.candidate_id}：{item.reason}{detail}")
            return "\n".join(lines)
